import logging
import secrets

from django.contrib.auth.models import User
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Short TTL, but not as short as the WebView case alone would want. 60s was sized for the app's own
# WebView, where the window between mint and use is sub-second.
#
# The OAuth connect flows broke that: there the handoff opens in ASWebAuthenticationSession, and the
# OS draws its own consent sheet before the URL is fetched. A user who reads that sheet for a minute
# lands on an expired token, which redirects to the web login page -- byte-identical to the bug the
# handoff exists to fix. Five minutes covers a human reading a system prompt.
#
# What that costs: an *unused* token stays live for five minutes instead of one. It does not widen
# what a token can do -- 256 bits, bound server-side to one user, single-use by an atomic delete, and
# throttled on consume. It buys exactly one login as the user who asked for it.
HANDOFF_TTL_SECONDS = 300

# Namespaced so these never collide with other cache users; the token is the rest of the key.
_CACHE_PREFIX = "mobile_web_session_handoff:"

# Marks a browsing context the app opened. Set when a handoff token is consumed, and read by pages
# finishing an OAuth round trip (Square onboarding) to offer "Return to the app".
#
# The User-Agent can't answer this: the app hands OAuth to an in-app browser view, which sends
# Safari's or Chrome's User-Agent, so ``request.is_mobile_app`` is False for the whole round trip
# even though the user never left the app.
APP_ORIGINATED_SESSION_KEY = "opened_by_mobile_app"


def mark_session_opened_by_app(session) -> None:
    """Record that this session belongs to a browsing context the app opened."""
    session[APP_ORIGINATED_SESSION_KEY] = True


def session_opened_by_app(request) -> bool:
    """True when this request's browsing context came from the app, including one carrying the app's own
    User-Agent (its WebView reaching a page without a handoff).
    """
    if getattr(request, "is_mobile_app", False):
        return True
    session = getattr(request, "session", None)
    return bool(session and session.get(APP_ORIGINATED_SESSION_KEY))


class WebSessionService:
    """Bridges a native JWT session into a real Django/allauth session cookie.

    A one-time handoff token is minted (Bearer-authenticated) and stored server-side bound to the user.
    The WebView loads the consume URL itself, so the cookie is set by the server on a response the
    WebView loads -- never reconstructed in Dart -- and keeps its HttpOnly, Secure and SameSite flags.
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

        ``None`` when the token is missing, expired, used, or its user is gone or inactive. Single-use is
        enforced by the delete, not the get: Redis DEL returns truthy only for the caller that removed the
        key, so two concurrent consumers can't both win.
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
