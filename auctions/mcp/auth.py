"""Who is calling ``/mcp/``, and what they may do.

Two ``Authorization: Bearer`` credentials: OAuth 2.1 (``django-oauth-toolkit``) and a per-user
:class:`auctions.models.UserAPIKey` (prefix ``ak_``). Session cookies are refused: this is a
CSRF-exempt write endpoint. A credential only narrows its owner's permissions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from django.apps import apps
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Requests per hour by default. Must exceed :data:`DEFAULT_WRITE_BUDGET`, since writes are
#: requests too.
DEFAULT_RATE_LIMIT = 3000

#: Writes per hour per credential: a ceiling on a runaway or injected agent, not a security
#: boundary. 300 stopped real bulk jobs.
DEFAULT_WRITE_BUDGET = 2000

#: How often ``last_used_at`` is written; hourly is precise enough.
LAST_USED_INTERVAL_SECONDS = 3600

#: Write tools need the ``write`` scope; reads need ``read``.
SCOPE_READ = "read"
SCOPE_WRITE = "write"


# No per-user opt-in on this endpoint: agents bring their own model and can't exceed their owner's
# permissions. ``is_active`` is still checked on every credential; see :data:`INACTIVE_MESSAGE`.


#: Shown when the account is inactive. Says nothing about why (deleted or banned).
INACTIVE_MESSAGE = "This account is no longer active on this site."


@dataclass
class Refusal:
    """A recognised credential we won't act on, with a reason, answered with 403.

    A 401 would make an OAuth client fetch another valid token and loop with no message.
    """

    message: str


@dataclass
class Credential:
    """An authenticated caller: who they are, what they may do, and what proved it."""

    user: Any
    writes: bool = False
    #: ``"oauth"`` or ``"key"``; keys the rate limit.
    kind: str = ""
    #: The ``UserAPIKey`` or ``AccessToken`` row.
    token: Any = None

    @property
    def rate_limit(self) -> int:
        return getattr(self.token, "rate_limit", None) or DEFAULT_RATE_LIMIT

    @property
    def label(self) -> str:
        """The caller's name for history lines: the OAuth application or the key's name, never the
        client-supplied ``clientInfo``.
        """
        if self.kind == "oauth":
            application = getattr(self.token, "application", None)
            return (getattr(application, "name", "") or "an assistant").strip()
        return (getattr(self.token, "name", "") or "an API key").strip()

    @property
    def cache_key(self) -> str:
        return f"mcp-rate-{self.kind}-{getattr(self.token, 'pk', 'none')}"

    @property
    def write_budget(self) -> int:
        return getattr(self.token, "write_budget", None) or DEFAULT_WRITE_BUDGET

    @property
    def write_cache_key(self) -> str:
        return f"mcp-writes-{self.kind}-{getattr(self.token, 'pk', 'none')}"


def oauth_enabled() -> bool:
    """Whether this deployment is also an OAuth authorization server."""
    return apps.is_installed("oauth2_provider")


def bearer_token(request) -> str:
    """The raw credential from ``Authorization: Bearer …``, or an empty string."""
    header = request.META.get("HTTP_AUTHORIZATION", "") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return value.strip()


def resource_metadata_url(request) -> str:
    """Where clients find out how to authenticate (RFC 9728).

    The path form ``/.well-known/oauth-protected-resource/mcp`` reports ``resource`` as
    ``https://host/mcp``, which Claude requires. Built from the request so every host advertises itself.
    """
    path = (request.path or "/mcp").rstrip("/")
    return request.build_absolute_uri(f"/.well-known/oauth-protected-resource{path}")


def challenge(request) -> str:
    """The ``WWW-Authenticate`` header for a 401. Claude only reads ``resource_metadata`` on a 401."""
    return f'Bearer resource_metadata="{resource_metadata_url(request)}"'


def _from_oauth(request) -> Credential | Refusal | None:
    """An OAuth 2.1 access token issued by this site's authorization server."""
    if not oauth_enabled():
        return None
    raw = bearer_token(request)
    if not raw:
        return None
    from oauth2_provider.models import get_access_token_model

    token = get_access_token_model().objects.filter(token=raw).select_related("user__userdata").first()
    # Expiry and scope together; ``read`` is the floor.
    if token is None or not token.is_valid([SCOPE_READ]):
        return None
    if token.user is not None and not token.user.is_active:
        # The toolkit doesn't check the user, so an inactive account must be refused here.
        return Refusal(INACTIVE_MESSAGE)
    if token.user is None:
        # Client-credentials tokens have no user to act as.
        return None
    return Credential(
        user=token.user,
        writes=token.is_valid([SCOPE_WRITE]),
        kind="oauth",
        token=token,
    )


def _from_api_key(request) -> Credential | Refusal | None:
    """A ``UserAPIKey`` in the same Bearer header, identified by the ``ak_`` prefix."""
    from auctions.models import UserAPIKey

    raw = bearer_token(request)
    if not raw.startswith(UserAPIKey.key_prefix):
        return None
    key = UserAPIKey.verify(raw)
    if key is None:
        return None
    if not key.user.is_active:
        return Refusal(INACTIVE_MESSAGE)
    _touch(key)
    return Credential(user=key.user, writes=key.allow_writes, kind="key", token=key)


def _touch(key) -> None:
    """Record key use, at most hourly."""
    marker = f"mcp-key-used-{key.pk}"
    if cache.get(marker):
        return
    cache.set(marker, True, timeout=LAST_USED_INTERVAL_SECONDS)
    type(key).objects.filter(pk=key.pk).update(last_used_at=timezone.now())


def authenticate(request) -> Credential | Refusal | None:
    """The caller, a :class:`Refusal`, or ``None`` without a credential.

    The ``ak_`` prefix check runs first because it's cheaper. A Refusal is truthy, so it's never
    overridden by the other credential type.
    """
    if not bearer_token(request):
        return None
    return _from_api_key(request) or _from_oauth(request)


def within_write_budget(credential: Credential) -> bool:
    """Count one write against the hourly budget; False when spent.

    Separate from :func:`within_rate_limit`: this bounds damage from prompt injection. Writes need the
    owner's real permissions, no tool changes more than one row, and this caps the count. Attempts are
    counted, not successes.
    """
    key = credential.write_cache_key
    count = cache.get_or_set(key, 0, timeout=3600)
    if count >= credential.write_budget:
        return False
    try:
        cache.incr(key)
    except ValueError:  # the window expired between the read and the increment
        cache.set(key, 1, timeout=3600)
    return True


def within_rate_limit(credential: Credential) -> bool:
    """A fixed-window counter per credential."""
    key = credential.cache_key
    count = cache.get_or_set(key, 0, timeout=3600)
    if count >= credential.rate_limit:
        return False
    try:
        cache.incr(key)
    except ValueError:  # the window expired between the read and the increment
        cache.set(key, 1, timeout=3600)
    return True


#: Dynamic client registrations per address per window. DCR must be open to anonymous callers, so
#: this bounds the Application table.
DCR_REGISTRATIONS_PER_HOUR = 24
DCR_WINDOW_SECONDS = 3600


def client_ip(request) -> str:
    """The caller's address, trusting the proxy's left-most X-Forwarded-For entry."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def throttle_registration(view):
    """Rate-limit OAuth client registration per address, wrapping the toolkit's DCR view in
    ``fishauctions/urls.py``.
    """
    import functools

    from django.http import JsonResponse

    @functools.wraps(view)
    def guarded(request, *args, **kwargs):
        if request.method not in ("POST", "PUT", "DELETE"):
            return view(request, *args, **kwargs)
        key = f"mcp-dcr-{client_ip(request)}"
        count = cache.get_or_set(key, 0, timeout=DCR_WINDOW_SECONDS)
        if count >= DCR_REGISTRATIONS_PER_HOUR:
            logger.warning("Refused a dynamic client registration from %s: over the hourly limit", key)
            response = JsonResponse(
                {
                    "error": "temporarily_unavailable",
                    "error_description": "Too many client registrations from this address. Try again later.",
                },
                status=429,
            )
            response["Retry-After"] = str(DCR_WINDOW_SECONDS)
            return response
        try:
            cache.incr(key)
        except ValueError:  # the window expired between the read and the increment
            cache.set(key, 1, timeout=DCR_WINDOW_SECONDS)
        return view(request, *args, **kwargs)

    return guarded
