"""Native social sign-in for the mobile app: verify a provider credential, then let allauth decide.

The app POSTs a native credential to ``/api/mobile/auth/social/``. After verification, finding,
connecting or creating the user and the email-verification gate are allauth's: this module must
never decide which local account to sign into. Each provider ends with
``provider.sociallogin_from_response()`` and ``complete_social_login()``, giving a JWT pair or a
pending web flow (:class:`PendingSocialLogin`).

================  =====================================  =========================================
Provider          What proves identity                   Email
================  =====================================  =========================================
Google            ID token signature + audience          Trusted when ``email_verified``
Apple             ID token signature + audience + nonce  Trusted when the *token* carries it
Facebook (iOS)    Limited Login JWT + audience + nonce   Never trusted — allauth confirms it
Facebook (Droid)  ``debug_token`` says it's our app      Never trusted — allauth confirms it
================  =====================================  =========================================

* **The nonce.** Apple and Facebook Limited Login tokens must carry ``sha256`` of the raw nonce the
  app sends us, or a captured token works anywhere.
* **Apple's name/email hints** arrive outside the token and are unauthenticated: a hint email is
  used only when the token has none, and never as verified.

Unlike the legacy ``/auth/google/``, a match on an unconfirmed local address isn't auto-verified:
allauth wipes that account's password and requires confirmation, locking out a squatter.
"""

from __future__ import annotations

import hashlib
import logging
import secrets

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# allauth's provider ids, so native and web flows share SocialAccount rows.
PROVIDER_APPLE = "apple"
PROVIDER_GOOGLE = "google"
PROVIDER_FACEBOOK = "facebook"
SUPPORTED_PROVIDERS = (PROVIDER_APPLE, PROVIDER_GOOGLE, PROVIDER_FACEBOOK)

# The app watches for this exact path; changing it needs an app release.
SOCIAL_DONE_PATH = "/api/mobile/auth/social/done/"

# Single-use either way.
PENDING_TTL_SECONDS = 15 * 60

_PENDING_PREFIX = "mobile_social_pending:"
_CONTINUE_PREFIX = "mobile_social_continue:"

# Tells the done view which pending record to bind.
PENDING_TOKEN_SESSION_KEY = "mobile_social_pending_token"


class SocialAuthError(Exception):
    pass


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _check_nonce(raw_nonce: str, claims: dict) -> None:
    """Reject unless the token's ``nonce`` claim is ``sha256`` of this request's raw nonce. A missing
    claim is rejected too.
    """
    token_nonce = claims.get("nonce")
    if not token_nonce:
        msg = "Token is missing its nonce."
        raise SocialAuthError(msg)
    if not raw_nonce:
        msg = "A nonce is required for this provider."
        raise SocialAuthError(msg)
    if not secrets.compare_digest(str(token_nonce), _sha256_hex(raw_nonce)):
        msg = "Nonce mismatch."
        raise SocialAuthError(msg)


def _get_provider(request, provider_id: str):
    """The allauth provider instance (with its configured app), or a 401-shaped error."""
    from allauth.socialaccount.adapter import get_adapter

    try:
        return get_adapter().get_provider(request, provider_id)
    except Exception as exc:
        # Usually SocialApp.DoesNotExist: the provider isn't configured.
        logger.warning("Social provider %s is not configured on this deployment.", provider_id, exc_info=exc)
        msg = f"{provider_id} sign-in is not configured."
        raise SocialAuthError(msg) from exc


# ---------------------------------------------------------------------------
# Per-provider verification
# ---------------------------------------------------------------------------


def _verify_google(request, data: dict):
    """Verify a Google ID token like ``/auth/google/``: audience-bound, unverified email rejected, no nonce."""
    id_token = data.get("id_token")
    if not id_token:
        msg = "id_token is required for Google."
        raise SocialAuthError(msg)
    client_id = settings.GOOGLE_OAUTH_CLIENT_ID
    if not client_id:
        msg = "Google sign-in is not configured."
        raise SocialAuthError(msg)

    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    try:
        claims = google_id_token.verify_oauth2_token(
            id_token,
            google_requests.Request(),
            audience=client_id,
        )
    except ValueError as exc:
        logger.warning("Google ID token verification failed.", exc_info=exc)
        msg = "Invalid ID token."
        raise SocialAuthError(msg) from exc

    if not claims.get("email_verified"):
        msg = "Google account email is not verified."
        raise SocialAuthError(msg)

    provider = _get_provider(request, PROVIDER_GOOGLE)
    return provider.sociallogin_from_response(request, claims)


def _verify_apple(request, data: dict):
    """Verify a native Sign in with Apple identity token.

    The audience is the app's bundle id, not the web Services ID; ``APPLE_ALLOWED_AUDIENCES`` accepts
    both.
    """
    from allauth.socialaccount.providers.apple.views import AppleOAuth2Adapter

    id_token = data.get("id_token")
    if not id_token:
        msg = "id_token is required for Apple."
        raise SocialAuthError(msg)

    provider = _get_provider(request, PROVIDER_APPLE)
    try:
        # Signature, issuer, audience, expiry and jti replay; raises on failure.
        claims = AppleOAuth2Adapter.get_verified_identity_data(provider, id_token)
    except Exception as exc:
        logger.warning("Apple identity token verification failed.", exc_info=exc)
        msg = "Invalid ID token."
        raise SocialAuthError(msg) from exc

    _check_nonce(data.get("nonce", ""), claims)

    response = dict(claims)
    _apply_apple_first_authorization_hints(response, data)
    return provider.sociallogin_from_response(request, response)


def _apply_apple_first_authorization_hints(response: dict, data: dict) -> None:
    """Fold Apple's one-time name and email into the provider response, without trusting them.

    Apple sends them only on first authorization, so store them now. The name is kept. The email is
    used only when the token has none, and never marked verified, so it goes through allauth's
    ordinary confirmation.
    """
    first_name = (data.get("first_name") or "").strip()
    last_name = (data.get("last_name") or "").strip()
    if first_name or last_name:
        # The shape AppleProvider.extract_common_fields reads.
        response["name"] = {"firstName": first_name, "lastName": last_name}

    if response.get("email"):
        return
    hint_email = (data.get("email") or "").strip()
    if not hint_email:
        return
    response["email"] = hint_email
    # Explicitly unverified.
    response["email_verified"] = False


def _verify_facebook(request, data: dict):
    """Verify a Facebook credential in either shape the app sends.

    * ``id_token``: Limited Login (iOS without tracking consent). JWKS signature, issuer, audience,
      nonce.
    * ``access_token``: classic (Android), via ``inspect_token``, which must check ``data.app_id`` or
      any Facebook app's token would be accepted.

    Neither produces a trusted email.
    """
    from allauth.socialaccount.providers.facebook import flows as facebook_flows

    provider = _get_provider(request, PROVIDER_FACEBOOK)
    id_token = data.get("id_token")
    access_token = data.get("access_token")

    if id_token:
        from allauth.socialaccount.internal import jwtkit

        try:
            claims = jwtkit.verify_and_decode(
                credential=id_token,
                keys_url=provider.limited_login_jwks_url,
                issuer=provider.limited_login_expected_jwt_issuer,
                audience=provider.app.client_id,
                lookup_kid=jwtkit.lookup_kid_jwk,
            )
        except Exception as exc:
            logger.warning("Facebook Limited Login token verification failed.", exc_info=exc)
            msg = "Invalid ID token."
            raise SocialAuthError(msg) from exc
        # allauth's verify_limited_login_token drops the nonce, so check the raw claims first.
        _check_nonce(data.get("nonce", ""), claims)
        fake_response = {
            graph_field: claims[jwt_field]
            for jwt_field, graph_field in facebook_flows.JWT_FIELD_TO_GRAPH_API_FIELD_MAP.items()
            if jwt_field in claims
        }
        return provider.sociallogin_from_response(request, fake_response)

    if access_token:
        try:
            return facebook_flows.verify_token(request, provider, access_token)
        except Exception as exc:
            logger.warning("Facebook access token verification failed.", exc_info=exc)
            msg = "Invalid access token."
            raise SocialAuthError(msg) from exc

    msg = "id_token or access_token is required for Facebook."
    raise SocialAuthError(msg)


_VERIFIERS = {
    PROVIDER_GOOGLE: _verify_google,
    PROVIDER_APPLE: _verify_apple,
    PROVIDER_FACEBOOK: _verify_facebook,
}


def build_sociallogin(request, data: dict):
    """Verify the credential in ``data`` and return an unsaved allauth ``SocialLogin``.

    Its ``state`` points at the mobile completion path. Raises :class:`SocialAuthError`.
    """
    provider_id = (data.get("provider") or "").strip().lower()
    if provider_id not in _VERIFIERS:
        msg = "Unsupported provider."
        raise SocialAuthError(msg)
    sociallogin = _VERIFIERS[provider_id](request, data)
    sociallogin.state["process"] = "login"
    sociallogin.state["next"] = SOCIAL_DONE_PATH
    return sociallogin


# ---------------------------------------------------------------------------
# Pending logins (the web continuation)
# ---------------------------------------------------------------------------


class PendingSocialLogin:
    """A social login allauth couldn't finish unattended, parked for the web to finish.

    1. :meth:`create` stores the serialized ``SocialLogin`` and any resolved user under a
       ``pending_token``, plus a single-use continue token for the WebView URL.
    2. :meth:`consume_continue_token` burns it so the continue view can rebuild the flow.
    3. :meth:`bind_user` records who signed in; :func:`resolve_completed_user` re-checks everything
       before the app gets a JWT. The record is never what authorizes it.
    """

    @staticmethod
    def create(*, provider: str, uid: str, serialized_login: dict | None, user_pk: int | None) -> tuple[str, str]:
        """Store a pending login. Returns ``(pending_token, continue_token)``."""
        pending_token = secrets.token_urlsafe(32)
        continue_token = secrets.token_urlsafe(32)
        record = {
            "provider": provider,
            "uid": uid,
            "sociallogin": serialized_login,
            # Resolved but not yet signed in (usually an unconfirmed address), so a retry can finish.
            "user_pk": user_pk,
            "completed_user_pk": None,
        }
        cache.set(_PENDING_PREFIX + pending_token, record, timeout=PENDING_TTL_SECONDS)
        cache.set(_CONTINUE_PREFIX + continue_token, pending_token, timeout=PENDING_TTL_SECONDS)
        return pending_token, continue_token

    @staticmethod
    def consume_continue_token(continue_token: str) -> tuple[str, dict] | None:
        """Burn a continue token atomically, returning ``(pending_token, record)`` or ``None``. The delete
        enforces single use.
        """
        if not continue_token:
            return None
        key = _CONTINUE_PREFIX + continue_token
        pending_token = cache.get(key)
        if pending_token is None or not cache.delete(key):
            return None
        record = cache.get(_PENDING_PREFIX + pending_token)
        if record is None:
            return None
        return pending_token, record

    @staticmethod
    def get(pending_token: str) -> dict | None:
        if not pending_token:
            return None
        return cache.get(_PENDING_PREFIX + pending_token)

    @staticmethod
    def bind_user(pending_token: str, user_pk: int) -> None:
        """Record the user the web flow signed in. First writer wins."""
        record = PendingSocialLogin.get(pending_token)
        if record is None or record.get("completed_user_pk"):
            return
        record["completed_user_pk"] = user_pk
        cache.set(_PENDING_PREFIX + pending_token, record, timeout=PENDING_TTL_SECONDS)

    @staticmethod
    def discard(pending_token: str) -> None:
        cache.delete(_PENDING_PREFIX + pending_token)


def resolve_completed_user(pending_token: str):
    """The user a finished continuation belongs to, or ``None`` if unfinished or unsafe.

    Identity comes from the ``SocialAccount`` for the verified ``(provider, uid)``, not the cached pk,
    so a user who confirmed their email later can finish with a plain retry. The web login's gates are
    re-checked from scratch.
    """
    from allauth.socialaccount.models import SocialAccount

    from auctions.mobile.services.auth import MobileAuthService

    record = PendingSocialLogin.get(pending_token)
    if record is None:
        return None
    account = (
        SocialAccount.objects.filter(provider=record["provider"], uid=record["uid"]).select_related("user").first()
    )
    if account is None:
        return None
    user = account.user
    # A SocialAccount must not have moved between users mid-flow.
    expected_pk = record.get("completed_user_pk") or record.get("user_pk")
    if expected_pk and expected_pk != user.pk:
        logger.warning("Pending social login %s resolved to an unexpected user; refusing.", record["provider"])
        return None
    if not user.is_active:
        return None
    if not MobileAuthService.email_verification_satisfied(user):
        return None
    return user
