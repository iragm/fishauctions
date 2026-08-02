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
  ``find_person`` /   -> the palette's own scoped search helpers in ``command_palette``
  ``my_context``
  ``open_page``       -> ``command_palette.resolve_page`` and ``command_palette.search``
  ``print_labels`` /  -> navigate only; they resolve a URL and never perform the action
  ``renew_membership``

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

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.urls import reverse

from . import command_palette
from .models import AuctionTOS, ClubMember, Lot
from .services import (
    check_in_auctiontos,
    lot_add_block,
    recalculate_seller_invoice,
    save_new_lot,
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


def resolve_auction(user, hint: str = ""):
    """Find the auction the user means, or fall back to their most recent one.

    Scoped to auctions the user has actually joined or created (``_joined_auctions``), so a hint
    can never reach an auction they have no relationship with. Returns ``(auction, error_or_None)``.
    """
    joined = command_palette._joined_auctions(user)
    if not hint:
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


# --- add_lot -----------------------------------------------------------------


def add_lot(request, params: dict[str, Any]) -> dict[str, Any]:
    """Add one lot, for the user themselves or (admins only) for one of their bidders.

    Validation and permissions are entirely ``QuickAddLot`` + ``services.lot_add_block`` -- the
    same form and the same gate the bulk-add page uses, with the same kwargs.
    """
    from .forms import quick_add_lot_form_class

    user = request.user
    auction, error = resolve_auction(user, _str(params, "auction"))
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

    reserve = _decimal(params, "reserve_price")
    if reserve is None:
        reserve = _decimal(params, "price")
    data = {
        "lot_name": lot_name[:40],
        "quantity": _int(params, "quantity", 1),
        # The page submits this from a hidden input pre-filled with the auction minimum, so a
        # request that doesn't mention a price behaves the same way here.
        "reserve_price": reserve if reserve is not None else auction.minimum_bid,
        "buy_now_price": _decimal(params, "buy_now_price"),
        "donation": bool(params.get("donation")),
        "i_bred_this_fish": bool(params.get("i_bred_this_fish")),
        "custom_field_1": _str(params, "custom_field_1"),
        "custom_dropdown": _str(params, "custom_dropdown"),
        "species_category": _category_pk(),
    }
    form = quick_add_lot_form_class()(data, auction=auction, tos=tos, is_admin=is_admin)
    if not form.is_valid():
        return _form_problem(form)

    lot = form.save(commit=False)
    save_new_lot(lot, auction=auction, tos=tos, added_by=user)
    recalculate_seller_invoice(auction, tos)
    auction.create_history(
        applies_to="LOTS",
        action=f"Added lot {lot.lot_number_display} {lot.lot_name} (command palette)",
        user=user,
    )
    who = "you" if for_self else (tos.name or f"bidder {tos.bidder_number}")
    return _ok(
        f"Added “{lot.lot_name}” to {auction.title} for {who}.",
        lot_id=lot.pk,
        lot_name=lot.lot_name,
        followups=[
            _lot_label_followup(lot),
            {"label": "View this lot", "url": lot.get_absolute_url()},
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
    auction, error = resolve_auction(user, _str(params, "auction"))
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
    auction, error = resolve_auction(user, _str(params, "auction"))
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
    return user_context(request.user)


def user_context(user) -> dict[str, Any]:
    """The compact context block handed to the model with every assist request.

    Deliberately small: a username, the palette club, the last auction and the user's role in it,
    and their memberships. Enough for "renew my membership" to know which club is meant.
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
    lot_id = _int(params, "lot_id")
    if lot_id:
        lot = Lot.objects.filter(pk=lot_id, is_deleted=False).first()
        if not lot:
            return _error("I couldn't find that lot any more.")
        return _ok(
            f"Opening the label for {lot.lot_name}.",
            url=reverse("single_lot_label", kwargs={"pk": lot.pk}),
        )
    auction, error = resolve_auction(user, _str(params, "auction"))
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


def open_page(request, params: dict[str, Any]) -> dict[str, Any]:
    """Resolve a "take me to X" request against the palette's own Go-To matchers.

    Runs ``command_palette.search`` and returns the first page-ish destination, so the model can
    never invent a URL -- it can only pick from what the palette itself would have offered.
    """
    query = _str(params, "page") or _str(params, "query")
    if not query:
        return _error("Where would you like to go?")
    groups = command_palette.search(request, query)
    preferred = [g for g in groups if g["label"] == "Go to"] or groups
    for group in preferred:
        for item in group["items"]:
            if item.get("url"):
                return _ok(f"Opening {item['title']}.", url=item["url"], title=item["title"])
    return _error(f"I couldn't find a page for “{query}”.")


# --- registry ----------------------------------------------------------------

register(
    Action(
        name="add_lot",
        description=(
            "Add one lot to an auction. Defaults to the user's most recent auction and to the "
            "user themselves as the seller. Only auction admins may pass 'bidder' to add a lot "
            "for someone else."
        ),
        params={
            "name": "string, required. What the lot is, e.g. 'blue shrimp'.",
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
        name="open_page",
        description=(
            "Navigate to a page on the site. Use for anything that is just 'take me to X' — "
            "invoices, settings, a club page, an auction page, adding users, and so on."
        ),
        params={"page": "string, required. What the user wants to reach, in their words."},
        danger=DANGER_NAVIGATE,
        resolver=open_page,
        aliases={"query"},
        examples=["take me to my invoice", "auction rules"],
    )
)


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
