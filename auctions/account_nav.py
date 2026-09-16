"""The **Account setup** menu: which pages are in it, which one you're on, and where /account/setup/ lands.

It replaces `preferences_ribbon.html`, whose four tabs plus a `More` dropdown privileged four pages
and hid the ones people come looking for, and couldn't grow past a fifth tab at phone width.

Account pages are navigated like club pages (`club_sidebar.html`): a column at >=lg and one button
opening an offcanvas below it, in `btn-primary` since every one of these pages is the reader's own.

Three things share this list and must agree: `account_sidebar.html` via `groups_for()`;
`AccountSetupRedirect`, which lands on the page you were last on (`LANDING` is the default and
`remember()` records the visit); and `active_page()`, which decides whether a page draws the sidebar
at all -- so a page missing here has no navigation, not merely no highlight.

`Row.gate` is the only per-user condition, and only the two payment rows carry one. Everything else
is everybody's, `Delete account` above all: App Store Review 5.1.1(v) requires it to be reachable
from inside the app, where these pages render with no navbar.
"""

from collections.abc import Callable
from dataclasses import dataclass

from django.urls import reverse

#: Where /account/setup/ goes when there's nothing to go back to.
LANDING = "contact_info"

#: Session key holding the last account page this user opened.
SESSION_KEY = "last_account_page"

#: Pages never remembered as somewhere to send a person back to: landing on "Delete account"
#: because that's where they were last would read as an accusation.
NOT_REMEMBERED = frozenset({"account_delete"})


@dataclass(frozen=True)
class Row:
    """One link in the menu.

    `url_name` is reversed at render time and is what `active_page()` matches against, so the highlight
    can't drift from the link.
    """

    url_name: str
    label: str
    icon: str
    #: Optional `f(user) -> bool`; a row with no gate is shown to everybody signed in.
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
    """Unchanged, ``can_take_card_payments`` included.

    ``square_enabled`` alone used to hide the entry, leaving an organizer with no button and no way to
    ask; the page now carries the request-access button, and the gate on connecting is in
    `SquareConnectView`.
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
            # Named for what you do here: the page is the per-lot subscription switches, and the
            # email settings behind them are on Notifications.
            Row("messages", "Chat notification setup", "bi-chat"),
        ),
    ),
    Group(
        "Signing in",
        (
            Row("change_username", "Change username", "bi-person-lines-fill"),
            Row("account_change_password", "Change password", "bi-person-fill-lock"),
            Row("account_email", "Change email", "bi-envelope"),
            # Named for what it does: the site takes Apple and Facebook sign-ins too.
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
            # /account/ redirects to the reader's own public page -- what other people see. One row:
            # the navbar's "Account information" and the ribbon's "My account" were one URL.
            Row("account", "Public user page", "bi-person-fill"),
            # Not `text-danger`: painting one nav row red makes it the loudest thing in the menu.
            # The page itself is where the red lives.
            Row("account_delete", "Delete account", "bi-trash"),
        ),
    ),
)

#: Every page that draws the sidebar, including ones no row points at.
PAGE_NAMES = frozenset(row.url_name for group in GROUPS for row in group.rows) | {
    # /account/ redirects here, so this is the URL people are on when they pick "My account". Only
    # your own page counts -- see `active_page()`.
    "userpage",
}


def active_page(request):
    """The name of the account page this request is on, or None.

    None is the whole condition for the sidebar, so a page added to `GROUPS` gains the navigation and
    one removed loses it, with no second list.
    """
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return None
    match = getattr(request, "resolver_match", None)
    name = getattr(match, "url_name", None) if match else None
    if name not in PAGE_NAMES:
        return None
    if name == "userpage":
        # Somebody else's profile is not your account page, and it is the same view and URL name.
        if match.kwargs.get("slug") != request.user.username:
            return None
        return "account"
    return name


def remember(request, name):
    """Record `name` as where to send this person when they next pick Account.

    Only writes when the value changes, or every render would dirty the session.
    """
    if name in NOT_REMEMBERED:
        return
    session = getattr(request, "session", None)
    if session is None or session.get(SESSION_KEY) == name:
        return
    session[SESSION_KEY] = name


def landing_url(request):
    """Where /account/setup/ sends this person: the page they were last on, else Contact info.

    A remembered name is checked against `PAGE_NAMES` before it is reversed.
    """
    remembered = getattr(request, "session", {}).get(SESSION_KEY)
    if remembered in PAGE_NAMES and remembered not in NOT_REMEMBERED and remembered != "userpage":
        return reverse(remembered)
    return reverse(LANDING)


def groups_for(user, active=None):
    """The menu as the template draws it: groups of `{label, icon, url, active}` rows.

    A group whose rows are all gated away is dropped.
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
