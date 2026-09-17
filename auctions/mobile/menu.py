"""The app's navigation drawer, built here and served in /api/mobile/config/.

The drawer used to be compiled into the app as a copy of the web navbar's account dropdown, so
every new link needed an app-store release, and the superuser **Admin** menu and **About site**
link could never be carried at all -- who may see them is a server question.

`menu_for(user)` returns the whole drawer for one user, gated exactly as `base.html` gates the
navbar, and `MobileConfigView` serves it.

Deliberately not shared with `base.html`: the drawer has no Clubs dropdown, no sign-in pair, and its
own ordering. `auctions/test_mobile_menu.py` keeps them from drifting -- it pulls the real navbar's
account links out of the HTML and fails if one is missing here. Web-only links go in its allowlist.

Four drawer rows are the app's own, because none is a URL: **Sign out** (it clears the JWT pair, the
cookie jar, the cached profile, the offline files and the Square authorization), **Offline mode**
and **Tap to Pay** (native screens), and **Clubs** (from `clubs/mine/`).

Shape::

    {"version": 1, "sections": [{"id": "main", "title": "…", "icon": "bi-…",
                                 "collapsed": true, "items": [{"title": …, "path": …, "icon": …}]}]}

`id` is the merge anchor for the app's own rows: `main` and `account` are known, any other id is an
ordinary section. `collapsed` renders the group as an expandable tile, which keeps a twelve-item
Admin menu from burying the rest. Icons are Bootstrap Icons class names; the app maps each to a
Material icon and falls back to a chevron, so a new icon needs no release.

The app reads this payload, then the last good one on the device, then a bundled skeleton, so an
unreadable payload is ignored and yesterday's menu keeps rendering. Bad rows are dropped one at a
time -- which makes a bad deploy cheap, but means a row that stops being emitted disappears
silently, so prefer failing the drift test.
"""

from django.conf import settings
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from auctions import dmca

# Advisory; the app ignores it. Bump it only if the shape changes incompatibly.
MENU_VERSION = 1


def _row(title, path, icon=""):
    """One drawer row, or None if it isn't fit to send.

    `title` and `path` are required, and `path` must be site-relative: these load in the app's own
    WebView chrome. Query strings survive -- the `?days=30` on the admin links is load-bearing.
    """
    if not title or not path:
        return None
    # allowed_hosts=None means site-relative only.
    if not url_has_allowed_host_and_scheme(path, allowed_hosts=None):
        return None
    row = {"title": title, "path": path}
    if icon:
        row["icon"] = icon
    return row


def _section(section_id, rows, *, title="", icon="", collapsed=False):
    """One group of rows, or None when every row in it was dropped."""
    rows = [row for row in rows if row]
    if not rows:
        return None
    section = {"id": section_id}
    if title:
        section["title"] = title
    if icon:
        section["icon"] = icon
    if collapsed:
        section["collapsed"] = True
    section["items"] = rows
    return section


def _main_section():
    """The two public destinations, which everyone gets. The app appends Offline mode and Clubs here,
    which is why it stays short.
    """
    return _section(
        "main",
        [
            _row("Auctions", reverse("auctions"), "bi-hammer"),
            _row("Lots", reverse("allLots"), "bi-grid"),
        ],
    )


def _lots_section():
    """The navbar's "Lots" account-dropdown header, in the same order."""
    return _section(
        "lots",
        [
            _row("Selling", reverse("selling"), "bi-cash-coin"),
            _row("Watched lots", reverse("watched"), "bi-star-fill"),
            _row("Bids", reverse("my_bids"), "bi-coin"),
            _row("Won lots", reverse("won_lots"), "bi-calendar-check"),
        ],
        title="My lots",
    )


def _account_section():
    """The navbar's account rows: Invoices, Feedback, Account.

    The web folded its settings pages behind one **Account** row (``auctions/account_nav.py``) that
    lands where you were last, and each of those pages carries the Account setup sidebar, which is the
    app's navigation there too.

    Sign out is the app's own, and it merges Tap to Pay into this section by its ``id``.
    """
    return _section(
        "account",
        [
            _row("Invoices", reverse("my_invoices"), "bi-bag"),
            _row("Feedback", reverse("feedback"), "bi-chat-heart"),
            _row("Account", reverse("account_setup"), "bi-person-gear"),
        ],
        title="Account",
    )


def _admin_section():
    """Superusers only, and collapsed: twelve rows would bury everything else.

    The query strings are the navbar's defaults; without them an admin page shows a different window of
    data.
    """
    return _section(
        "admin",
        [
            _row("User stats", reverse("admin_dashboard"), "bi-speedometer2"),
            _row("Setup Checklist", reverse("admin_setup_checklist"), "bi-check2-square"),
            _row("User map", reverse("admin_user_map") + "?view=recent&filter=24", "bi-geo-alt"),
            _row("Traffic", reverse("admin_traffic") + "?days=30", "bi-graph-up"),
            _row("Referrers", reverse("admin_referrers") + "?days=30", "bi-signpost-split"),
            _row("Usability", reverse("admin_usability") + "?days=30", "bi-clipboard-data"),
            _row("Club health", reverse("admin_club_health"), "bi-heart-pulse"),
            _row("Lifecycle", reverse("admin_lifecycle"), "bi-people"),
            _row("Session replay", reverse("admin_session_replay"), "bi-list-ol"),
            _row("User signups", reverse("admin_user_signups") + "?days=90", "bi-person-plus"),
            _row("Command palette searches", reverse("command_palette_analytics"), "bi-search"),
            _row("Lots with no scientific name", reverse("species_gaps"), "bi-tags"),
            _row("Assistant skill requests", reverse("assistant_skill_requests"), "bi-stars"),
            _row("Admin site", reverse("admin:auth_user_changelist") + "?o=-6", "bi-person-fill-lock"),
            _row("Test error messages", reverse("admin_error"), "bi-exclamation-triangle"),
        ],
        title="Admin",
        icon="bi-shield-lock",
        collapsed=True,
    )


def _about_section():
    """Collapsed, and last. "About site" is gated on ENABLE_PROMO_PAGE and the copyright row on whether a
    DMCA agent is configured, exactly as the navbar gates them -- a row only one side gates is what
    ``NavbarDriftTests`` exists to catch.
    """
    rows = []
    if settings.ENABLE_PROMO_PAGE:
        rows.append(_row("About site", reverse("promo"), "bi-globe"))
    rows.append(_row("FAQ", reverse("faq"), "bi-question-circle"))
    rows.append(_row("Terms and Conditions", reverse("tos"), "bi-file-text"))
    rows.append(_row("Privacy policy", reverse("privacy_policy"), "bi-shield-lock"))
    if dmca.is_configured():
        rows.append(_row("Copyright / DMCA", reverse("dmca"), "bi-c-circle"))
    return _section("about", rows, title="About", icon="bi-info-circle", collapsed=True)


def menu_for(user):
    """The whole drawer for `user`; an AnonymousUser gets the signed-out navbar.

    The one part of /api/mobile/config/ that varies by user, so that endpoint must never be cached
    without varying on the caller.
    """
    signed_in = bool(user and user.is_authenticated)
    sections = [_main_section()]
    if signed_in:
        sections.append(_lots_section())
        sections.append(_account_section())
    if signed_in and user.is_superuser:
        sections.append(_admin_section())
    sections.append(_about_section())
    return {"version": MENU_VERSION, "sections": [section for section in sections if section]}
