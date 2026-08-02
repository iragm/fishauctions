"""Sign in with Apple: redeeming the authorization code, and revoking the grant on deletion.

Apple requires that an app offering Sign in with Apple also *revokes* the Apple token when the user
deletes their account — deletion that leaves the grant standing is incomplete by Apple's rules and
is an App Review item. Revoking needs a refresh token, and Apple only issues one in exchange for the
one-shot ``authorization_code`` the app receives at sign-in. So this module has two halves:

1. :func:`redeem_authorization_code` runs at sign-in, once, and stores what comes back on the
   ``SocialToken`` allauth keeps for the account (``token`` = access token, ``token_secret`` =
   refresh token — allauth's own layout for Apple, see ``AppleOAuth2Adapter.parse_token``).
2. :func:`revoke_account` runs from the account-deletion flow and calls Apple's ``/auth/revoke``.

Both need the team key (``APPLE_SIGN_IN_TEAM_ID`` / ``KEY_ID`` / ``KEY_FILE``). A deployment that
hasn't configured it can still *offer* native Apple sign-in — verifying an identity token needs only
Apple's public JWKS — but cannot revoke, so :func:`revocation_configured` exists to say so out loud
rather than have deletions quietly skip a step Apple checks for.

Everything here is best-effort at the call site: Apple being unreachable must never be what stops
someone's account from being deleted. Failures are logged loudly; the local deletion proceeds.
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

    Native sign-in is issued to the app's bundle id, so that's what the token endpoints expect —
    *not* the web Services ID, even though that's what allauth sends for the web flow. When only one
    is configured, it's the one to use.
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
    """Exchange Apple's one-shot ``authorization_code`` for tokens. ``None`` on any failure.

    Called at sign-in for the sole purpose of obtaining the refresh token that makes deletion-time
    revocation possible. Nothing about signing in depends on it: identity has already been proved by
    the identity token, so a failure here is logged and ignored rather than blocking the login.
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

    ``SocialToken.token_secret`` is where allauth's Apple adapter puts the refresh token, so a
    natively signed-in account ends up indistinguishable from a web one and
    :func:`revoke_account` needs no special case. Only overwrites the refresh token when Apple
    actually sent one — it isn't resent on every exchange, and clobbering it with an empty string
    would silently disarm revocation.
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
    """Revoke the Apple grant behind ``social_account``. True if Apple accepted the revocation.

    Prefers the refresh token, which is what Apple's docs call for and what actually ends the grant;
    falls back to the access token so an account stored before revocation was wired up still gets a
    best attempt. Returns False (with a log line) when there is nothing to revoke or Apple refuses —
    the caller carries on deleting either way.
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
    """Revoke every Apple grant this user holds. Returns how many Apple accepted.

    Called from account deletion, before the ``SocialAccount``/``SocialToken`` rows are dropped —
    the tokens are the only way to reach Apple, so once they're gone the grant can never be revoked.
    """
    from allauth.socialaccount.models import SocialAccount

    from auctions.mobile.services.social_auth import PROVIDER_APPLE

    revoked = 0
    for account in SocialAccount.objects.filter(user=user, provider=PROVIDER_APPLE):
        if revoke_account(account):
            revoked += 1
    return revoked
