import logging
import secrets

from django.contrib.auth.models import User
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Short TTL: the app POSTs for a token and immediately loads the consume URL, so the window
# between mint and use is sub-second. 60s tolerates clock skew / a slow WebView load without
# leaving a usable credential lying around.
HANDOFF_TTL_SECONDS = 60

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
