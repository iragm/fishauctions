import logging
import secrets

from django.contrib.auth.models import User
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Short TTL, but not as short as the WebView case alone would want. The original 60s was sized for
# the app's own WebView: it POSTs for a token and immediately loads the consume URL, so the window
# between mint and use is sub-second and 60s was pure slack for clock skew and a slow load.
#
# The OAuth connect flows broke that premise. There the handoff is opened in
# ASWebAuthenticationSession (Chrome Auth Tab on Android), and the OS draws its own consent sheet --
# "<app> wants to use auction.fish to sign in" -- *before* the URL is ever fetched. A user who reads
# that sheet for a minute lands on an expired token, and an expired token redirects to the web login
# page (see MobileWebSessionConsumeView), which is byte-identical to the bug the handoff is there to
# fix: "connecting anything signs me out". Five minutes covers a human reading a system prompt.
#
# What that costs, precisely: an *unused* token stays live for five minutes instead of one. It does
# not widen what a token can do. It is 256 bits from secrets.token_urlsafe, bound server-side to one
# user, and single-use by an atomic delete -- so it is worthless the instant the browser view loads
# it, which is the whole of its intended life. The consume view is throttled (mobile_auth), and the
# token is never a session by itself: it buys exactly one login as the user who asked for it.
HANDOFF_TTL_SECONDS = 300

# Namespaced so these never collide with other cache users. The token itself is the rest of the key.
_CACHE_PREFIX = "mobile_web_session_handoff:"

# Marks a browsing context the app opened. Set when a handoff token is consumed, which is the only
# way a session is created from inside the app, and read by pages that finish an OAuth round trip
# (Square onboarding) to offer a "Return to the app" button instead of a dead end.
#
# The User-Agent can't answer this question: the app hands Square/PayPal OAuth to an in-app browser
# view (SFSafariViewController / Chrome Custom Tabs), which sends Safari's/Chrome's User-Agent, not
# ours -- so ``request.is_mobile_app`` is False for the whole OAuth round trip even though the user
# never left the app. The session is what survives it.
APP_ORIGINATED_SESSION_KEY = "opened_by_mobile_app"


def mark_session_opened_by_app(session) -> None:
    """Record that this session belongs to a browsing context the app opened."""
    session[APP_ORIGINATED_SESSION_KEY] = True


def session_opened_by_app(request) -> bool:
    """True when this request's browsing context came from the app (see the key's docstring).

    Also true for a request carrying the app's own User-Agent, which covers the app's WebView
    reaching a page directly without a handoff.
    """
    if getattr(request, "is_mobile_app", False):
        return True
    session = getattr(request, "session", None)
    return bool(session and session.get(APP_ORIGINATED_SESSION_KEY))


class WebSessionService:
    """Bridges a native JWT session into a real Django/allauth session cookie.

    A one-time handoff token is minted (Bearer-authenticated) and stored server-side bound to the
    user. The WebView then loads the consume URL itself, so the session cookie is set by the server
    on a response the WebView loads — never reconstructed in Dart — and keeps its HttpOnly/Secure/
    SameSite flags. The token, not the cookie, crosses the Dart layer.
    """

    @staticmethod
    def create_handoff_token(user: User) -> str:
        """Mint a single-use, short-TTL token bound to ``user`` and store it server-side."""
        token = secrets.token_urlsafe(32)
        cache.set(_CACHE_PREFIX + token, user.pk, timeout=HANDOFF_TTL_SECONDS)
        return token

    @staticmethod
    def consume_handoff_token(token: str) -> User | None:
        """Atomically claim a handoff token, returning its user or ``None``.

        Returns ``None`` if the token is missing, expired, already used, or its user is gone/inactive.
        Single-use is enforced by the delete, not the get: Redis DEL is atomic and returns truthy only
        for the one caller that actually removed the key, so two concurrent consumers can't both win.
        """
        if not token:
            return None
        key = _CACHE_PREFIX + token
        user_id = cache.get(key)
        if user_id is None:
            return None
        if not cache.delete(key):
            # Lost the race — another request already consumed this token.
            return None
        return User.objects.filter(pk=user_id, is_active=True).first()
