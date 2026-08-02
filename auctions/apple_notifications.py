"""Sign in with Apple server-to-server notifications — Apple telling us an account changed.

Apple only tells you about a change to a Sign in with Apple account *once*, by POSTing a signed
notification to a URL registered in the developer portal. There is no queue to re-read and no API to
poll, so anything that isn't handled when it arrives is simply never learned. The four things Apple
sends are all things that silently break an account if they're ignored:

===============  ==========================================================================
``consent-revoked``  The user disconnected this app in their Apple Account settings. Their
                     Apple tokens are dead; Apple's guidance is to treat it as a sign-out.
``account-delete``   The user deleted their Apple ID outright. That ``sub`` will never
                     authenticate again — for an account that had no other way in, this is
                     the person losing access permanently.
``email-disabled``   The user turned off forwarding on their Hide My Email relay address.
                     Mail to it is discarded from then on, with no bounce and no error.
``email-enabled``    Forwarding was turned back on.
===============  ==========================================================================

Registering the endpoint is also how Apple expects a site to keep a signed-in session alive without
re-validating the refresh token against ``/auth/token`` on a schedule: the notification is the push
version of that poll. Nothing here is optional for an app shipping Sign in with Apple.

**django-allauth does not implement this.** Its Apple provider (65.x) covers the OAuth flow and
nothing else — there is no notification view, URL or setting anywhere in the package. What it *does*
have is the JWT machinery (:mod:`allauth.socialaccount.internal.jwtkit`) and the ``SocialAccount``
rows that identify who a notification is about, and both are used here rather than reimplemented.

One deliberate departure from allauth: :func:`allauth.socialaccount.internal.jwtkit.verify_and_decode`
blacklists each ``jti`` as it verifies, which is right for a login credential and wrong for a
webhook. Apple retries a notification until it gets a 2xx, so the *second* delivery of a payload we
failed to process is the one that matters — and allauth's blacklist would reject it as a replay,
permanently. So the signature check is assembled from jwtkit's parts and the ``jti`` is used the
other way round: as an idempotency key that answers "already done, thanks" with a 200.

Everything the handlers do is idempotent, because a retry after a partial failure is normal.
"""

from __future__ import annotations

import json
import logging

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_KEYS_URL = "https://appleid.apple.com/auth/keys"  # nosec - public JWKS, not a secret

# Apple signs with RS256 today. Pinning the exact algorithm would break the day they rotate, so the
# allowlist is "any asymmetric algorithm" instead — which is the only property that actually matters
# here. Without it, `alg` comes from the attacker-supplied JWT header, and `alg: HS256` invites the
# classic confusion attack where a public key is used as an HMAC secret.
ALLOWED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"})

REQUEST_TIMEOUT_SECONDS = 10

# Apple's public keys change rarely and this endpoint is unauthenticated by nature, so the JWKS is
# cached rather than re-fetched per request — otherwise anyone can make us call Apple in a loop.
JWKS_CACHE_KEY = "apple_signin_jwks"
JWKS_CACHE_SECONDS = 60 * 60
# A payload naming an unknown `kid` forces a re-fetch (that is how key rotation is noticed), so the
# re-fetch itself needs a floor or the cache above achieves nothing.
JWKS_REFETCH_LOCK_KEY = "apple_signin_jwks_refetch"
JWKS_REFETCH_LOCK_SECONDS = 60

# How long a processed notification is remembered, so Apple's retries of something already handled
# are answered instead of re-run. Comfortably longer than Apple's retry window.
PROCESSED_CACHE_PREFIX = "apple_s2s_jti:"
PROCESSED_CACHE_SECONDS = 60 * 60 * 48

EVENT_CONSENT_REVOKED = "consent-revoked"
EVENT_ACCOUNT_DELETE = "account-delete"
EVENT_EMAIL_DISABLED = "email-disabled"
EVENT_EMAIL_ENABLED = "email-enabled"

# Addresses here are only ever reachable while Apple forwards them, which is exactly what
# `email-disabled` and `account-delete` turn off.
PRIVATE_RELAY_DOMAIN = "privaterelay.appleid.com"


class AppleNotificationError(Exception):
    """The payload isn't a notification we can trust. Surfaces as a 400."""


def notifications_configured() -> bool:
    """True when a notification can be verified at all.

    Verification turns on the audience: Apple sets ``aud`` to the client id the notification was
    configured against, and a deployment with no Apple identifiers has nothing to compare it to.
    Accepting an unchecked audience would mean honouring "delete this account" from anybody's Apple
    app, so the endpoint refuses rather than guesses.
    """
    return bool(getattr(settings, "APPLE_ALLOWED_AUDIENCES", []))


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _fetch_jwks(force: bool = False) -> dict:
    """Apple's JWKS, from cache unless a re-fetch is called for and allowed."""
    import requests

    if not force:
        cached = cache.get(JWKS_CACHE_KEY)
        if cached:
            return cached
    elif not cache.add(JWKS_REFETCH_LOCK_KEY, True, JWKS_REFETCH_LOCK_SECONDS):
        # Someone is already making us chase an unknown kid; serve what we have.
        return cache.get(JWKS_CACHE_KEY) or {}
    response = requests.get(APPLE_KEYS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    keys_data = response.json()
    cache.set(JWKS_CACHE_KEY, keys_data, JWKS_CACHE_SECONDS)
    return keys_data


def _signing_key(signed_payload: str):
    """(algorithm, public key) for ``signed_payload``, or raise :class:`AppleNotificationError`."""
    import jwt
    from allauth.socialaccount.internal import jwtkit

    try:
        header = jwt.get_unverified_header(signed_payload)
    except jwt.PyJWTError as exc:
        msg = "Payload is not a JWT."
        raise AppleNotificationError(msg) from exc
    algorithm = header.get("alg")
    kid = header.get("kid")
    if algorithm not in ALLOWED_ALGORITHMS or not kid:
        msg = f"Unacceptable JWT header (alg={algorithm!r}, kid={kid!r})."
        raise AppleNotificationError(msg)

    for force in (False, True):
        keys_data = _fetch_jwks(force=force)
        try:
            key = jwtkit.lookup_kid_jwk(keys_data, kid) if keys_data.get("keys") else None
        except Exception as exc:  # allauth raises OAuth2Error for a JWK it can't build
            msg = f"Apple's key {kid} could not be used."
            raise AppleNotificationError(msg) from exc
        if key is not None:
            return algorithm, key
    msg = f"No Apple signing key matches kid {kid}."
    raise AppleNotificationError(msg)


def verify_notification(signed_payload: str) -> dict:
    """Verify Apple's signed notification and return its claims.

    Checks the signature against Apple's published keys, that Apple issued it, that it was meant for
    *this* app (``aud`` is one of ours) and that it hasn't expired. Deliberately does **not** consume
    the ``jti`` — see the module docstring.
    """
    import jwt

    if not signed_payload or not isinstance(signed_payload, str):
        msg = "Missing payload."
        raise AppleNotificationError(msg)
    algorithm, key = _signing_key(signed_payload)
    try:
        return jwt.decode(
            signed_payload,
            key=key,
            algorithms=[algorithm],
            issuer=APPLE_ISSUER,
            audience=list(settings.APPLE_ALLOWED_AUDIENCES),
            options={
                "verify_signature": True,
                "verify_iss": True,
                "verify_aud": True,
                # Apple's notification payloads carry no `exp`; PyJWT skips the check when the claim
                # is absent, and `require` below makes sure the claims we *do* rely on are present.
                "verify_exp": True,
                "require": ["iss", "aud", "iat"],
            },
        )
    except jwt.PyJWTError as exc:
        msg = f"Notification failed verification: {exc}"
        raise AppleNotificationError(msg) from exc


def parse_events(claims: dict) -> list[dict]:
    """The events inside a verified notification.

    Apple puts the event in an ``events`` claim as a *JSON-encoded string*, not as a nested object —
    an easy thing to get wrong, and the reason a plain ``claims["events"]["type"]`` silently sees
    nothing. Both shapes (and a list, which Apple's docs leave room for) are accepted.
    """
    raw = claims.get("events")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            msg = "The events claim is not valid JSON."
            raise AppleNotificationError(msg) from exc
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [event for event in raw if isinstance(event, dict)]
    msg = "The notification carries no events."
    raise AppleNotificationError(msg)


# ---------------------------------------------------------------------------
# Event handling
# ---------------------------------------------------------------------------


def _apple_accounts(sub: str):
    from allauth.socialaccount.models import SocialAccount

    from auctions.mobile.services.social_auth import PROVIDER_APPLE

    return SocialAccount.objects.filter(provider=PROVIDER_APPLE, uid=sub).select_related("user")


def _sign_out_everywhere(user) -> None:
    """End the app's sessions for *user*.

    Only the mobile refresh tokens, which are the long-lived ones — a phone signed in months ago
    stays signed in until its token is retired, so this is the part that would otherwise outlive the
    revocation by a year. Web sessions are left alone on purpose: with ``SESSION_COOKIE_AGE`` set to
    effectively forever, the session table has no expiry to prune against and finding one user's
    rows means decoding every row in it, on a public endpoint anyone can POST to.
    """
    from auctions.account_deletion import blacklist_refresh_tokens

    blacklist_refresh_tokens(user)


def _can_still_sign_in(user) -> bool:
    """Whether *user* has any way back into their account that doesn't go through Apple.

    Written to answer "no" only when it is certain, because the caller's response to "no" is to
    schedule the account for deletion. Any of these is enough:

    * another linked social account,
    * a usable password (this site accepts a username, so no working inbox is needed), or
    * a verified address that isn't an Apple relay — enough to do a password reset.

    A ``@privaterelay.appleid.com`` address doesn't count: it only ever worked because Apple was
    forwarding it, and the events that ask this question are the ones that stop the forwarding.
    """
    from allauth.account.models import EmailAddress
    from allauth.socialaccount.models import SocialAccount

    if SocialAccount.objects.filter(user=user).exists():
        return True
    if user.has_usable_password():
        return True
    return (
        EmailAddress.objects.filter(user=user, verified=True)
        .exclude(email__iendswith=f"@{PRIVATE_RELAY_DOMAIN}")
        .exists()
    )


def _handle_consent_revoked(sub: str) -> None:
    """The user disconnected this app at Apple. Sign them out; leave the account alone.

    Apple's own guidance for this event is "treat it as a sign-out request and delete the tokens" —
    not as a deletion. The ``SocialAccount`` row stays for the same reason: ``sub`` is stable, so
    re-authorizing later lands on the same row and therefore the same site account. Dropping the row
    would make the next Sign in with Apple look like a brand-new person and strand the old account.
    Only the tokens go, and they are dead at Apple's end anyway.
    """
    from allauth.socialaccount.models import SocialToken

    for account in _apple_accounts(sub):
        SocialToken.objects.filter(account=account).delete()
        _sign_out_everywhere(account.user)
        logger.info("Apple consent revoked for user %s; tokens dropped and app sessions ended.", account.user_id)


def _handle_account_delete(sub: str) -> None:
    """The Apple ID itself is gone. Drop the dead link, and delete the site account if it's stranded.

    Unlike a revocation, this ``sub`` can never come back, so the ``SocialAccount`` row is deleted:
    keeping it would only give a future Apple ID a chance to collide with a link that no longer means
    anything.

    Whether the *site* account goes with it depends on whether the person can still reach it.
    Apple's rule is that deleting the Apple ID should delete the account it made, but plenty of these
    accounts also have a password or a Google login, and "they deleted their Apple ID" is not
    "they left this site". So the site account is only scheduled for deletion when Apple was the only
    door (:func:`_can_still_sign_in`), which is also the only case where nothing is lost by it: the
    normal 30-day grace period is cancelled by signing in, and someone who can't sign in at all
    hasn't got an account left to save. Anything else keeps its account and just loses the Apple
    button, with a log line saying so.
    """
    from allauth.socialaccount.models import SocialToken

    from auctions.account_deletion import request_deletion

    for account in _apple_accounts(sub):
        user = account.user
        SocialToken.objects.filter(account=account).delete()
        account.delete()
        _sign_out_everywhere(user)
        if _can_still_sign_in(user):
            logger.info(
                "Apple ID deleted for user %s; unlinked Apple sign-in. The account has another way in and was kept.",
                user.pk,
            )
            continue
        if not user.is_active:
            # Already deleted or disabled; request_deletion would only re-arm the timer.
            continue
        due = request_deletion(user)
        logger.warning(
            "Apple ID deleted for user %s, which was their only way to sign in; account scheduled for deletion on %s.",
            user.pk,
            due,
        )


def _handle_email_forwarding(sub: str, email: str, *, enabled: bool) -> None:
    """Record that a Hide My Email address did or didn't just stop working.

    A disabled relay address swallows everything sent to it — no bounce, no error, no confirmation
    email, no invoice — so it gets the same ``email_address_status`` treatment as an SES hard bounce
    (``auctions.signals.bounce_handler``), which is what the rest of the site already reads to warn
    an admin that a member is unreachable. Re-enabling puts it back to UNKNOWN rather than VALID:
    forwarding being switched on is not evidence that anything was ever delivered.

    The allauth ``EmailAddress`` row is deliberately untouched. Marking it unverified would lock the
    person out of a site where email verification is mandatory, which is a far bigger punishment than
    the problem — they can still sign in with Apple, and signing in is how they'd fix it.
    """
    from auctions.models import AuctionTOS, ClubMember

    if not email:
        return
    status = "BAD" if not enabled else "UNKNOWN"
    tos_rows = AuctionTOS.objects.filter(email__iexact=email)
    member_rows = ClubMember.objects.filter(email__iexact=email, is_deleted=False)
    if enabled:
        # Only undo what a disable did. A VALID address that someone actually confirmed keeps that.
        tos_rows = tos_rows.filter(email_address_status="BAD")
        member_rows = member_rows.filter(email_address_status="BAD")
    # Bulk updates, matching bounce_handler: the actor here is Apple, not a club admin, so this
    # writes no ClubHistory and fires none of the save() side effects (invoice recalculation,
    # mailing-list sync) that have nothing to do with a forwarding switch.
    updated = tos_rows.update(email_address_status=status) + member_rows.update(email_address_status=status)
    logger.info(
        "Apple relay forwarding %s for sub %s; %s record(s) marked %s.",
        "enabled" if enabled else "disabled",
        sub,
        updated,
        status,
    )


def handle_event(event: dict) -> str:
    """Act on one verified event. Returns the event type, or ``"ignored"``.

    Unknown types are ignored rather than treated as an error: Apple adds event types, and answering
    a new one with a failure would make Apple retry it for a day.
    """
    event_type = (event.get("type") or "").strip()
    sub = (event.get("sub") or "").strip()
    if not sub:
        logger.warning("Apple notification event %r carries no sub; ignoring.", event_type)
        return "ignored"

    if event_type == EVENT_CONSENT_REVOKED:
        _handle_consent_revoked(sub)
    elif event_type == EVENT_ACCOUNT_DELETE:
        _handle_account_delete(sub)
    elif event_type in (EVENT_EMAIL_DISABLED, EVENT_EMAIL_ENABLED):
        _handle_email_forwarding(sub, (event.get("email") or "").strip(), enabled=event_type == EVENT_EMAIL_ENABLED)
    else:
        logger.info("Ignoring unknown Apple notification event type %r.", event_type)
        return "ignored"
    return event_type


def process_notification(signed_payload: str) -> list[str]:
    """Verify and act on one notification. Returns the event types handled."""
    claims = verify_notification(signed_payload)
    jti = claims.get("jti")
    cache_key = f"{PROCESSED_CACHE_PREFIX}{jti}" if jti else None
    if cache_key and cache.get(cache_key):
        logger.info("Apple notification %s already processed; acknowledging the retry.", jti)
        return []
    handled = [handle_event(event) for event in parse_events(claims)]
    # Set only once everything succeeded — a handler that raised leaves the key unset so Apple's
    # retry runs it again rather than being told it was already done.
    if cache_key:
        cache.set(cache_key, True, PROCESSED_CACHE_SECONDS)
    return handled


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


@method_decorator(csrf_exempt, name="dispatch")
class AppleServerNotificationView(View):
    """``POST /apple/notifications`` — the URL registered in Apple's developer portal.

    Registered without a trailing slash because that is the URL Apple was given, and ``APPEND_SLASH``
    cannot rescue a POST (the redirect drops the body). CSRF is exempt for the usual webhook reason:
    the caller is Apple, and the signature on the payload is what authenticates it — nothing here
    trusts the session, the request body, or anything else the request claims about itself.

    Status codes are chosen around Apple's retry behaviour: only a 2xx stops the retries, so a
    payload we couldn't process must not return one.
    """

    def post(self, request, *args, **kwargs):
        if not notifications_configured():
            logger.error(
                "Apple sent a server-to-server notification but Sign in with Apple is not configured "
                "here (APPLE_SIGN_IN_BUNDLE_ID / APPLE_SIGN_IN_SERVICES_ID); it cannot be verified."
            )
            # 503, not 400: nothing is wrong with the notification. Apple retries, so fixing the
            # configuration recovers whatever was sent in the meantime.
            return JsonResponse({"error": "not configured"}, status=503)

        signed_payload = self._signed_payload(request)
        try:
            handled = process_notification(signed_payload)
        except AppleNotificationError as exc:
            # Either not from Apple or not for us. Logged rather than raised: this endpoint is public
            # and a stream of junk POSTs shouldn't be a stream of 500 emails to the admins.
            logger.warning("Rejected an Apple server-to-server notification: %s", exc)
            return JsonResponse({"error": "invalid payload"}, status=400)
        # Anything else is our bug or our database, and a 500 is what makes Apple send it again.
        return JsonResponse({"handled": handled}, status=200)

    @staticmethod
    def _signed_payload(request) -> str:
        """The signed JWT out of the request body.

        Apple posts ``{"payload": "<jwt>"}`` as JSON. Form encoding and the ``signedPayload`` spelling
        (which Apple uses for App Store notifications) are accepted too — a wrong guess here would
        look exactly like a signature failure and be needlessly hard to diagnose.
        """
        try:
            body = json.loads(request.body or b"{}")
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        for source in (body, request.POST):
            for field in ("payload", "signedPayload"):
                value = source.get(field)
                if value:
                    return value
        return ""

    def get(self, request, *args, **kwargs):
        """A liveness check for whoever is setting this up in the developer portal.

        Apple never GETs this URL, but an admin pasting it into a browser will, and "405 Method Not
        Allowed" reads like a broken endpoint. Says nothing an unauthenticated caller shouldn't know.
        """
        return HttpResponse(
            "ok" if notifications_configured() else "Sign in with Apple is not configured",
            content_type="text/plain",
            status=200 if notifications_configured() else 503,
        )
