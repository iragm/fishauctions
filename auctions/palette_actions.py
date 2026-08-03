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
  ``check_in``        -> ``services.check_in_auctiontos`` (extracted from ``views.AuctionCheckIn``)
  ``undo_sale``       -> :class:`auctions.views.AuctionUnsellLot`'s own ``find_lot`` / ``unsell``
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

from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.urls import reverse
from django.utils.html import strip_tags
from django.utils.http import urlencode
from django.utils.text import Truncator

from . import command_palette, palette_routes
from .models import AuctionTOS, ClubMember, Lot
from .services import (
    check_in_auctiontos,
    clone_lot_values,
    copy_lot_images,
    lot_add_block,
    recalculate_seller_invoice,
    save_new_lot,
    user_can_clone_lot,
)

logger = logging.getLogger(__name__)

DANGER_SAFE = "safe"
DANGER_CONFIRM = "confirm"
DANGER_NAVIGATE = "navigate"

# How many candidates to name when a lookup is ambiguous ("which bob?").
AMBIGUOUS_LIMIT = 6


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
        auction = command_palette._last_auction(user)
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


def _lot_label_followup(lot) -> dict[str, str]:
    """A 'print this lot's label' followup, so "print that label" has somewhere obvious to go."""
    return {"label": f"Print label for {lot.lot_name}", "url": reverse("single_lot_label", kwargs={"pk": lot.pk})}


def plain_text(value: str, limit: int = 1500) -> str:
    """Rich text (a summernote field) as something worth putting in a prompt.

    Auction rules and club descriptions are stored as HTML. Handed to the model raw they are mostly
    markup, which costs tokens and reads badly; this strips the tags, unescapes the entities,
    collapses the whitespace and truncates. Long rules get cut off rather than dropped -- the
    opening paragraphs are where the rules people ask about actually live.
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
        return None
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


def add_lot(request, params: dict[str, Any]) -> dict[str, Any]:
    """Add one lot, for the user themselves or (admins only) for one of their bidders.

    Validation and permissions are entirely ``QuickAddLot`` + ``services.lot_add_block`` -- the
    same form and the same gate the bulk-add page uses, with the same kwargs.
    """
    from .forms import quick_add_lot_form_class

    user = request.user
    auction, error = resolve_auction(user, _str(params, "auction"), _page(request))
    if error:
        return _error(error)

    is_admin = _is_auction_admin(user, auction)
    bidder = _str(params, "bidder") or _str(params, "seller")
    if bidder:
        if not is_admin:
            return _error(f"Only admins of {auction.title} can add lots for someone else.")
        tos, problem = resolve_person(user, auction, bidder)
        if problem:
            return problem
        own_tos = _own_tos(user, auction)
        for_self = bool(own_tos and own_tos.pk == tos.pk)
    else:
        tos = _own_tos(user, auction)
        for_self = True

    block = lot_add_block(auction, tos, is_admin, bulk=False)
    if block:
        return _error(block[1])

    lot_name = _str(params, "name") or _str(params, "lot_name")
    if not lot_name:
        return _need("What should the lot be called?")

    # A previous listing of the same thing is the best source for everything the user didn't say:
    # its photos, its description, and — when they named it exactly — its capitalisation.
    previous, name_was_exact = find_lot_to_copy(tos.user, lot_name, exclude_auction=auction)
    if previous:
        data = clone_lot_values(previous)
        data["species_category"] = previous.species_category_id
        if not name_was_exact:
            # A partial match reuses the old lot's contents but not its name: "add shrimp" must not
            # come out as a lot called "Blue Dream Shrimp — F1 juveniles".
            data["lot_name"] = tidy_lot_name(lot_name)
    else:
        data = {"lot_name": tidy_lot_name(lot_name), "species_category": _category_pk()}

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
    recalculate_seller_invoice(auction, tos)
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
        followups=[
            {"label": "View this lot", "url": lot.get_absolute_url()},
            _lot_label_followup(lot),
            *([{"label": "Edit this lot", "url": reverse("edit_lot", kwargs={"pk": lot.pk})}] if previous else []),
        ],
    )


def _category_pk():
    from .models import Category

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
    return _ok(
        str(result.get("success_message") or f"Sold lot {lot.lot_number_display}."),
        lot_id=lot.pk,
        followups=[_lot_label_followup(lot)],
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
    check_in_auctiontos(tos, acting_user=user, bidder_number=_str(params, "bidder_number"))
    who = tos.name or f"bidder {tos.bidder_number}"
    if already:
        return _ok(f"{who} was already checked in to {auction.title}.")
    return _ok(f"Checked {who} in to {auction.title} as bidder {tos.bidder_number}.")


# --- add_person --------------------------------------------------------------


def add_person(request, params: dict[str, Any]) -> dict[str, Any]:
    """Add a person to an auction (admins / club staff only).

    The counterpart to ``add_lot``, and the reason "add mike smith" doesn't become a lot called
    "Mike Smith": without this the model's only "add" verb was ``add_lot``, so a person's name had
    nowhere else to go.

    Validation is entirely :class:`auctions.forms.QuickAddTOS` -- the bulk-add page's own form,
    built through ``quick_add_tos_form_class`` with the same fields -- so the duplicate-bidder-number
    and duplicate-email rules are the page's rules, not a second copy of them.
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
    tos = form.save(commit=False)
    tos.auction = auction
    tos.manually_added = True
    tos.save()
    auction.create_history(
        applies_to="USERS",
        action=f"Added {tos.name} (command palette)",
        user=user,
    )
    followups = [
        {"label": f"Everyone in {auction.title}", "url": reverse("auction_tos_list", kwargs={"slug": auction.slug})}
    ]
    if tos.bidder_number:
        # Adding somebody at the door is almost always followed by taking their lots.
        followups.insert(
            0,
            {
                "label": f"Add lots for {tos.name}",
                "url": reverse("bulk_add_lots", kwargs={"slug": auction.slug, "bidder_number": tos.bidder_number}),
            },
        )
    return _ok(f"Added {tos.name} to {auction.title} as bidder {tos.bidder_number}.", followups=followups)


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


def user_context(user, page: dict[str, Any] | None = None) -> dict[str, Any]:
    """The compact context block handed to the model with every assist request.

    Deliberately small: a username, the palette club, the last auction and the user's role in it,
    their memberships, and the page they're looking at right now. Enough for "renew my membership"
    to know which club is meant, and for "add a lot" to mean the auction on screen.
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
        data["looking_at_right_now"] = page
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
                "means": str(getattr(field, "help_text", "") or ""),
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
_AUCTION_SETTINGS = (
    "minimum_bid",
    "buy_now",
    "max_lots_per_user",
    "allow_additional_lots_as_donation",
    "lot_entry_fee",
    "winning_bid_percent_to_club",
    "registration_fee",
    "unsold_lot_fee",
    "only_approved_sellers",
    "only_approved_bidders",
    "allow_bulk_adding_lots",
    "use_check_in_mode",
)


def _resolve_described_auction(user, hint: str, page: dict[str, Any] | None):
    """An auction to describe, scoped to what this user can see rather than what they've joined.

    Wider than ``resolve_auction`` on purpose: asking "what are the rules for the spring auction"
    is the question you ask *before* joining, and refusing to answer it until you have joined is
    backwards. Still scoped -- ``_visible_auctions`` excludes anything not published to this user.
    """
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
        current = visible.filter(slug=page_slug).first()
        if current:
            return current, None
    auction = command_palette._last_auction(user)
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
        "starts": auction.date_start,
        "ends": auction.date_end,
        "lot_submission_opens": auction.lot_submission_start_date,
        "lot_submission_closes": auction.lot_submission_end_date,
        "lot_submission_open_now": bool(auction.can_submit_lots),
        "over": bool(auction.pretty_much_over),
        "uses_check_in": bool(auction.use_check_in_mode),
        "you_have_joined": bool(tos),
        "your_bidder_number": tos.bidder_number if tos else None,
        "you_are_an_admin": is_admin,
        "pickup_locations": [location.name for location in auction.location_qs[:10]],
        # The rules are a summernote field; the model is given the words, not the markup.
        "rules": plain_text(auction.summernote_description),
        "settings": _settings_block(auction, _AUCTION_SETTINGS),
    }
    if is_admin or auction.make_stats_public:
        lots = Lot.objects.filter(auction=auction, is_deleted=False)
        data["participants"] = AuctionTOS.objects.filter(auction=auction).count()
        data["lots"] = lots.count()
        data["lots_sold"] = lots.filter(Q(winner__isnull=False) | Q(auctiontos_winner__isnull=False)).count()
    if is_admin:
        data["_admin"] = {
            "checked_in": AuctionTOS.objects.filter(auction=auction, checked_in__isnull=False).count(),
            "cached_stats": auction.cached_stats,
            "stats_last_updated": auction.last_stats_update,
        }
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
        "description": plain_text(club.description),
        "contact_email": club.contact_email,
        "membership_enabled": club.enable_membership,
        "annual_membership_fee": club.membership_annual_fee,
        "your_membership_expires": membership.membership_expiration_date if membership else None,
        "you_are_a_member": bool(membership),
        "you_can_manage_members": can_manage,
        "points_program": _settings_block(club, _CLUB_BAP_SETTINGS),
        "category_point_overrides": [
            {"category": str(override.category), "points": override.points}
            for override in club.bap_category_overrides.select_related("category")[:25]
        ],
    }
    if can_manage:
        members = ClubMember.objects.filter(club=club, is_deleted=False)
        data["_admin"] = {
            "members": members.count(),
            "members_with_an_account": members.filter(user__isnull=False).count(),
            "points_last_recalculated": club.last_bap_recalculation,
        }
    return {"found": True, "club": data}


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
        "description": plain_text(lot.summernote_description, limit=600),
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
    if is_admin:
        seller = lot.auctiontos_seller
        data["_admin"] = {
            "seller": seller.name if seller else None,
            "seller_bidder_number": seller.bidder_number if seller else None,
            "winner_bidder_number": lot.auctiontos_winner.bidder_number if lot.auctiontos_winner else None,
        }
    return {"found": True, "lot": data}


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
    if not (lot.winner or lot.auctiontos_winner):
        return _error(f"Lot {lot.lot_number_display} hasn't been sold, so there's nothing to undo.")
    result = view.unsell(lot)
    return _ok(str(result.get("success_message") or f"Un-sold lot {lot.lot_number_display}."), lot_id=lot.pk)


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
        },
        danger=DANGER_CONFIRM,
        resolver=add_lot,
        aliases={"seller", "lot_name", "price", "i_bred_this_fish", "custom_field_1", "custom_dropdown"},
        confirm_template="Add a lot",
        examples=["add a lot of blue shrimp", "add 3 guppies for bidder 14"],
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
            "lot_id": "integer, optional. A specific lot to print the label for.",
            "auction": "string, optional. Defaults to the user's last auction.",
        },
        danger=DANGER_NAVIGATE,
        resolver=print_labels,
        examples=["print my labels", "print that label"],
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
    )
)


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


def registry_for_prompt() -> list[dict[str, Any]]:
    """The action list as handed to the model, generated from the registry itself.

    Generated rather than hand-written so a new action is described to the model the moment it
    is registered, and can never drift from what the server will actually accept.
    """
    return [action.schema() for action in ACTIONS.values()]
