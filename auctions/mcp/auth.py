"""Who is calling ``/mcp/``, and what they may do.

Two ways in, and a third that is refused on purpose.

**OAuth 2.1** is the one Claude's own hosted surfaces use -- claude.ai, Desktop, mobile and Claude
Code all run a real authorization-code flow with PKCE, and there is no way to paste a key into any
of them. The authorization server is ``django-oauth-toolkit`` running inside this project, so the
consent screen is this site's own login. Turned on by adding ``oauth2_provider`` to
``INSTALLED_APPS``; a deployment that does not want to be an authorization server simply doesn't,
and the key path below still works.

**A per-user API key** (:class:`auctions.models.UserAPIKey`, prefix ``ak_``) covers everything that
cannot do an OAuth dance: ``claude mcp add --header``, a cron job, a club's own script.

**A session cookie is refused**, and that is the most important line in this module. ``/mcp/`` is a
CSRF-exempt POST endpoint that performs writes. If it honoured cookies, any page on the internet
could post to it and act as whoever was signed in -- the site's own CSRF protection is what
normally stops that, and bearer credentials are what replace it here. This is the same rule
``mobile/permissions.IsMobileAuthenticated`` applies for the same reason.

A credential can only ever narrow what its owner may do (:attr:`Credential.writes`). It can never
widen it: every tool goes through the resolver, which asks the database what *this user* is allowed
to do on *this auction*, exactly as it does for somebody clicking buttons.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from django.apps import apps
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Requests per hour for a credential that names no limit of its own.
DEFAULT_RATE_LIMIT = 600

#: How often a key's ``last_used_at`` is worth writing. Every request would be a database write per
#: tool call to record something nobody reads to the minute; the column exists to answer "is this
#: key still in use", and an hour is precise enough for that.
LAST_USED_INTERVAL_SECONDS = 3600

#: The scope an OAuth token needs before a write tool will run for it. Reads need ``read``.
SCOPE_READ = "read"
SCOPE_WRITE = "write"


def opted_in(user) -> bool:
    """Whether this person has the feature turned on at all.

    ``UserData.use_llm_search`` is the same per-user flag that opens the natural-language command
    palette, and it gates this endpoint for the same reason: it is one beta, reached two ways, and
    a rollout control that only covers half of it is decorative. Without this a person could skip
    the page that explains any of this, run the OAuth flow a client offers them, and be connected.

    Deliberately *not* also gated on a language model being configured site-wide: an agent
    connecting here brings its own, so this works on an install that has no API key of its own.
    """
    userdata = getattr(user, "userdata", None)
    return bool(userdata and userdata.use_llm_search)


@dataclass
class Credential:
    """An authenticated caller: who they are, what they may do, and what proved it."""

    user: Any
    writes: bool = False
    #: ``"oauth"`` or ``"key"``. Recorded, and used to key the rate limit.
    kind: str = ""
    #: The ``UserAPIKey`` or OAuth ``AccessToken`` row, for throttling and for a log line.
    token: Any = None

    @property
    def rate_limit(self) -> int:
        return getattr(self.token, "rate_limit", None) or DEFAULT_RATE_LIMIT

    @property
    def cache_key(self) -> str:
        return f"mcp-rate-{self.kind}-{getattr(self.token, 'pk', 'none')}"


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
    """Where a client should look to find out how to authenticate.

    RFC 9728 puts the document at the *origin's* ``/.well-known/oauth-protected-resource``, and
    appends the resource's own path component after it -- so an endpoint at ``/mcp`` is described
    at ``/.well-known/oauth-protected-resource/mcp``. That form is the one that matters here,
    because the ``resource`` it reports is then ``https://host/mcp`` rather than
    ``https://host``, and Claude requires ``resource`` to match the URL the user typed into it.

    Built from the request's own path rather than hard-coded, so it stays right if the endpoint is
    ever mounted somewhere else, and so staging, production and a development box each advertise
    themselves rather than a name baked into settings.
    """
    path = (request.path or "/mcp").rstrip("/")
    return request.build_absolute_uri(f"/.well-known/oauth-protected-resource{path}")


def challenge(request) -> str:
    """The ``WWW-Authenticate`` header to put on a 401.

    Claude only honours this on a ``401`` -- never on a ``200`` -- and without the
    ``resource_metadata`` pointer it has to guess at the well-known paths, which costs round trips
    on every connection and fails outright on a host that cannot serve them.
    """
    return f'Bearer resource_metadata="{resource_metadata_url(request)}"'


def _from_oauth(request) -> Credential | None:
    """An OAuth 2.1 access token issued by this site's authorization server."""
    if not oauth_enabled():
        return None
    raw = bearer_token(request)
    if not raw:
        return None
    from oauth2_provider.models import get_access_token_model

    token = get_access_token_model().objects.filter(token=raw).select_related("user__userdata").first()
    # ``is_valid(scopes)`` is expiry *and* scope in one call. Reading is the floor: a token that
    # was granted neither scope has nothing here it is allowed to do, and saying so at the door
    # beats handing it a tool list it will be refused on every entry of.
    if token is None or not token.is_valid([SCOPE_READ]):
        return None
    if not opted_in(token.user):
        return None
    if token.user is None:
        # A client-credentials token has no user behind it. Every tool here acts as a person and
        # checks that person's permissions, so a token with nobody attached has nothing to act as.
        # (Claude does not issue these either: every connection is user-consented.)
        return None
    return Credential(
        user=token.user,
        writes=token.is_valid([SCOPE_WRITE]),
        kind="oauth",
        token=token,
    )


def _from_api_key(request) -> Credential | None:
    """A ``UserAPIKey``. Same ``Authorization: Bearer`` header; told apart by the ``ak_`` prefix."""
    from auctions.models import UserAPIKey

    raw = bearer_token(request)
    if not raw.startswith(UserAPIKey.key_prefix):
        return None
    key = UserAPIKey.verify(raw)
    if key is None or not opted_in(key.user):
        return None
    _touch(key)
    return Credential(user=key.user, writes=key.allow_writes, kind="key", token=key)


def _touch(key) -> None:
    """Record that a key was used, at most once an hour. See :data:`LAST_USED_INTERVAL_SECONDS`."""
    marker = f"mcp-key-used-{key.pk}"
    if cache.get(marker):
        return
    cache.set(marker, True, timeout=LAST_USED_INTERVAL_SECONDS)
    type(key).objects.filter(pk=key.pk).update(last_used_at=timezone.now())


def authenticate(request) -> Credential | None:
    """The caller behind one request, or ``None`` when there isn't a valid one.

    Order matters only for speed: an ``ak_`` prefix is decided by a string comparison, an OAuth
    token by a query, so the cheap check that can rule itself out goes first. Neither can match a
    credential meant for the other -- the prefix is what separates them.
    """
    if not bearer_token(request):
        return None
    return _from_api_key(request) or _from_oauth(request)


def within_rate_limit(credential: Credential) -> bool:
    """A fixed-window counter per credential. Coarse on purpose; this is a bound, not a queue."""
    key = credential.cache_key
    count = cache.get_or_set(key, 0, timeout=3600)
    if count >= credential.rate_limit:
        return False
    try:
        cache.incr(key)
    except ValueError:  # the window expired between the read and the increment
        cache.set(key, 1, timeout=3600)
    return True
