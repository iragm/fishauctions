"""Sign in with Apple: redeeming the authorization code, and revoking the grant on deletion.

Apple requires an app offering Sign in with Apple to revoke the token when the user deletes their
account, and revoking needs a refresh token, which Apple only issues in exchange for the one-shot
``authorization_code`` from sign-in. So:

1. :func:`redeem_authorization_code` runs once at sign-in and stores what comes back on allauth's
   ``SocialToken`` (``token`` = access, ``token_secret`` = refresh, allauth's own Apple layout).
2. :func:`revoke_account` runs from account deletion and calls Apple's ``/auth/revoke``.

Both need the team key. A deployment without it can still offer native Apple sign-in -- verifying an
identity token needs only Apple's JWKS -- but cannot revoke, so :func:`revocation_configured` says
so rather than letting deletions skip a step Apple checks for.

Everything is best-effort at the call site: Apple being unreachable must never stop a deletion.
"""

import logging
import time

from django.conf import settings

logger = logging.getLogger(__name__)

APPLE_TOKEN_URL = "https://appleid.apple.com/auth/token"  # nosec - public endpoint, not a secret
APPLE_REVOKE_URL = "https://appleid.apple.com/auth/revoke"
APPLE_AUDIENCE = "https://appleid.apple.com"

# Apple caps the client secret JWT at six months; an hour is plenty for one request and keeps a
# leaked assertion nearly worthless.
CLIENT_SECRET_TTL_SECONDS = 60 * 60
REQUEST_TIMEOUT_SECONDS = 10


def revocation_configured() -> bool:
    """True when we hold everything needed to talk to Apple's token endpoints."""
    return bool(
        getattr(settings, "APPLE_SIGN_IN_TEAM_ID", "")
        and getattr(settings, "APPLE_SIGN_IN_KEY_ID", "")
        and getattr(settings, "APPLE_SIGN_IN_PRIVATE_KEY", "")
        and _client_id()
    )


def _client_id() -> str:
    """The identifier Apple issued the grant to.

    Native sign-in is issued to the app's bundle id, which is what the token endpoints expect -- not the
    web Services ID allauth sends for the web flow.
    """
    return getattr(settings, "APPLE_SIGN_IN_BUNDLE_ID", "") or getattr(settings, "APPLE_SIGN_IN_SERVICES_ID", "")


def _client_secret() -> str:
    """Apple's client secret: an ES256 JWT signed with the team's .p8 key."""
    import jwt

    now = int(time.time())
    return jwt.encode(
        payload={
            "iss": settings.APPLE_SIGN_IN_TEAM_ID,
            "aud": APPLE_AUDIENCE,
            "sub": _client_id(),
            "iat": now,
            "exp": now + CLIENT_SECRET_TTL_SECONDS,
        },
        key=settings.APPLE_SIGN_IN_PRIVATE_KEY,
        algorithm="ES256",
        headers={"kid": settings.APPLE_SIGN_IN_KEY_ID},
    )


def redeem_authorization_code(authorization_code: str) -> dict | None:
    """Exchange Apple's one-shot ``authorization_code`` for tokens; ``None`` on any failure.

    Called only to obtain the refresh token that makes deletion-time revocation possible: identity is
    already proved by the identity token, so a failure is logged and ignored.
    """
    import requests

    if not authorization_code or not revocation_configured():
        return None
    try:
        response = requests.post(
            APPLE_TOKEN_URL,
            data={
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "code": authorization_code,
                "grant_type": "authorization_code",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()
    except Exception:
        logger.exception("Failed to redeem Apple authorization code; account deletion won't be able to revoke.")
        return None


def store_tokens(social_account, token_data: dict) -> None:
    """Persist Apple's tokens on ``social_account`` the way allauth's web flow does.

    ``SocialToken.token_secret`` is where allauth's Apple adapter puts the refresh token, so a natively
    signed-in account is indistinguishable from a web one. Only overwrites the refresh token when Apple
    sent one: it isn't resent on every exchange, and clobbering it would disarm revocation.
    """
    from allauth.socialaccount.models import SocialToken

    if not token_data:
        return
    access_token = token_data.get("access_token") or ""
    refresh_token = token_data.get("refresh_token") or ""
    if not access_token and not refresh_token:
        return
    token, _ = SocialToken.objects.get_or_create(
        account=social_account,
        app=None,
        defaults={"token": access_token, "token_secret": refresh_token},
    )
    changed = []
    if access_token and token.token != access_token:
        token.token = access_token
        changed.append("token")
    if refresh_token and token.token_secret != refresh_token:
        token.token_secret = refresh_token
        changed.append("token_secret")
    if changed:
        token.save(update_fields=changed)


def revoke_account(social_account) -> bool:
    """Revoke the Apple grant behind ``social_account``; True if Apple accepted it.

    Prefers the refresh token, which is what actually ends the grant, and falls back to the access token
    for accounts stored before revocation was wired up. False (with a log line) when there is nothing to
    revoke or Apple refuses; the caller carries on deleting.
    """
    import requests
    from allauth.socialaccount.models import SocialToken

    if not revocation_configured():
        logger.warning(
            "Apple sign-in revocation is not configured (APPLE_SIGN_IN_TEAM_ID / KEY_ID / KEY_FILE); "
            "cannot revoke the grant for social account %s.",
            social_account.pk,
        )
        return False

    token = SocialToken.objects.filter(account=social_account).first()
    if token is None:
        logger.info("No stored Apple token for social account %s; nothing to revoke.", social_account.pk)
        return False
    if token.token_secret:
        value, hint = token.token_secret, "refresh_token"
    elif token.token:
        value, hint = token.token, "access_token"
    else:
        return False

    try:
        response = requests.post(
            APPLE_REVOKE_URL,
            data={
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "token": value,
                "token_type_hint": hint,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except Exception:
        logger.exception("Apple token revocation failed for social account %s.", social_account.pk)
        return False
    logger.info("Revoked Apple grant for social account %s (%s).", social_account.pk, hint)
    return True


def revoke_all_for_user(user) -> int:
    """Revoke every Apple grant this user holds; returns how many Apple accepted.

    Called before the ``SocialAccount`` and ``SocialToken`` rows are dropped: they are the only way to
    reach Apple.
    """
    from allauth.socialaccount.models import SocialAccount

    from auctions.mobile.services.social_auth import PROVIDER_APPLE

    revoked = 0
    for account in SocialAccount.objects.filter(user=user, provider=PROVIDER_APPLE):
        if revoke_account(account):
            revoked += 1
    return revoked
