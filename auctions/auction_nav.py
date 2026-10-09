"""The auction admin menu: the ribbon's tabs, and the page its **More** tab opens.

One list so the two can't disagree. `auction_ribbon.html` draws the `tabs` group as tabs;
`/auctions/<slug>/pages/` (More) draws every group, tabs included, with a line about each. More was a
dropdown until it held twenty-odd unsorted links.

`Row.gate` takes the auction, never the user. The ribbon also renders for a club member who may add
and edit people without running the auction (`can_add_edit_people`); More shows them only the
`people` rows, the pages that let them in. `_in_person` rows are hidden from online auctions (the
auction-type exception in style_reference.md).
"""

from collections.abc import Callable
from dataclasses import dataclass

from django.urls import reverse


@dataclass(frozen=True)
class Row:
    #: A string, or `f(auction) -> str` for the two that change with the auction.
    label: str | Callable
    icon: str
    description: str
    #: `f(auction) -> str`
    url: Callable
    #: Optional `f(auction) -> bool`
    gate: Callable | None = None
    #: The `active_tab` value that highlights this row's tab; only rows in the `tabs` group have one.
    tab: str = ""
    #: Open to whoever manages the auction's people, not only its admins.
    people: bool = False


@dataclass(frozen=True)
class Group:
    title: str
    rows: tuple[Row, ...]
    #: Also drawn as the ribbon's tabs.
    tabs: bool = False


def _named(url_name):
    return lambda auction: reverse(url_name, kwargs={"slug": auction.slug})


def _in_person(auction):
    return not auction.is_online


def _check_in(auction):
    return auction.use_check_in_mode


def _club(auction):
    return bool(auction.club_id)


def _location_label(auction):
    return "Locations" if len(auction.locations) > 1 else "Location"


def _check_in_label(auction):
    """Without check-in mode, scanning a card adds the member to the auction."""
    return "Check in" if auction.use_check_in_mode else "Scan club cards"


GROUPS = (
    Group(
        "",
        (
            Row("Main", "bi-house-fill", "The auction's front page", _named("auction_main"), tab="main", people=True),
            Row(
                "Users",
                "bi-people-fill",
                "Everyone in it, and their invoices",
                _named("auction_tos_list"),
                tab="users",
                people=True,
            ),
            Row("Lots", "bi-calendar", "Every lot", _named("auction_lot_list"), tab="lots"),
        ),
        tabs=True,
    ),
    Group(
        "Set up",
        (
            Row("Rules", "bi-gear-fill", "Dates, fees and every setting", _named("edit_auction")),
            Row(
                "Custom fields",
                "bi-ui-checks-grid",
                "What sellers fill in when they add a lot",
                _named("edit_auction_custom_fields"),
            ),
            Row(_location_label, "bi-geo-alt-fill", "Where it is, or where to pick up", lambda a: a.location_link),
        ),
    ),
    Group(
        "Print",
        (
            Row("Print labels", "bi-tags", "Lot labels for everyone", _named("auction_printing"), gate=_in_person),
            Row(
                "Print paddles",
                "bi-123",
                "A bidder number sheet for each person",
                _named("auction_paddles"),
                gate=_in_person,
                people=True,
            ),
            Row("Printable lot list", "bi-printer", "Every lot on paper", _named("auction_printable_lot_list")),
        ),
    ),
    Group(
        "On the day",
        (
            Row(
                _check_in_label,
                "bi-upc-scan",
                "Scan membership cards",
                _named("auction_quick_check_in"),
                gate=_club,
                people=True,
            ),
            Row(
                "Self-checkin",
                "bi-person-check",
                "A kiosk where members scan their own card",
                _named("auction_self_check_in"),
                gate=_check_in,
                people=True,
            ),
            Row(
                "Recruit volunteers",
                "bi-people",
                "Ask app users to help with a job",
                _named("auction_volunteers"),
                gate=_in_person,
            ),
            Row(
                "Lot queue",
                "bi-list-ol",
                "The running order for the auctioneer",
                _named("auction_lot_queue"),
                gate=_in_person,
            ),
            Row(
                "Set lot winners",
                "bi-calendar-check",
                "Record who won each lot",
                lambda a: a.set_lot_winners_link,
                gate=_in_person,
            ),
            Row(
                "Door prizes",
                "bi-gift",
                "Draw a random checked-in name",
                _named("auction_door_prizes"),
                gate=_check_in,
                people=True,
            ),
            Row(
                "Checkout",
                "bi-bag-heart",
                "Take payment and mark invoices paid",
                _named("auction_quick_checkout"),
                gate=_in_person,
            ),
        ),
    ),
    Group(
        "Watch",
        (
            Row("Stats", "bi-graph-up", "Views, bids and new lots", _named("auction_stats")),
            Row("Chat messages", "bi-chat-fill", "Every question asked on a lot", _named("auction_chat")),
            Row("Lot map", "bi-geo-alt", "Where unsold lots are on the tables", _named("auction_lot_map")),
            Row("Feedback", "bi-emoji-smile", "What people said afterwards", _named("auction_survey_results")),
            Row("Admin history", "bi-clock-history", "Who changed what", _named("auction_history")),
        ),
    ),
    Group(
        "",
        (
            Row("Help", "bi-question-circle-fill", "The guide for this auction", _named("auction_help")),
            Row(
                "Copy to new auction",
                "bi-plus-circle",
                "A new auction with these rules",
                lambda a: reverse("create_auction") + f"?copy={a.slug}",
            ),
            Row("Delete auction", "bi-x-circle", "Remove it for good", _named("auction_delete")),
        ),
    ),
)


def groups_for(auction, active_tab=None, people_only=False):
    """The menu as the templates draw it: groups of `{label, icon, description, url, active}` rows.

    `people_only` keeps the `people` rows. A group whose rows are all gated away is dropped.
    """
    drawn = []
    for group in GROUPS:
        rows = []
        for row in group.rows:
            if (row.gate and not row.gate(auction)) or (people_only and not row.people):
                continue
            rows.append(
                {
                    "label": row.label(auction) if callable(row.label) else row.label,
                    "icon": row.icon,
                    "description": row.description,
                    "url": row.url(auction),
                    "active": bool(row.tab) and row.tab == active_tab,
                }
            )
        if rows:
            drawn.append({"title": group.title, "tabs": group.tabs, "rows": rows})
    return drawn
