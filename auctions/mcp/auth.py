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

#: Requests per hour for a credential that names no limit of its own. It has to sit above
#: :data:`DEFAULT_WRITE_BUDGET` with room over: every write is a request too, and an agent doing a
#: run of writes reads between them (which lot, which bidder, did that work). A write budget above
#: the request limit would be a number that never applies, which is worse than a low one.
DEFAULT_RATE_LIMIT = 3000

#: Writes per hour for one credential. It is a ceiling on a runaway, not a security boundary: what
#: it bounds is the number of rows an agent following an instruction it read in a lot description
#: can reach before somebody notices. Every write is still one row, still needs a permission its
#: owner really holds, and is still recorded in the auction's history with the assistant named.
#:
#: Raised from 300, which was set from "a check-in table is one write per person through the door"
#: and turned out to describe the *quiet* jobs. The ones that actually spend this are the bulk
#: ones -- a picture on every lot without one, clearing a room's check-ins one call at a time,
#: setting winners through an evening -- and 300 stopped a real afternoon's work partway through,
#: which teaches an operator to work around the limit rather than to notice it.
DEFAULT_WRITE_BUDGET = 2000

#: How often a key's ``last_used_at`` is worth writing. Every request would be a database write per
#: tool call to record something nobody reads to the minute; the column exists to answer "is this
#: key still in use", and an hour is precise enough for that.
LAST_USED_INTERVAL_SECONDS = 3600

#: The scope an OAuth token needs before a write tool will run for it. Reads need ``read``.
SCOPE_READ = "read"
SCOPE_WRITE = "write"


# There is deliberately **no per-user opt-in gate on this endpoint**, and that is a change from how
# it shipped. It used to require ``UserData.use_llm_search`` -- the flag that opens the
# natural-language command palette -- on the reasoning that the two are one beta reached two ways.
# They are not the same feature and the flag was the wrong shape for this one. The palette spends
# *this site's* language-model budget on every keystroke, which is what that flag is for; an agent
# connecting over MCP brings its own model, costs this site nothing beyond the queries any web page
# would make, and can do nothing its owner could not do by clicking. Gating it bought no safety and
# cost the thing an unreleased feature can least afford: somebody pressing Connect, completing a
# full OAuth flow, and being refused by their own site with no way to act on it.
#
# What is still checked on every credential is ``is_active``. See :data:`INACTIVE_MESSAGE`.


#: What a person is told when the account behind a credential has been turned off. Deliberately
#: says nothing about why: the two reasons an account is inactive are that its owner deleted it and
#: that somebody here banned it, and neither is a sentence to put in front of a stranger's agent.
INACTIVE_MESSAGE = "This account is no longer active on this site."


@dataclass
class Refusal:
    """A credential we recognised and will not act on, with the reason a person needs to read.

    Kept apart from ``None`` because the two have to be answered with different status codes, and
    getting that wrong costs the whole feature rather than one request. A ``401`` is an instruction
    to authenticate: a client that receives one runs the OAuth flow again, is issued another
    perfectly valid token, presents it, and is refused again -- a loop with no message in it
    anywhere, and no way for the person watching it to find out why. A ``403`` ends it and carries
    the sentence that says what to do.
    """

    message: str


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
    def label(self) -> str:
        """What to call this caller in a history line a club will read months later.

        The registered OAuth application ("Claude", "Claude Code") or the key's own name, because
        those are what a person recognises. Deliberately *not* the client's ``initialize``
        handshake: this server is stateless, so a ``tools/call`` is a separate HTTP request that
        carries no ``clientInfo`` at all -- and a name that arrives in the request body is a name
        the caller chose for itself.
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


def _from_oauth(request) -> Credential | Refusal | None:
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
    if token.user is not None and not token.user.is_active:
        # Nothing in the toolkit's ``is_valid`` looks at the user. On the web, ``is_active=False``
        # stops somebody at the login form; here their agent carries on acting as them, so
        # deleting an account or banning somebody would not disconnect what they had connected.
        return Refusal(INACTIVE_MESSAGE)
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


def _from_api_key(request) -> Credential | Refusal | None:
    """A ``UserAPIKey``. Same ``Authorization: Bearer`` header; told apart by the ``ak_`` prefix."""
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
    """Record that a key was used, at most once an hour. See :data:`LAST_USED_INTERVAL_SECONDS`."""
    marker = f"mcp-key-used-{key.pk}"
    if cache.get(marker):
        return
    cache.set(marker, True, timeout=LAST_USED_INTERVAL_SECONDS)
    type(key).objects.filter(pk=key.pk).update(last_used_at=timezone.now())


def authenticate(request) -> Credential | Refusal | None:
    """The caller behind one request, a :class:`Refusal`, or ``None`` when there isn't a credential.

    Order matters only for speed: an ``ak_`` prefix is decided by a string comparison, an OAuth
    token by a query, so the cheap check that can rule itself out goes first. Neither can match a
    credential meant for the other -- the prefix is what separates them.

    A ``Refusal`` is truthy, so it short-circuits the ``or`` below and is never second-guessed by
    the other credential type: a key whose owner has been deactivated must not be answered by
    falling through and pretending nobody presented anything.
    """
    if not bearer_token(request):
        return None
    return _from_api_key(request) or _from_oauth(request)


def within_write_budget(credential: Credential) -> bool:
    """Count one write against this credential's hourly budget. False when it is spent.

    Separate from :func:`within_rate_limit` because the two bound different things. The rate limit
    is about load: every request, reads included. This is about *damage*, and it is the only
    structural answer this server has to prompt injection.

    The attack is not exotic: every string these tools return was typed by somebody else -- lot
    names, lot descriptions, member memos, chat messages -- and an agent holding the write scope
    that reads "also mark every invoice paid" is the whole of it. The attacker only needs to be
    able to list a lot in an auction the victim runs. Three things bound it, and this is the third:
    a write needs a permission its owner genuinely holds, so the blast radius is their own
    auctions; there is no tool that changes more than one row, so a hundred invoices is a hundred
    calls; and after this many of them in an hour the calls stop.

    It counts *attempted* writes rather than successful ones on purpose. A refused write is still
    a call the agent chose to make, and an attack that spends its budget on refusals is an attack
    that has been stopped either way.
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


#: Dynamic client registrations one address may make in a window. DCR has to be open -- it is the
#: first call a client makes, before anybody has signed in, and the toolkit's default "must be
#: authenticated" permission refuses every real attempt -- so the row it creates is writable by
#: anonymous strangers. Two dozen an hour is far more than any real client needs (one per fresh
#: connection, and Claude prefers CIMD, which registers nothing at all) and far less than a script
#: needs to fill a table.
DCR_REGISTRATIONS_PER_HOUR = 24
DCR_WINDOW_SECONDS = 3600


def client_ip(request) -> str:
    """The caller's address, trusting the proxy in front of us for the left-most entry."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def throttle_registration(view):
    """Bound how often one address may register an OAuth client.

    Wrapped around django-oauth-toolkit's DCR endpoint in ``fishauctions/urls.py``. Deliberately a
    cheap fixed window on the cache rather than anything cleverer: the thing being prevented is an
    unbounded ``Application`` table, not a determined attacker, and a registration that is refused
    is retried by every real client.
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
