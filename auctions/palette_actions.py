"""The things the command palette's natural-language assist is allowed to do.

Every capability is one :class:`Action` in :data:`ACTIONS`. An action carries the description and
parameter schema the model is shown, a danger level that decides how it reaches the user, and a
resolver that does the work.

**No drift.** A resolver never re-implements a permission check or a validation rule. It calls the
same form, the same shared function, or the same view method the web page calls, so a lot added by
voice goes through the identical gauntlet as one added by clicking:

  ``add_lot``         -> :class:`auctions.forms.QuickAddLot` (the bulk-add page's form, same
                         ``auction``/``tos``/``is_admin`` kwargs) + ``services.lot_add_block`` +
                         ``services.save_new_lot`` + ``services.recalculate_seller_invoice``
  ``set_lot_winner``  -> :class:`auctions.views.DynamicSetLotWinner`'s own ``validate_lot`` /
                         ``validate_price`` / ``validate_winner`` / ``cross_check_price_and_winner``
                         / ``commit_winner`` methods, on a real instance of the view
  ``edit_lot``        -> the same ``QuickAddLot``, with the lot as its instance, which is exactly
                         how the bulk-add page's formset edits these fields
  ``check_in``        -> ``services.check_in_auctiontos`` (extracted from ``views.AuctionCheckIn``)
  ``undo_sale``       -> :class:`auctions.views.AuctionUnsellLot`'s own ``find_lot`` / ``unsell``
  ``add_person`` /    -> :class:`auctions.forms.QuickAddTOS` and
  ``update_person``      :class:`auctions.forms.CreateEditAuctionTOS`, the bulk-add and edit forms,
                         plus ``services.ensure_club_member`` in club-managed auctions
  ``set_invoice_status`` -> :class:`auctions.views.InvoicePaid`, on a real instance of the view, so
                         the ledger entries, the renewal and the notifications are the button's own
  ``add_club_member`` -> :class:`auctions.forms.ClubMemberAdminForm`, the club's own member form
  ``update_club_member``
  ``renew_member``    -> ``views.renew_club_member``, shared with the Renew button and the club API
  ``award_points``    -> :class:`auctions.forms.BapAwardForm`, the Add points modal's form
  ``watch_lot``       -> the same ``Watch`` row the star on the lot page toggles
  ``find_person`` /   -> the palette's own scoped search helpers in ``command_palette``
  ``find_lot`` /
  ``my_context``
  ``go_to_page`` /    -> ``palette_routes``, the catalog of every page on the site
  ``find_page``
  ``print_labels`` /  -> navigate only; they resolve a URL and never perform the action
  ``renew_membership``

**Navigation is one skill, not three hundred.** Every page the site has is a
:class:`~auctions.palette_routes.Route`, and ``go_to_page`` reaches all of them. Adding a URL to
``urls.py`` without either cataloguing it or writing down why it isn't a destination fails
``auctions/test_palette_routes.py``, so the assistant can't silently fall behind the UI.

**Doing is not navigating.** That route audit only ever promised the assistant could *reach* every
page, and a capability with no page of its own -- most of this site's write surface is an HTMx
endpoint -- satisfied it by being written down as "not a destination". :data:`SKILLS` and
:data:`NOT_A_SKILL` at the bottom of this file close that gap: every view on the site that accepts
a POST names the skill that covers it or says why it doesn't need one, and
``auctions/test_palette_skills.py`` fails the build when a new one appears in neither.

Danger levels:

  ``safe``     -- reads nothing changes. Runs during assist, result goes straight back.
  ``confirm``  -- writes to the database. Assist returns a countdown; the *execute* endpoint runs it
                  (and re-runs the whole resolver, permissions included -- the countdown is UX only).
  ``navigate`` -- destructive, or involves money. We take the user to the page and never act.

Resolvers return one of the JSON shapes the front end understands::

    {"error": "lot submission has ended for this auction"}
    {"more_info_needed": "which bob?", "options": [...]}
    {"ok": True, "summary": "Added ...", "followups": [{"label": ..., "url": ...}], ...}
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import models
from django.db.models import Q
from django.forms import model_to_dict
from django.urls import reverse
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.http import urlencode
from django.utils.text import Truncator

from . import command_palette, palette_routes
from .models import AuctionTOS, ClubMember, Lot
from .services import (
    apply_club_member_to_tos,
    check_in_auctiontos,
    clone_lot_values,
    copy_lot_images,
    ensure_club_member,
    existing_tos_for_club_member,
    lot_add_block,
    recalculate_seller_invoice,
    save_new_lot,
    user_can_clone_lot,
)

logger = logging.getLogger(__name__)

DANGER_SAFE = "safe"
DANGER_CONFIRM = "confirm"
DANGER_NAVIGATE = "navigate"

# Who a skill is worth describing to. A pre-filter for the prompt, *not* the security boundary --
# every resolver re-checks permissions, and ``parse_reply`` will happily accept an action that
# wasn't advertised. Its job is to keep an ordinary bidder's prompt from being three quarters club
# administration, which costs tokens on every call and gives the model wrong answers to choose from.
NEEDS_ANYONE = ""
NEEDS_AUCTION_ADMIN = "auction_admin"
NEEDS_CLUB_ADMIN = "club_admin"

# How many candidates to name when a lookup is ambiguous ("which bob?").
AMBIGUOUS_LIMIT = 6

# How much of a summernote field (auction rules, club description, lot description) is worth sending.
#
# These are the only unbounded text on the site, and they are paid for on every lookup that carries
# them whether or not the question was about them. The opening paragraphs are where the rules people
# ask about actually live, so the tail is the cheap thing to lose; anyone who wants all of it is
# better served by the page, which the answer can link to.
RULES_LIMIT = 800
DESCRIPTION_LIMIT = 500
#: Cap on one setting's explanation in a ``_settings_block``. See :func:`_settings_block`.
MEANS_LIMIT = 140


@dataclass
class Action:
    """One capability, as described to the model and as executed on the server."""

    name: str
    description: str
    params: dict[str, str]
    danger: str
    resolver: Callable[..., dict[str, Any]]
    #: Read-only actions the model may call mid-conversation to look something up.
    lookup: bool = False
    #: Short human sentence used in the countdown card before the action runs.
    confirm_template: str = ""
    examples: list[str] = field(default_factory=list)
    #: Accepted-but-undocumented parameter spellings, so a near-miss from the model still works
    #: without widening what the prompt advertises.
    aliases: set[str] = field(default_factory=set)
    #: Who this is worth describing to. See :func:`actions_for`.
    needs: str = NEEDS_ANYONE

    def accepts(self, key: str) -> bool:
        return key in self.params or key in self.aliases

    def schema(self) -> dict[str, Any]:
        """The JSON blob describing this action in the system prompt."""
        data: dict[str, Any] = {
            "skill": self.name,
            "description": self.description,
            "params": self.params,
            "danger": self.danger,
        }
        if self.examples:
            data["examples"] = self.examples
        return data


ACTIONS: dict[str, Action] = {}


def register(action: Action) -> Action:
    ACTIONS[action.name] = action
    return action


def get_action(name: str) -> Action | None:
    """Look an action up by name. Unknown names return ``None`` -- never guess."""
    if not isinstance(name, str):
        return None
    return ACTIONS.get(name.strip().lower())


# --- helpers -----------------------------------------------------------------


def _error(message: str) -> dict[str, Any]:
    return {"error": message}


def _need(message: str, options: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"more_info_needed": message}
    if options:
        result["options"] = options[:AMBIGUOUS_LIMIT]
    return result


def _ok(summary: str, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": True, "summary": summary}
    result.update(extra)
    return result


def _str(params: dict[str, Any], key: str, default: str = "") -> str:
    value = params.get(key, default)
    if value is None:
        return default
    return str(value).strip()


def _int(params: dict[str, Any], key: str, default: int | None = None) -> int | None:
    value = params.get(key)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _decimal(params: dict[str, Any], key: str) -> Decimal | None:
    value = params.get(key)
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _page(request) -> dict[str, Any]:
    """What the user is currently looking at, as worked out by ``palette_routes``.

    Attached to the request by ``palette_assist`` before any resolver runs. Always a dict, so
    resolvers can read it without checking whether the client sent a path.
    """
    return getattr(request, "palette_page", None) or {}


def resolve_auction(user, hint: str = "", page: dict[str, Any] | None = None):
    """Find the auction the user means, in order: what they said, what they're looking at, last used.

    Scoped to auctions the user has actually joined or created (``_joined_auctions``), so neither a
    hint nor a page they claim to be on can reach an auction they have no relationship with.
    Returns ``(auction, error_or_None)``.
    """
    joined = command_palette._joined_auctions(user)
    if not hint:
        # The page they're on beats the stickier ``last_auction_used``: someone standing in one
        # auction's lot list and saying "add a lot" means this auction, whatever they touched last.
        page_slug = (page or {}).get("auction")
        if page_slug:
            current = joined.filter(slug=page_slug).first()
            if current:
                return current, None
            # The page context reaches auctions the user hasn't joined (see
            # ``palette_routes.page_context_from_path``), and this is where that stops. Say which
            # auction and why, rather than quietly acting on whichever auction they last used: they
            # are looking at *this* one, so a lot added to a different auction is a lot lost.
            title = (page or {}).get("auction_title") or "that auction"
            return None, (
                f"You haven't joined {title} yet, so I can't do that there. "
                "Open its page to join, or tell me which auction you meant."
            )
        # Re-scoped rather than trusted. ``last_auction_used`` is a stored pointer, and every
        # writer of it happens to set an auction the user joined -- but the pointer outlives the
        # relationship, so an admin deleting their participant row, or the auction being deleted,
        # would leave it naming something this function promises it will never return.
        auction = joined.filter(pk=getattr(command_palette._last_auction(user), "pk", None)).first()
        if not auction:
            return None, "I don't know which auction you mean, and you don't have a recent one."
        return auction, None
    match = joined.filter(Q(slug=hint) | Q(title__iexact=hint)).first()
    if not match:
        match = joined.filter(title__icontains=hint).first()
    if not match:
        return None, f"I couldn't find an auction called “{hint}” that you're part of."
    return match, None


def resolve_person(user, auction, hint: str):
    """Resolve a person in an auction from a bidder number or a name.

    Returns ``(tos, problem)`` where ``problem`` is already one of the JSON result shapes -- an
    ambiguous name comes back as ``more_info_needed`` listing the candidates ("which bob?").
    """
    hint = (hint or "").strip()
    if not hint:
        return None, _need("Who should this be for? Give me a name or a bidder number.")
    people = AuctionTOS.objects.filter(auction=auction)
    exact = people.filter(bidder_number__iexact=hint).first()
    if exact:
        return exact, None
    matches = list(people.filter(Q(name__icontains=hint) | Q(email__iexact=hint))[: AMBIGUOUS_LIMIT + 1])
    if not matches:
        return None, _error(f"I couldn't find anyone called “{hint}” in {auction.title}.")
    if len(matches) > 1:
        options = [
            {
                "label": f"{tos.name or tos.email or 'Bidder'} (bidder {tos.bidder_number})",
                "value": tos.bidder_number or str(tos.pk),
            }
            for tos in matches
        ]
        return None, _need(f"There's more than one “{hint}” in {auction.title}. Which one?", options)
    return matches[0], None


def _own_tos(user, auction):
    return AuctionTOS.objects.filter(auction=auction).filter(Q(user=user) | Q(email=user.email)).first()


def _is_auction_admin(user, auction) -> bool:
    """The same non-raising admin test the views use (``Auction.permission_check``)."""
    return bool(auction and auction.permission_check(user))


def _edit_person_url(auction, tos) -> str:
    """Where to send someone who needs to fill in or fix a participant's details.

    The auction's user list, filtered to that one person, rather than the edit form itself: the form
    is an HTMx modal (``auctiontosadmin``, and ``clubmember_admin`` in club-managed auctions), and in
    club-managed mode which of the two applies depends on whether the row has a member record. The
    list is a real page, works in both modes, and puts them one click from the right form.
    """
    query = tos.bidder_number or tos.name or ""
    url = reverse("auction_tos_list", kwargs={"slug": auction.slug})
    return f"{url}?{urlencode({'query': query})}" if query else url


def _lot_label_followup(lot) -> dict[str, str]:
    """A 'print this lot's label' followup, so "print that label" has somewhere obvious to go."""
    return {"label": f"Print label for {lot.lot_name}", "url": reverse("single_lot_label", kwargs={"pk": lot.pk})}


def local_time(auction, value) -> str | None:
    """A datetime as the auction's own clock reads it, for an answer somebody has to act on.

    The raw field is UTC with microseconds, and handed over as-is the model repeated it: "starts on
    2026-08-06 at 18:06:35 UTC" is both unreadable and, for anyone actually going to the auction,
    the wrong time. ``Auction.timezone`` is the same property the rest of the site formats dates
    with, so this says what the auction page says.
    """
    if not value:
        return None
    try:
        return value.astimezone(auction.timezone).strftime("%A, %B %-d %Y at %-I:%M %p %Z")
    except Exception:  # pragma: no cover - a naive or broken date is not worth losing the answer to
        logger.exception("Could not localize a date for %s", auction)
        return str(value)


def plain_text(value: str, limit: int = 1500) -> str:
    """Rich text (a summernote field) as something worth putting in a prompt.

    Auction rules and club descriptions are stored as HTML. Handed to the model raw they are mostly
    markup, which costs tokens and reads badly; this strips the tags, unescapes the entities,
    collapses the whitespace and truncates. Long rules get cut off rather than dropped -- the
    opening paragraphs are where the rules people ask about actually live.
    Human: ah, to be an llm and write that last line
    """
    text = html.unescape(strip_tags(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return Truncator(text).chars(limit, truncate="…")


#: Words that stay lowercase inside a lot name unless they start it.
_LOWERCASE_WORDS = frozenset({"a", "an", "and", "of", "or", "the", "with", "in", "on", "for", "to", "x", "per"})

#: A letter or two followed by digits: the plec/catfish codes (L134, LDA08) that must stay uppercase.
_CODE = re.compile(r"^[a-z]{1,3}\d+[a-z]?$")


def tidy_lot_name(name: str) -> str:
    """Give a spoken or all-lowercase lot name the casing a person would have typed.

    "blue shrimp" becomes "Blue Shrimp", and "l134" becomes "L134". Only ever applied to text with
    no capitals of its own: the moment the user (or the model) has capitalised anything, that is a
    decision, and re-casing it would wreck species names, breeder initials and deliberate ALL CAPS.
    """
    name = (name or "").strip()
    if not name or name != name.lower():
        return name
    words = []
    for index, word in enumerate(name.split()):
        if _CODE.match(word):
            words.append(word.upper())
        elif index and word in _LOWERCASE_WORDS:
            words.append(word)
        else:
            words.append(word[:1].upper() + word[1:])
    return " ".join(words)


def find_lot_to_copy(seller_user, name: str, exclude_auction=None):
    """The seller's own most recent lot of this thing, so a re-listing keeps its photos.

    Someone selling blue shrimp every month has already written the description and taken the
    photo; "add blue shrimp" should not throw that away and produce a bare, pictureless lot. An
    exact name match wins, and a partial one is accepted only when it is unambiguous, because
    attaching the wrong photo to a lot is worse than attaching none.

    Scoped to lots ``seller_user`` owns, and re-checked with ``services.user_can_clone_lot`` -- the
    same rule the "Copy to new lot" button enforces.

    Returns ``(lot_or_None, name_was_exact)``. The second value decides whether the old lot's
    *name* is reused as well as its contents: on an exact match it is strictly better than anything
    we would generate, but on a partial one ("add shrimp" finding "Blue Dream Shrimp") adopting it
    would rename the lot to something the user never said.
    """
    name = (name or "").strip()
    if not seller_user or not name:
        # Two values, like every other exit. Returning a bare ``None`` here made ``add_lot`` raise
        # TypeError while unpacking, which ``run_action`` turned into "Something went wrong doing
        # that." -- for every seller without a linked account, which at an in-person auction is
        # most of them.
        return None, False
    owned = Lot.objects.filter(user=seller_user, is_deleted=False).exclude(auction__is_deleted=True)
    if exclude_auction:
        # A lot already in this auction is the thing being added, not a previous listing of it.
        owned = owned.exclude(auction=exclude_auction)
    match = owned.filter(lot_name__iexact=name).order_by("-date_posted").first()
    exact = match is not None
    if match is None:
        partial = list(owned.filter(lot_name__icontains=name).order_by("-date_posted")[:2])
        # Two different past lots contain these words, so we can't tell which one they mean.
        if len(partial) == 1:
            match = partial[0]
    if match is None or not user_can_clone_lot(seller_user, match):
        return None, False
    return match, exact


# --- add_lot -----------------------------------------------------------------


def _resolve_lot_seller(request, params: dict[str, Any]):
    """Which auction a lot is going into, and whose it is. Returns ``(auction, tos, for_self, problem)``.

    Shared by ``add_lot`` and ``add_lots`` so a batch resolves the auction and the seller once
    rather than once per lot -- at a drop-off table doing forty lots that is forty repeated
    permission checks and forty repeated participant lookups.
    """
    user = request.user
    auction, error = resolve_auction(user, _str(params, "auction"), _page(request))
    if error:
        return None, None, False, _error(error)

    is_admin = _is_auction_admin(user, auction)
    bidder = _str(params, "bidder") or _str(params, "seller")
    if bidder:
        if not is_admin:
            return None, None, False, _error(f"Only admins of {auction.title} can add lots for someone else.")
        tos, problem = resolve_person(user, auction, bidder)
        if problem:
            return None, None, False, problem
        own_tos = _own_tos(user, auction)
        for_self = bool(own_tos and own_tos.pk == tos.pk)
    else:
        tos = _own_tos(user, auction)
        for_self = True

    block = lot_add_block(auction, tos, is_admin, bulk=False)
    if block:
        return None, None, False, _error(block[1])
    return auction, tos, for_self, None


def _create_one_lot(request, auction, tos, for_self, params: dict[str, Any]) -> dict[str, Any]:
    """Build and save one lot. The body of ``add_lot``, called once per lot by ``add_lots``.

    Validation and permissions are entirely ``QuickAddLot`` -- the same form and the same kwargs
    the bulk-add page uses. The caller has already resolved and gated the auction and the seller.
    """
    from .forms import quick_add_lot_form_class

    user = request.user
    is_admin = _is_auction_admin(user, auction)
    lot_name = _str(params, "name") or _str(params, "lot_name")
    if not lot_name:
        return _need("What should the lot be called?")
    missing = _missing_required_lot_fields(auction, params)
    if missing:
        # Asked before the form gets a chance to, so the question names the club's own label for
        # the field rather than the database column the form would complain about.
        return _need(missing)

    # A previous listing of the same thing is the best source for everything the user didn't say:
    # its photos, its description, and — when they named it exactly — its capitalisation.
    previous, name_was_exact = find_lot_to_copy(tos.user, lot_name, exclude_auction=auction)
    if previous:
        data = clone_lot_values(previous)
        # This dict is form *data*, not initial, so the two foreign keys have to be pks.
        data["species_category"] = previous.species_category_id
        data["species"] = previous.species_id
        if not name_was_exact:
            # A partial match reuses the old lot's contents but not its name: "add shrimp" must not
            # come out as a lot called "Blue Dream Shrimp — F1 juveniles".
            data["lot_name"] = tidy_lot_name(lot_name)
    else:
        data = {"lot_name": tidy_lot_name(lot_name), "species_category": _category_pk(lot_name)}

    reserve = _decimal(params, "reserve_price")
    if reserve is None:
        reserve = _decimal(params, "price")
    quantity = _int(params, "quantity")
    buy_now = _decimal(params, "buy_now_price")
    # Anything the user actually asked for overrides what the old lot happened to say.
    data["lot_name"] = str(data.get("lot_name") or lot_name)[:40]
    if quantity is not None:
        data["quantity"] = quantity
    data.setdefault("quantity", 1)
    if reserve is not None:
        data["reserve_price"] = reserve
    # The page submits this from a hidden input pre-filled with the auction minimum, so a
    # request that doesn't mention a price behaves the same way here.
    if data.get("reserve_price") is None:
        data["reserve_price"] = auction.minimum_bid
    if buy_now is not None:
        data["buy_now_price"] = buy_now
    for key in ("donation", "i_bred_this_fish"):
        if key in params:
            data[key] = bool(params.get(key))
    for key in ("custom_field_1", "custom_dropdown"):
        if params.get(key):
            data[key] = _str(params, key)

    form = quick_add_lot_form_class()(data, auction=auction, tos=tos, is_admin=is_admin)
    if not form.is_valid():
        return _form_problem(form)

    lot = form.save(commit=False)
    save_new_lot(lot, auction=auction, tos=tos, added_by=user)
    copied_images = copy_lot_images(previous, lot) if previous else []
    auction.create_history(
        applies_to="LOTS",
        action=f"Added lot {lot.lot_number_display} {lot.lot_name} (command palette)",
        user=user,
    )
    who = "you" if for_self else (tos.name or f"bidder {tos.bidder_number}")
    summary = f"Added “{lot.lot_name}” to {auction.title} for {who}."
    if previous:
        # Never silent: copying someone's old photos onto a new lot is a decision they get to see
        # and undo, and the Edit followup is how they undo it.
        reused = "description and photos" if copied_images else "description"
        whose = "the last one you listed" if for_self else "the last one they listed"
        summary += f" Reused the {reused} from {whose}."
    return _ok(
        summary,
        lot_id=lot.pk,
        lot_name=lot.lot_name,
        reused_a_previous_lot=bool(previous),
        followups=[
            {"label": "View this lot", "url": lot.get_absolute_url()},
            _lot_label_followup(lot),
            *([{"label": "Edit this lot", "url": reverse("edit_lot", kwargs={"pk": lot.pk})}] if previous else []),
        ],
    )


def add_lot(request, params: dict[str, Any]) -> dict[str, Any]:
    """Add one lot, for the user themselves or (admins only) for one of their bidders.

    Validation and permissions are entirely ``QuickAddLot`` + ``services.lot_add_block`` -- the
    same form and the same gate the bulk-add page uses, with the same kwargs.
    """
    auction, tos, for_self, problem = _resolve_lot_seller(request, params)
    if problem:
        return problem
    result = _create_one_lot(request, auction, tos, for_self, params)
    if result.get("ok"):
        recalculate_seller_invoice(auction, tos)
        result["auction"] = auction.slug
    return result


#: The most lots one spoken command may create. Past this it is a box being unpacked, and the bulk
#: add page -- with a row per lot on screen -- is the right tool.
MAX_LOTS_PER_BATCH = 12

#: Keys that describe ONE lot, and so may appear per item in an ``add_lots`` list.
_PER_LOT_KEYS = (
    "name",
    "quantity",
    "reserve_price",
    "buy_now_price",
    "donation",
    "i_bred_this_fish",
    "custom_field_1",
    "custom_dropdown",
)


def add_lots(request, params: dict[str, Any]) -> dict[str, Any]:
    """Add several lots to one auction in a single command.

    At a drop-off table this is the actual usage pattern -- "a java fern, a heater and three
    guppies" is one sentence, and ``add_lot`` being strictly singular meant it could only ever
    become one lot, because ``MAX_ROUNDS`` stops the model chaining three calls.

    Each lot goes through ``_create_one_lot``, so every one of them gets the same form, the same
    validation and the same previous-listing reuse a single ``add_lot`` would have given it. One
    bad lot does not lose the others: the failures are reported by name alongside the successes,
    which is the only useful thing to do when the user has already walked away with the box.

    The seller's invoice is recalculated once at the end rather than once per lot -- it is a full
    re-price of everything they brought, and doing it forty times is forty times the work for the
    same answer.
    """
    raw = params.get("lots")
    if isinstance(raw, str):
        # The model sometimes sends a comma-separated string rather than a list. Splitting it is
        # strictly better than refusing: the alternative is a correction round that costs a full
        # model call to learn something we can already read.
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, list) or not raw:
        return _need("What lots should I add? Give me a list.")
    if len(raw) > MAX_LOTS_PER_BATCH:
        return _error(
            f"That's {len(raw)} lots at once — I'll do up to {MAX_LOTS_PER_BATCH}. "
            "Use the bulk add page for a whole box."
        )

    auction, tos, for_self, problem = _resolve_lot_seller(request, params)
    if problem:
        return problem

    added: list[dict[str, Any]] = []
    failed: list[str] = []
    for item in raw:
        if isinstance(item, str):
            one = {"name": item}
        elif isinstance(item, dict):
            # Only the per-lot keys. Anything else (a second auction, a different bidder) is a
            # decision that belongs to the batch as a whole and was already made above.
            one = {key: item.get(key) for key in _PER_LOT_KEYS if item.get(key) is not None}
        else:
            continue
        # The batch's own defaults apply to every lot that didn't override them, so "add three
        # guppies and a java fern, all donations" works.
        for key in _PER_LOT_KEYS:
            if key not in one and key in params and key != "name":
                one[key] = params[key]
        result = _create_one_lot(request, auction, tos, for_self, one)
        if result.get("ok"):
            added.append(result)
        else:
            reason = result.get("error") or result.get("more_info_needed") or "couldn't be added"
            failed.append(f"{_str(one, 'name') or 'a lot'} ({reason})")

    if added:
        recalculate_seller_invoice(auction, tos)
    if not added:
        return _error("None of those could be added: " + "; ".join(failed))

    names = ", ".join(f"“{item['lot_name']}”" for item in added)
    who = "you" if for_self else (tos.name or f"bidder {tos.bidder_number}")
    summary = f"Added {len(added)} lot{'s' if len(added) != 1 else ''} to {auction.title} for {who}: {names}."
    if failed:
        summary += " I couldn't add: " + "; ".join(failed) + "."
    return _ok(
        summary,
        auction=auction.slug,
        # The last one added, so "print that label" after a batch means something rather than
        # nothing. Which of several it should mean is genuinely ambiguous; the most recent is the
        # one still in the user's hand.
        lot_id=added[-1]["lot_id"],
        lot_name=added[-1]["lot_name"],
        followups=[
            {
                "label": f"Print {'these labels' if len(added) > 1 else 'this label'}",
                "url": reverse("print_my_unprinted_labels", kwargs={"slug": auction.slug})
                if for_self
                else reverse(
                    "print_unprinted_labels_by_bidder_number",
                    kwargs={"slug": auction.slug, "bidder_number": tos.bidder_number},
                ),
            },
            {
                "label": f"All lots in {auction.title}",
                "url": reverse("auction_lot_list", kwargs={"slug": auction.slug}),
            },
        ],
    )


def _category_pk(lot_name: str = ""):
    """The category a lot of this name belongs in, falling back to Uncategorized.

    ``guess_category`` is the site's own guesser -- it reads the categories real people picked for
    lots with these words in the name, and it is what the add-lot form's category field autofills
    from. Without it every palette-added lot landed in "Uncategorized", which is quietly worse than
    a form-added one: the category drives browsing, the category-based notification emails and BAP
    eligibility, so a mis-filed lot is a lot fewer people see and a lot that may not earn points.
    """
    from .models import Category, guess_category

    if lot_name:
        try:
            guess = guess_category(lot_name)
        except Exception:  # pragma: no cover - a guess is never worth losing the lot over
            logger.exception("guess_category failed for %r", lot_name)
            guess = None
        if guess:
            return guess.pk
    category = Category.objects.filter(name="Uncategorized").first()
    return category.pk if category else None


def _form_problem(form) -> dict[str, Any]:
    """Turn form errors into the spec's result shapes.

    A missing required field is something the user can supply -> ``more_info_needed``; anything
    else (a rule the auction enforces) is an ``error``.
    """
    missing = []
    problems = []
    for name, errors in form.errors.items():
        label = form.fields[name].label if name in form.fields else name
        for message in errors:
            if "required" in message.lower():
                missing.append(str(label or name))
            else:
                problems.append(str(message))
    if problems:
        return _error(" ".join(problems))
    if missing:
        return _need("I still need: " + ", ".join(sorted(set(missing))))
    return _error("That didn't validate, but no reason was given.")


# --- set_lot_winner ----------------------------------------------------------


def set_lot_winner(request, params: dict[str, Any]) -> dict[str, Any]:
    """Record who won a lot in an in-person auction.

    Runs on a real :class:`auctions.views.DynamicSetLotWinner` instance so the lot/price/winner
    validation, the cross-checks and the commit are literally the view's own methods.
    """
    from .views import DynamicSetLotWinner

    user = request.user
    auction, error = resolve_auction(user, _str(params, "auction"), _page(request))
    if error:
        return _error(error)
    if not _is_auction_admin(user, auction):
        return _error(f"You don't have permission to set lot winners in {auction.title}.")
    if auction.is_online:
        return _error(f"{auction.title} is an online auction — winners come from the bids automatically.")

    view = DynamicSetLotWinner()
    view.request = request
    view.auction = auction
    view.kwargs = {}
    action = "save"

    lot, lot_error = view.validate_lot(_str(params, "lot"), action)
    price, price_error = view.validate_price(_str(params, "price"), action)
    winner, winner_error = view.validate_winner(_str(params, "winner"), action)
    price_error, winner_error = view.cross_check_price_and_winner(
        lot, price, winner, action, lot_error, price_error, winner_error
    )
    if lot_error:
        # A bad lot number is a dead end; the other two are things the user can just tell us.
        return _error(str(lot_error))
    if winner_error:
        return _need(str(winner_error))
    if price_error:
        return _need(str(price_error))
    if not (lot and winner and price):
        return _need("I need a lot number, a bidder number and a price.")

    result: dict[str, Any] = {"success_message": None}
    view.commit_winner(lot, winner, price, action, result)
    summary = str(result.get("success_message") or f"Sold lot {lot.lot_number_display}.")
    next_lot = result.get("next_queued_lot_number")
    if next_lot:
        summary += f" Lot {next_lot} is up next."
    return _ok(
        summary,
        lot_id=lot.pk,
        auction=auction.slug,
        bidder_number=winner.bidder_number,
        followups=[_lot_label_followup(lot)],
        undo={
            "action": "undo_sale",
            "params": {"lot": lot.lot_number_display, "auction": auction.slug},
            "describes": f"selling lot {lot.lot_number_display}",
        },
    )


def no_sale(request, params: dict[str, Any]) -> dict[str, Any]:
    """Record that a lot didn't sell. "Pass", "no sale", "lot 14 didn't sell".

    The second-most-common thing an auctioneer says, and it had no action: ``set_lot_winner`` and
    ``undo_sale`` bracketed the in-person flow with nothing in the middle. It is not destructive in
    the sense the navigate-only exemptions mean -- it is the ordinary outcome for a good fraction of
    lots, it writes one row of history, and ``undo_sale``'s own page can put the lot back.

    Runs ``DynamicSetLotWinner.end_unsold`` on a real instance of the view, so the history entry,
    the lot-page websocket message and the queue advance are literally the "not sold" button's own.
    """
    from .views import DynamicSetLotWinner

    user = request.user
    auction, error = resolve_auction(user, _str(params, "auction"), _page(request))
    if error:
        return _error(error)
    if not _is_auction_admin(user, auction):
        return _error(f"You don't have permission to end lots in {auction.title}.")
    if auction.is_online:
        return _error(f"{auction.title} is an online auction — lots end on their own when time runs out.")

    lot_hint = _str(params, "lot")
    if not lot_hint:
        return _need("Which lot number didn't sell?")

    view = DynamicSetLotWinner()
    view.request = request
    view.auction = auction
    view.kwargs = {}
    lot, lot_error = view.validate_lot(lot_hint, "end_unsold")
    if lot_error:
        if lot and (lot.winner or lot.auctiontos_winner):
            # The view's own message ("This lot has already been sold") is correct and is kept, but
            # on its own it is a dead end. Saying "no sale" about something already sold is far more
            # likely to be a misheard lot number than a request to wipe the sale -- and wiping one
            # is undo_sale's job, so point at it by name.
            return _error(
                f"{lot_error} — lot {lot.lot_number_display} went for {lot.winning_price}. "
                f"Say “undo lot {lot.lot_number_display}” if that was wrong."
            )
        return _error(str(lot_error))
    if not lot:
        return _error(f"I couldn't find lot {lot_hint} in {auction.title}.")
    message = view.end_unsold(lot)
    auction.create_history(
        applies_to="LOTS",
        action=f"Marked lot {lot.lot_number_display} as ended without being sold (command palette)",
        user=user,
    )
    result: dict[str, Any] = {}
    view.pop_queue_and_set_next(lot, result)
    next_lot = result.get("next_queued_lot_number")
    summary = str(message or f"Lot {lot.lot_number_display} didn't sell.")
    if next_lot:
        summary += f" Lot {next_lot} is up next."
    return _ok(
        summary,
        lot_id=lot.pk,
        lot_name=lot.lot_name,
        auction=auction.slug,
        undo={
            "action": "undo_sale",
            "params": {"lot": lot.lot_number_display, "auction": auction.slug},
            "describes": f"passing on lot {lot.lot_number_display}",
        },
    )


def draw_door_prize(request, params: dict[str, Any]) -> dict[str, Any]:
    """Pick a door prize winner at random from the people who have checked in.

    A live-event moment where saying it out loud genuinely beats finding a page, and the blast
    radius is one timestamp. The draw is ``services.draw_door_prize`` -- the same pool, rule and
    cryptographic RNG the door prize page uses -- so a winner drawn here shows up on that page and
    can never be drawn twice.
    """
    from .services import draw_door_prize as pick_a_winner
    from .views import user_can_add_edit_people

    user = request.user
    auction, error = resolve_auction(user, _str(params, "auction"), _page(request))
    if error:
        return _error(error)
    if not (_is_auction_admin(user, auction) or user_can_add_edit_people(user, auction)):
        return _error(f"You don't have permission to draw door prizes in {auction.title}.")
    winner = pick_a_winner(auction, acting_user=user)
    if not winner:
        checked_in = AuctionTOS.objects.filter(auction=auction, checked_in__isnull=False).exists()
        if not checked_in:
            return _error(f"Nobody has checked in to {auction.title} yet, so there's no one to draw from.")
        return _error(f"Everyone who's checked in to {auction.title} has already won a door prize.")
    return _ok(
        f"{winner.name} wins! (bidder {winner.bidder_number})",
        bidder_number=winner.bidder_number,
        auction=auction.slug,
        followups=[
            {"label": "Door prize winners", "url": reverse("auction_door_prizes", kwargs={"slug": auction.slug})}
        ],
    )


# --- check_in ----------------------------------------------------------------


def check_in(request, params: dict[str, Any]) -> dict[str, Any]:
    """Check a participant in to an in-person auction (admins / club staff only)."""
    from .views import user_can_add_edit_people

    user = request.user
    auction, error = resolve_auction(user, _str(params, "auction"), _page(request))
    if error:
        return _error(error)
    # Same question AuctionViewMixin.can_add_edit_people asks, in the same order.
    if not (_is_auction_admin(user, auction) or user_can_add_edit_people(user, auction)):
        return _error(f"You don't have permission to check people in to {auction.title}.")
    if not auction.use_check_in_mode:
        return _error(f"{auction.title} doesn't use check-in.")

    tos, problem = resolve_person(user, auction, _str(params, "person") or _str(params, "bidder"))
    if problem:
        return problem
    already = bool(tos.checked_in)
    check_in_auctiontos(
        tos,
        acting_user=user,
        bidder_number=_str(params, "bidder_number"),
        note="(command palette)",
    )
    who = tos.name or f"bidder {tos.bidder_number}"
    if already:
        return _ok(
            f"{who} was already checked in to {auction.title}.",
            auction=auction.slug,
            bidder_number=tos.bidder_number,
        )
    return _ok(
        f"Checked {who} in to {auction.title} as bidder {tos.bidder_number}.",
        auction=auction.slug,
        bidder_number=tos.bidder_number,
    )


# --- add_person --------------------------------------------------------------


def add_person(request, params: dict[str, Any]) -> dict[str, Any]:
    """Add a person to an auction (admins / club staff only).

    The counterpart to ``add_lot``, and the reason "add mike smith" doesn't become a lot called
    "Mike Smith": without this the model's only "add" verb was ``add_lot``, so a person's name had
    nowhere else to go.

    Validation is entirely :class:`auctions.forms.QuickAddTOS` -- the bulk-add page's own form,
    built through ``quick_add_tos_form_class`` with the same fields -- so the duplicate-bidder-number
    and duplicate-email rules are the page's rules, not a second copy of them.

    In a **club-managed** auction the ClubMember owns the bidder number and the permissions, so this
    creates or finds that member first and adopts its shadow participant row, exactly like the app's
    offline "add user" op (``auctions.mobile.services.offline``). Skipping that step is not a
    cosmetic difference: it produced a participant with a bidder number the club had never heard of,
    invisible to every club admin screen, and whose number could not even be corrected afterwards --
    the participant edit form hides that field in club-managed mode precisely because the club is
    supposed to own it.
    """
    from .forms import quick_add_tos_form_class
    from .views import user_can_add_edit_people

    user = request.user
    auction, error = resolve_auction(user, _str(params, "auction"), _page(request))
    if error:
        return _error(error)
    if not (_is_auction_admin(user, auction) or user_can_add_edit_people(user, auction)):
        return _error(f"You don't have permission to add people to {auction.title}.")

    name = _str(params, "name") or _str(params, "person")
    if not name:
        return _need("What's their name?")

    existing = AuctionTOS.objects.filter(auction=auction, name__iexact=name).first()
    if existing:
        return _error(
            f"{existing.name} is already in {auction.title} as bidder {existing.bidder_number}."
            if existing.bidder_number
            else f"{existing.name} is already in {auction.title}."
        )

    location = auction.location_qs.first()
    if not location:
        return _error(f"{auction.title} doesn't have a pickup location yet, so nobody can be added to it.")
    data = {
        "name": name,
        "email": _str(params, "email"),
        "phone_number": _str(params, "phone_number"),
        "address": _str(params, "address"),
        "bidder_number": _str(params, "bidder_number"),
        "pickup_location": location.pk,
    }
    form = quick_add_tos_form_class()(data, auction=auction, bidder_numbers_on_this_form=[])
    if not form.is_valid():
        return _form_problem(form)
    member, _created = ensure_club_member(
        auction,
        name=name,
        email=data["email"],
        phone_number=data["phone_number"],
        address=data["address"],
        bidder_number=data["bidder_number"],
    )
    # Creating a ClubMember also creates its shadow participant row (see
    # ``signals.propagate_clubmember_to_shadow_tos``), so adopt that one. A second row for the same
    # person in one auction means two invoices and two bidder numbers.
    tos = existing_tos_for_club_member(auction, member)
    if tos is None:
        tos = form.save(commit=False)
        tos.auction = auction
    else:
        existing_name = (tos.name or "").strip()
        if existing_name and existing_name.casefold() not in {name.casefold(), "unknown"}:
            # ``ensure_club_member`` matches on email, so "add jane doe, jane@x" can land on the
            # member record that email already belongs to -- and adopting its row would rename a
            # real participant to somebody else. The form's duplicate-email rule catches this
            # whenever the shadow row carries the same address, but the member's email and the
            # participant row's can differ. Refuse and name the collision rather than merging two
            # people on the strength of an address.
            return _error(
                f"{data['email'] or name} already belongs to {existing_name} in {auction.title}. "
                f"Add {name} with a different email, or edit {existing_name} instead."
            )
        tos.name = name
        for field in ("email", "phone_number", "address"):
            if data[field]:
                setattr(tos, field, data[field])
        tos.pickup_location = tos.pickup_location or location
    tos.manually_added = True
    apply_club_member_to_tos(auction, tos, member)
    tos.save()
    auction.create_history(
        applies_to="USERS",
        action=f"Added {tos.name} (command palette)",
        user=user,
    )
    followups = []
    if tos.bidder_number:
        # Adding somebody at the door is almost always followed by taking their lots.
        followups.append(
            {
                "label": f"Add lots for {tos.name}",
                "url": reverse("bulk_add_lots", kwargs={"slug": auction.slug, "bidder_number": tos.bidder_number}),
            }
        )
    missing = [label for label, value in (("email", tos.email), ("phone number", tos.phone_number)) if not value]
    if missing:
        # Someone added by voice at the door has a name and nothing else, and nothing on screen said
        # so -- the next person to look them up found a blank contact card and no idea it was blank
        # on purpose. Say it, and put the place to fix it one click away.
        followups.append({"label": f"Add {tos.name}'s details", "url": _edit_person_url(auction, tos)})
    followups.append(
        {"label": f"Everyone in {auction.title}", "url": reverse("auction_tos_list", kwargs={"slug": auction.slug})}
    )
    summary = f"Added {tos.name} to {auction.title} as bidder {tos.bidder_number}."
    if missing:
        summary += f" No {' or '.join(missing)} yet — tell me it, or use the link below."
    return _ok(summary, followups=followups, bidder_number=tos.bidder_number, person=tos.name, auction=auction.slug)


# --- update_person -----------------------------------------------------------

#: Contact fields ``update_person`` may write, and what to call each one in a sentence.
_CONTACT_FIELDS = (
    ("email", "email"),
    ("phone_number", "phone number"),
    ("address", "address"),
    ("name", "name"),
)

#: The rest of what the participant edit modal can set, and what to call each one.
#:
#: ``memo`` is here because the note field had its own endpoint and no skill, so "note that bob paid
#: cash" had nowhere to go. The two permission flags are here because "let bob bid" is a sentence
#: somebody says at a check-in desk roughly hourly, and because refusing to say it left the model
#: reaching for ``check_in`` -- which was, in fairness, the closest thing on the list.
#:
#: Deliberately *not* ``is_admin``: handing somebody administrative control of an auction is not a
#: thing to do on the strength of a misheard sentence, and the participant edit modal is one click
#: away for anyone who means it.
_PERSON_ADMIN_FIELDS = (
    ("bidder_number", "bidder number"),
    ("memo", "note"),
    ("bidding_allowed", "bidding"),
    ("selling_allowed", "selling"),
)

_PERSON_FIELDS = _CONTACT_FIELDS + _PERSON_ADMIN_FIELDS


def _change_phrase(label: str, value: Any) -> str:
    """ "bidding on", "email to bob@example.com" -- how one change reads back to the user."""
    if isinstance(value, bool):
        return f"{label} {'on' if value else 'off'}"
    return f"{label} to {value}"


def update_person(request, params: dict[str, Any]) -> dict[str, Any]:
    """Change a participant's contact details (admins / club staff only).

    The obvious companion to ``add_person``, and its absence was doing real damage rather than just
    leaving a gap: with no verb for "change bob's email", the model reached for whichever registered
    action was nearest, and the nearest one to "set bob's phone number to 555-1212" is ``check_in``,
    which takes a ``bidder_number`` -- so the user got a countdown, watched it run, and nothing about
    the phone number changed.

    Validation is :class:`auctions.forms.CreateEditAuctionTOS`, the same form behind the participant
    edit modal, so the duplicate-email and duplicate-bidder-number rules are the page's rules. In a
    club-managed auction the ClubMember owns these fields, so the change is written there and copied
    down -- editing only the participant row would be undone the next time the member syncs.
    """
    from .forms import CreateEditAuctionTOS
    from .views import user_can_add_edit_people

    user = request.user
    auction, error = resolve_auction(user, _str(params, "auction"), _page(request))
    if error:
        return _error(error)
    if not (_is_auction_admin(user, auction) or user_can_add_edit_people(user, auction)):
        return _error(f"You don't have permission to change people in {auction.title}.")

    tos, problem = resolve_person(user, auction, _str(params, "person") or _str(params, "name"))
    if problem:
        return problem

    changes: dict[str, Any] = {key: _str(params, key) for key, _label in _PERSON_FIELDS if _str(params, key)}
    # ``name`` is how the person was found as well as something that can be changed, so it only
    # counts as a change when it was sent as ``new_name``. Otherwise "change bob's email" would be
    # read as "rename bob to bob".
    changes.pop("name", None)
    if not changes.get("phone_number") and _str(params, "phone"):
        changes["phone_number"] = _str(params, "phone")
    new_name = _str(params, "new_name")
    if new_name:
        changes["name"] = new_name
    for key in ("bidding_allowed", "selling_allowed"):
        # Sent as booleans, so "was it sent" and "what was it set to" are different questions:
        # ``selling_allowed: false`` is the whole point of "stop bob selling anything".
        if key in params:
            changes[key] = bool(params[key])
    if not changes:
        return _need(
            f"What should I change about {tos.name}? I can set their email, phone, address, "
            "bidder number, or whether they can bid or sell."
        )

    # The form's own field list, read off the form, so adding a field to the participant edit modal
    # doesn't quietly make every palette update fail validation on a field it never sent.
    data = model_to_dict(tos, fields=CreateEditAuctionTOS.Meta.fields)
    data = {key: ("" if value is None else value) for key, value in data.items()}
    data.update(changes)
    form = CreateEditAuctionTOS(data=data, auction=auction, is_edit_form=True, auctiontos=tos)
    if not form.is_valid():
        return _form_problem(form)

    member = tos.clubmember if auction.is_club_managed else None
    # Read before anything is written, so "undo that" can put back exactly what was there. The
    # bidder number is captured too and used to find them again: a rename would otherwise leave the
    # undo looking for somebody who no longer answers to that name.
    previous = {name: getattr(tos, name) for name, _label in _PERSON_FIELDS if name in changes}
    was_bidder_number = tos.bidder_number
    for name, _label in _PERSON_FIELDS:
        if name in changes:
            setattr(tos, name, form.cleaned_data[name])
            if member:
                setattr(member, name, form.cleaned_data[name])
    if member:
        # The ClubMember post_save signal only syncs the bidder number and the two permission flags
        # down to shadow rows, so the participant row is saved on its own below regardless.
        member.save(update_fields=sorted(changes))
    tos.save()
    auction.create_history(
        applies_to="USERS",
        action=f"Changed {', '.join(label for key, label in _PERSON_FIELDS if key in changes)} "
        f"for {tos.name} (command palette)",
        user=user,
    )
    told = ", ".join(_change_phrase(label, form.cleaned_data[key]) for key, label in _PERSON_FIELDS if key in changes)
    undo_params: dict[str, Any] = {
        "person": tos.bidder_number or was_bidder_number or tos.name,
        "auction": auction.slug,
    }
    for key, value in previous.items():
        # ``name`` is the one field whose parameter is spelled differently on the way back in --
        # ``person`` finds them, ``new_name`` renames them.
        undo_params["new_name" if key == "name" else key] = "" if value is None else value
    return _ok(
        f"Set {tos.name}'s {told}.",
        followups=[{"label": f"{tos.name}'s details", "url": _edit_person_url(auction, tos)}],
        bidder_number=tos.bidder_number,
        person=tos.name,
        auction=auction.slug,
        undo={"action": "update_person", "params": undo_params, "describes": f"the change to {tos.name}"},
    )


# --- searching lots ----------------------------------------------------------


def search_lots(request, params: dict[str, Any]) -> dict[str, Any]:
    """Open the lot list filtered to what the user is looking for.

    "find shrimp in this auction" wants to *see* the shrimp, not be told how many there are. This
    builds the same URL the search box on the lot list produces (``?q=`` plus ``?auction=``, both
    read by :class:`auctions.filters.LotFilter`), so the answer is the real, filterable,
    sortable page rather than a list the palette has to re-implement.
    """
    term = _str(params, "query") or _str(params, "q") or _str(params, "name")
    if not term:
        return _error("What should I search for?")
    query: dict[str, str] = {"q": term}
    where = ""
    # Only scope to an auction when the user meant one. "find shrimp" across the whole site is a
    # perfectly good search, and defaulting it to their last auction would silently hide results.
    hint = _str(params, "auction")
    if hint or params.get("this_auction") or (_page(request).get("auction") and not params.get("everywhere")):
        auction, error = resolve_auction(request.user, hint, _page(request))
        if error:
            return _error(error)
        query["auction"] = auction.slug
        where = f" in {auction.title}"
    return _ok(
        f"Searching for “{term}”{where}.",
        url=reverse("allLots") + "?" + urlencode(query),
        **({"auction": query["auction"]} if "auction" in query else {}),
    )


# --- read-only lookups -------------------------------------------------------


def find_person(request, params: dict[str, Any]) -> dict[str, Any]:
    """Look someone up among the club members and auction participants the user administers.

    Uses the palette's own scoped searches, so it can never surface someone the user has no
    administrative relationship with.
    """
    user = request.user
    query = _str(params, "name") or _str(params, "query")
    if not query:
        return _error("Give me a name, email or bidder number to look for.")
    people = []
    for item in command_palette._member_search_items(user, query):
        people.append({"kind": "club_member", "name": item["title"], "detail": item["subtitle"], "url": item["url"]})
    for item in command_palette._auctiontos_search_items(user, query):
        people.append({"kind": "participant", "name": item["title"], "detail": item["subtitle"], "url": item["url"]})
    # Participants of the current auction, but only for someone who administers it -- the palette's
    # own participant search is scoped to _admin_auction_ids for exactly this reason, and a plain
    # attendee must not be able to enumerate the room's names and bidder numbers.
    auction = command_palette._last_auction(user)
    if auction and _is_auction_admin(user, auction):
        for tos in AuctionTOS.objects.filter(auction=auction).filter(
            Q(name__icontains=query) | Q(bidder_number__iexact=query)
        )[:AMBIGUOUS_LIMIT]:
            people.append(
                {
                    "kind": "participant",
                    "name": tos.name or tos.email or "",
                    "bidder_number": tos.bidder_number,
                    "auction": auction.title,
                }
            )
    if not people:
        return {"found": False, "people": [], "summary": f"Nobody matching “{query}”."}
    return {"found": True, "people": people[:AMBIGUOUS_LIMIT], "summary": f"{len(people)} match(es) for “{query}”."}


def my_context(request, params: dict[str, Any]) -> dict[str, Any]:
    """Who the user is and what they're currently working on: clubs, last auction, role."""
    return user_context(request.user, _page(request))


def lot_fields_in_use(auction) -> dict[str, Any]:
    """The optional per-lot fields this auction has switched on, under the club's own labels.

    ``add_lot`` has always applied ``i_bred_this_fish``, ``custom_field_1`` and ``custom_dropdown``,
    but they lived in ``aliases`` -- which deliberately never reaches the prompt -- so the model had
    no reason to ever send them. "Add some guppies, I bred these" silently dropped the breeder flag,
    which is the one flag a BAP club actually cares about.

    Promoting them to documented parameters is half the fix; the other half is this, because
    "custom_field_1" means nothing to anybody. The club named these fields ("Scientific name",
    "CARES species"), and the name is the only way the model can tell that the words in front of it
    belong in one. Empty for the great majority of auctions, which use none of them.
    """
    fields: dict[str, Any] = {}
    if auction.use_i_bred_this_fish_field:
        fields["i_bred_this_fish"] = {
            "label": "Breeder points",
            "means": "the seller bred or grew this themselves",
        }
    if auction.custom_field_1 != "disable":
        fields["custom_field_1"] = {
            "label": auction.custom_field_1_name or "Notes",
            "required": auction.custom_field_1 == "required",
        }
    if auction.use_custom_dropdown_field != "disable":
        from .models import AuctionDropdown

        # The same rows the add-lot page's dropdown is built from. Sent so the model picks one of
        # the club's own options rather than inventing a plausible-sounding value, which would go
        # straight onto the printed label.
        options = list(
            AuctionDropdown.objects.filter(auction=auction).order_by("createdon").values_list("value", flat=True)[:20]
        )
        fields["custom_dropdown"] = {
            "label": auction.custom_dropdown_name or "Category",
            "required": auction.use_custom_dropdown_field == "required",
            "options": options,
        }
    return fields


def _missing_required_lot_fields(auction, params: dict[str, Any]) -> str | None:
    """A question naming the fields this auction requires on every lot and the command didn't give.

    The quick-add form would reject the lot for these anyway, but its message names the database
    column. Asking by the club's own label ("What should I put for Scientific name?") is the
    difference between a question the user can answer and one they can't.
    """
    wanted = []
    for key, spec in lot_fields_in_use(auction).items():
        if spec.get("required") and not _str(params, key):
            wanted.append(str(spec["label"]))
    if not wanted:
        return None
    return f"{auction.title} needs {' and '.join(wanted)} on every lot. What should I put?"


def _auction_facts(user, slug: str) -> dict[str, Any] | None:
    """The handful of facts about the auction on screen that questions are actually asked about.

    "Is this an online auction?" and "when does this auction start?" are the two most obvious things
    to ask the box while looking at an auction, and neither was answerable: the context block named
    the page and the auction's title and stopped there, so the model had nothing and answered from
    what it knew about auction sites in general -- "Yes", about an in-person auction.

    Cheap enough to send every time (a dozen short fields), and it saves the round trip a describe_*
    lookup would have cost, on the queries most likely to be typed here.

    *slug* comes from the path the user is on, so this deliberately doesn't re-scope it: every field
    below is on the auction's own page, which is public to anyone holding the slug. What it does say
    is whether they have joined -- without that the model reads the rest of this block as "you are in
    this auction" and offers to add lots to an auction the user is only reading about.
    """
    from .models import Auction

    auction = Auction.objects.filter(slug=slug, is_deleted=False).first()
    if not auction:
        return None
    tos = _own_tos(user, auction)
    return {
        "title": auction.title,
        "is_online": auction.is_online,
        # Spelled out both ways round: the model reads a bare ``"is_online": false`` past a question
        # phrased in the positive far too easily.
        "format": "online auction" if auction.is_online else "in-person auction",
        "starts": local_time(auction, auction.date_start),
        "ends": local_time(auction, auction.date_end),
        "lot_submission_closes": local_time(auction, auction.lot_submission_end_date),
        "lot_submission_open_now": bool(auction.can_submit_lots),
        "over": bool(auction.pretty_much_over),
        "you_are_an_admin": _is_auction_admin(user, auction),
        "you_have_joined": bool(tos),
        "your_bidder_number": tos.bidder_number if tos else None,
        "lot_fields_this_auction_uses": lot_fields_in_use(auction),
        # Said in words as well as in a boolean, for the same reason ``format`` is: this is the fact
        # that decides whether the next sentence should be an action or "you'd have to join first".
        "note": (
            f"You are looking at {auction.title}."
            if tos
            else (
                f"The user is looking at {auction.title} but has NOT joined it. They must join "
                "before they can bid, sell or be given a bidder number — send them to this "
                "auction's page (go_to_page auction_main) to do that."
            )
        ),
    }


def user_context(user, page: dict[str, Any] | None = None) -> dict[str, Any]:
    """The compact context block handed to the model with every assist request.

    Deliberately small: a username, the palette club, the last auction and the user's role in it,
    their memberships, and the page they're looking at right now -- including, when that page is an
    auction, the few facts about it people ask for by name (see :func:`_auction_facts`). Enough for
    "renew my membership" to know which club is meant, for "add a lot" to mean the auction on
    screen, and for "when does this start?" to be answered without a lookup.
    """
    auction = command_palette._last_auction(user)
    club = command_palette._palette_club(user)
    memberships = []
    for member in ClubMember.objects.filter(user=user, is_deleted=False).select_related("club")[:10]:
        if not member.club:
            continue
        memberships.append(
            {
                "club": member.club.name,
                "slug": member.club.slug,
                "expires": member.membership_expiration_date.strftime("%Y-%m-%d")
                if member.membership_expiration_date
                else None,
            }
        )
    data: dict[str, Any] = {
        "username": user.username,
        "palette_club": club.name if club else None,
        "memberships": memberships,
        "admin_clubs": [c.name for c in command_palette._admin_clubs(user)],
    }
    if page:
        data["looking_at_right_now"] = dict(page)
        facts = _auction_facts(user, page["auction"]) if page.get("auction") else None
        if facts:
            data["looking_at_right_now"]["this_auction"] = facts
    if auction:
        tos = _own_tos(user, auction)
        data["last_auction"] = {
            "title": auction.title,
            "slug": auction.slug,
            "is_online": auction.is_online,
            "is_admin": _is_auction_admin(user, auction),
            "joined": bool(tos),
            "bidder_number": tos.bidder_number if tos else None,
            "lot_submission_open": bool(auction.can_submit_lots),
            "over": bool(auction.pretty_much_over),
            "uses_check_in": bool(auction.use_check_in_mode),
        }
    else:
        data["last_auction"] = None
    return data


# --- navigate-only -----------------------------------------------------------


def print_labels(request, params: dict[str, Any]) -> dict[str, Any]:
    """Resolve the right label-printing page. Never prints -- printing is a page the user opens.

    ``scope`` picks between the user's own labels, only their unprinted ones, the whole-auction
    admin printing page, or a single lot (which is what "print that label" after adding a lot
    resolves to, via the lot id carried in the conversation context).
    """
    user = request.user
    # A lot named in the request wins; otherwise the lot whose page they're standing on, which is
    # what "print this label" means when you're looking at a lot.
    lot_id = _int(params, "lot_id") or _page(request).get("lot_id")
    if lot_id:
        lot = Lot.objects.filter(pk=lot_id, is_deleted=False).first()
        if not lot:
            return _error("I couldn't find that lot any more.")
        return _ok(
            f"Opening the label for {lot.lot_name}.",
            url=reverse("single_lot_label", kwargs={"pk": lot.pk}),
        )
    auction, error = resolve_auction(user, _str(params, "auction"), _page(request))
    if error:
        return _error(error)
    scope = (_str(params, "scope") or "mine").lower()
    # "Print bidder 14's labels" is the front-desk task, and the only scope this action was missing
    # even though the routes for it have always existed. A bidder number wins over ``scope``: naming
    # somebody is a more specific instruction than any of the words in the enum.
    bidder = _str(params, "bidder") or _str(params, "bidder_number")
    if bidder:
        if not _is_auction_admin(user, auction):
            return _error(f"Only admins can print someone else's labels in {auction.title}.")
        tos, problem = resolve_person(user, auction, bidder)
        if problem:
            return problem
        if not tos.bidder_number:
            return _error(f"{tos.name} doesn't have a bidder number yet, so there are no labels to print.")
        unprinted = scope in {"unprinted", "new"}
        route = "print_unprinted_labels_by_bidder_number" if unprinted else "print_labels_by_bidder_number"
        return _ok(
            f"Opening {'unprinted ' if unprinted else ''}labels for {tos.name or 'bidder'} "
            f"(bidder {tos.bidder_number}).",
            url=reverse(route, kwargs={"slug": auction.slug, "bidder_number": tos.bidder_number}),
            bidder_number=tos.bidder_number,
        )
    if scope in {"auction", "all", "everyone"}:
        if not _is_auction_admin(user, auction):
            return _error(f"Only admins can print everyone's labels in {auction.title}.")
        return _ok(
            f"Opening the printing page for {auction.title}.",
            url=reverse("auction_printing", kwargs={"slug": auction.slug}),
        )
    if scope in {"unprinted", "new"}:
        return _ok(
            f"Opening your unprinted labels for {auction.title}.",
            url=reverse("print_my_unprinted_labels", kwargs={"slug": auction.slug}),
        )
    return _ok(
        f"Opening your labels for {auction.title}.",
        url=reverse("print_my_labels", kwargs={"slug": auction.slug}),
    )


#: Spoken phrasings that name a preference the field's own verbose name and help text don't.
#:
#: Everything else is matched against the model's own words (see :func:`_resolve_preference`), which
#: is why this list is short: it only has to cover the settings people ask for by a name nobody
#: wrote on the form. "Dark mode" is here and resolves to nothing on purpose -- see below.
_PREFERENCE_ALIASES = {
    "email": "email_visible",
    "show my email": "email_visible",
    "hide my email": "email_visible",
    "show my username": "username_visible",
    "anonymous": "username_visible",
    "units": "distance_unit",
    "miles": "distance_unit",
    "kilometers": "distance_unit",
    "km": "distance_unit",
    "currency": "preferred_currency",
    "new auction emails": "email_me_about_new_auctions",
    "auction emails": "email_me_about_new_auctions",
    "in person auction emails": "email_me_about_new_in_person_auctions",
    "chat emails": "email_me_about_new_chat_replies",
    "comment emails": "email_me_when_people_comment_on_my_lots",
    "reminder emails": "send_reminder_emails_about_joining_auctions",
    "push notifications": "push_notifications_instead_of_email",
    "push": "push_notifications_instead_of_email",
    "notify me when lots sell": "push_notifications_when_lots_sell",
    "selling notifications": "push_notifications_when_lots_sell",
    "nearby auctions": "show_nearby_auctions",
    "share my photos": "share_lot_images",
    "share images": "share_lot_images",
    "add images automatically": "auto_add_images",
}

#: Words that mean "on" and "off" when a preference is a checkbox.
_TRUTHY = frozenset({"on", "yes", "true", "enable", "enabled", "show", "1"})
_FALSY = frozenset({"off", "no", "false", "disable", "disabled", "hide", "stop", "0"})


def _preference_fields() -> tuple[list[str], Any]:
    """The preferences form's field list and the model behind it, imported lazily."""
    from .forms import ChangeUserPreferencesForm
    from .models import UserData

    return list(ChangeUserPreferencesForm.Meta.fields), UserData


def _resolve_preference(hint: str) -> str | None:
    """Turn what somebody said into one field name on the preferences form, or ``None``.

    Matched against the field's own name, verbose name and help text -- the same three things
    ``command_palette._user_pref_field_items`` searches to decide which preference a query is about,
    so the palette's search and this action agree on what "username" means. An exact alias wins, a
    field-name match beats a verbose-name match, and a help-text-only match is last, because help
    text is a sentence and a sentence matches loosely.
    """
    hint = (hint or "").strip().lower()
    if not hint:
        return None
    fields, model = _preference_fields()
    normalized = re.sub(r"[^a-z0-9 ]+", " ", hint)
    normalized = " ".join(normalized.split())
    if normalized in _PREFERENCE_ALIASES:
        return _PREFERENCE_ALIASES[normalized]
    underscored = normalized.replace(" ", "_")
    if underscored in fields:
        return underscored
    best = None
    for candidate in model._meta.get_fields():
        if candidate.name not in fields:
            continue
        verbose = str(getattr(candidate, "verbose_name", "") or "").lower()
        help_text = str(getattr(candidate, "help_text", "") or "").lower()
        if normalized and normalized in candidate.name.replace("_", " "):
            return candidate.name
        if verbose and normalized in verbose:
            best = best or candidate.name
        elif help_text and normalized in help_text and best is None:
            best = candidate.name
    return best


def update_preferences(request, params: dict[str, Any]) -> dict[str, Any]:
    """Change one of the user's own preferences without sending them to the preferences page.

    "Stop emailing me about new auctions", "switch me to kilometres", "hide my email" are one-line
    requests that became a navigation to a page with thirty checkboxes on it.

    Saved through :class:`auctions.forms.ChangeUserPreferencesForm` -- the preferences page's own
    form, with the same ``user`` kwarg -- so the distance-unit conversion, the push-notification
    gating and every other rule on that page apply here identically. Deliberately one setting at a
    time: a countdown card the user is meant to read in five seconds can name one change honestly
    and cannot name six.
    """
    from .forms import ChangeUserPreferencesForm

    user = request.user
    userdata = getattr(user, "userdata", None)
    if userdata is None:
        return _error("I couldn't find your preferences.")
    hint = _str(params, "setting") or _str(params, "preference") or _str(params, "name")
    field_name = _resolve_preference(hint)
    if not field_name:
        return _need(
            f"I'm not sure which setting “{hint}” is. Which one did you mean?"
            if hint
            else "Which setting should I change?",
            [
                {"label": "Emails about new auctions", "value": "email me about new auctions"},
                {"label": "Miles or kilometres", "value": "distance unit"},
                {"label": "Whether my email is visible", "value": "email visible"},
            ],
        )
    fields, model = _preference_fields()
    field = model._meta.get_field(field_name)

    # The unbound form first: its ``initial`` holds the *display* values (distances converted to km
    # for a km user), and its ``clean`` converts them back on the way in. Building the POST data out
    # of the raw model values instead would put miles into a form that is about to treat them as
    # kilometres, and silently shrink three of the user's search radii every time they changed any
    # unrelated checkbox.
    unbound = ChangeUserPreferencesForm(user, instance=userdata)
    data = model_to_dict(userdata, fields=fields)
    data.update(unbound.initial)

    raw = params.get("value")
    if isinstance(field, models.BooleanField):
        value = _preference_boolean(raw)
        if value is None:
            return _need(f"Should “{field.verbose_name or field_name}” be on or off?")
        # An unchecked checkbox is an absent key, not False -- this is a real POST body.
        if value:
            data[field_name] = True
        else:
            data.pop(field_name, None)
    elif raw in (None, ""):
        return _need(f"What should “{field.verbose_name or field_name}” be?")
    else:
        data[field_name] = raw
    was = getattr(userdata, field_name)

    form = ChangeUserPreferencesForm(user, data, instance=userdata)
    if not form.is_valid():
        return _form_problem(form)
    form.save()
    userdata.refresh_from_db()
    now = getattr(userdata, field_name)
    if was == now:
        return _ok(f"“{_preference_label(field)}” was already {_preference_phrase(field, now)}.")
    return _ok(
        f"Set “{_preference_label(field)}” to {_preference_phrase(field, now)}.",
        followups=[{"label": "All my preferences", "url": reverse("preferences")}],
        undo={
            "action": "update_preferences",
            "params": {"setting": field_name, "value": was},
            "describes": f"the change to “{_preference_label(field)}”",
        },
    )


def _preference_boolean(raw) -> bool | None:
    """A spoken on/off as a boolean, or ``None`` when it wasn't one."""
    if isinstance(raw, bool):
        return raw
    if raw is None:
        # "turn off new auction emails" carries the value in the verb, and the model routinely sends
        # only the setting. Defaulting either way would be a guess, so this is left to the caller.
        return None
    text = str(raw).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return None


def _preference_label(field) -> str:
    return str(getattr(field, "verbose_name", "") or field.name.replace("_", " "))


def _preference_phrase(field, value) -> str:
    """A stored preference value said the way the form says it, not the way the database stores it."""
    choices = getattr(field, "choices", None)
    if choices:
        for stored, label in choices:
            if stored == value:
                return str(label)
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value)


def join_auction(request, params: dict[str, Any]) -> dict[str, Any]:
    """Take the user to an auction's page so they can join it. Never joins on their behalf.

    "Sign me up for the fall auction" used to degrade to a generic navigation, because every other
    auction resolver is scoped to ``_joined_auctions`` -- which by definition cannot find the
    auction somebody is asking to join. This one searches what they can *see*, so the answer lands
    on the right auction's page instead of the auction list.

    It stops at the page on purpose, and that is not timidity: joining means agreeing to that
    auction's rules, and agreeing to something on somebody's behalf is not a thing this assistant
    does. Where there is more than one pickup location the choice of location is part of joining and
    is made on that page too, so the answer says which locations there are rather than picking one.
    """
    user = request.user
    hint = _str(params, "auction") or _str(params, "name")
    auction, error = _resolve_described_auction(user, hint, _page(request))
    if error:
        return _error(error)
    tos = _own_tos(user, auction)
    locations = list(auction.location_qs[:10])
    if tos:
        where = tos.pickup_location.name if tos.pickup_location else None
        summary = f"You're already in {auction.title} as bidder {tos.bidder_number or '(number not set yet)'}."
        if where and len(locations) > 1:
            summary += f" Your pickup location is {where}."
        return _ok(summary, url=auction.get_absolute_url(), route="auction_main")
    summary = f"Opening {auction.title} so you can join it — you'll need to agree to its rules."
    if len(locations) > 1:
        summary += " It has more than one pickup location, so pick yours on that page: " + ", ".join(
            location.name for location in locations
        )
    return _ok(summary + ".", url=auction.get_absolute_url(), route="auction_main")


def send_membership_card(request, params: dict[str, Any]) -> dict[str, Any]:
    """Email the user their own club membership card again.

    The admin-side endpoints for this are excused from the skill audit as "reached from an emailed
    link" and "acts on the row you're looking at", and both are true from an admin's side. A member
    asking for their own card is neither: the token is theirs and the row is them. It sends to the
    address already on the membership -- never to one supplied in the command -- so this cannot be
    turned into a way to mail somebody else's card somewhere else.
    """
    from .tasks import send_membership_card_email

    user = request.user
    hint = _str(params, "club")
    members = [
        member
        for member in ClubMember.objects.filter(user=user, is_deleted=False).select_related("club")
        if member.club and member.club.show_member_barcode
    ]
    if not members:
        return _error("None of your clubs issue membership cards.")
    if hint:
        matches = [member for member in members if hint.lower() in (member.club.name or "").lower()] or [
            member for member in members if hint.lower() in (member.club.abbreviation or "").lower()
        ]
    else:
        club = command_palette._palette_club(user)
        matches = [member for member in members if club and member.club_id == club.id] or members
    if not matches:
        return _error(f"I couldn't find a membership at “{hint}” that has a card.")
    if len(matches) > 1:
        return _need(
            "Which club's card?",
            [{"label": member.club.name, "value": member.club.name} for member in matches],
        )
    member = matches[0]
    if not member.email:
        return _error(
            f"Your {member.club.name} membership has no email address on it, so there's nowhere to send the card."
        )
    try:
        sent = send_membership_card_email(member)
    except Exception:
        logger.exception("Palette failed to send a membership card to club member %s", member.pk)
        sent = False
    if not sent:
        return _error(f"I couldn't send your {member.club.name} card just now. Try again in a minute.")
    return _ok(f"Sent your {member.club.name} membership card to {member.email}.")


def renew_membership(request, params: dict[str, Any]) -> dict[str, Any]:
    """Take the user to their club's membership payment page. Never takes payment.

    Renewal is money, so this is navigate-only by design: we work out *which* club is meant and
    open that club's payment page, and the user does the rest.
    """
    user = request.user
    hint = _str(params, "club")
    members = list(ClubMember.objects.filter(user=user, is_deleted=False).select_related("club"))
    members = [m for m in members if m.club]
    if hint:
        matches = [m for m in members if hint.lower() in (m.club.name or "").lower()]
        if not matches:
            matches = [m for m in members if hint.lower() in (m.club.abbreviation or "").lower()]
    else:
        club = command_palette._palette_club(user)
        matches = [m for m in members if club and m.club_id == club.id] or members
    if not matches:
        return _error("I couldn't work out which club you mean — you don't have a membership I can see.")
    if len(matches) > 1:
        return _need(
            "Which club's membership?",
            [{"label": m.club.name, "value": m.club.name} for m in matches],
        )
    club = matches[0].club
    return _ok(
        f"Opening the membership payment page for {club.name}.",
        url=reverse("club_membership_pay", kwargs={"slug": club.slug}),
    )


def go_to_page(request, params: dict[str, Any]) -> dict[str, Any]:
    """Open any page on the site, by route key from the catalog in the prompt.

    Three ways in, tried in order, so a slightly-wrong answer from the model still lands somewhere
    sensible instead of erroring:

    1. ``page`` is a key from :data:`auctions.palette_routes.ROUTES` -- the normal case.
    2. ``page`` is free text ("treasurer report") -- matched against the catalog, and an ambiguous
       match comes back as ``more_info_needed`` with the candidates.
    3. Nothing matches -- fall back to the ordinary palette search, which knows about lots,
       people and ``CommandPalettePage`` rows that aren't URLs at all.

    The model never supplies a URL or a primary key: it names a destination, and
    ``palette_routes.resolve_route`` works out the parameters from the user's own scoped objects.
    """
    query = _str(params, "page") or _str(params, "query")
    if not query:
        return _error("Where would you like to go?")

    # Two destinations in the app aren't pages at all: lot scanning and Tap to Pay are native
    # screens behind a fishauctions:// deep link. Checked before the catalog because the catalog
    # holds near misses for both ("tap to pay" is a keyword on the Square payout settings page),
    # and on a phone the native screen is what someone asking for it by name means.
    app_destination = command_palette.app_deep_link_by_name(request, query)
    if app_destination:
        return _ok(f"Opening {app_destination['title']}.", url=app_destination["url"], title=app_destination["title"])

    route = palette_routes.get_route(query)
    if route is None:
        matches = palette_routes.match_routes(query, request.user, limit=4)
        if len(matches) == 1:
            route = matches[0]
        elif matches:
            # Several plausible destinations: ask rather than guess, but keep the keys so the
            # answer comes back as a route rather than another round of free text.
            top = matches[0]
            second = matches[1]
            if top.search_text.count(query.lower()) or len(matches) > 3:
                route = top
            else:
                return _need(
                    f"Did you want {top.label.lower()} or {second.label.lower()}?",
                    [{"label": match.label, "value": match.key} for match in matches],
                )

    if route is not None:
        result = palette_routes.resolve_route(request, route, params)
        if "error" not in result:
            return result
        if result.get("denied"):
            # "You aren't allowed in there" is the answer, not a reason to go looking for somewhere
            # else to put them. Guessing past a refusal would land them on a page they didn't ask
            # for and hide the fact that they don't have access.
            return _error(result["error"])
        problem = result["error"]
    else:
        problem = ""

    # Last resort: the ordinary palette search, which reaches objects (a lot, a person, an invoice)
    # that have no place in a catalog of pages.
    groups = command_palette.search(request, query)
    preferred = [g for g in groups if g["label"] == "Go to"] or groups
    for group in preferred:
        for item in group["items"]:
            if item.get("url"):
                return _ok(f"Opening {item['title']}.", url=item["url"], title=item["title"])
    return _error(problem or f"I couldn't find a page for “{query}”.")


# --- lookups over lots and pages ---------------------------------------------


def find_lot(request, params: dict[str, Any]) -> dict[str, Any]:
    """Look a lot up by number or name, within the auctions the user is part of.

    The counterpart to ``find_person``: turns "the blue shrimp lot" into a lot id the other
    actions can use, without the model ever guessing a primary key.
    """
    from django.db.models import Q

    user = request.user
    query = _str(params, "lot") or _str(params, "query") or _str(params, "name")
    if not query:
        return _error("Give me a lot number or name to look for.")
    auction = None
    hint = _str(params, "auction")
    if hint:
        auction, error = resolve_auction(user, hint)
        if error:
            return _error(error)
    lots = Lot.objects.filter(is_deleted=False)
    if not user.is_superuser:
        lots = lots.filter(Q(user=user) | Q(auction__in=command_palette._joined_auctions(user)))
    if auction:
        lots = lots.filter(auction=auction)
    matches = list(
        lots.filter(Q(custom_lot_number__iexact=query) | Q(lot_name__icontains=query)).select_related("auction")[
            : AMBIGUOUS_LIMIT + 1
        ]
    )
    if not matches:
        return {"found": False, "lots": [], "summary": f"No lot matching “{query}”."}
    return {
        "found": True,
        "lots": [
            {
                "lot_id": lot.pk,
                "lot_number": lot.lot_number_display,
                "name": lot.lot_name,
                "auction": lot.auction.title if lot.auction else None,
                "sold": bool(lot.winner or lot.auctiontos_winner),
                "price": str(lot.winning_price) if lot.winning_price else None,
            }
            for lot in matches[:AMBIGUOUS_LIMIT]
        ],
        "summary": f"{len(matches)} lot(s) matching “{query}”.",
    }


# --- describing things -------------------------------------------------------
#
# The lookups above turn a name into an id so an action can run. These answer questions instead:
# "what are the rules for this auction", "how do I earn BAP points", "how many people are coming".
#
# Two rules hold everywhere in this section:
#
#   1. **Scope first.** Every object is fetched through the same scoped querysets the rest of the
#      palette uses (``_visible_auctions``, ``_joined_auctions``, ``_admin_clubs``), so a lookup can
#      never describe something the user couldn't already open.
#   2. **Public fields, then admin fields.** ``_admin`` blocks are only added once
#      ``_is_auction_admin`` / ``_can_manage_members`` has said yes. Everything outside them is
#      what any visitor to the page would see anyway.


def _settings_block(obj, names: tuple[str, ...]) -> list[dict[str, Any]]:
    """Describe a handful of a model's settings using the model's own ``help_text``.

    The help text on these fields is already a plain-English explanation of the rule -- "Minimum
    days between awarding BAP points for lots with the same name" says it better than anything
    restated here would, and it is the same sentence the club admin read when they set it. Reading
    it out of ``_meta`` rather than copying it means the answer follows the field: change the rule
    or its wording and this follows along, with no second copy to forget about.
    """
    block = []
    for name in names:
        try:
            field = obj._meta.get_field(name)
        except Exception:  # pragma: no cover - a renamed field shouldn't break an answer
            continue
        value = getattr(obj, name, None)
        block.append(
            {
                "setting": str(field.verbose_name),
                "value": value,
                # Capped: a few of these help texts run to a paragraph aimed at the admin setting
                # them up, and the whole block has to fit inside one lookup reply. The first
                # sentence is the rule; the rest is advice for whoever is configuring it.
                "means": plain_text(str(getattr(field, "help_text", "") or ""), limit=MEANS_LIMIT),
            }
        )
    return block


#: How a club awards points. Every one of these carries its own explanation in ``help_text``.
_CLUB_BAP_SETTINGS = (
    "enable_breeder_award_program",
    "points_per_lot",
    "min_quantity",
    "days_between_same_name_lots",
    "points_for_custom_checkbox",
    "only_donation_lots",
    "only_sold_lots",
    "auto_add_points",
    "only_active_members_can_participate",
    "separate_hap",
    "separate_cap",
)

#: The auction settings people actually ask about ("can I use buy now", "how many lots can I bring").
#:
#: The alternate-fee block is here because "what's the split?" is one of the most common questions an
#: auction gets and, without these, the only thing the model could see was the standard percentage.
#: Asked about a split it had no figures for, it invented one -- a "prize split", in an auction with
#: no prizes. A wrong number about money is the worst answer this thing can give.
_AUCTION_SETTINGS = (
    "minimum_bid",
    "buy_now",
    "max_lots_per_user",
    "allow_additional_lots_as_donation",
    "lot_entry_fee",
    "winning_bid_percent_to_club",
    "registration_fee",
    "unsold_lot_fee",
    "alternate_split_mode",
    "alternative_split_label",
    "lot_entry_fee_for_club_members",
    "winning_bid_percent_to_club_for_club_members",
    "registration_fee_for_club_members",
    "pre_register_lot_entry_fee_discount",
    "pre_register_lot_discount_percent",
    "tax",
    "only_approved_sellers",
    "only_approved_bidders",
    "allow_bulk_adding_lots",
    # Not ``use_check_in_mode``: that's a property, so ``_settings_block`` skipped it silently and
    # it was never described at all. This is the field it reads.
    "manage_users_through_club",
)


#: Money-ish auction fields that are deliberately *not* described, with the reason.
#:
#: The list above is hand-written, so it goes stale the moment somebody adds a fee to ``Auction`` --
#: and a fee the assistant can't see is worse than one it has never heard of, because asked about
#: "the split" it answers from the fees it *can* see and sounds just as certain. So every field on
#: ``Auction`` whose name looks like money has to be in one list or the other, and
#: ``auctions/test_palette_assist.py`` fails the build when a new one is in neither.
SETTINGS_NOT_DESCRIBED: dict[str, str] = {
    "reserve_price": "Not a fee: it's the allow/require/disable mode for whether sellers may set one.",
    "bump_cost": "What the site charges to promote a lot in search; nothing to do with auction fees.",
    "lot_promotion_cost": "Site-side advertising cost, not something the club charges anybody.",
    "add_membership_fee_to_invoices_for_expired_members": (
        "The club's membership fee, not the auction's. describe_club answers questions about that."
    ),
}

#: The same rule for :data:`_CLUB_BAP_SETTINGS`. "How do I earn points?" is answered entirely out of
#: that list, so a points rule missing from it is a points rule the answer quietly gets wrong.
POINTS_NOT_DESCRIBED: dict[str, str] = {
    "last_bap_recalculation": "When the totals were last recomputed, not a rule. Already in the admin block.",
    "next_bap_recalculation": "Internal scheduling for the recalculation job; nobody asks the palette about it.",
}


def _resolve_described_auction(user, hint: str, page: dict[str, Any] | None):
    """An auction to describe: the one they named if they can see it, else the one they're on.

    Wider than ``resolve_auction`` on purpose: asking "what are the rules for the spring auction"
    is the question you ask *before* joining, and refusing to answer it until you have joined is
    backwards. A *name* is still scoped through ``_visible_auctions``, so the model can't turn this
    into a search for auctions the user was never shown; a *path* isn't, because the user is looking
    at the page it names.
    """
    from .models import Auction

    visible = command_palette._visible_auctions(user)
    if hint:
        match = visible.filter(Q(slug=hint) | Q(title__iexact=hint)).first()
        if not match:
            match = visible.filter(title__icontains=hint).first()
        if not match:
            return None, f"I couldn't find an auction called “{hint}”."
        return match, None
    page_slug = (page or {}).get("auction")
    if page_slug:
        # Not re-scoped through ``visible``, unlike the hint above: the slug came from the path the
        # browser is on, and every field this lookup returns is already on that page. Scoping it
        # meant "what are the rules for this auction" -- asked *on the rules page* -- answered about
        # some other auction the user had joined months ago.
        current = Auction.objects.filter(slug=page_slug, is_deleted=False).first()
        if current:
            return current, None
    # Re-scoped for the same reason as in ``resolve_auction``: the stored pointer outlives the
    # relationship, and this function's whole contract is that it never describes an auction the
    # user can't see.
    auction = visible.filter(pk=getattr(command_palette._last_auction(user), "pk", None)).first()
    if not auction:
        return None, "I don't know which auction you mean."
    return auction, None


def describe_auction(request, params: dict[str, Any]) -> dict[str, Any]:
    """Everything knowable about one auction: dates, rules text, fees, and (for admins) its stats.

    This is what answers "what are the rules", "when does lot submission close", "how much does the
    club take" and "how many people have signed up" -- questions whose answer is on the page but
    which nobody wants to go and read.
    """
    user = request.user
    auction, error = _resolve_described_auction(user, _str(params, "auction") or _str(params, "name"), _page(request))
    if error:
        return _error(error)
    is_admin = _is_auction_admin(user, auction)
    tos = _own_tos(user, auction)
    data: dict[str, Any] = {
        "title": auction.title,
        "club": auction.club.name if auction.club else None,
        "is_online": auction.is_online,
        "in_person": not auction.is_online,
        "starts": local_time(auction, auction.date_start),
        "ends": local_time(auction, auction.date_end),
        "lot_submission_opens": local_time(auction, auction.lot_submission_start_date),
        "lot_submission_closes": local_time(auction, auction.lot_submission_end_date),
        "lot_submission_open_now": bool(auction.can_submit_lots),
        "over": bool(auction.pretty_much_over),
        "uses_check_in": bool(auction.use_check_in_mode),
        "you_have_joined": bool(tos),
        "your_bidder_number": tos.bidder_number if tos else None,
        "you_are_an_admin": is_admin,
        "pickup_locations": [location.name for location in auction.location_qs[:10]],
        # Fees before rules: the reply is truncated at MAX_LOOKUP_RESULT_CHARS, and if anything has
        # to be lost it must be the prose and not the numbers.
        "settings": _settings_block(auction, _AUCTION_SETTINGS),
    }
    if is_admin or auction.make_stats_public:
        lots = Lot.objects.filter(auction=auction, is_deleted=False)
        data["participants"] = AuctionTOS.objects.filter(auction=auction).count()
        data["lots"] = lots.count()
        data["lots_sold"] = lots.filter(Q(winner__isnull=False) | Q(auctiontos_winner__isnull=False)).count()
    if is_admin:
        # Deliberately not ``cached_stats``: it is the chart data for the stats page, ~700 tokens of
        # labelled series that answer no question anyone asks the palette, and it used to be most of
        # what this lookup spent its budget on.
        data["_admin"] = {
            "checked_in": AuctionTOS.objects.filter(auction=auction, checked_in__isnull=False).count(),
        }
    # The rules are a summernote field; the model is given the words, not the markup, and only as
    # many of them as an answer in a small box can use.
    data["rules"] = plain_text(auction.summernote_description, limit=RULES_LIMIT)
    return {"found": True, "auction": data}


def describe_club(request, params: dict[str, Any]) -> dict[str, Any]:
    """A club: what it is, what membership costs, and exactly how its points are awarded.

    The points rules come out of the model's own help text (see :func:`_settings_block`), which is
    what makes "how do I earn BAP points in my club?" answerable at all -- the rules are a dozen
    interacting settings, not a paragraph anybody wrote down.
    """
    from .models import Club

    user = request.user
    hint = _str(params, "club") or _str(params, "name")
    club = palette_routes._club_from_hint(user, hint or (_page(request).get("club") or ""))
    if club is None and hint:
        club = Club.objects.filter(active=True).filter(Q(name__icontains=hint) | Q(abbreviation__iexact=hint)).first()
    if club is None:
        return _error("I couldn't work out which club you mean.")
    can_manage = command_palette._can_manage_members(user, club)
    membership = ClubMember.objects.filter(user=user, club=club, is_deleted=False).first()
    data: dict[str, Any] = {
        "name": club.name,
        "abbreviation": club.abbreviation,
        "description": plain_text(club.description, limit=DESCRIPTION_LIMIT),
        "contact_email": club.contact_email,
        "membership_enabled": club.enable_membership,
        "annual_membership_fee": club.membership_annual_fee,
        "your_membership_expires": membership.membership_expiration_date if membership else None,
        "you_are_a_member": bool(membership),
        "you_can_manage_members": can_manage,
        "points_program": _settings_block(club, _CLUB_BAP_SETTINGS),
        "category_point_overrides": [
            {"category": str(override.category), "points": override.points}
            for override in club.bap_category_overrides.select_related("category")[:12]
        ],
        # "When's the next meeting?" is what members ask a club between auctions, and it was the one
        # thing this lookup couldn't say -- despite ClubEvent already driving the club page's event
        # list, the club's ical feed, its Google Calendar and its Discord events.
        "upcoming_events": _club_events(club),
    }
    if membership:
        # describe_club has always explained in detail how points are *earned*. How many the person
        # asking actually has lived nowhere at all, which is an odd thing for a lookup this careful
        # about the rules to be missing.
        expires = membership.effective_expiration_date
        data["your_membership"] = {
            "paid_up": bool(membership.is_paid_member),
            "expires": expires.strftime("%B %-d, %Y") if expires else None,
        }
        if club.enable_breeder_award_program:
            data["your_membership"]["bap_points"] = membership.bap_points
            data["your_membership"]["bap_points_this_year"] = membership.bap_points_ytd
            if club.separate_hap:
                data["your_membership"]["hap_points"] = membership.hap_points
            if club.separate_cap:
                data["your_membership"]["cap_points"] = membership.culture_points
    if can_manage:
        members = ClubMember.objects.filter(club=club, is_deleted=False)
        data["_admin"] = {
            "members": members.count(),
            "members_with_an_account": members.filter(user__isnull=False).count(),
            "points_last_recalculated": club.last_bap_recalculation,
        }
    return {"found": True, "club": data}


def _club_events(club, limit: int = 5) -> list[dict[str, Any]]:
    """The club's next few calendar entries, whatever put them there.

    Deliberately not filtered by ``source``: a meeting an admin typed into Google Calendar and one
    they added on the club page are the same answer to "when's the next meeting?", and the member
    asking has no idea (and no reason to care) which pipeline it arrived through.
    """
    events = club.events.filter(date_start__gte=timezone.now()).order_by("date_start")[:limit]
    return [
        {
            "title": event.title,
            "starts": event.date_start.strftime("%A, %B %-d %Y at %-I:%M %p"),
            "where": event.location or None,
            "from_an_auction": event.source in (event.SOURCE_AUCTION, event.SOURCE_PICKUP),
        }
        for event in events
    ]


def describe_lot(request, params: dict[str, Any]) -> dict[str, Any]:
    """One lot in detail: what it is, what it went for, and (for the auction's admins) who sold it.

    Scoped exactly like ``find_lot`` -- the user's own lots plus the auctions they're part of.
    """
    user = request.user
    found = find_lot(request, params)
    if "error" in found:
        return found
    if not found.get("found"):
        return found
    lots = found["lots"]
    if len(lots) > 1:
        return {
            "found": True,
            "ambiguous": True,
            "lots": lots,
            "note": "More than one lot matches; ask which one, or pass a lot number.",
        }
    lot = Lot.objects.filter(pk=lots[0]["lot_id"], is_deleted=False).select_related("auction").first()
    if not lot:
        return {"found": False, "summary": "That lot doesn't exist any more."}
    is_admin = _is_auction_admin(user, lot.auction)
    data: dict[str, Any] = {
        "name": lot.lot_name,
        "lot_number": lot.lot_number_display,
        "auction": lot.auction.title if lot.auction else None,
        "quantity": lot.quantity,
        "description": plain_text(lot.summernote_description, limit=DESCRIPTION_LIMIT),
        "category": str(lot.species_category) if lot.species_category_id else None,
        "reserve_price": lot.reserve_price,
        "buy_now_price": lot.buy_now_price,
        "donation": lot.donation,
        "breeder_points": lot.i_bred_this_fish,
        "sold": bool(lot.winner or lot.auctiontos_winner),
        "winning_price": lot.winning_price,
        "images": lot.image_count,
        "yours": bool(lot.user_id and lot.user_id == user.pk),
    }
    data.update(_lot_live_state(lot, user))
    data.update(_lot_whereabouts(lot, user))
    if is_admin:
        seller = lot.auctiontos_seller
        data["_admin"] = {
            "seller": seller.name if seller else None,
            "seller_bidder_number": seller.bidder_number if seller else None,
            "winner_bidder_number": lot.auctiontos_winner.bidder_number if lot.auctiontos_winner else None,
        }
    return {"found": True, "lot": data}


def _lot_live_state(lot, user) -> dict[str, Any]:
    """What a lot is doing *right now*: the price, the bids, whether you're winning, when it closes.

    "What's it at now?", "am I still winning?" and "when does this close?" are the three most-asked
    questions about an online lot, and describe_lot could answer none of them -- it reported the
    reserve, the buy-now and, once it was all over, the winning price.

    ``high_bid`` is the public price (what the next bidder would pay); ``max_bid`` is the top proxy
    bid and is deliberately NOT here -- it is the one number on this site that must never reach a
    bidder, and a lookup result is read back out loud. Sealed-bid auctions hide the price from
    everyone until they close, so they get the same treatment here that the lot page gives them.
    """
    if not lot.auction or lot.sold:
        return {}
    if not (lot.auction.is_online or lot.auction.online_bidding != "disable"):
        # An in-person auction with online bidding switched off has no live price to report: the
        # only bidding is in the room, and nothing about it is written down until it sells.
        return {"bidding": "in the room only — this auction doesn't take online bids"}
    state: dict[str, Any] = {
        "bids": lot.number_of_bids,
        "bidding_closes": local_time(lot.auction, lot.calculated_end),
    }
    if lot.sealed_bid:
        state["current_price"] = None
        state["note"] = "This is a sealed-bid auction, so nobody can see the bids until it closes."
        return state
    state["current_price"] = str(lot.high_bid)
    high_bidder = lot.high_bidder
    state["you_are_the_high_bidder"] = bool(high_bidder and user.is_authenticated and high_bidder.pk == user.pk)
    # Only ever the user's own standing. Who *else* is winning is behind ``high_bidder_display``'s
    # username_visible check on the lot page, and is nobody's business through a lookup.
    if not state["you_are_the_high_bidder"] and lot.number_of_bids:
        state["someone_else_is_winning"] = True
    error = lot.bidding_error
    if error:
        state["you_cannot_bid_because"] = strip_tags(str(error))
    return state


def _lot_whereabouts(lot, user) -> dict[str, Any]:
    """Whether this lot's physical location is known, and how to actually go and find it.

    "Which table is lot 14 on?" is the single most-asked question on the floor of an in-person
    auction. The answer genuinely lives in the app: ``LotPosition`` stores metres in an arbitrary
    auction-local frame, which is a perfect input to the app's map and a useless thing to say out
    loud ("it's at 4.2, -1.8"). So this reports whether it has been scanned and sends people to the
    thing that can point at it, rather than reading coordinates to somebody holding a phone.
    """
    if not lot.auction or lot.auction.is_online:
        return {}
    position = getattr(lot, "ar_position", None)
    if position is None:
        return {
            "location_known": False,
            "location_note": (
                "Nobody has scanned this lot's location yet. Lots are located by walking the room with the mobile app."
            ),
        }
    return {
        "location_known": True,
        "location_note": (
            "This lot has been located. Open it in the mobile app to be walked to it — the map is "
            "in metres from an arbitrary origin, so there's no table number to read out."
        ),
        "location_scanned": local_time(lot.auction, position.updated_at),
    }


def describe_person(request, params: dict[str, Any]) -> dict[str, Any]:
    """One participant in an auction, for the people running it.

    Admin-only by construction: it goes through ``resolve_person``, which is scoped to a single
    auction, and refuses outright unless the caller administers that auction. A participant asking
    about another participant gets nothing -- the room's names, numbers and invoices are not public.
    """
    user = request.user
    auction, error = resolve_auction(user, _str(params, "auction"), _page(request))
    if error:
        return _error(error)
    if not _is_auction_admin(user, auction):
        return _error(f"Only admins of {auction.title} can look up someone's details.")
    tos, problem = resolve_person(user, auction, _str(params, "name") or _str(params, "person"))
    if problem:
        return problem
    lots = Lot.objects.filter(auctiontos_seller=tos, is_deleted=False)
    won = Lot.objects.filter(auctiontos_winner=tos, is_deleted=False)
    invoice = tos.invoice
    return {
        "found": True,
        "person": {
            "name": tos.name,
            "bidder_number": tos.bidder_number,
            "auction": auction.title,
            "email": tos.email,
            "checked_in": bool(tos.checked_in),
            "bidding_allowed": tos.bidding_allowed,
            "selling_allowed": tos.selling_allowed,
            "pickup_location": tos.pickup_location.name if tos.pickup_location else None,
            "lots_brought": lots.count(),
            "lots_sold": lots.filter(Q(winner__isnull=False) | Q(auctiontos_winner__isnull=False)).count(),
            "lots_won": won.count(),
            "invoice_status": invoice.get_status_display() if invoice else None,
            "invoice_total": str(invoice.rounded_net) if invoice else None,
            "has_an_account": bool(tos.user_id),
        },
    }


# --- counting and listing ----------------------------------------------------
#
# The describe_* lookups above each answer about *one* object. These answer about a set of them:
# "how many lots have sold", "who hasn't paid", "what did I win". Every one of these is a question
# somebody asks out loud during an auction, and before this section the honest answer the assistant
# could give was "here's the stats page" -- which on a phone, mid-auction, holding a clipboard, is
# not an answer.
#
# The counting is never re-implemented. Every number below is a property the stats page, the invoice
# page or the check-in page already renders, so the palette and the page can never disagree.


def _time_left(auction) -> dict[str, Any]:
    """How much longer this auction runs, said the way a person would say it.

    ``minutes_to_end`` is the site's own countdown (the lot pages and the notification cron both run
    off it), so this is a phrasing layer over it rather than a second clock. In-person auctions have
    no meaningful end time of their own -- ``date_end`` is a formality, and what actually closes is
    online bidding -- so they get the online-bidding deadline when there is one and nothing when
    there isn't, rather than a countdown to a date nobody is watching.
    """
    now = timezone.now()
    if auction.is_online:
        deadline, what = auction.date_end, "bidding closes"
    else:
        deadline, what = auction.date_online_bidding_ends, "online bidding closes"
        if auction.online_bidding == "disable":
            deadline = None
    data: dict[str, Any] = {
        "started": bool(auction.started),
        "over": bool(auction.pretty_much_over),
        "closed": bool(auction.closed or auction.in_person_closed),
    }
    if not deadline:
        data["note"] = (
            "This is an in-person auction with no online bidding, so it doesn't count down — "
            "it runs until the auctioneer finishes."
        )
        return data
    data["closes"] = local_time(auction, deadline)
    seconds = (deadline - now).total_seconds()
    if seconds <= 0:
        data["time_left"] = "none — it's already closed"
        return data
    days, remainder = divmod(int(seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        phrase = f"{days} day{'s' if days != 1 else ''} and {hours} hour{'s' if hours != 1 else ''}"
    elif hours:
        phrase = f"{hours} hour{'s' if hours != 1 else ''} and {minutes} minute{'s' if minutes != 1 else ''}"
    else:
        phrase = f"{minutes} minute{'s' if minutes != 1 else ''}"
    data["time_left"] = phrase
    data["what_happens"] = what
    # Online auctions extend a lot's end when a bid lands in its last 15 minutes, so a bare
    # countdown is a lower bound rather than the truth. Say so, or the answer is confidently wrong
    # for exactly the lots anybody is still watching.
    if auction.is_online and not auction.sealed_bid:
        data["note"] = "Individual lots can run up to an hour past this if last-minute bids keep coming."
    return data


def auction_numbers(request, params: dict[str, Any]) -> dict[str, Any]:
    """The running totals somebody asks for out loud while an auction is happening.

    "How many lots have sold?", "what's the gross so far?", "how many people have checked in?",
    "how many are still unsold?" -- the auctioneer's questions, and the ones the stats page exists
    to answer once you've walked over to a laptop and loaded it.

    Every figure is the auction's own property (``total_sold_lots``, ``gross``, ``median_lot_price``
    and friends -- the ones the stats page renders), so this can't drift from the page. What is
    returned depends on who is asking, in the same two tiers ``describe_auction`` already uses:
    counts for admins and for auctions whose stats are public, and the money only for admins.
    """
    user = request.user
    auction, error = _resolve_described_auction(user, _str(params, "auction") or _str(params, "name"), _page(request))
    if error:
        return _error(error)
    is_admin = _is_auction_admin(user, auction)
    data: dict[str, Any] = {"auction": auction.title, "time": _time_left(auction)}
    if not (is_admin or auction.make_stats_public):
        data["note"] = (
            f"{auction.title} doesn't publish its numbers, so I can only say how long is left. "
            "Its admins can see the rest."
        )
        return {"found": True, "numbers": data}

    lots = Lot.objects.filter(auction=auction, is_deleted=False)
    sold = auction.total_sold_lots
    data.update(
        {
            "lots_total": lots.count(),
            "lots_sold": sold,
            # Deliberately counted rather than subtracted: a lot can be removed (banned) or a
            # donation with no winner, and "total minus sold" quietly calls those unsold.
            "lots_unsold": lots.filter(winning_price__isnull=True, banned=False).count(),
            "lots_removed": lots.filter(banned=True).count(),
            "participants": AuctionTOS.objects.filter(auction=auction).count(),
            "sellers": auction.number_of_sellers if hasattr(auction, "number_of_sellers") else None,
            "buyers": auction.number_of_buyers,
        }
    )
    if auction.use_check_in_mode:
        data["checked_in"] = AuctionTOS.objects.filter(auction=auction, checked_in__isnull=False).count()
        data["not_checked_in"] = AuctionTOS.objects.filter(auction=auction, checked_in__isnull=True).count()
    if not is_admin:
        return {"found": True, "numbers": data}
    from .models import Invoice

    invoices = Invoice.objects.filter(auction=auction)
    data["_admin"] = {
        "gross": str(auction.gross),
        "median_lot_price": str(auction.median_lot_price),
        "total_to_sellers": str(auction.total_to_sellers),
        "club_profit": str(auction.club_profit),
        "donations": str(auction.total_donations),
        "invoices_paid": invoices.filter(status="PAID").count(),
        "invoices_unpaid": invoices.exclude(status="PAID").count(),
    }
    return {"found": True, "numbers": data}


def my_activity(request, params: dict[str, Any]) -> dict[str, Any]:
    """What the user themselves has done: lots in, lots sold, lots won, what they owe.

    The bidder-side counterpart to ``describe_person``, which is admin-only by construction. Without
    this, "what did I win?", "what do I owe?", "did my lots sell?" and "what am I watching?" all fell
    through to a navigation to the invoice page -- fine on a laptop, useless by voice.

    Scoped to one auction (the one they're looking at, else their last one) because that is how the
    questions are asked, and because a lifetime total answers none of them. The invoice figures are
    the invoice's own properties, so they are the numbers on their invoice page to the cent.
    """
    from .models import Watch

    user = request.user
    auction, error = resolve_auction(user, _str(params, "auction"), _page(request))
    data: dict[str, Any] = {"memberships": _membership_facts(user)}
    if error:
        # Not an error: somebody with no auctions still gets a real answer about their memberships,
        # and telling them they have no auctions is the answer to "what did I win".
        data["note"] = error
        return {"found": True, "activity": data}

    tos = _own_tos(user, auction)
    data["auction"] = auction.title
    if not tos:
        data["note"] = f"You haven't joined {auction.title}, so you have nothing in it yet."
        return {"found": True, "activity": data}

    mine = Lot.objects.filter(auctiontos_seller=tos, is_deleted=False)
    won = Lot.objects.filter(auctiontos_winner=tos, is_deleted=False)
    invoice = tos.invoice
    data.update(
        {
            "your_bidder_number": tos.bidder_number,
            "checked_in": bool(tos.checked_in),
            "lots_submitted": mine.count(),
            "lots_sold": mine.filter(winning_price__isnull=False).count(),
            "lots_unsold": mine.filter(winning_price__isnull=True, banned=False).count(),
            "lots_won": won.count(),
            "watching": Watch.objects.filter(user=user, lot_number__auction=auction).count(),
            "lot_submission_open": bool(auction.can_submit_lots),
        }
    )
    if invoice:
        data["invoice"] = {
            "status": invoice.get_status_display(),
            # ``rounded_net`` is signed and ``user_should_be_paid`` is the site's own reading of that
            # sign. Handed the bare number the model read it as "you owe $40" to somebody who was
            # owed $40 -- the worst kind of wrong answer this feature can give.
            "total": str(invoice.absolute_amount),
            "you_owe_the_club": not invoice.user_should_be_paid,
            "the_club_owes_you": bool(invoice.user_should_be_paid),
            "sold_gross": str(invoice.total_sold_gross),
            "lots_bought": invoice.lots_bought,
        }
    else:
        data["invoice"] = None
    soon = _watched_ending_soon(user, auction)
    if soon is None:
        # Said rather than omitted: "nothing is coming up" and "this auction doesn't track what's
        # coming up" are different answers, and only one of them means stop watching your phone.
        data["watching_ending_soon"] = (
            f"{auction.title} isn't using the lot queue, so there's no way to tell what's coming up next."
        )
    else:
        data["watching_ending_soon"] = soon
    return {"found": True, "activity": data}


def _membership_facts(user) -> list[dict[str, Any]]:
    """Every club membership the user holds: whether it's current, when it lapses, and their points.

    ``describe_club`` explains in detail how points are *earned* and has never been able to say how
    many you *have*, which is the question people actually ask. ``is_paid_member`` and
    ``effective_expiration_date`` are the club's own single sources of truth for "am I paid up" --
    the same two the wallet pass and the members list use -- so this can't disagree with the card in
    somebody's phone.
    """
    facts = []
    for member in ClubMember.objects.filter(user=user, is_deleted=False).select_related("club")[:10]:
        if not member.club:
            continue
        expires = member.effective_expiration_date
        entry: dict[str, Any] = {
            "club": member.club.name,
            "paid_up": bool(member.is_paid_member),
            "expires": expires.strftime("%B %-d, %Y") if expires else None,
            "expiring_soon": bool(member.is_expiring_soon),
        }
        if member.club.enable_breeder_award_program:
            entry["points"] = {
                "bap": member.bap_points,
                "bap_this_year": member.bap_points_ytd,
            }
            # Only mentioned by clubs that run them separately, so the answer doesn't list two
            # programs of zero points at a club that has never heard of either.
            if member.club.separate_hap:
                entry["points"]["hap"] = member.hap_points
            if member.club.separate_cap:
                entry["points"]["cap"] = member.culture_points
        facts.append(entry)
    return facts


#: How far ahead "ending soon" looks in an online auction. Long enough to be worth acting on, short
#: enough that the answer is about tonight rather than about the whole auction.
WATCHED_SOON_HOURS = 24


def _watched_ending_soon(user, auction, limit: int = 5):
    """Lots on the user's watch list that are about to be sold, or ``None`` when that can't be known.

    Two entirely different mechanisms behind one question. An online auction has real end times, so
    "soon" is a time window. An in-person auction has no end times at all -- what is about to be sold
    is whatever the auctioneer queued up -- so the answer comes from the lot queue, and an auction
    not using the queue gets ``None``, which the caller turns into "this auction doesn't queue lots"
    rather than a "nothing coming up" that would read as an answer.

    The sorting and the window are done in Python because ``calculated_end`` is a property, not a
    column -- a lot's real end moves with the auction's dynamic-end rule, and the stored ``date_end``
    is not what anybody is watching. The set is one person's watch list in one auction, so this is a
    few rows, not a scan.
    """
    from .models import LotQueueEntry, Watch

    watched_ids = set(Watch.objects.filter(user=user, lot_number__auction=auction).values_list("lot_number", flat=True))
    if not watched_ids:
        return []
    if auction.is_online:
        cutoff = timezone.now() + timezone.timedelta(hours=WATCHED_SOON_HOURS)
        live = [
            lot
            for lot in Lot.objects.filter(pk__in=watched_ids, is_deleted=False, winning_price__isnull=True)
            if lot.calculated_end and lot.calculated_end <= cutoff
        ]
        live.sort(key=lambda lot: lot.calculated_end)
        return [
            {
                "lot_number": lot.lot_number_display,
                "name": lot.lot_name,
                "ends": local_time(auction, lot.calculated_end),
            }
            for lot in live[:limit]
        ]
    entries = LotQueueEntry.objects.filter(auction=auction).select_related("lot")
    if not entries.exists():
        return None
    return [
        {"lot_number": entry.lot.lot_number_display, "name": entry.lot.lot_name, "place_in_queue": entry.order}
        for entry in entries.filter(lot__in=watched_ids)[:limit]
    ]


#: How many rows a list_* lookup returns. The model is being asked to read these back in two or
#: three sentences, so a longer list is tokens spent on something nobody will hear.
LIST_LIMIT = 15


def list_people(request, params: dict[str, Any]) -> dict[str, Any]:
    """The people in an auction matching one status: unpaid, not checked in, possible duplicates.

    These are the end-of-auction cleanup questions -- "who hasn't paid?", "who's still not checked
    in?", "did I add Bob twice?" -- and each one is a filter the invoice page or the check-in page
    already runs. One lookup with a status enum rather than four bespoke actions, because the
    permission check, the scoping and the shape of the answer are identical for all of them.

    Admin-only: the room's names, bidder numbers and invoice statuses are not public, which is the
    same reason ``describe_person`` refuses.
    """
    user = request.user
    auction, error = resolve_auction(user, _str(params, "auction"), _page(request))
    if error:
        return _error(error)
    if not _is_auction_admin(user, auction):
        return _error(f"Only admins of {auction.title} can list who's in it.")
    people = AuctionTOS.objects.filter(auction=auction)
    status = (_str(params, "status") or "all").lower().replace(" ", "_").replace("-", "_")
    # ``auctiontos`` is the reverse accessor from Invoice back to AuctionTOS (Invoice's own field is
    # ``auctiontos_user``, related_name="auctiontos"). Confusing, and it is the only way to filter
    # participants by invoice status without walking every row in Python.
    # ``kind`` rather than re-testing ``status`` further down: the spellings are aliases, and
    # ``status.startswith("duplicate")`` quietly missed "possible_duplicates" — the one word the
    # action's own parameter documentation tells the model to send.
    kind = "all"
    if status in {"unpaid", "not_paid", "owing"}:
        # ``.distinct()`` because this joins Invoice, and a participant with more than one invoice
        # row would otherwise be counted (and listed) once per invoice.
        people = people.filter(auctiontos__isnull=False).exclude(auctiontos__status="PAID").distinct()
        label, kind = "haven't paid", "invoice"
    elif status == "paid":
        people = people.filter(auctiontos__status="PAID").distinct()
        label, kind = "have paid", "invoice"
    elif status in {"not_checked_in", "unchecked", "missing", "no_show"}:
        people = people.filter(checked_in__isnull=True)
        label = "haven't checked in"
    elif status in {"checked_in", "arrived", "here"}:
        people = people.filter(checked_in__isnull=False)
        label = "have checked in"
    elif status in {"duplicates", "duplicate", "possible_duplicates"}:
        # ``possible_duplicate`` is already computed and stored when people are added; nothing has
        # ever read it back out loud. Merging them is destructive and stays a page, but *finding*
        # them is a pure read and is the question asked at the check-in desk.
        people = people.filter(possible_duplicate__isnull=False).select_related("possible_duplicate")
        label, kind = "might be duplicates", "duplicate"
    else:
        label = "are in this auction"
    total = people.count()
    rows = []
    for tos in people.order_by("bidder_number", "name")[:LIST_LIMIT]:
        row: dict[str, Any] = {"name": tos.name or tos.email or "(no name)", "bidder_number": tos.bidder_number}
        if kind == "duplicate" and tos.possible_duplicate:
            row["might_be_the_same_as"] = tos.possible_duplicate.name
        invoice = tos.invoice if kind == "invoice" else None
        if invoice:
            # Unsigned, with the direction in words. "Who hasn't paid" legitimately includes sellers
            # the club owes money to, and a bare negative number was read straight back as what they
            # owe -- with the sign lost somewhere between the JSON and the sentence.
            row["invoice_total"] = str(invoice.absolute_amount)
            row["the_club_owes_them"] = bool(invoice.user_should_be_paid)
        rows.append(row)
    return {
        "found": bool(total),
        "auction": auction.title,
        "people": rows,
        "count": total,
        "showing": len(rows),
        "summary": f"{total} people in {auction.title} {label}.",
    }


def list_lots(request, params: dict[str, Any]) -> dict[str, Any]:
    """The lots in an auction matching one status: unsold, sold, still without a winner, or mine.

    The lot-side twin of :func:`list_people`. "Which lots have no winner yet?" is the question that
    drives the end of an in-person auction, and it had no answer at all.

    Unlike ``list_people`` this is not admin-only: ``mine`` is a seller asking about their own lots,
    which is exactly the gap this whole section exists to close. Every other status is scoped to
    auctions the user has joined, and the seller's name is only ever added for an admin.
    """
    user = request.user
    auction, error = resolve_auction(user, _str(params, "auction"), _page(request))
    if error:
        return _error(error)
    is_admin = _is_auction_admin(user, auction)
    lots = Lot.objects.filter(auction=auction, is_deleted=False)
    status = (_str(params, "status") or "all").lower().replace(" ", "_").replace("-", "_")
    if status in {"mine", "my_lots", "yours"}:
        tos = _own_tos(user, auction)
        if not tos:
            return _error(f"You haven't joined {auction.title}, so you have no lots in it.")
        lots = lots.filter(auctiontos_seller=tos)
        label = "you brought"
    elif status in {"unsold", "not_sold", "no_winner", "unclaimed", "passed"}:
        lots = lots.filter(winning_price__isnull=True, banned=False)
        label = "have no winner yet"
    elif status == "sold":
        lots = lots.filter(winning_price__isnull=False)
        label = "have sold"
    elif status in {"donations", "donated"}:
        lots = lots.filter(donation=True)
        label = "are donations"
    else:
        label = "are in this auction"
    # No further scoping by role: a participant can already browse every lot in an auction they
    # joined, so nothing here is hidden from them. The seller's name is another matter, and it is
    # added per row below only for admins.
    total = lots.count()
    rows = []
    for lot in lots.order_by("lot_number_int", "custom_lot_number")[:LIST_LIMIT]:
        row: dict[str, Any] = {
            "lot_number": lot.lot_number_display,
            "name": lot.lot_name,
            "sold": bool(lot.winning_price),
            "price": str(lot.winning_price) if lot.winning_price else None,
        }
        if is_admin and lot.auctiontos_seller:
            row["seller"] = lot.auctiontos_seller.name
            row["seller_bidder_number"] = lot.auctiontos_seller.bidder_number
        rows.append(row)
    return {
        "found": bool(total),
        "auction": auction.title,
        "lots": rows,
        "count": total,
        "showing": len(rows),
        "summary": f"{total} lots in {auction.title} {label}.",
    }


def recent_changes(request, params: dict[str, Any]) -> dict[str, Any]:
    """What has been changed in this auction lately, newest first. Auction admins only.

    Every write the palette performs already appends "(command palette)" to an
    ``auction.create_history`` entry, and until now nothing read any of it back. "What did you just
    do?" and "who checked Bob in?" are the same question asked of the same table, and it is the list
    an undo has to choose from.
    """
    from .models import AuctionHistory

    user = request.user
    auction, error = resolve_auction(user, _str(params, "auction"), _page(request))
    if error:
        return _error(error)
    if not _is_auction_admin(user, auction):
        return _error(f"Only admins of {auction.title} can see its history.")
    history = AuctionHistory.objects.filter(auction=auction).select_related("user")
    if params.get("mine"):
        history = history.filter(user=user)
    if params.get("assistant"):
        history = history.filter(action__icontains="(command palette)")
    rows = [
        {
            "what": entry.action,
            "who": entry.user.username if entry.user else "the system",
            "when": local_time(auction, entry.timestamp),
            "by_the_assistant": "(command palette)" in (entry.action or ""),
        }
        for entry in history.order_by("-timestamp")[:LIST_LIMIT]
    ]
    return {
        "found": bool(rows),
        "auction": auction.title,
        "changes": rows,
        "summary": f"The last {len(rows)} changes in {auction.title}." if rows else "Nothing has changed yet.",
    }


def lot_queue(request, params: dict[str, Any]) -> dict[str, Any]:
    """What lot is being sold now, and what's next. For an auctioneer holding a microphone.

    The queue is built on the Lot queue page by scanning lot QR codes; the head of it is what the
    set-lot-winners page pulls up and what the kiosk projects for the room. "What lot are we on?"
    and "what's next?" are about as strong a voice-first case as this feature has, and both were
    navigations to a page you can't read while calling an auction.

    An auction that isn't using the queue gets told so plainly rather than getting an empty list,
    because "nothing is queued" and "this auction doesn't queue" are different answers to the same
    question and only one of them is worth acting on.
    """
    from .models import LotQueueEntry, Watch

    user = request.user
    auction, error = resolve_auction(user, _str(params, "auction"), _page(request))
    if error:
        return _error(error)
    if auction.is_online:
        return _error(f"{auction.title} is an online auction, so there's no lot queue — lots end on their own clock.")
    entries = list(LotQueueEntry.objects.filter(auction=auction).select_related("lot").order_by("order")[:LIST_LIMIT])
    if not entries:
        return {
            "found": False,
            "auction": auction.title,
            "queue": [],
            "summary": (
                f"There's nothing in {auction.title}'s lot queue right now. "
                "Lots are queued by scanning them on the Lot queue page."
            ),
        }
    watched = set(Watch.objects.filter(user=user, lot_number__auction=auction).values_list("lot_number", flat=True))
    queue = [
        {
            "position": "now" if index == 0 else index + 1,
            "lot_number": entry.lot.lot_number_display,
            "name": entry.lot.lot_name,
            "you_are_watching_it": entry.lot_id in watched,
        }
        for index, entry in enumerate(entries)
    ]
    return {
        "found": True,
        "auction": auction.title,
        "current_lot": queue[0],
        "queue": queue,
        "summary": f"Lot {queue[0]['lot_number']} ({queue[0]['name']}) is up now, with {len(queue) - 1} behind it.",
    }


def my_messages(request, params: dict[str, Any]) -> dict[str, Any]:
    """Questions people have asked on the user's own lots.

    ``LotHistory`` is the seller's inbox and nothing has ever read it out. A buyer asking "are these
    captive bred?" and getting no reply is a lost sale, and the seller has no reason to go and look
    unless something tells them there's something to look at.

    Reading is safe and is all this does. Replying is not: a chat posted to the wrong lot is public,
    permanent and addressed to a stranger, so the answer here carries a link to each lot and the
    reply happens on the page, where the question is on screen above the box.
    """
    from .models import LotHistory

    user = request.user
    messages = (
        LotHistory.objects.filter(changed_price=False, removed=False, lot__is_deleted=False)
        # Both ways a lot can be "mine". ``Lot.user`` is the owner, but at an in-person auction most
        # lots are entered against a participant row by an admin and carry no owner at all, so
        # filtering on that alone hid every question asked of an in-person seller.
        .filter(Q(lot__user=user) | Q(lot__auctiontos_seller__user=user))
        .exclude(user=user)
        .select_related("lot", "lot__auction")
        .distinct()
    )
    hint = _str(params, "auction")
    page_slug = _page(request).get("auction")
    if hint or page_slug:
        auction, error = resolve_auction(user, hint, _page(request))
        if error:
            return _error(error)
        messages = messages.filter(lot__auction=auction)
    unread = messages.filter(seen=False).count()
    rows = [
        {
            "lot_number": item.lot.lot_number_display,
            "lot": item.lot.lot_name,
            "asked": Truncator(item.message or "").chars(200, truncate="…"),
            "when": local_time(item.lot.auction, item.timestamp) if item.lot.auction else str(item.timestamp),
            "you_have_seen_it": bool(item.seen),
            "url": item.lot.get_absolute_url(),
        }
        for item in messages.order_by("-timestamp")[:LIST_LIMIT]
    ]
    return {
        "found": bool(rows),
        "messages": rows,
        "unread": unread,
        "summary": (f"{unread} unread question(s) on your lots." if rows else "Nobody has asked you anything."),
    }


def club_numbers(request, params: dict[str, Any]) -> dict[str, Any]:
    """A club's own numbers: how many members, how many are paid up, and what's in the books.

    The money here is a **read**. The palette's money rule ("navigate-only") is about money changing
    hands -- taking a payment, renewing a membership, placing a bid -- and looking at a balance moves
    nothing. It is still gated exactly as the pages are: the member counts need ``permission_view``
    (the members list), and the balance needs ``permission_money`` or ``permission_edit_club``, which
    is precisely what :class:`auctions.views.ClubMoneyBalanceView` requires.
    """
    from django.db.models import Sum

    from .models import ClubMoney

    user = request.user
    hint = _str(params, "club") or _str(params, "name")
    club = palette_routes._club_from_hint(user, hint or (_page(request).get("club") or ""))
    if club is None:
        return _error("I couldn't work out which club you mean.")
    if not command_palette._can_manage_members(user, club):
        return _error(f"You don't have permission to see {club.name}'s numbers.")
    members = ClubMember.objects.filter(club=club, is_deleted=False)
    # ``is_paid_member`` is the club's own single source of truth for "are their dues current", and
    # it reads ``self.club.membership_system`` -- so this counts in Python rather than translating
    # that rule into a WHERE clause that would immediately be a second copy of it. ``select_related``
    # is load-bearing, not a tidy-up: without it each row re-fetches its club, which is one query per
    # member on the biggest club on the site.
    all_members = list(members.select_related("club"))
    paid_up = sum(1 for member in all_members if member.is_paid_member)
    data: dict[str, Any] = {
        "club": club.name,
        "members": len(all_members),
        "paid_up": paid_up,
        "lapsed": len(all_members) - paid_up,
        "expiring_within_30_days": sum(1 for member in all_members if member.is_expiring_soon),
        "members_with_an_account": members.filter(user__isnull=False).count(),
    }
    if command_palette._perm(user, club, "permission_money") or command_palette._perm(
        user, club, "permission_edit_club"
    ):
        balance = ClubMoney.objects.filter(club=club).aggregate(total=Sum("amount"))["total"]
        data["_money"] = {
            "balance": str(balance if balance is not None else Decimal("0.00")),
            "note": "This is the club's book balance, the same figure the treasurer report opens with.",
        }
    return {"found": True, "club_numbers": data}


def _user_coordinates(user):
    """Where to search from. Returns ``(latitude, longitude, problem_or_None)``."""
    userdata = getattr(user, "userdata", None)
    latitude = getattr(userdata, "latitude", None)
    longitude = getattr(userdata, "longitude", None)
    if latitude and longitude:
        return latitude, longitude, None
    return (
        None,
        None,
        _need(
            "I don't know where you are yet. Set your location and I can find what's near you.",
            [{"label": "Set my location", "value": "set my location"}],
        ),
    )


def auctions_near_me(request, params: dict[str, Any]) -> dict[str, Any]:
    """Upcoming auctions near the user, including ones they haven't joined.

    Everything else in the palette is scoped to auctions the user is already part of, and that is
    right for lots and people -- it is what stops the box surfacing a stranger's inventory. It also
    meant the single highest-intent question anybody can ask this site, "is there an auction near
    me?", had no answer at all.

    ``models.nearby_auctions`` is the purpose-built, permission-safe answer: it filters to
    ``promote_this_auction`` (auctions that asked to be found), respects the user's ignore list, and
    knows the date window. It is what the "auctions near you" notification runs on, so this surfaces
    exactly what that would have told them.
    """
    from .models import nearby_auctions as nearby

    user = request.user
    latitude, longitude, problem = _user_coordinates(user)
    if problem:
        return problem
    distance = _int(params, "distance") or 100
    # A wider search than the site's own notification radius is fine -- this was asked for out loud,
    # not pushed at somebody -- but an unbounded one turns "near me" into "anywhere".
    distance = max(10, min(distance, 500))
    auctions, distances = nearby(latitude, longitude, distance=distance, include_already_joined=True, user=user)
    nearest = sorted(zip(auctions, distances, strict=False), key=lambda pair: pair[1])[:LIST_LIMIT]
    # One query for the lot of them. Asking ``_own_tos`` per auction is a query per row, and this is
    # the one lookup that can legitimately come back with fifteen auctions the user has never seen.
    joined = set(
        AuctionTOS.objects.filter(auction__in=[auction.pk for auction, _ in nearest])
        .filter(Q(user=user) | Q(email=user.email))
        .values_list("auction_id", flat=True)
    )
    rows = []
    for auction, miles in nearest:
        rows.append(
            {
                "title": auction.title,
                # Already rounded up to the nearest 10 by ``distance_to``, deliberately -- it is
                # what stops a handful of these answers being triangulated into somebody's address.
                # Do not make this more precise.
                "miles_away": round(float(miles)),
                "format": "online auction" if auction.is_online else "in-person auction",
                "starts": local_time(auction, auction.date_start),
                "ends": local_time(auction, auction.date_end),
                "you_have_joined": auction.pk in joined,
                "url": auction.get_absolute_url(),
            }
        )
    if not rows:
        return {
            "found": False,
            "auctions": [],
            "summary": f"Nothing within {distance} miles of you right now.",
        }
    return {
        "found": True,
        "auctions": rows,
        "summary": f"{len(rows)} auction(s) within {distance} miles of you.",
    }


def clubs_near_me(request, params: dict[str, Any]) -> dict[str, Any]:
    """Fish clubs near the user. How this site grows, and it was a navigation to a map.

    Distance uses the same ``distance_to`` SQL annotation the clubs map runs on, so the ordering
    here is the ordering there.
    """
    from .models import Club, distance_to

    user = request.user
    latitude, longitude, problem = _user_coordinates(user)
    if problem:
        return problem
    distance = max(10, min(_int(params, "distance") or 100, 500))
    clubs = (
        Club.objects.filter(active=True, latitude__isnull=False, longitude__isnull=False)
        .annotate(distance=distance_to(latitude, longitude))
        .exclude(distance__gt=distance)
        .order_by("distance")[:LIST_LIMIT]
    )
    mine = {
        member.club_id for member in ClubMember.objects.filter(user=user, is_deleted=False).only("club_id", "user_id")
    }
    rows = [
        {
            "name": club.name,
            "abbreviation": club.abbreviation,
            "miles_away": round(float(club.distance)),
            "you_are_a_member": club.pk in mine,
            "url": reverse("club_detail", kwargs={"slug": club.slug}),
        }
        for club in clubs
    ]
    if not rows:
        return {"found": False, "clubs": [], "summary": f"No clubs within {distance} miles of you."}
    return {"found": True, "clubs": rows, "summary": f"{len(rows)} club(s) within {distance} miles of you."}


def search_help(request, params: dict[str, Any]) -> dict[str, Any]:
    """Search the site's own written help -- the FAQ and the blog -- for a how-does-this-work question.

    "How does proxy bidding work?", "what's a donation lot?", "how do I print labels?" had no action,
    so they were answered from the model's own priors: plausible, confident, and about some other
    auction site. This grounds them in text somebody here wrote and can edit.

    Both models store rendered HTML alongside the markdown source; the source is what gets searched
    and sent, because the rendered field is mostly tags.
    """
    from .models import FAQ, BlogPost

    query = _str(params, "query") or _str(params, "question")
    if not query:
        return _error("What would you like me to look up?")
    words = re.findall(r"[A-Za-z0-9']{3,}", query.lower())[:6]
    if not words:
        return {"found": False, "help": [], "summary": f"Nothing written down about “{query}”."}
    faq_q = Q()
    blog_q = Q()
    for word in words:
        faq_q |= Q(question__icontains=word) | Q(answer__icontains=word)
        blog_q |= Q(title__icontains=word) | Q(body__icontains=word)
    results = []
    for entry in FAQ.objects.filter(faq_q)[:4]:
        results.append(
            {
                "source": "FAQ",
                "question": entry.question,
                "answer": plain_text(entry.answer, limit=600),
                "url": reverse("faq") + f"#{entry.slug}",
            }
        )
    for post in BlogPost.objects.filter(blog_q).order_by("-date_posted")[:3]:
        results.append(
            {
                "source": "Blog",
                "question": post.title,
                "answer": plain_text(post.body, limit=600),
                "url": reverse("blog_post", kwargs={"slug": post.slug}),
            }
        )
    if not results:
        return {
            "found": False,
            "help": [],
            "summary": (
                f"Nothing written down here about “{query}”. Say so rather than answering from "
                "general knowledge — this site works differently from other auction sites."
            ),
        }
    return {"found": True, "help": results, "summary": f"{len(results)} help article(s) about “{query}”."}


def find_page(request, params: dict[str, Any]) -> dict[str, Any]:
    """Search the page catalog. A safety net for when the prompt's catalog wasn't enough."""
    query = _str(params, "query") or _str(params, "page")
    if not query:
        return _error("What page are you looking for?")
    matches = palette_routes.match_routes(query, request.user, limit=6)
    if not matches:
        return {"found": False, "pages": [], "summary": f"No page matching “{query}”."}
    return {
        "found": True,
        "pages": [{"page": route.key, "label": route.label, "section": route.section} for route in matches],
        "summary": f"{len(matches)} page(s) matching “{query}”.",
    }


# --- undo a sale -------------------------------------------------------------


def undo_sale(request, params: dict[str, Any]) -> dict[str, Any]:
    """Un-sell a lot in an in-person auction.

    Wraps :class:`auctions.views.AuctionUnsellLot`'s own ``unsell`` helper, so the invoice
    recalculation and history entry are exactly the ones the Undo button produces.
    """
    from .views import AuctionUnsellLot

    user = request.user
    auction, error = resolve_auction(user, _str(params, "auction"), _page(request))
    if error:
        return _error(error)
    if not _is_auction_admin(user, auction):
        return _error(f"You don't have permission to change sales in {auction.title}.")
    if auction.is_online:
        return _error(f"{auction.title} is an online auction — winners come from the bids automatically.")

    lot_hint = _str(params, "lot")
    if not lot_hint:
        return _need("Which lot number should I un-sell?")

    view = AuctionUnsellLot()
    view.request = request
    view.auction = auction
    view.kwargs = {}
    lot = view.find_lot(lot_hint)
    if not lot:
        return _error(f"I couldn't find lot {lot_hint} in {auction.title}.")
    sold = bool(lot.winner or lot.auctiontos_winner)
    # ``unsell`` clears the winner *and* sets the lot active again, so it is equally the way back
    # from a "no sale" -- which ends a lot with no winner at all. Refusing unless there was a winner
    # meant the one thing this action's own description promises ("putting it back up for sale") was
    # impossible for the commonest way a lot comes off the block.
    if not sold and lot.active:
        return _error(f"Lot {lot.lot_number_display} is still up for sale, so there's nothing to undo.")
    result = view.unsell(lot)
    if not sold:
        result["success_message"] = f"Lot {lot.lot_number_display} {lot.lot_name} is back up for sale."
    return _ok(
        str(result.get("success_message") or f"Un-sold lot {lot.lot_number_display}."),
        lot_id=lot.pk,
        auction=auction.slug,
    )


# --- undo --------------------------------------------------------------------
#
# Mishearing is the characteristic failure of a voice interface, so a general "undo that" is closer
# to a safety feature than a convenience. Two decisions make it safe to have at all:
#
#   1. **An action describes its own reversal.** A resolver returns ``undo={"action", "params"}``
#      alongside its result, because it -- and only it -- knows what it changed and what was there
#      before. There is no table of inverses to keep in step with the resolvers, and an action with
#      nothing to say here simply cannot be undone, which is the correct default.
#   2. **The reversal is an ordinary action.** ``undo_last`` runs it through ``run_action`` like any
#      other command, so every permission check runs again, from scratch, as the person undoing.
#      Undo grants nothing that saying the reversing command out loud would not.
#
# What is deliberately NOT reversible: anything whose reversal is a delete. Undoing "add mike smith"
# means deleting a participant, and a delete is destructive whether or not something else caused it.
# Those refuse and say where to go, which is the same answer the rest of this file gives.

#: How long "undo that" reaches back. Long enough to cover "…no, wait" and a moment's conversation,
#: short enough that it can never mean a command from earlier in the evening.
UNDO_WINDOW_SECONDS = 600

#: How many recent reversible commands to keep per user.
UNDO_STACK_SIZE = 5


def _undo_key(user) -> str:
    return f"palette_undo_{getattr(user, 'pk', 0)}"


def _undo_stack(user) -> list[dict[str, Any]]:
    """The user's undoable commands, oldest first, with anything past the window dropped.

    The window is enforced here rather than left to the cache's TTL because every write resets that
    TTL: push a second command, or pop one, and an entry from nine minutes ago would get another ten
    minutes of life. Each entry carries the time it happened, so this is the same answer however
    often the key has been rewritten since.
    """
    from django.utils.dateparse import parse_datetime

    cutoff = timezone.now() - timezone.timedelta(seconds=UNDO_WINDOW_SECONDS)
    stack = []
    for item in cache.get(_undo_key(user)) or []:
        if not isinstance(item, dict):
            continue
        happened = parse_datetime(str(item.get("at") or ""))
        if happened is None or happened < cutoff:
            continue
        stack.append(item)
    return stack


def remember_undo(user, action_name: str, result: dict[str, Any]) -> None:
    """Push a completed command's own description of how to reverse it onto the user's undo stack.

    Held in the cache rather than the database on purpose: it expires by itself, it is per user, and
    an undo that is still on offer an hour later is an undo nobody meant.
    """
    undo = result.get("undo") if isinstance(result, dict) else None
    if not isinstance(undo, dict) or not undo.get("action"):
        return
    entry = {
        "action": str(undo["action"])[:50],
        "params": undo.get("params") if isinstance(undo.get("params"), dict) else {},
        "describes": str(undo.get("describes") or "")[:200],
        "was": action_name,
        "summary": str(result.get("summary") or "")[:300],
        "at": timezone.now().isoformat(),
    }
    stack = _undo_stack(user)
    stack.append(entry)
    cache.set(_undo_key(user), stack[-UNDO_STACK_SIZE:], timeout=UNDO_WINDOW_SECONDS)


def undo_last(request, params: dict[str, Any]) -> dict[str, Any]:
    """Reverse the last thing this assistant did, when that thing said how.

    "Undo that" after a misheard check-in, a wrong bidder number or a mangled lot name used to mean
    working out for yourself which page to go and fix. This covers the commands that can describe
    their own reversal and refuses the rest by name, rather than guessing at an inverse.
    """
    user = request.user
    stack = _undo_stack(user)
    if not stack:
        return _error(
            "I don't have anything to undo — either nothing's been done in the last few minutes, "
            "or the last thing isn't something I can reverse. Ask me what's changed recently and "
            "I'll show you."
        )
    entry = stack[-1]
    action = get_action(entry.get("action", ""))
    if action is None:  # pragma: no cover - only reachable if an action is removed mid-window
        cache.set(_undo_key(user), stack[:-1], timeout=UNDO_WINDOW_SECONDS)
        return _error("I can't undo that any more.")
    result = run_action(request, action.name, dict(entry.get("params") or {}))
    if "error" in result:
        # Left on the stack: the reversal failed for a reason (somebody else changed the row, an
        # invoice was closed), and dropping it would hide that a second "undo that" is worth trying
        # after fixing whatever the reason was.
        return _error(f"I couldn't undo that: {result['error']}")
    if "more_info_needed" in result:
        return result
    # Popped only on success, and popped before anything else can go wrong: an undo that stays on
    # the stack gets applied twice by the next "undo that", which for a toggle means putting it
    # straight back.
    cache.set(_undo_key(user), stack[:-1], timeout=UNDO_WINDOW_SECONDS)
    what = entry.get("describes") or f"the last {entry.get('was', 'command').replace('_', ' ')}"
    return _ok(
        f"Undid {what}. {result.get('summary', '')}".strip(),
        **{key: value for key, value in result.items() if key in ("lot_id", "lot_name", "bidder_number", "auction")},
    )


# --- acting on one lot -------------------------------------------------------


def _resolve_lot(request, params):
    """The lot the user means, as an object. Returns ``(lot, problem)``.

    Three ways in, in the order they should win: the lot they named, the lot whose page they are
    standing on, and -- when the name matched several -- a question listing them. ``find_lot`` does
    the searching and the scoping; this turns its answer into something an action can write to.
    """
    hint = _str(params, "lot") or _str(params, "query") or _str(params, "name")
    if not hint:
        # No name, so they mean the lot on screen. Not re-scoped, for the same reason the auction on
        # screen isn't: they are looking at its page.
        lot_id = _int(params, "lot_id") or _page(request).get("lot_id")
        if lot_id:
            lot = Lot.objects.filter(pk=lot_id, is_deleted=False).select_related("auction").first()
            if lot:
                return lot, None
        return None, _need("Which lot? Give me a lot number or its name.")
    found = find_lot(request, params)
    if "error" in found:
        return None, found
    if not found.get("found"):
        return None, _error(f"I couldn't find a lot called “{hint}”.")
    matches = found["lots"]
    if len(matches) > 1:
        return None, _need(
            f"There's more than one lot matching “{hint}”. Which one?",
            [{"label": f"{lot['name']} (lot {lot['lot_number']})", "value": lot["lot_number"]} for lot in matches],
        )
    lot = Lot.objects.filter(pk=matches[0]["lot_id"], is_deleted=False).select_related("auction").first()
    if not lot:
        return None, _error("I couldn't find that lot any more.")
    return lot, None


def watch_lot(request, params: dict[str, Any]) -> dict[str, Any]:
    """Add or remove a lot from the user's watch list.

    The same ``Watch`` row the star on the lot page toggles, so the watch list, the "lots I'm
    watching" page and the notifications that run off it all see it immediately.
    """
    from .models import Watch

    user = request.user
    lot, problem = _resolve_lot(request, params)
    if problem:
        return problem
    # "watch this" is the overwhelmingly common case, so watching is the default and un-watching has
    # to be asked for. Both spellings, because the model sends either.
    watching = params.get("watching")
    if watching is None:
        watching = not (_str(params, "action").lower() in {"unwatch", "remove", "stop"} or params.get("unwatch"))
    existing = Watch.objects.filter(lot_number=lot, user=user).first()
    if watching:
        if not existing:
            Watch.objects.create(lot_number=lot, user=user)
        summary = f"{lot.lot_name} is on your watch list."
        followups = [
            {"label": "View this lot", "url": lot.get_absolute_url()},
            {"label": "Everything I'm watching", "url": reverse("watched")},
        ]
        if _preference_boolean(params.get("notify")):
            note, prefs_needed = _enable_selling_notifications(user)
            summary += " " + note
            if prefs_needed:
                followups.insert(0, {"label": "Turn on notifications", "url": reverse("preferences")})
        return _ok(
            summary,
            lot_id=lot.pk,
            lot_name=lot.lot_name,
            followups=followups,
            undo={
                "action": "watch_lot",
                "params": {"lot_id": lot.pk, "watching": False},
                "describes": f"watching {lot.lot_name}",
            },
        )
    if existing:
        existing.delete()
    return _ok(
        f"Took {lot.lot_name} off your watch list.",
        lot_id=lot.pk,
        lot_name=lot.lot_name,
        followups=[{"label": "Everything I'm watching", "url": reverse("watched")}],
        undo={
            "action": "watch_lot",
            "params": {"lot_id": lot.pk, "watching": True},
            "describes": f"un-watching {lot.lot_name}",
        },
    )


def _enable_selling_notifications(user) -> tuple[str, bool]:
    """Turn on "tell me when a watched lot is about to sell", or explain why we can't.

    "Watch this and tell me when it ends" is one intent that used to be split across a covered
    capability (``watch_lot``) and one excused as machine-only
    (``UpdateLotPushNotificationsView``). The flag it flips is the same one that view flips.

    The catch is that the flag alone does nothing: the alert is a push, so it needs the app (or a
    browser subscription) on the far end. Turning it on for somebody with no device would be a
    promise we can't keep, so that case reports honestly and sends them to preferences, where the
    form explains what to install.

    Returns ``(sentence, they_need_to_visit_preferences)``.
    """
    userdata = getattr(user, "userdata", None)
    if userdata is None:
        return "I couldn't check your notification settings.", True
    if not userdata.has_app_push:
        return (
            "I can't notify you yet though — that needs the app on your phone, or notifications "
            "turned on in your browser.",
            True,
        )
    if userdata.push_notifications_when_lots_sell:
        return "You'll get a notification when it's about to be sold.", False
    userdata.push_notifications_when_lots_sell = True
    userdata.save(update_fields=["push_notifications_when_lots_sell"])
    return "I've also turned on notifications for when watched lots are about to be sold.", False


#: What ``edit_lot`` may change, and what to call each one in a sentence. Deliberately the fields on
#: the quick-add form and no more: the description and the photos are not things anybody dictates.
_LOT_FIELDS = (
    ("lot_name", "name"),
    ("quantity", "quantity"),
    ("reserve_price", "minimum bid"),
    ("buy_now_price", "buy now price"),
    ("donation", "donation"),
    ("i_bred_this_fish", "breeder points"),
    ("custom_field_1", "extra field"),
    ("custom_dropdown", "category"),
)


def edit_lot(request, params: dict[str, Any]) -> dict[str, Any]:
    """Change the price, quantity or name of a lot that's already been added.

    Validation is ``QuickAddLot`` with the lot as its instance -- the same form, built the same way,
    that the bulk-add page's formset uses to edit these very fields, so the auction's rules about
    buy-now, reserve prices and whole-dollar bids are enforced once and in one place.

    Who may edit is the lot page's rule (the seller, or the auction's admins) plus the model's own
    ``can_be_edited``, which is what stops somebody making a lot a donation thirty seconds before
    the auction ends.
    """
    from .forms import QUICK_ADD_LOT_FIELDS, quick_add_lot_form_class

    user = request.user
    lot, problem = _resolve_lot(request, params)
    if problem:
        return problem
    auction = lot.auction
    if not auction:
        return _error(f"{lot.lot_name} isn't part of an auction, so it has to be edited on its own page.")
    is_admin = _is_auction_admin(user, auction)
    seller = lot.auctiontos_seller
    owns = lot.user_id == user.pk or (seller and seller.user_id == user.pk)
    if not (owns or is_admin):
        return _error(f"{lot.lot_name} isn't your lot.")
    if not is_admin and lot.cannot_be_edited_reason:
        return _error(str(lot.cannot_be_edited_reason))
    if seller is None and not is_admin:
        # ``QuickAddLot.clean`` counts a seller's lots against the auction's limit, and it reaches
        # for the participant row to do it. A lot with no seller row is a legacy oddity, not
        # something to crash on.
        return _error(f"{lot.lot_name} has no seller in {auction.title}, so it has to be edited on its own page.")

    changes: dict[str, Any] = {}
    if _str(params, "name") and _str(params, "name") != _str(params, "lot"):
        # ``name`` doubles as a way of naming the lot, so it only renames when it isn't the thing
        # that found it -- otherwise "change the blue shrimp quantity" renames it to itself.
        changes["lot_name"] = tidy_lot_name(_str(params, "name"))
    if _str(params, "new_name"):
        changes["lot_name"] = tidy_lot_name(_str(params, "new_name"))
    quantity = _int(params, "quantity")
    if quantity is not None:
        changes["quantity"] = quantity
    for key, target in (("reserve_price", "reserve_price"), ("price", "reserve_price"), ("buy_now_price", None)):
        value = _decimal(params, key)
        if value is not None:
            changes[target or key] = value
    for key in ("donation", "i_bred_this_fish"):
        if key in params:
            changes[key] = bool(params.get(key))
    for key in ("custom_field_1", "custom_dropdown"):
        if params.get(key):
            changes[key] = _str(params, key)
    if not changes:
        return _need(f"What should I change about {lot.lot_name}? I can set its name, quantity or prices.")

    data = model_to_dict(lot, fields=QUICK_ADD_LOT_FIELDS)
    data = {key: ("" if value is None else value) for key, value in data.items()}
    # What was there before each of the fields about to change, so "undo that" can put it back.
    # Read off ``data`` rather than the instance because the form is about to mutate the instance.
    previous = {key: data.get(key) for key in changes}
    data.update(changes)
    form = quick_add_lot_form_class()(data, instance=lot, auction=auction, tos=seller, is_admin=is_admin)
    if not form.is_valid():
        return _form_problem(form)
    lot = form.save()
    if seller:
        # Prices and donation status are what the seller's fees are computed from, so an edit that
        # doesn't do this leaves an invoice that disagrees with the lot it's charging for.
        recalculate_seller_invoice(auction, seller)
    told = ", ".join(label for key, label in _LOT_FIELDS if key in changes)
    auction.create_history(
        applies_to="LOTS",
        action=f"Edited {told} on lot {lot.lot_number_display} (command palette)",
        user=user,
    )
    undo_params: dict[str, Any] = {"lot_id": lot.pk}
    for key, value in previous.items():
        undo_params["new_name" if key == "lot_name" else key] = "" if value is None else value
    return _ok(
        f"Changed the {told} on {lot.lot_name}.",
        lot_id=lot.pk,
        lot_name=lot.lot_name,
        auction=auction.slug,
        followups=[
            {"label": "View this lot", "url": lot.get_absolute_url()},
            _lot_label_followup(lot),
        ],
        undo={"action": "edit_lot", "params": undo_params, "describes": f"the change to {lot.lot_name}"},
    )


# --- invoices ----------------------------------------------------------------

#: What people call each invoice status out loud, mapped to what the model calls it. The display
#: names ("Open", "Ready", "Paid") are what the invoice page's own buttons say.
_INVOICE_STATUSES = {
    "paid": "PAID",
    "pay": "PAID",
    "settled": "PAID",
    "ready": "UNPAID",
    "unpaid": "UNPAID",
    "due": "UNPAID",
    "open": "DRAFT",
    "draft": "DRAFT",
    "reopen": "DRAFT",
}

#: The way back: one spoken word per stored status, so an undo can name the status it is restoring
#: in the same vocabulary this action accepts.
_INVOICE_STATUS_WORDS = {"PAID": "paid", "UNPAID": "ready", "DRAFT": "open"}


def set_invoice_status(request, params: dict[str, Any]) -> dict[str, Any]:
    """Mark one person's invoice paid, ready, or open again. Auction admins only.

    Runs on a real :class:`auctions.views.InvoicePaid` instance, so the club ledger entries, the
    membership renewal, the notification scheduling and the history line are the endpoint's own --
    the same ones the Paid button on the invoice produces. This is the busiest button on the site on
    auction day, which is exactly why it must not have a second implementation.
    """
    from .views import InvoicePaid

    user = request.user
    auction, error = resolve_auction(user, _str(params, "auction"), _page(request))
    if error:
        return _error(error)
    if not _is_auction_admin(user, auction):
        return _error(f"Only admins of {auction.title} can change invoices in {auction.title}.")
    tos, problem = resolve_person(user, auction, _str(params, "person") or _str(params, "bidder"))
    if problem:
        return problem
    wanted = (_str(params, "status") or "paid").lower()
    status = _INVOICE_STATUSES.get(wanted)
    if not status:
        return _need(f"I don't know what “{wanted}” means for an invoice. Paid, ready, or open?")
    invoice = tos.invoice
    if not invoice:
        return _error(f"{tos.name} doesn't have an invoice in {auction.title} yet.")
    if invoice.status == status:
        return _ok(
            f"{tos.name}'s invoice is already {invoice.get_status_display().lower()}.",
            bidder_number=tos.bidder_number,
            auction=auction.slug,
        )

    was = invoice.status
    view = InvoicePaid()
    view.request = request
    view.post(request, pk=invoice.pk, status=status)
    invoice.refresh_from_db()
    return _ok(
        f"{tos.name}'s invoice in {auction.title} is now marked "
        f"{invoice.get_status_display().lower()} — it {invoice.invoice_summary_short}.",
        followups=[{"label": f"{tos.name}'s invoice", "url": invoice.get_absolute_url()}],
        bidder_number=tos.bidder_number,
        person=tos.name,
        auction=auction.slug,
        undo={
            "action": "set_invoice_status",
            "params": {
                "person": tos.bidder_number or tos.name,
                "status": _INVOICE_STATUS_WORDS[was],
                "auction": auction.slug,
            },
            "describes": f"the change to {tos.name}'s invoice",
        },
    )


# --- club members ------------------------------------------------------------


def _resolve_club(user, hint: str, page: dict[str, Any] | None = None):
    """The club the user means, scoped to clubs they belong to or administer. ``(club, error)``."""
    club = palette_routes._club_from_hint(user, hint or (page or {}).get("club") or "")
    if club is None:
        if hint:
            return None, f"I couldn't find a club called “{hint}” that you're part of."
        return None, "I don't know which club you mean."
    return club, None


def _can_edit_members(user, club) -> bool:
    """The same question the member admin views ask before they let anyone write."""
    from .views import check_club_permission

    return bool(check_club_permission(user, club, "permission_add_edit"))


def _resolve_member(club, hint: str):
    """Find one club member by name, email, bidder number or membership number. ``(member, problem)``."""
    hint = (hint or "").strip()
    if not hint:
        return None, _need("Which member? Give me a name or a membership number.")
    members = ClubMember.objects.filter(club=club, is_deleted=False)
    exact = members.filter(Q(bidder_number__iexact=hint) | Q(email__iexact=hint)).first()
    if exact:
        return exact, None
    matches = list(members.filter(name__icontains=hint)[: AMBIGUOUS_LIMIT + 1])
    if not matches:
        return None, _error(f"I couldn't find anyone called “{hint}” in {club.name}.")
    if len(matches) > 1:
        return None, _need(
            f"There's more than one “{hint}” in {club.name}. Which one?",
            [
                {"label": f"{member.name} ({member.email or 'no email'})", "value": member.email or member.name}
                for member in matches
            ],
        )
    return matches[0], None


def _member_followups(club, member) -> list[dict[str, str]]:
    return [{"label": f"Members of {club.name}", "url": reverse("club_admin", kwargs={"slug": club.slug})}]


def _club_member_form(club, data, instance=None):
    """The club's own member form, built the way ``ClubMemberCreateView`` builds it.

    No auction is passed: the auction-scoped half of that form (pickup location, alternate fees) is
    the check-in flow, and ``add_person`` is the skill for adding somebody to an auction.
    """
    from .forms import ClubMemberAdminForm

    return ClubMemberAdminForm(data, instance=instance, club=club)


def add_club_member(request, params: dict[str, Any]) -> dict[str, Any]:
    """Add a new member to a club (club admins only).

    ``add_person`` puts somebody in an *auction*; this puts them in the *club*, which is what "sign
    Jane up as a member" means at a meeting with no auction running. Validation and the duplicate
    checks are ``ClubMemberAdminForm``, the form behind the Add member button, and the club history
    line matches the one that button writes.
    """
    from .models import ClubHistory

    user = request.user
    club, error = _resolve_club(user, _str(params, "club"), _page(request))
    if error:
        return _error(error)
    if not _can_edit_members(user, club):
        return _error(f"You don't have permission to add members to {club.name}.")
    name = _str(params, "name") or _str(params, "person")
    if not name:
        return _need("What's their name?")
    existing = ClubMember.objects.filter(club=club, is_deleted=False, name__iexact=name).first()
    if existing:
        return _error(f"{existing.name} is already a member of {club.name}.")
    data = {
        "name": name,
        "email": _str(params, "email"),
        "phone_number": _str(params, "phone_number") or _str(params, "phone"),
        "address": _str(params, "address"),
        "bidder_number": _str(params, "bidder_number"),
        "memo": _str(params, "memo"),
        "contact_status": "contact",
        "send_welcome_email": bool(params.get("send_welcome_email", False)),
        "bidding_allowed": True,
        "selling_allowed": True,
    }
    form = _club_member_form(club, data)
    if not form.is_valid():
        return _form_problem(form)
    member = form.save(commit=False)
    member.club = club
    member.added_by = user
    member.source = "manually_added"
    member.save()
    ClubHistory.objects.create(club=club, user=user, action=f"Added member {member}", applies_to="MEMBERS")
    summary = f"Added {member.name} to {club.name}"
    summary += f" as member {member.bidder_number}." if member.bidder_number else "."
    if not member.email:
        summary += " No email yet — tell me it, or use the link below."
    return _ok(summary, followups=_member_followups(club, member), person=member.name)


def update_club_member(request, params: dict[str, Any]) -> dict[str, Any]:
    """Change a club member's contact details (club admins only).

    The club-level twin of ``update_person``: same form the member edit modal uses, so the duplicate
    checks and the "an admin edited this" bookkeeping are the page's, not a second copy.
    """
    from .models import ClubHistory

    user = request.user
    club, error = _resolve_club(user, _str(params, "club"), _page(request))
    if error:
        return _error(error)
    if not _can_edit_members(user, club):
        return _error(f"You don't have permission to change members of {club.name}.")
    member, problem = _resolve_member(club, _str(params, "person") or _str(params, "name"))
    if problem:
        return problem
    changes = {}
    for key, alias in (("email", ""), ("phone_number", "phone"), ("address", ""), ("memo", "")):
        value = _str(params, key) or (_str(params, alias) if alias else "")
        if value:
            changes[key] = value
    if _str(params, "new_name"):
        changes["name"] = _str(params, "new_name")
    if _str(params, "bidder_number"):
        changes["bidder_number"] = _str(params, "bidder_number")
    if not changes:
        return _need(f"What should I change about {member.name}? I can set their email, phone or address.")
    data = model_to_dict(
        member, fields=[field for field in _club_member_form(club, None).fields if field != "send_welcome_email"]
    )
    data = {key: ("" if value is None else value) for key, value in data.items()}
    data.update(changes)
    data["send_welcome_email"] = False
    form = _club_member_form(club, data, instance=member)
    if not form.is_valid():
        return _form_problem(form)
    member = form.save()
    ClubHistory.objects.create(
        club=club, user=user, action=f"Edited member {member} (command palette)", applies_to="MEMBERS"
    )
    told = ", ".join(sorted(changes))
    return _ok(
        f"Updated {member.name}'s {told.replace('_', ' ')} in {club.name}.",
        followups=_member_followups(club, member),
        person=member.name,
    )


def renew_member(request, params: dict[str, Any]) -> dict[str, Any]:
    """Extend a club member's membership by one period (club admins only).

    Calls ``views.renew_club_member``, which is the function behind the Renew button and behind the
    clubs' own API, so the expiration maths, the ledger entry, the history line and the confirmation
    email are all the ones a renewal is supposed to produce.

    Not to be confused with ``renew_membership``, which is *the user's own* membership and only ever
    navigates -- that one involves paying.
    """
    from .views import renew_club_member

    user = request.user
    club, error = _resolve_club(user, _str(params, "club"), _page(request))
    if error:
        return _error(error)
    if not _can_edit_members(user, club):
        return _error(f"You don't have permission to renew memberships in {club.name}.")
    member, problem = _resolve_member(club, _str(params, "person") or _str(params, "name"))
    if problem:
        return problem
    renew_club_member(member, acting_user=user)
    expires = member.membership_expiration_date
    when = expires.strftime("%B %-d %Y") if expires else "an unknown date"
    return _ok(
        f"Renewed {member.name}'s membership of {club.name}. It now runs to {when}.",
        followups=_member_followups(club, member),
        person=member.name,
    )


def award_points(request, params: dict[str, Any]) -> dict[str, Any]:
    """Give a club member breeder award points (BAP/HAP/CAP). Club BAP admins only.

    Validation is ``BapAwardForm``, the form in the Add points modal, built with the same
    ``show_hap`` / ``show_cap`` flags -- so a club that doesn't run a separate HAP can't be given HAP
    points by voice, exactly as on the page.
    """
    from .forms import BapAwardForm
    from .models import ClubHistory
    from .views import check_club_permission

    user = request.user
    club, error = _resolve_club(user, _str(params, "club"), _page(request))
    if error:
        return _error(error)
    if not check_club_permission(user, club, "permission_manage_bap"):
        return _error(f"You don't have permission to award points in {club.name}.")
    member, problem = _resolve_member(club, _str(params, "person") or _str(params, "name"))
    if problem:
        return problem
    points = _int(params, "points")
    hap = _int(params, "hap_points")
    cap = _int(params, "cap_points")
    if points is None and hap is None and cap is None:
        return _need(f"How many points should {member.name} get?")
    data = {
        "club_member": member.pk,
        "date": _str(params, "date") or timezone.now().date().isoformat(),
        "points": points or 0,
        "hap_points": hap or 0,
        "cap_points": cap or 0,
        "notes": _str(params, "notes") or _str(params, "reason"),
    }
    form = BapAwardForm(data, club=club, show_hap=club.separate_hap, show_cap=club.separate_cap)
    if not form.is_valid():
        return _form_problem(form)
    award = form.save(commit=False)
    award.awarded_by = user
    award.save()
    ClubHistory.objects.create(club=club, user=user, action=f"Added BAP award: {award}", applies_to="BAP")
    earned = ", ".join(
        f"{value} {label}"
        for value, label in ((points, "BAP"), (hap, "HAP"), (cap, "CAP"))
        if value  # a zero in one of the three columns is not worth saying out loud
    )
    return _ok(
        f"Gave {member.name} {earned} point(s) in {club.name}.",
        followups=_member_followups(club, member),
        person=member.name,
    )


# --- registry ----------------------------------------------------------------

register(
    Action(
        name="add_lot",
        description=(
            "Add one lot — an item for sale — to an auction. Defaults to the user's most recent "
            "auction and to the user themselves as the seller. Only auction admins may pass "
            "'bidder' to add a lot for someone else. A lot is a thing: fish, plants, shrimp, food, "
            "equipment. If what they want to add is a PERSON (a first name and a surname, 'add "
            "mike smith'), they mean add_person, not a lot called Mike Smith."
        ),
        params={
            "name": "string, required. What the item is, e.g. 'blue shrimp'. Never a person's name.",
            "auction": "string, optional. Auction slug or title; omit for the user's last auction.",
            "quantity": "integer, optional, default 1.",
            "bidder": "string, optional, ADMINS ONLY. Bidder number or name to add the lot for.",
            "reserve_price": "number, optional. The minimum bid; omit for the auction's minimum.",
            "buy_now_price": "number, optional.",
            "donation": "boolean, optional.",
            "i_bred_this_fish": (
                "boolean, optional. True when the seller bred or grew this themselves — 'I bred "
                "these', 'these are mine'. This is what earns breeder award points, so never drop it."
            ),
            "custom_field_1": (
                "string, optional. Only for auctions that use a custom text field; its label is in "
                "'lot_fields_this_auction_uses' in the context below."
            ),
            "custom_dropdown": (
                "string, optional. Only for auctions that use a custom dropdown; its label and "
                "allowed values are in 'lot_fields_this_auction_uses' in the context below."
            ),
        },
        danger=DANGER_CONFIRM,
        resolver=add_lot,
        aliases={"seller", "lot_name", "price"},
        confirm_template="Add a lot",
        examples=["add a lot of blue shrimp", "add 3 guppies for bidder 14"],
    )
)

register(
    Action(
        name="add_lots",
        description=(
            "Add SEVERAL lots to one auction at once. Use this whenever the user names more than "
            "one thing in a single sentence — 'add a java fern, a heater and three guppies'. Each "
            "entry in 'lots' may be a plain name, or an object with a name plus any of the "
            "per-lot fields add_lot takes. Anything set at the top level (bidder, donation, "
            "i_bred_this_fish) applies to every lot that doesn't set it itself."
        ),
        params={
            "lots": (
                "array, required. The things to add, e.g. ['java fern', 'heater'] or "
                "[{'name': 'guppies', 'quantity': 3}]."
            ),
            "auction": "string, optional. Auction slug or title; omit for the user's last auction.",
            "bidder": "string, optional, ADMINS ONLY. Bidder number or name to add the lots for.",
            "donation": "boolean, optional. Applies to every lot in the list.",
            "i_bred_this_fish": "boolean, optional. Applies to every lot in the list.",
        },
        danger=DANGER_CONFIRM,
        resolver=add_lots,
        aliases={"seller", "items", "names"},
        confirm_template="Add several lots",
        examples=["add a java fern, a heater and three guppies", "add 3 lots of shrimp for bidder 14"],
    )
)

register(
    Action(
        name="no_sale",
        description=(
            "Record that a lot did NOT sell in an in-person auction, ending it with no winner. "
            "Auction admins only. This is what 'pass', 'no sale', 'lot 14 didn't sell' and "
            "'nobody wanted it' mean. It is the ordinary outcome for a lot, not an undo — to "
            "reverse a sale that was recorded wrongly, use undo_sale."
        ),
        params={
            "lot": "string, required. The lot number as called out.",
            "auction": "string, optional. Defaults to the user's last auction.",
        },
        danger=DANGER_CONFIRM,
        resolver=no_sale,
        confirm_template="Mark a lot as not sold",
        examples=["lot 14 didn't sell", "pass on lot 22", "no sale"],
        needs=NEEDS_AUCTION_ADMIN,
    )
)

register(
    Action(
        name="draw_door_prize",
        description=(
            "Pick a door prize winner at random from the people who have checked in and haven't "
            "already won one. For auction admins and club staff. 'draw a door prize', 'pick a "
            "winner', 'who wins the door prize?'."
        ),
        params={"auction": "string, optional. Defaults to the user's last auction."},
        danger=DANGER_CONFIRM,
        resolver=draw_door_prize,
        confirm_template="Draw a door prize",
        examples=["draw a door prize", "pick a door prize winner"],
        needs=NEEDS_AUCTION_ADMIN,
    )
)

register(
    Action(
        name="update_preferences",
        description=(
            "Change ONE of this user's own preferences without sending them to the preferences "
            "page: whether their email or username is visible, miles or kilometres, their "
            "currency, and every notification email they can turn on or off. 'stop emailing me "
            "about new auctions', 'switch me to kilometres', 'hide my email', 'turn on push "
            "notifications'. Say the setting in the user's own words — it gets matched against the "
            "real settings. One setting per call."
        ),
        params={
            "setting": "string, required. Which preference, in the user's words.",
            "value": (
                "string or boolean, required. For a checkbox: true/false (or 'on'/'off'). "
                "For a choice: the value, e.g. 'km' or 'miles'."
            ),
        },
        danger=DANGER_CONFIRM,
        resolver=update_preferences,
        aliases={"preference", "name"},
        confirm_template="Change a preference",
        examples=["stop emailing me about new auctions", "switch me to kilometres", "hide my email address"],
    )
)

register(
    Action(
        name="join_auction",
        description=(
            "Open an auction's page so the user can join it. This is the ONLY action that can "
            "reach an auction the user has not already joined, so use it for 'sign me up for the "
            "fall auction', 'join the spring auction', 'am I registered for this one?'. It never "
            "joins on their behalf — joining means agreeing to that auction's rules, and picking a "
            "pickup location, which they do on the page."
        ),
        params={"auction": "string, optional. Auction title; omit for the one they're looking at."},
        danger=DANGER_NAVIGATE,
        resolver=join_auction,
        aliases={"name"},
        examples=["sign me up for the fall auction", "am I registered for this auction?"],
    )
)

register(
    Action(
        name="send_membership_card",
        description=(
            "Email the user their OWN club membership card again, to the address already on their "
            "membership. 'send me my membership card', 'I lost my card', 'resend my card'."
        ),
        params={"club": "string, optional. Club name; omit to use the club they last used."},
        danger=DANGER_CONFIRM,
        resolver=send_membership_card,
        confirm_template="Send your membership card",
        examples=["send me my membership card again"],
    )
)

register(
    Action(
        name="set_lot_winner",
        description=(
            "Record the winner and selling price of a lot in an in-person auction. Auction admins "
            "only. Needs the lot number, the winning bidder number, and the price."
        ),
        params={
            "lot": "string, required. The lot number as called out.",
            "winner": "string, required. The winning bidder number.",
            "price": "number, required. The winning price.",
            "auction": "string, optional. Defaults to the user's last auction.",
        },
        danger=DANGER_CONFIRM,
        resolver=set_lot_winner,
        confirm_template="Record a sale",
        examples=["lot 101 sold to bidder 14 for 25"],
        needs=NEEDS_AUCTION_ADMIN,
    )
)

register(
    Action(
        name="check_in",
        description=(
            "Check a participant in to an in-person auction so they can bid. For auction admins and club staff only."
        ),
        params={
            "person": "string, required. Name or bidder number of the person arriving.",
            "auction": "string, optional. Defaults to the user's last auction.",
            "bidder_number": "string, optional. Assign this bidder number while checking in.",
        },
        danger=DANGER_CONFIRM,
        resolver=check_in,
        aliases={"bidder"},
        confirm_template="Check someone in",
        examples=["check in bob", "check in bidder 22"],
        needs=NEEDS_AUCTION_ADMIN,
    )
)

register(
    Action(
        name="add_person",
        description=(
            "Add a person to an auction so they can bid and sell. For auction admins and club "
            "staff only. This is what 'add mike smith' means: a person's name is a person, not a "
            "lot. Use check_in instead when they are already in the auction and are arriving."
        ),
        params={
            "name": "string, required. The person's name.",
            "auction": "string, optional. Defaults to the user's last auction.",
            "email": "string, optional.",
            "phone_number": "string, optional.",
            "bidder_number": "string, optional. Omit to let the auction assign the next one.",
        },
        danger=DANGER_CONFIRM,
        resolver=add_person,
        aliases={"person", "address"},
        confirm_template="Add someone to the auction",
        examples=["add mike smith", "add a new bidder called Jane Doe"],
        needs=NEEDS_AUCTION_ADMIN,
    )
)

register(
    Action(
        name="update_person",
        description=(
            "Change something about a person in an auction: their email, phone number, address, the "
            "spelling of their name, their bidder number, the admin note on them, or whether they "
            "are allowed to bid or sell. For auction admins and club staff only. This is what "
            "'change bob's email to bob@example.com', 'bob is bidder 12 now', 'let jane bid', "
            "'stop bob selling' and 'note that bob paid cash' all mean. Use 'person' for who to "
            "change and 'new_name' (not 'name') when the change is to their name. This does NOT "
            "check anybody in, and it never makes anyone an admin."
        ),
        params={
            "person": "string, required. Their current name or bidder number.",
            "email": "string, optional. Their new email address.",
            "phone_number": "string, optional. Their new phone number.",
            "address": "string, optional. Their new mailing address.",
            "new_name": "string, optional. A corrected spelling of their name.",
            "bidder_number": "string, optional. A new bidder number for them.",
            "memo": "string, optional. An admin-only note about them.",
            "bidding_allowed": "boolean, optional. False to stop them bidding, true to allow it.",
            "selling_allowed": "boolean, optional. False to stop them selling, true to allow it.",
            "auction": "string, optional. Defaults to the user's last auction.",
        },
        danger=DANGER_CONFIRM,
        resolver=update_person,
        aliases={"name", "phone"},
        confirm_template="Update someone's details",
        examples=["change bob's email to bob@example.com", "let jane bid", "note that bob paid cash"],
        needs=NEEDS_AUCTION_ADMIN,
    )
)

register(
    Action(
        name="edit_lot",
        description=(
            "Change a lot that has already been added: its name, quantity, minimum bid, buy now "
            "price, or whether it's a donation. The seller or an auction admin only. This is what "
            "'make lot 14 twenty dollars', 'change the shrimp to 3 of them' and 'that one's a "
            "donation' mean. To find out about a lot instead of changing it, use describe_lot."
        ),
        params={
            "lot": "string, optional. Lot number or name; omit for the lot they're looking at.",
            "new_name": "string, optional. A new name for the lot.",
            "quantity": "integer, optional.",
            "reserve_price": "number, optional. The minimum bid.",
            "buy_now_price": "number, optional.",
            "donation": "boolean, optional.",
            "i_bred_this_fish": "boolean, optional. Whether the seller bred this themselves (breeder award points).",
            "custom_field_1": "string, optional. Only for auctions using a custom text field — see the context below.",
            "custom_dropdown": "string, optional. Only for auctions using a custom dropdown — see the context below.",
        },
        danger=DANGER_CONFIRM,
        resolver=edit_lot,
        aliases={"name", "query", "lot_id", "price"},
        confirm_template="Change a lot",
        examples=["make lot 14 twenty dollars", "change the quantity on the blue shrimp to 3"],
    )
)

register(
    Action(
        name="watch_lot",
        description=(
            "Add a lot to the user's watch list, or take it off again. Any signed-in user, on any "
            "lot they can see. 'watch this', 'save that lot', 'stop watching the plecos'."
        ),
        params={
            "lot": "string, optional. Lot number or name; omit for the lot they're looking at.",
            "watching": "boolean, optional, default true. False to remove it from the watch list.",
            "notify": (
                "boolean, optional. True when they also want telling as it sells — 'watch this and "
                "let me know when it ends'. Needs the app; the answer says so when they don't have it."
            ),
        },
        danger=DANGER_CONFIRM,
        resolver=watch_lot,
        aliases={"name", "query", "lot_id", "unwatch", "action"},
        confirm_template="Update your watch list",
        examples=["watch this lot", "stop watching lot 12"],
    )
)

register(
    Action(
        name="set_invoice_status",
        description=(
            "Mark one person's invoice in an auction as paid, ready, or open again. Auction admins "
            "only. This is the checkout desk: 'bob paid', 'mark bidder 14 paid', 'reopen jane's "
            "invoice'. It records that money changed hands — it does not take a payment."
        ),
        params={
            "person": "string, required. Their name or bidder number.",
            "status": "string, optional: 'paid' (default), 'ready', or 'open'.",
            "auction": "string, optional. Defaults to the user's last auction.",
        },
        danger=DANGER_CONFIRM,
        resolver=set_invoice_status,
        aliases={"bidder", "name"},
        confirm_template="Change an invoice",
        examples=["bidder 14 paid", "mark bob's invoice paid"],
        needs=NEEDS_AUCTION_ADMIN,
    )
)

register(
    Action(
        name="add_club_member",
        description=(
            "Add a new member to a club. Club admins only. This is for club membership — someone "
            "joining the club itself. If they are being added to an AUCTION so they can bid today, "
            "that is add_person instead."
        ),
        params={
            "name": "string, required. The person's name.",
            "club": "string, optional. Club name; omit for the club they're looking at.",
            "email": "string, optional.",
            "phone_number": "string, optional.",
            "address": "string, optional.",
        },
        danger=DANGER_CONFIRM,
        resolver=add_club_member,
        aliases={"person", "memo", "bidder_number", "phone", "send_welcome_email"},
        confirm_template="Add a club member",
        examples=["add jane doe as a new member", "sign mike up for the club"],
        needs=NEEDS_CLUB_ADMIN,
    )
)

register(
    Action(
        name="update_club_member",
        description=(
            "Change a club member's details: email, phone number, address, membership number, the "
            "spelling of their name, or the admin note on them. Club admins only. Use 'person' for "
            "who to change and 'new_name' when the change is to their name."
        ),
        params={
            "person": "string, required. Their name, email or membership number.",
            "club": "string, optional. Club name; omit for the club they're looking at.",
            "email": "string, optional.",
            "phone_number": "string, optional.",
            "address": "string, optional.",
            "new_name": "string, optional.",
            "memo": "string, optional. An admin-only note about them.",
        },
        danger=DANGER_CONFIRM,
        resolver=update_club_member,
        aliases={"name", "phone", "bidder_number"},
        confirm_template="Update a club member",
        examples=["change jane's email in the club to jane@example.com"],
        needs=NEEDS_CLUB_ADMIN,
    )
)

register(
    Action(
        name="renew_member",
        description=(
            "Renew SOMEONE ELSE's club membership, extending it by one period and recording the "
            "payment in the club's books. Club admins only. 'renew bob's membership', 'bob paid his "
            "dues'. For the user's OWN membership use renew_membership, which takes them to the "
            "page where they can pay."
        ),
        params={
            "person": "string, required. The member's name, email or membership number.",
            "club": "string, optional. Club name; omit for the club they're looking at.",
        },
        danger=DANGER_CONFIRM,
        resolver=renew_member,
        aliases={"name"},
        confirm_template="Renew a membership",
        examples=["renew bob's membership", "mike paid his dues"],
        needs=NEEDS_CLUB_ADMIN,
    )
)

register(
    Action(
        name="award_points",
        description=(
            "Give a club member breeder award points (BAP, and HAP or CAP where the club runs them "
            "separately). Club BAP admins only. 'give bob 10 points for the corydoras', '5 hap "
            "points for jane'. To explain how points are earned, use describe_club instead."
        ),
        params={
            "person": "string, required. The member's name, email or membership number.",
            "points": "integer, optional. BAP points.",
            "hap_points": "integer, optional. Only if the club runs a separate HAP.",
            "cap_points": "integer, optional. Only if the club runs a separate CAP.",
            "notes": "string, optional. What the points are for.",
            "club": "string, optional. Club name; omit for the club they're looking at.",
        },
        danger=DANGER_CONFIRM,
        resolver=award_points,
        aliases={"name", "reason", "date"},
        confirm_template="Award points",
        examples=["give bob 10 points for the corydoras", "5 hap points for jane"],
        needs=NEEDS_CLUB_ADMIN,
    )
)

register(
    Action(
        name="search_lots",
        description=(
            "Show the user lots matching what they're looking for, by opening the lot list "
            "filtered to their search. This is the right answer for 'find shrimp', 'what plants "
            "are in this auction', 'any cichlids?' — anything where they want to SEE lots. Do not "
            "use find_lot for this; find_lot is for turning a name into a lot number before "
            "acting on it."
        ),
        params={
            "query": "string, required. What to search lot names for, in the user's words.",
            "auction": "string, optional. Omit to search the auction they're looking at.",
            "everywhere": "boolean, optional. True to search the whole site rather than one auction.",
        },
        danger=DANGER_NAVIGATE,
        resolver=search_lots,
        aliases={"q", "name", "this_auction"},
        examples=["find shrimp in this auction", "show me the plants", "any pleco lots?"],
    )
)

register(
    Action(
        name="describe_auction",
        description=(
            "Get the full details of an auction: its dates, whether lot submission is open, its "
            "pickup locations, its fees and settings, and the full text of its rules. Use this to "
            "ANSWER questions about how an auction works — what the rules say, when things close, "
            "how much the club takes, how many lots there are."
        ),
        params={"auction": "string, optional. Auction title; omit for the one they're looking at."},
        danger=DANGER_SAFE,
        resolver=describe_auction,
        aliases={"name"},
        lookup=True,
    )
)

register(
    Action(
        name="describe_club",
        description=(
            "Get the full details of a club: what it is, what membership costs, and exactly how "
            "its breeder award (BAP/HAP/CAP) points are awarded, including the per-category "
            "overrides. Every point setting comes back with an explanation of what it does, so "
            "use this to ANSWER 'how do I earn points', not to guess."
        ),
        params={"club": "string, optional. Club name; omit for the club they're looking at."},
        danger=DANGER_SAFE,
        resolver=describe_club,
        aliases={"name"},
        lookup=True,
    )
)

register(
    Action(
        name="describe_lot",
        description=(
            "Get the full details of one lot: its description, category, prices, whether it sold "
            "and for how much. Use this to answer questions about a specific lot."
        ),
        params={
            "lot": "string, required. Lot number or name.",
            "auction": "string, optional.",
        },
        danger=DANGER_SAFE,
        resolver=describe_lot,
        aliases={"query", "name"},
        lookup=True,
    )
)

register(
    Action(
        name="describe_person",
        description=(
            "Get everything about one participant in an auction: their bidder number, whether "
            "they've checked in, how many lots they brought, won and sold, and their invoice "
            "status. Auction admins only."
        ),
        params={
            "name": "string, required. Their name or bidder number.",
            "auction": "string, optional. Defaults to the user's last auction.",
        },
        danger=DANGER_SAFE,
        resolver=describe_person,
        aliases={"person", "query"},
        lookup=True,
        needs=NEEDS_AUCTION_ADMIN,
    )
)

register(
    Action(
        name="find_person",
        description=(
            "Look up a person among the club members and auction participants this user "
            "administers. Use this to turn a name into a bidder number before another action."
        ),
        params={"name": "string, required. Name, email or bidder number to search for."},
        danger=DANGER_SAFE,
        resolver=find_person,
        aliases={"query"},
        lookup=True,
    )
)

register(
    Action(
        name="my_context",
        description=(
            "Get details about the current user: their clubs and memberships, their most recent "
            "auction, whether they administer it, and whether lot submission is open."
        ),
        params={},
        danger=DANGER_SAFE,
        resolver=my_context,
        lookup=True,
    )
)

register(
    Action(
        name="print_labels",
        description=(
            "Open a label printing page. Never prints by itself. Pass lot_id to print one "
            "specific lot's label (use the lot_id from an earlier add_lot in this conversation)."
        ),
        params={
            "scope": "string, optional: 'mine' (default), 'unprinted', or 'auction' (admins only).",
            "bidder": (
                "string, optional, ADMINS ONLY. A bidder number or name to print that one person's "
                "labels — 'print bidder 14's labels'. Combine with scope 'unprinted' for only their new ones."
            ),
            "lot_id": "integer, optional. A specific lot to print the label for.",
            "auction": "string, optional. Defaults to the user's last auction.",
        },
        danger=DANGER_NAVIGATE,
        resolver=print_labels,
        aliases={"bidder_number", "person"},
        examples=["print my labels", "print that label", "print bidder 14's labels"],
    )
)

register(
    Action(
        name="renew_membership",
        description=(
            "Open the membership payment page for one of the user's clubs. This never takes "
            "payment — it only takes the user to the page where they can pay."
        ),
        params={"club": "string, optional. Club name; omit to use the club they last used."},
        danger=DANGER_NAVIGATE,
        resolver=renew_membership,
        examples=["renew my membership"],
    )
)

register(
    Action(
        name="go_to_page",
        description=(
            "Open any page on the site. 'page' must be one of the destination keys listed under "
            "'Pages you can open' below — that list is every page this site has. Use 'target' when "
            "the page is about a particular thing: an auction name, a bidder, a lot, a club. This "
            "is the right answer for anything phrased as 'take me to', 'open', 'show me' or 'where "
            "is', and it is also the correct last resort when no other action fits."
        ),
        params={
            "page": "string, required. A destination key from the list below.",
            "target": (
                "string, optional. Which auction / club / lot / person the page is about. "
                "Omit to use whatever the user is currently looking at."
            ),
            "tab": "string, optional, only for club_detail_tab: bap, hap, culture or my-points.",
        },
        danger=DANGER_NAVIGATE,
        resolver=go_to_page,
        aliases={"query", "club", "lot_id"},
        examples=["take me to my invoice", "auction rules", "open the treasurer report"],
    )
)

register(
    Action(
        name="find_page",
        description=(
            "Search the list of pages when you aren't sure which destination key to use. Returns "
            "keys you can pass straight to go_to_page."
        ),
        params={"query": "string, required. What the user is trying to reach, in their words."},
        danger=DANGER_SAFE,
        resolver=find_page,
        aliases={"page"},
        lookup=True,
    )
)

register(
    Action(
        name="find_lot",
        description=(
            "Look up a lot by its number or name, in auctions this user is part of. Use this to "
            "turn 'the blue shrimp' into a lot number before acting on it."
        ),
        params={
            "lot": "string, required. Lot number or part of the lot's name.",
            "auction": "string, optional. Defaults to searching every auction the user is in.",
        },
        danger=DANGER_SAFE,
        resolver=find_lot,
        aliases={"query", "name"},
        lookup=True,
    )
)

register(
    Action(
        name="undo_last",
        description=(
            "Reverse the last thing you did for this user, if it can be reversed. Use this for a "
            "bare 'undo that', 'no wait', 'that was wrong', 'never mind' — anything that refers "
            "back to the previous command without saying what it was. If they name what to undo "
            "('undo lot 14'), use the specific action instead. Adding things cannot be undone this "
            "way, because that would mean deleting them."
        ),
        params={},
        danger=DANGER_CONFIRM,
        resolver=undo_last,
        confirm_template="Undo the last thing",
        examples=["undo that", "no wait, that was wrong", "never mind"],
    )
)

register(
    Action(
        name="auction_numbers",
        description=(
            "Get the running totals for an auction: how many lots there are, how many have sold, "
            "how many are still unsold, how many people have signed up and checked in, how long is "
            "left before it closes, and — for its admins — the gross, the median price and how many "
            "invoices are still unpaid. This is what answers 'how's it going?', 'how many have "
            "sold?', 'what's the gross?', 'how long is left?' and 'how many people are here?'."
        ),
        params={"auction": "string, optional. Auction title; omit for the one they're looking at."},
        danger=DANGER_SAFE,
        resolver=auction_numbers,
        aliases={"name"},
        lookup=True,
    )
)

register(
    Action(
        name="my_activity",
        description=(
            "Get what THIS user has going on in an auction: their bidder number, how many lots they "
            "brought, how many sold, how many they won, what their invoice says, what they're "
            "watching — plus their club memberships, whether their dues are paid up, when they "
            "expire and how many breeder award points they have. Use this for anything phrased as "
            "'what did I win', 'what do I owe', 'did my lots sell', 'am I paid up', 'how many "
            "points do I have', 'what am I watching'."
        ),
        params={"auction": "string, optional. Auction title; omit for the one they're looking at."},
        danger=DANGER_SAFE,
        resolver=my_activity,
        lookup=True,
    )
)

register(
    Action(
        name="list_people",
        description=(
            "List the people in an auction matching one status. Auction admins only. This answers "
            "'who hasn't paid?', 'who hasn't checked in?', 'who's here?' and 'did I add anyone "
            "twice?'. To look up ONE person use describe_person instead."
        ),
        params={
            "status": (
                "string, required. One of: 'unpaid', 'paid', 'checked_in', 'not_checked_in', 'duplicates', or 'all'."
            ),
            "auction": "string, optional. Defaults to the auction they're looking at.",
        },
        danger=DANGER_SAFE,
        resolver=list_people,
        aliases={"query", "filter"},
        lookup=True,
        needs=NEEDS_AUCTION_ADMIN,
    )
)

register(
    Action(
        name="list_lots",
        description=(
            "List the lots in an auction matching one status: 'unsold' (no winner yet), 'sold', "
            "'mine' (the user's own lots), 'donations', or 'all'. This answers 'which lots haven't "
            "sold?', 'did my lots sell?' and 'what's left?'. If they want to SEE and browse lots "
            "matching a search term, use search_lots instead."
        ),
        params={
            "status": "string, required. One of: 'unsold', 'sold', 'mine', 'donations', 'all'.",
            "auction": "string, optional. Defaults to the auction they're looking at.",
        },
        danger=DANGER_SAFE,
        resolver=list_lots,
        aliases={"query", "filter"},
        lookup=True,
    )
)

register(
    Action(
        name="recent_changes",
        description=(
            "Get the recent changes made in an auction — who did what and when, newest first. "
            "Auction admins only. This is what answers 'what did you just do?', 'what's changed?' "
            "and 'who checked Bob in?'."
        ),
        params={
            "auction": "string, optional. Defaults to the auction they're looking at.",
            "mine": "boolean, optional. True for only changes this user made.",
            "assistant": "boolean, optional. True for only changes made through this assistant.",
        },
        danger=DANGER_SAFE,
        resolver=recent_changes,
        lookup=True,
        needs=NEEDS_AUCTION_ADMIN,
    )
)

register(
    Action(
        name="lot_queue",
        description=(
            "Get the lot queue for an in-person auction: which lot is being sold right now and "
            "what is coming up behind it. This answers 'what lot are we on?', 'what's next?' and "
            "'is anything I'm watching coming up?'."
        ),
        params={"auction": "string, optional. Defaults to the auction they're looking at."},
        danger=DANGER_SAFE,
        resolver=lot_queue,
        lookup=True,
    )
)

register(
    Action(
        name="my_messages",
        description=(
            "Get the questions people have asked on the user's own lots, newest first. This answers "
            "'has anyone asked me anything?', 'any questions on my lots?', 'did anyone comment?'. "
            "It only reads them — replying happens on the lot's own page."
        ),
        params={"auction": "string, optional. Omit for every auction they've sold in."},
        danger=DANGER_SAFE,
        resolver=my_messages,
        lookup=True,
    )
)

register(
    Action(
        name="club_numbers",
        description=(
            "Get a club's own numbers: how many members it has, how many are paid up, how many "
            "lapse soon, and — for its treasurers — the book balance. This answers 'how many "
            "members do we have?', 'how many renewed?', 'what's our balance?'. Club staff only."
        ),
        params={"club": "string, optional. Club name; omit for the club they're looking at."},
        danger=DANGER_SAFE,
        resolver=club_numbers,
        aliases={"name"},
        lookup=True,
        needs=NEEDS_CLUB_ADMIN,
    )
)

register(
    Action(
        name="auctions_near_me",
        description=(
            "Find auctions happening near the user, including ones they have not joined. This is "
            "the ONLY way to reach an auction they aren't already part of, so it is the right "
            "answer for 'is there an auction near me?', 'what's coming up?', 'when's the next "
            "in-person one?' and anything else asking what exists rather than about an auction "
            "they're already in."
        ),
        params={"distance": "integer, optional. Search radius in miles, default 100."},
        danger=DANGER_SAFE,
        resolver=auctions_near_me,
        aliases={"miles", "radius"},
        lookup=True,
    )
)

register(
    Action(
        name="clubs_near_me",
        description=(
            "Find fish clubs near the user. This answers 'is there a fish club near me?', 'what "
            "clubs are around here?', 'who should I join?'."
        ),
        params={"distance": "integer, optional. Search radius in miles, default 100."},
        danger=DANGER_SAFE,
        resolver=clubs_near_me,
        aliases={"miles", "radius"},
        lookup=True,
    )
)

register(
    Action(
        name="search_help",
        description=(
            "Search this site's own FAQ and blog for how something works. Use this for ANY "
            "platform question — 'how does proxy bidding work?', 'what's a donation lot?', 'how do "
            "I print labels?', 'what does buy now mean?'. This site does not work the same way as "
            "other auction sites, so answer from what this returns and never from general "
            "knowledge. If it finds nothing, say so."
        ),
        params={"query": "string, required. What they want to know, in their words."},
        danger=DANGER_SAFE,
        resolver=search_help,
        aliases={"question", "q"},
        lookup=True,
    )
)

register(
    Action(
        name="undo_sale",
        description=(
            "Clear the winner and price on a lot that was sold by mistake in an in-person "
            "auction, putting it back up for sale. Auction admins only."
        ),
        params={
            "lot": "string, required. The lot number to un-sell.",
            "auction": "string, optional. Defaults to the user's last auction.",
        },
        danger=DANGER_CONFIRM,
        resolver=undo_sale,
        confirm_template="Undo a sale",
        examples=["undo lot 14", "that last one was wrong, unsell it"],
        needs=NEEDS_AUCTION_ADMIN,
    )
)


#: Parameters worth naming in a countdown summary, best-identifying first.
_SUBJECT_PARAMS = ("name", "person", "lot", "lot_name", "bidder", "winner", "page", "query")


def default_summary(action: Action, params: dict[str, Any]) -> str:
    """A countdown headline for when the model didn't write one.

    The model is asked for a summary and often just doesn't send one, which left the card reading
    "Add someone to the auction." over a five second countdown -- an accurate description of the
    wrong half of the sentence. Who or what it is about is the part worth checking before it runs,
    and the server already has it in the parameters.
    """
    verb = action.confirm_template or action.name.replace("_", " ")
    subject = ""
    for key in _SUBJECT_PARAMS:
        value = _str(params, key)
        if value:
            subject = value
            break
    if not subject:
        return f"{verb}."
    return f"{verb}: {subject}."


def action_context(request, action: Action, params: dict[str, Any]) -> str:
    """Which auction (or club, or lot) an action is about to touch, phrased for the user.

    "Adding a lot" is not enough to check before a countdown runs out -- to *which* auction is the
    part worth catching, and the palette knows the answer before it acts. Read-only and best-effort:
    this runs to decorate a message, so anything it can't work out comes back as an empty string
    rather than an error.
    """
    try:
        # Which object matters is read off the action's own parameters rather than a list kept here,
        # so a new action gets a context line the moment it declares it works on an auction.
        if action.accepts("club") and not action.accepts("auction"):
            club = palette_routes._club_from_hint(request.user, _str(params, "club") or _str(params, "name"))
            return club.name if club else ""
        wants_auction = action.accepts("auction") or (
            # go_to_page takes any page in the catalog, so ask the route instead of the action.
            action.name == "go_to_page" and palette_routes.route_needs_an_auction(_str(params, "page"))
        )
        if wants_auction:
            hint = _str(params, "auction") or (_str(params, "target") if action.name == "go_to_page" else "")
            auction, error = resolve_auction(request.user, hint, _page(request))
            return auction.title if not error else ""
    except Exception:  # pragma: no cover - never let a label break the request
        logger.exception("Could not describe the context for %s", action.name)
    return ""


def run_action(request, name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Execute a registered action after re-validating everything it depends on.

    This is the single entry point for both the assist loop and the execute endpoint, which is
    what makes the countdown pure UX: the execute endpoint calls this and the resolver re-runs
    every permission and validation check from scratch.
    """
    action = get_action(name)
    if action is None:
        return _error("I don't know how to do that.")
    if not isinstance(params, dict):
        return _error("Those instructions didn't make sense.")
    unknown = sorted(key for key in params if not action.accepts(key))
    if unknown:
        # Strict: the model is untrusted input, so a parameter we never advertised is a refusal,
        # not something to quietly drop and act on anyway.
        logger.info("Rejected unknown params %s for action %s", unknown, action.name)
        return _error(f"I don't understand “{unknown[0]}” for that.")
    try:
        return action.resolver(request, params)
    except PermissionDenied:
        return _error("You don't have permission to do that.")
    except Exception:
        logger.exception("Palette action %s failed", action.name)
        return _error("Something went wrong doing that.")


def _runs_a_club(user) -> bool:
    """Whether this user administers any club, by any of the permissions that let them write.

    One query, and deliberately generous: a skill missing from the prompt is a skill the assistant
    appears not to have, which is the failure this whole file exists to stop. Being shown a skill
    you turn out not to be allowed to use costs a sentence; not being shown one you are allowed to
    use costs the feature.
    """
    return (
        ClubMember.objects.filter(user=user, is_deleted=False)
        .filter(
            Q(permission_admin=True)
            | Q(permission_add_edit=True)
            | Q(permission_view=True)
            | Q(permission_manage_bap=True)
            | Q(permission_manage_auctions=True)
        )
        .exists()
    )


def administers_anything(user) -> bool:
    """Whether this user runs any auction or club at all.

    The gate on the shortened repeat-write countdown (``palette_assist.TRUST_WINDOW_SECONDS``).
    Somebody working a drop-off table has approved the same card thirty times and is being slowed
    down by it; somebody selling one lot a month has not, and the five seconds is the only chance
    they get to notice a misheard name. This is the same question ``actions_for`` asks to decide
    which skills are worth describing, kept in one place so the two can't drift apart.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return _runs_a_club(user) or bool(command_palette._admin_auction_ids(user))


def actions_for(user=None) -> list[Action]:
    """The skills worth describing to this user. ``None`` means "all of them" (for the audit).

    The same idea as ``palette_routes._permitted_routes``, and for the same reason: every action's
    schema costs prompt tokens on every round, and a bidder who runs no auction and no club has no
    use for two thirds of them.
    """
    if user is None:
        return list(ACTIONS.values())
    if getattr(user, "is_superuser", False):
        return list(ACTIONS.values())
    runs_a_club = _runs_a_club(user)
    # Club staff run their club's auctions, so anyone with club permissions keeps the auction skills
    # even when they've never been named an admin on an auction directly.
    runs_an_auction = runs_a_club or bool(command_palette._admin_auction_ids(user))
    allowed = []
    for action in ACTIONS.values():
        if action.needs == NEEDS_AUCTION_ADMIN and not runs_an_auction:
            continue
        if action.needs == NEEDS_CLUB_ADMIN and not runs_a_club:
            continue
        allowed.append(action)
    return allowed


def registry_for_prompt(user=None) -> list[dict[str, Any]]:
    """The action list as handed to the model, generated from the registry itself.

    Generated rather than hand-written so a new action is described to the model the moment it
    is registered, and can never drift from what the server will actually accept.
    """
    return [action.schema() for action in actions_for(user)]


# --- the skill audit ---------------------------------------------------------
#
# ``palette_routes`` guarantees the assistant can *reach* every page. This table is the other half
# of that promise: that it can *do* everything the rest of the UI does, or that somebody has written
# down why it doesn't.
#
# The gap between the two is where this feature kept embarrassing itself. Every capability on this
# site is a view you can POST to, and the route audit is happy to write "JSON/HTMX endpoint, not a
# page a person can be sent to" against a URL that adds a club member -- true, and completely
# useless to the person asking the palette to add one. Sixty-odd capabilities sat behind that
# sentence. So a view that accepts a POST now has to name the skill that covers it, or say why it
# doesn't need one, and ``auctions/test_palette_skills.py`` fails the build if it does neither.
#
# The reasons below are the honest ones, not a shrug. Roughly:
#
#   * it's a page with a form on it, and go_to_page opens the form
#   * it's destructive, involves money, or acts on rows in bulk, so we take the user there instead
#   * it needs a file, a photo, or a row already in front of you
#   * nobody asks for it by name -- it's a scanner, a webhook, an autocomplete feed

#: Views a registered action covers. The value is the action's name, checked against the registry.
SKILLS: dict[str, str] = {
    "AuctionBulkPrinting": "print_labels",
    "AuctionCheckIn": "check_in",
    "AuctionTOSAdmin": "update_person",
    "AuctionUnsellLot": "undo_sale",
    "BapAwardAdminView": "award_points",
    "BulkAddLots": "add_lot",
    "BulkAddUsers": "add_person",
    "ClubMemberAdminView": "update_club_member",
    "ClubMemberCreateView": "add_club_member",
    "ClubMemberRenewView": "renew_member",
    "DynamicSetLotWinner": "set_lot_winner",
    "InvoicePaid": "set_invoice_status",
    "LotAdmin": "edit_lot",
    "LotCreateView": "add_lot",
    "LotUpdate": "edit_lot",
    "SaveLotAjax": "edit_lot",
    "WatchOrUnwatch": "watch_lot",
    "AddSingleAuctionTOSToClub": "add_club_member",
    "AddTosMemo": "update_person",
    "AuctionDoorPrizes": "draw_door_prize",
    "UserPreferencesUpdate": "update_preferences",
    "UpdateLotPushNotificationsView": "watch_lot",
}

# The reasons. Shared constants, because most of these are the same handful of decisions and a
# reason worth writing once is worth reusing.
_FORM_PAGE = (
    "A page with a form on it. go_to_page opens the form; the fields on it are more than one "
    "spoken sentence can carry, and the user gets to see what they're setting."
)
_SETUP = (
    "One-off setup for an outside service (keys, OAuth, channel and list pickers). Done once, from "
    "the settings page, by somebody looking at the other service's screen at the same time."
)
_MONEY = "Money changes hands here. Navigate-only by design, like every other money path in the palette."
_DESTRUCTIVE = (
    "Destructive and not undoable. The palette takes the user to the page, where the thing being "
    "destroyed is named on screen before they confirm it."
)
_BULK = (
    "Acts on every row matching the current filter. Which rows those are is the whole question, and "
    "it is answered by looking at the page, not by a summary sentence."
)
_NEEDS_A_FILE = "Needs a file — a CSV, a spreadsheet, a photo — that a typed or spoken command can't hand over."
_NEEDS_THE_ROW = (
    "Acts on one row of a table you're already looking at, and identifying it out loud is harder than clicking it."
)
_MACHINE = "Called by the browser, a scanner or another program. Nobody asks for this by name."
_WEBHOOK = "Webhook or callback. Reached by another server, never by a person."
_TOKEN = "Reached from an emailed link carrying a token nobody can be asked to say out loud."
_EXTERNAL_API = "REST API for a club's own website or software. Authenticated by an API key, not by a person."
_PALETTE = "The palette's own endpoint. It is the thing running the skills."

#: Views with no skill, and why. Every entry is a decision somebody made on purpose.
NOT_A_SKILL: dict[str, str] = {
    # Speaker directory
    "SpeakerCreateView": _FORM_PAGE,
    "SpeakerUpdateView": _FORM_PAGE,
    "SpeakerDeleteView": _DESTRUCTIVE,
    "SpeakerTagView": (
        "Tagging is a toggle on a speaker's page, and the tag list is on screen while you pick. "
        "Saying which of fourteen tags you meant is slower than clicking it."
    ),
    "SpeakerCommentView": (
        "The comment is a paragraph about how a talk went. Dictating one into a command box is "
        "worse than typing it into the box on the speaker's page, which the palette can reach."
    ),
    "SpeakerCommentDeleteView": _NEEDS_THE_ROW,
    # Pages with forms on them
    "AccountDeleteView": _DESTRUCTIVE,
    "AdminUserFlow": _FORM_PAGE,
    "AuctionCreateView": (
        "Creating an auction is twenty decisions about dates, fees and rules. The create page walks "
        "through them; a one-line command would guess at most of them and get the fees wrong."
    ),
    "AuctionCustomFieldsUpdate": _FORM_PAGE,
    "AuctionLabelConfig": _FORM_PAGE,
    "AuctionUpdate": _FORM_PAGE,
    "AuctionVolunteers": _FORM_PAGE,
    "SpeciesCreateView": (
        "Adding a species is a taxonomic decision, and the form is where the decisions are visible: "
        "whether this is a species or a strain of one, which species it is a strain of, which of "
        "its names people actually type. Half-filling that from one spoken line is how a wrong "
        "name ends up on a printed label and in breeder points, which the whole species feature is "
        "written to avoid. The palette navigates to the page instead."
    ),
    "SpeciesSearchCacheForgetView": (
        "One button on the species gaps page, and the decision is the row next to it: this "
        "remembered answer is wrong, throw it away. Naming the row out loud means saying a "
        "normalised lot name exactly, which nobody can do without reading it off the page they "
        "are already on. The palette navigates there instead."
    ),
    "SpeciesApproveView": (
        "Approving a species for the whole site is a judgement about somebody else's taxonomy, and "
        "the evidence is the row: what the auction admin typed, which category it landed in, how "
        "many lots are waiting on it. That is a page to read, not a sentence to say -- and getting "
        "it wrong puts a wrong name in every club's picker at once."
    ),
    "ClubBapSettingsView": _FORM_PAGE,
    "ClubDetailView": _FORM_PAGE,
    "ClubEditView": _FORM_PAGE,
    "ClubEmailSettingsView": _FORM_PAGE,
    "ClubEventCreateView": (
        "A date, a start time, an end time and a place. Getting any one of them wrong makes a "
        "calendar entry that is worse than no entry, so the palette opens the form instead."
    ),
    "ClubEventUpdateView": _NEEDS_THE_ROW,
    "ClubMembershipSettingsView": _FORM_PAGE,
    "ClubMemberRenewPageView": (
        "Sets an expiration date by hand, overriding the club's renewal rules. renew_member is the "
        "skill for an ordinary renewal; overriding the date deliberately means seeing the page."
    ),
    "InvoiceView": _MONEY,
    "LotQRView": _FORM_PAGE,
    "LotQueueView": _FORM_PAGE,
    "MyAccount": _FORM_PAGE,
    "MyLastAuctionLots": _FORM_PAGE,
    "PickupLocationsCreate": _FORM_PAGE,
    "PickupLocationsUpdate": _FORM_PAGE,
    "UserLabelPrefsView": _FORM_PAGE,
    "UserLocationUpdate": _FORM_PAGE,
    "UsernameUpdate": _FORM_PAGE,
    "VolunteerJobAccept": (
        "The page a volunteer notification opens. Signing up means reading what the job is and when "
        "it starts, which is what the page is for."
    ),
    "AuctionInfo": (
        "POSTing here joins the auction, which means agreeing to that auction's rules. Agreeing to "
        "something on somebody's behalf is not a thing this assistant does — it sends them to the "
        "page, and the context block tells it when they haven't joined yet."
    ),
    # Destructive
    "AuctionDelete": _DESTRUCTIVE,
    "AuctionLotMapClear": _DESTRUCTIVE,
    "AuctionNoShowAction": _DESTRUCTIVE,
    "AuctionTOSDelete": _DESTRUCTIVE,
    "BapAwardDeleteView": _DESTRUCTIVE,
    "BidDelete": _DESTRUCTIVE,
    "ClubBapCategoryOverrideDeleteView": _DESTRUCTIVE,
    "ClubBapGenusOverrideDeleteView": _DESTRUCTIVE,
    "ClubMemberDeleteView": _DESTRUCTIVE,
    "ClubMemberMergeView": _DESTRUCTIVE,
    "ClubMemberPermanentDeleteView": _DESTRUCTIVE,
    "CreateUserBan": _DESTRUCTIVE,
    "UserUnban": _NEEDS_THE_ROW,
    "ImageDelete": _DESTRUCTIVE,
    "LotDelete": _DESTRUCTIVE,
    "LotDeactivate": _DESTRUCTIVE,
    "ClubAPIKeyRevokeView": _DESTRUCTIVE,
    "ClubAPIKeyFieldMapDeleteView": _DESTRUCTIVE,
    "PayPalSellerDeleteView": _DESTRUCTIVE,
    "SquareSellerDeleteView": _DESTRUCTIVE,
    # Bulk
    "AddAuctionUsersToClub": _BULK,
    "AuctionDisableBidding": _BULK,
    "BulkSetLotsWon": _BULK,
    "EnableBiddingForAllUsers": _BULK,
    "MarkInvoicesPaid": _BULK,
    "MarkInvoicesReady": _BULK,
    # Money
    "ClubLinkPaymentAccountView": _MONEY,
    "ClubMoneyBalanceView": _MONEY,
    "ClubMoneyCreateView": _MONEY,
    "ClubPayPalCredentialsView": _MONEY,
    "CreatePayPalOrderView": _MONEY,
    "CreateSquarePaymentLinkView": _MONEY,
    "LotRefundDialog": _MONEY,
    "PlaceBid": (
        "Bidding is money, and a misheard number is a bid somebody owes. The palette takes people "
        "to the lot, where the current price and the bid they're about to place are on screen."
    ),
    # Files and photos
    "BapAwardCSVImportView": _NEEDS_A_FILE,
    "ClubMemberCSVImportView": _NEEDS_A_FILE,
    "ImageCreateView": _NEEDS_A_FILE,
    "ImageUpdateView": _NEEDS_A_FILE,
    "ImagesPrimary": _NEEDS_A_FILE,
    "ImagesRotate": _NEEDS_A_FILE,
    "ImportFromGoogleDrive": _NEEDS_A_FILE,
    "ImportLotsFromCSV": _NEEDS_A_FILE,
    "QuickBulkAddImages": _NEEDS_A_FILE,
    # One row of a table you're already looking at
    "AuctionChatDeleteUndelete": _NEEDS_THE_ROW,
    "ClubBapCategoryOverrideSaveView": _NEEDS_THE_ROW,
    "ClubBapGenusOverrideSaveView": _NEEDS_THE_ROW,
    "ClubBapLotCategoryView": _NEEDS_THE_ROW,
    "ClubMemberReactivateView": _NEEDS_THE_ROW,
    "ClubMembershipNumberView": _NEEDS_THE_ROW,
    "ClubMemberResendCardView": _NEEDS_THE_ROW,
    "InvoiceRenewalNeededToggleView": _NEEDS_THE_ROW,
    "LotBapPointsView": _NEEDS_THE_ROW,
    "Feedback": _NEEDS_THE_ROW,
    "IgnoreAuction": _NEEDS_THE_ROW,
    "ClubMemberPermissionsView": (
        "Grants club administration rights. A permission change is exactly the thing that should "
        "take a deliberate click on a page naming the person, not a sentence that might be misheard."
    ),
    # Setup for outside services
    "BrevoConnectView": _SETUP,
    "BrevoDisconnectView": _SETUP,
    "BrevoListSelectView": _SETUP,
    "BrevoSyncNowView": _SETUP,
    "ClubAPIKeyCreateView": _SETUP,
    "ClubAPIKeyFieldMapCreateView": _SETUP,
    "ClubDiscordConfigView": _SETUP,
    "ClubDiscordEditRoleView": _SETUP,
    "ClubDiscordFetchRolesView": _SETUP,
    "ClubDiscordSendJoinMessageView": _SETUP,
    "ClubDiscordSetDefaultRoleView": _SETUP,
    "ClubGoogleCalendarConfigView": _SETUP,
    "ClubMemberDiscordAdminView": _SETUP,
    "GoogleCalendarDisconnectView": _SETUP,
    "GoogleCalendarSyncNowView": _SETUP,
    "MailchimpAudienceSelectView": _SETUP,
    "MailchimpDisconnectView": _SETUP,
    "MailchimpSyncNowView": _SETUP,
    # Machines
    "AuctionBarcodeScan": _MACHINE,
    "AuctionDropdownOptionsAPI": _MACHINE,
    "AuctionFinder": _MACHINE,
    "AuctionNotifications": _MACHINE,
    "CategoryFinder": _MACHINE,
    "ClickAd": _MACHINE,
    "FindImageIcon": _MACHINE,
    "LotChatSubscribe": _MACHINE,
    "LotNotifications": _MACHINE,
    "LotPushTestNotificationView": _MACHINE,
    "NoLotAuctions": _MACHINE,
    "PageViewCreate": _MACHINE,
    "SetCoordinates": _MACHINE,
    "SpeciesSuggestions": _MACHINE,
    "VoiceCommandLogView": _MACHINE,
    # Autocomplete and live validation feeds
    "AuctionAutocomplete": _MACHINE,
    "AuctionTOSAutocomplete": _MACHINE,
    "AuctionTOSValidation": _MACHINE,
    "CategoryAutocomplete": _MACHINE,
    "SpeciesAutocomplete": _MACHINE,
    "ClubMemberAutocomplete": _MACHINE,
    "ClubMemberMergeAutocomplete": _MACHINE,
    "ClubMemberValidation": _MACHINE,
    "GetClubs": _MACHINE,
    "LotAutocomplete": _MACHINE,
    # Webhooks, callbacks and tokens
    "BrevoWebhookView": _WEBHOOK,
    "DiscordInteractionsView": _WEBHOOK,
    "MailchimpWebhookView": _WEBHOOK,
    "PayPalSubscriptionWebhookView": _WEBHOOK,
    "PayPalWebhookView": _WEBHOOK,
    "SquarePaymentSuccessView": _WEBHOOK,
    "SquareWebhookView": _WEBHOOK,
    "ClubMemberSelfServiceView": _TOKEN,
    "InvoiceNoLoginView": _TOKEN,
    # Other programs
    "ClubMemberBapAwardAPIView": _EXTERNAL_API,
    "ClubMemberListCreateAPIView": _EXTERNAL_API,
    "ClubMemberRenewAPIView": _EXTERNAL_API,
    "PickupLocationsDelete": _DESTRUCTIVE,
    # Us
    "CommandPaletteAssistView": _PALETTE,
    "CommandPaletteCancelView": _PALETTE,
    "CommandPaletteExecuteView": _PALETTE,
    "CommandPaletteLogView": _PALETTE,
    "CommandPaletteReportView": _PALETTE,
}


def postable_views() -> dict[str, list[str]]:
    """Every view in ``auctions.views`` that accepts a POST, and the URL names that reach it.

    Keyed by class name rather than by URL name on purpose: several capabilities have no URL name at
    all (``api/watchitem/``, ``api/payinvoice/`` and a dozen others are registered without one), so a
    table keyed by URL name would have quietly claimed full coverage while missing them.

    Only ``auctions.views``: the mobile API is the app's own screens, and PassKit and Apple's
    server-to-server notifications are webhooks. Both are already written down in
    ``palette_routes.EXCLUDED``.
    """
    from django.urls import get_resolver

    def walk(resolver):
        for pattern in resolver.url_patterns:
            if hasattr(pattern, "url_patterns"):
                yield from walk(pattern)
            else:
                yield pattern

    found: dict[str, list[str]] = {}
    for pattern in walk(get_resolver()):
        callback = pattern.callback
        view = getattr(callback, "view_class", None) or getattr(callback, "cls", None)
        if view is None or view.__module__ != "auctions.views" or not hasattr(view, "post"):
            continue
        found.setdefault(view.__name__, [])
        if pattern.name:
            found[view.__name__].append(pattern.name)
    return found


def audit_skills() -> dict[str, list[str]]:
    """Compare the site's write surface against the action registry. Used by the skill audit test.

    ``uncovered`` is the one that matters: a view there is something the UI can do, the assistant
    can't, and nobody has said why. ``stale`` is a table entry for a view that no longer exists, and
    ``unregistered`` a skill name in :data:`SKILLS` that isn't a registered action.
    """
    live = postable_views()
    return {
        "covered": sorted(name for name in live if name in SKILLS),
        "excused": sorted(name for name in live if name in NOT_A_SKILL),
        "uncovered": sorted(name for name in live if name not in SKILLS and name not in NOT_A_SKILL),
        "stale": sorted((set(SKILLS) | set(NOT_A_SKILL)) - set(live)),
        "unregistered": sorted({skill for skill in SKILLS.values() if skill not in ACTIONS}),
    }
