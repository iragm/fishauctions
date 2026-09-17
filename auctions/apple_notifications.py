"""Sign in with Apple server-to-server notifications.

Apple POSTs each account change once (retrying until 2xx), with no API to poll:

``consent-revoked``  the user disconnected this app; treat as sign-out.
``account-delete``   the Apple ID is gone; that ``sub`` never authenticates again.
``email-disabled``   Hide My Email forwarding off; mail is silently discarded.
``email-enabled``    forwarding back on.

django-allauth has no notification support; its JWT helpers and ``SocialAccount`` rows are reused.
One departure: allauth blacklists each ``jti`` on verify, which would reject Apple's retries, so
``jti`` is used here as an idempotency key instead. All handlers are idempotent.
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

# Any asymmetric algorithm, so key rotation doesn't break us. Never HS256 (public-key-as-secret).
ALLOWED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"})

REQUEST_TIMEOUT_SECONDS = 10

# Cached so an unauthenticated endpoint can't make us call Apple in a loop.
JWKS_CACHE_KEY = "apple_signin_jwks"
JWKS_CACHE_SECONDS = 60 * 60
# An unknown kid forces a re-fetch (key rotation), so the re-fetch needs its own floor.
JWKS_REFETCH_LOCK_KEY = "apple_signin_jwks_refetch"
JWKS_REFETCH_LOCK_SECONDS = 60

# Processed notifications remembered past Apple's retry window.
PROCESSED_CACHE_PREFIX = "apple_s2s_jti:"
PROCESSED_CACHE_SECONDS = 60 * 60 * 48

EVENT_CONSENT_REVOKED = "consent-revoked"
EVENT_ACCOUNT_DELETE = "account-delete"
EVENT_EMAIL_DISABLED = "email-disabled"
EVENT_EMAIL_ENABLED = "email-enabled"

# Only reachable while Apple forwards, which email-disabled and account-delete stop.
PRIVATE_RELAY_DOMAIN = "privaterelay.appleid.com"


class AppleNotificationError(Exception):
    """The payload isn't a notification we can trust. Surfaces as a 400."""


def notifications_configured() -> bool:
    """True when notifications can be verified, i.e. there are Apple audiences to check ``aud`` against."""
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
        # Already re-fetching for an unknown kid; serve what we have.
        return cache.get(JWKS_CACHE_KEY) or {}
    response = requests.get(APPLE_KEYS_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    keys_data = response.json()
    cache.set(JWKS_CACHE_KEY, keys_data, JWKS_CACHE_SECONDS)
    return keys_data


def _find_jwk(keys_data: dict, kid: str) -> dict | None:
    """Apple's published key with this ``kid``, as the raw JWK."""
    for jwk in keys_data.get("keys") or []:
        if isinstance(jwk, dict) and jwk.get("kid") == kid:
            return jwk
    return None


def _signing_key(signed_payload: str):
    """(algorithm, public key) for ``signed_payload``, or raise :class:`AppleNotificationError`.

    **The algorithm comes from Apple's published key, never the token header**, which an attacker
    writes (``alg: HS256`` or ``none``). The header is read only for ``kid``, after a cheap sanity check.
    """
    import jwt
    from allauth.socialaccount.internal import jwtkit

    try:
        header = jwt.get_unverified_header(signed_payload)
    except jwt.PyJWTError as exc:
        msg = "Payload is not a JWT."
        raise AppleNotificationError(msg) from exc
    kid = header.get("kid")
    if header.get("alg") not in ALLOWED_ALGORITHMS or not kid or not isinstance(kid, str):
        msg = f"Unacceptable JWT header (alg={header.get('alg')!r}, kid={kid!r})."
        raise AppleNotificationError(msg)

    jwk = None
    for force in (False, True):
        # Unknown kid: forgery or rotation; one throttled re-fetch tells them apart.
        jwk = _find_jwk(_fetch_jwks(force=force), kid)
        if jwk is not None:
            break
    if jwk is None:
        msg = f"No Apple signing key matches kid {kid}."
        raise AppleNotificationError(msg)

    # allauth's default for a JWK without alg; Apple's all say RS256.
    algorithm = jwk.get("alg", "RS256")
    if algorithm not in ALLOWED_ALGORITHMS:
        msg = f"Apple's key {kid} names an algorithm we don't accept ({algorithm!r})."
        raise AppleNotificationError(msg)
    try:
        key = jwtkit.lookup_kid_jwk({"keys": [jwk]}, kid)
    except Exception as exc:
        msg = f"Apple's key {kid} could not be used."
        raise AppleNotificationError(msg) from exc
    if key is None:
        msg = f"Apple's key {kid} could not be used."
        raise AppleNotificationError(msg)
    return algorithm, key


def verify_notification(signed_payload: str) -> dict:
    """Verify Apple's signed notification and return its claims: signature, issuer, audience, expiry.
    Doesn't consume ``jti``. Empty ``APPLE_ALLOWED_AUDIENCES`` rejects everything.
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
                # Apple's payloads have no exp; require the claims we rely on.
                "verify_exp": True,
                "require": ["iss", "aud", "iat"],
            },
        )
    # PyJWT raises bare TypeError for mismatched key/algorithm; catch it rather than 500 on demand.
    except (jwt.PyJWTError, TypeError, ValueError) as exc:
        msg = f"Notification failed verification: {type(exc).__name__}: {exc}"
        raise AppleNotificationError(msg) from exc


def parse_events(claims: dict) -> list[dict]:
    """The events in a verified notification. Apple sends ``events`` as a JSON-encoded string; objects and
    lists are accepted too.
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
    """End *user*'s mobile refresh tokens. Web sessions are left alone: finding one user's sessions means
    decoding the whole table, on a public endpoint.
    """
    from auctions.account_deletion import blacklist_refresh_tokens

    blacklist_refresh_tokens(user)


def _can_still_sign_in(user) -> bool:
    """Whether *user* can sign in without Apple: another social account, a usable password, or a verified
    non-relay address. "No" schedules deletion, so it answers "no" only when certain.
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
    """The user disconnected this app at Apple: sign them out, keep the account and the ``SocialAccount``
    (``sub`` is stable, so re-authorizing lands on the same account).
    """
    from allauth.socialaccount.models import SocialToken

    for account in _apple_accounts(sub):
        SocialToken.objects.filter(account=account).delete()
        _sign_out_everywhere(account.user)
        logger.info("Apple consent revoked for user %s; tokens dropped and app sessions ended.", account.user_id)


def _handle_account_delete(sub: str) -> None:
    """The Apple ID is gone: delete the ``SocialAccount``, and schedule the site account for deletion only
    if Apple was its only way in (:func:`_can_still_sign_in`). Signing in cancels the grace period.
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
            # Already deleted or disabled; don't re-arm the timer.
            continue
        due = request_deletion(user)
        logger.warning(
            "Apple ID deleted for user %s, which was their only way to sign in; account scheduled for deletion on %s.",
            user.pk,
            due,
        )


def _handle_email_forwarding(sub: str, email: str, *, enabled: bool) -> None:
    """Mark a Hide My Email address unreachable (like an SES hard bounce) or back to UNKNOWN (not VALID:
    forwarding isn't delivery). The allauth ``EmailAddress`` is untouched, or verification would lock
    them out.
    """
    from auctions.models import AuctionTOS, ClubMember

    if not email:
        return
    status = "BAD" if not enabled else "UNKNOWN"
    tos_rows = AuctionTOS.objects.filter(email__iexact=email)
    member_rows = ClubMember.objects.filter(email__iexact=email, is_deleted=False)
    if enabled:
        # Only undo what a disable did.
        tos_rows = tos_rows.filter(email_address_status="BAD")
        member_rows = member_rows.filter(email_address_status="BAD")
    # Bulk updates like bounce_handler: no ClubHistory, no save() side effects.
    updated = tos_rows.update(email_address_status=status) + member_rows.update(email_address_status=status)
    logger.info(
        "Apple relay forwarding %s for sub %s; %s record(s) marked %s.",
        "enabled" if enabled else "disabled",
        sub,
        updated,
        status,
    )


def handle_event(event: dict) -> str:
    """Act on one verified event. Returns its type, or ``"ignored"`` for unknown types (Apple adds them)."""
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
    # Only after success, so a failed handler is retried.
    if cache_key:
        cache.set(cache_key, True, PROCESSED_CACHE_SECONDS)
    return handled


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


@method_decorator(csrf_exempt, name="dispatch")
class AppleServerNotificationView(View):
    """``POST /apple/notifications``, as registered with Apple.

    No trailing slash (APPEND_SLASH can't redirect a POST). CSRF-exempt: the signature authenticates.
    Only a 2xx stops Apple's retries, so an unprocessed payload never returns one.
    """

    def post(self, request, *args, **kwargs):
        if not notifications_configured():
            logger.error(
                "Apple sent a server-to-server notification but Sign in with Apple is not configured "
                "here (APPLE_SIGN_IN_BUNDLE_ID / APPLE_SIGN_IN_SERVICES_ID); it cannot be verified."
            )
            # 503: Apple retries, so fixing config recovers what was sent meanwhile.
            return JsonResponse({"error": "not configured"}, status=503)

        signed_payload = self._signed_payload(request)
        try:
            handled = process_notification(signed_payload)
        except AppleNotificationError as exc:
            # Logged, not raised: junk POSTs shouldn't email admins.
            logger.warning("Rejected an Apple server-to-server notification: %s", exc)
            return JsonResponse({"error": "invalid payload"}, status=400)
        # Anything else is ours; a 500 makes Apple resend.
        return JsonResponse({"handled": handled}, status=200)

    @staticmethod
    def _signed_payload(request) -> str:
        """The JWT from the body: JSON ``payload``, also form encoding and ``signedPayload``."""
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
        """A liveness page for an admin pasting the URL into a browser."""
        return HttpResponse(
            "ok" if notifications_configured() else "Sign in with Apple is not configured",
            content_type="text/plain",
            status=200 if notifications_configured() else 503,
        )
