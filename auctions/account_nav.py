"""The **Account setup** menu: which pages are in it, which one you're on, and where /account/setup/ lands.

This replaces `preferences_ribbon.html`, a row of `nav-tabs` with four tabs and a `More` dropdown
holding the other ten. A dropdown inside a tab strip is the worst of both: the four pages somebody
picked years ago are permanently privileged, and *Change password*, *Change email*, *Delete account*
and the payment connections -- the things people actually come looking for -- are behind a `More`
that reads as an overflow rather than as a menu. It also could not grow: a fifth tab wrapped the
strip onto two lines at phone width.

The Account pages are now navigated the same way club pages are (`club_sidebar.html`): a column on
the left at >=lg, and one named button opening an offcanvas below it. `btn-primary`, not the club
menu's `btn-info` -- `btn-info` marks the admin half of a page and every one of these pages is the
reader's own.

Three things read this module and they must agree, which is why they share one list:

* the sidebar itself, `account_sidebar.html`, over `groups_for()`;
* `AccountSetupRedirect` (the hamburger's one "Account" row), which lands on the page you were last
  on, or on Contact info; `LANDING` is that default and `remember()` is what records the visit;
* `active_page()`, which decides whether a page draws the sidebar at all -- so a page missing from
  the list here has no navigation, not merely no highlight.

`Row.gate` is the *only* per-user condition. Both payment rows carry one: PayPal and Square are
connections for somebody who takes money, and an ordinary bidder has nothing to connect them for.
Everything else here is everybody's -- `Delete account` above all, which App Store Review 5.1.1(v)
requires to be reachable from inside the app, and the app renders these pages with no navbar over
them, so the offcanvas button is the whole of that requirement.
"""

from collections.abc import Callable
from dataclasses import dataclass

from django.urls import reverse

#: Where /account/setup/ goes when there's nothing to go back to.
LANDING = "contact_info"

#: Session key holding the last account page this user opened.
SESSION_KEY = "last_account_page"

#: Pages that are never remembered as somewhere to send a person back to. Landing a returning
#: visitor on "Delete account" because that's where they were last would read as an accusation.
NOT_REMEMBERED = frozenset({"account_delete"})


@dataclass(frozen=True)
class Row:
    """One link in the menu.

    `url_name` is reversed at render time (never at import), and is also the name `active_page()`
    matches the current request against, so the highlight can't drift from the link.
    """

    url_name: str
    label: str
    icon: str
    #: Optional `f(user) -> bool`. A row with no gate is shown to everybody signed in.
    gate: Callable | None = None


@dataclass(frozen=True)
class Group:
    """A run of rows under an optional heading."""

    title: str
    rows: tuple[Row, ...]


def _userdata(user):
    return getattr(user, "userdata", None)


def _has_paypal(user):
    """The gate `preferences_ribbon.html` used, unchanged."""
    userdata = _userdata(user)
    return bool(userdata and userdata.paypal_enabled) or hasattr(user, "paypalseller")


def _has_square(user):
    """Also unchanged, `can_take_card_payments` included.

    `square_enabled` alone used to hide the whole entry, which left an organizer who wanted card
    payments with no button, no explanation and no way to ask. The page itself carries the
    request-access button now; the gate on *connecting* is still real and still lives in
    `SquareConnectView`. See `test_tap_to_pay`.
    """
    userdata = _userdata(user)
    return bool(userdata and (userdata.square_enabled or userdata.can_take_card_payments)) or hasattr(
        user, "squareseller"
    )


GROUPS = (
    Group(
        "",
        (
            Row("contact_info", "Contact info", "bi-telephone-fill"),
            Row("preferences", "Preferences", "bi-sliders"),
            Row("notification_preferences", "Notifications", "bi-bell"),
            Row("printing", "Label printing", "bi-tag"),
            Row("ignore_categories", "Ignore categories", "bi-ban"),
            # Named for what you do here rather than for what is listed: the page is the per-lot
            # subscription switches, and the two email settings behind them are on Notifications.
            Row("messages", "Chat notification setup", "bi-chat"),
        ),
    ),
    Group(
        "Signing in",
        (
            Row("change_username", "Change username", "bi-person-lines-fill"),
            Row("account_change_password", "Change password", "bi-person-fill-lock"),
            Row("account_email", "Change email", "bi-envelope"),
            # Named for what it does rather than for one of the providers: the site takes Apple and
            # Facebook sign-ins too, and "Sign in with Google" read as an instruction to do so.
            Row("socialaccount_connections", "Connect Google/Apple account", "bi-link-45deg"),
        ),
    ),
    Group(
        "Getting paid",
        (
            Row("paypal_seller", "PayPal account", "bi-paypal", gate=_has_paypal),
            Row("square_seller", "Square account", "bi-square", gate=_has_square),
        ),
    ),
    Group(
        "",
        (
            Row("user_api_keys", "AI agents", "bi-robot"),
            # /account/ is a redirect to the reader's own public page -- the page other people
            # see, which is why it is named for that rather than for the account. One row, not two:
            # the navbar's "Account information" and the ribbon's "My account" were one URL under
            # two names.
            Row("account", "Public user page", "bi-person-fill"),
            # Not in `text-danger`: this is a menu row, not the button. Painting one row of a nav
            # red makes it the loudest thing in the menu, which is the opposite of what a page
            # nobody should reach by accident wants. The page itself is where the red lives.
            Row("account_delete", "Delete account", "bi-trash"),
        ),
    ),
)

#: Every page that draws the sidebar, including the ones no row points at.
PAGE_NAMES = frozenset(row.url_name for group in GROUPS for row in group.rows) | {
    # /account/ redirects here, so this is the URL people are actually on when they pick
    # "My account". Only your own page counts -- see `active_page()`.
    "userpage",
}


def active_page(request):
    """The name of the account page this request is on, or None if it isn't on one.

    None is what keeps the sidebar off the rest of the site: it is the whole condition, so a page
    added to `GROUPS` gains the navigation and a page removed from it loses the navigation, with no
    second list to keep in step.
    """
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return None
    match = getattr(request, "resolver_match", None)
    name = getattr(match, "url_name", None) if match else None
    if name not in PAGE_NAMES:
        return None
    if name == "userpage":
        # Somebody else's profile is not your account page, and it is the same view and the same
        # URL name -- so this is the one row that has to be told apart by its argument.
        if match.kwargs.get("slug") != request.user.username:
            return None
        return "account"
    return name


def remember(request, name):
    """Record `name` as where to send this person when they next pick Account.

    Only writes when the value changes: every account page render would otherwise mark the session
    dirty and cost a write to the session store.
    """
    if name in NOT_REMEMBERED:
        return
    session = getattr(request, "session", None)
    if session is None or session.get(SESSION_KEY) == name:
        return
    session[SESSION_KEY] = name


def landing_url(request):
    """Where /account/setup/ sends this person: the page they were last on, else Contact info.

    A remembered name is checked against `PAGE_NAMES` before it is reversed, so a session written by
    an older deploy (or by hand) can only ever land on a page this menu still has.
    """
    remembered = getattr(request, "session", {}).get(SESSION_KEY)
    if remembered in PAGE_NAMES and remembered not in NOT_REMEMBERED and remembered != "userpage":
        return reverse(remembered)
    return reverse(LANDING)


def groups_for(user, active=None):
    """The menu as the template draws it: groups of `{label, icon, url, active}` rows.

    A group whose rows are all gated away is dropped rather than left as a heading with nothing
    under it.
    """
    drawn = []
    for group in GROUPS:
        rows = []
        for row in group.rows:
            if row.gate and not row.gate(user):
                continue
            rows.append(
                {
                    "label": row.label,
                    "icon": row.icon,
                    "url": reverse(row.url_name),
                    "active": row.url_name == active,
                }
            )
        if rows:
            drawn.append({"title": group.title, "rows": rows})
    return drawn
