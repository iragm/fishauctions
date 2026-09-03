"""The app's navigation drawer, built here and served in /api/mobile/config/.

The drawer used to be a hand-copied mirror of the web navbar's account dropdown, compiled into the
app: every link the navbar gained needed an app-store release before a phone could see it, so the
app was permanently a release or two behind, and two things it could never carry at all were the
superuser **Admin** menu and the **About site** link -- *who may see them* is a server question.

This module answers it. `menu_for(user)` returns the whole drawer for one user, gated exactly the
way `base.html` gates the navbar (`is_superuser` for Admin, `ENABLE_PROMO_PAGE` for About site,
authenticated for the two account groups), and `MobileConfigView` serves it. Adding a row, or a
whole section, is now a Django deploy.

Deliberately *not* shared with `base.html`. The drawer and the navbar are different surfaces with
different needs -- the drawer has no Clubs dropdown (the app builds that from `clubs/mine/`), no
sign-in/sign-up pair, and an ordering chosen for a phone -- so this is a second list rather than a
refactor of the template. `auctions/test_mobile_menu.py` is what keeps them from drifting apart: it
renders the real navbar for a user, pulls the account dropdown's links back out of the HTML, and
fails if one of them is missing here. Web-only links go in that test's allowlist, with a reason.

Four rows in the drawer are the app's own and are deliberately absent here, because none of them is
a URL: **Sign out** (it clears the JWT pair, the WebView cookie jar, the cached profile, the offline
files and the Square authorization -- a web `/logout/` link does one of those), **Offline mode** and
**Tap to Pay** (native screens with their own gating), and **Clubs** (already server-driven,
through `clubs/mine/`). The app merges those in itself.

Shape, and what the app does with a mistake:

    {"version": 1, "sections": [{"id": "main", "title": "…", "icon": "bi-…",
                                 "collapsed": true, "items": [{"title": …, "path": …, "icon": …}]}]}

`id` is the merge anchor for the app's own rows and is never shown; `main` and `account` are the two
it knows, every other id is an ordinary section, so adding one needs no release. `title` is the group
header (omitted on the top group). `collapsed` renders the group as an expandable tile carrying
`icon` -- what the navbar's dropdowns already are, and what keeps a twelve-item Admin menu from
burying the rest. Icons are Bootstrap Icons class names written exactly as the template writes them;
the app maps each to the nearest Material icon and falls back to a neutral chevron for one it does
not know, so a new icon never breaks anything and never needs a release either.

The app reads three tiers -- this payload, the last good one persisted on the device, then a tiny
bundled skeleton -- so a payload it cannot read is *ignored* and yesterday's menu keeps rendering.
Bad rows are dropped one at a time and a section left empty is dropped with them. That makes a bad
deploy cheap, but it also means a row that quietly stops being emitted here disappears silently:
prefer failing the drift test to trusting the client to notice.
"""

from django.conf import settings
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

# Advisory; the app ignores it today. Bump it if the *shape* ever changes incompatibly -- not for
# adding a row, a section or a key, all of which are free (unknown keys are ignored on both sides).
MENU_VERSION = 1


def _row(title, path, icon=""):
    """One drawer row, or None if it isn't fit to send.

    `title` and `path` are required. `path` must be site-relative: these rows load in the app's own
    WebView chrome, under the same rule `terms_url` and `privacy_policy_url` already live under, so
    an absolute URL on another host is dropped rather than followed. Query strings survive -- the
    `?days=30` on the admin links is load-bearing. Nothing built below can fail these checks (they
    all come from `reverse()`); the point is that a future row cannot smuggle an off-site link into
    the drawer by accident.
    """
    if not title or not path:
        return None
    # allowed_hosts=None means "no other host is allowed", i.e. site-relative only.
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
    """The two public destinations, which everyone gets signed in or out.

    The app appends Offline mode and Clubs to the end of this section, which is why it stays short.
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
    """The navbar's account rows, which are now three: Invoices, Feedback, Account.

    It used to be a flat list of every settings page. The web folded them behind one **Account**
    row (``auctions/account_nav.py``) that lands on the page you were last on, and each of those
    pages carries the Account setup sidebar -- which is the app's navigation there too, since the
    app draws no navbar. Keeping the flat list here would have been a second menu of the same pages
    that nothing kept in step with the first.

    Sign out is not here -- the app owns it (see the module docstring), and it merges Tap to Pay
    into this section by its ``id``, which is why the id stays ``account`` however short it gets.
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
    """Superusers only, and collapsed: twelve rows would bury everything else in a phone drawer.

    This has never been in the app before. The query strings are the defaults the navbar links carry
    -- an admin page opened without them shows a different window of data, not an error.
    """
    return _section(
        "admin",
        [
            _row("User stats", reverse("admin_dashboard"), "bi-speedometer2"),
            _row("Setup Checklist", reverse("admin_setup_checklist"), "bi-check2-square"),
            _row("User map", reverse("admin_user_map") + "?view=recent&filter=24", "bi-geo-alt"),
            _row("Traffic", reverse("admin_traffic") + "?days=30", "bi-graph-up"),
            _row("Referrers", reverse("admin_referrers") + "?days=30", "bi-signpost-split"),
            _row("User signups", reverse("admin_user_signups") + "?days=90", "bi-person-plus"),
            _row("User flow", reverse("admin_user_flow"), "bi-diagram-3"),
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
    """Collapsed, and last. "About site" is gated on ENABLE_PROMO_PAGE exactly as the navbar gates
    it -- a deployment with the promo page switched off has no such page to link to."""
    rows = []
    if settings.ENABLE_PROMO_PAGE:
        rows.append(_row("About site", reverse("promo"), "bi-globe"))
    rows.append(_row("FAQ", reverse("faq"), "bi-question-circle"))
    rows.append(_row("Terms and Conditions", reverse("tos"), "bi-file-text"))
    return _section("about", rows, title="About", icon="bi-info-circle", collapsed=True)


def menu_for(user):
    """The whole drawer for `user` (an AnonymousUser is fine, and is the signed-out navbar).

    This is the one part of /api/mobile/config/ that varies by user, which is why that endpoint must
    never be cached without varying on the caller.
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
