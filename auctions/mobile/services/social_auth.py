"""Native social sign-in for the mobile app: verify a provider credential, then let allauth decide.

The app obtains a credential natively (Sign in with Apple, Google Sign-In, Facebook Login) and POSTs
it to ``/api/mobile/auth/social/``. Everything after verification — finding an existing user,
connecting to one, creating one, the email-verification gate — is allauth's, not ours. That is the
whole design:

    Verifying a token proves *which provider account* is calling. It says nothing about which local
    account that should sign into. Deciding that is where account-takeover bugs live, and allauth
    already does it, using settings this deployment has run in production for the web flow. The one
    thing this module must never do is decide it a second time, differently.

So each provider path ends the same way: build the provider's response dict, hand it to
``provider.sociallogin_from_response()``, and hand the result to ``complete_social_login()``. What
comes back is either a signed-in session (→ a JWT pair) or an unfinished flow the user has to
finish on the web (→ a pending token and a URL; see :class:`PendingSocialLogin`).

Trust boundaries, provider by provider
--------------------------------------
================  =====================================  =========================================
Provider          What proves identity                   Email
================  =====================================  =========================================
Google            ID token signature + audience          Trusted when ``email_verified``
Apple             ID token signature + audience + nonce  Trusted when the *token* carries it
Facebook (iOS)    Limited Login JWT + audience + nonce   Never trusted — allauth confirms it
Facebook (Droid)  ``debug_token`` says it's our app      Never trusted — allauth confirms it
================  =====================================  =========================================

Two rules that are easy to get wrong, enforced here rather than left to a comment:

* **The nonce.** Apple and Facebook Limited Login tokens are bound to a nonce the app generated:
  the app sends ``sha256(raw)`` to the provider and the raw value to us, and we reject unless they
  match. Without it a captured ID token is a working credential from any app or any session.
* **Apple's name/email hints.** Apple returns the user's name and email exactly once, on the first
  authorization, *outside* the token — so the app forwards them as plain request fields. They are
  unauthenticated: identity comes from the token's ``sub``, and a hint email is used only when the
  verified token carries none, always as an *unverified* address. Treating a caller-supplied
  address as verified would let anyone sign in as anyone.

One deliberate difference from the legacy ``/auth/google/`` endpoint
--------------------------------------------------------------------
When a provider's verified address matches a *local* account whose own address was never confirmed,
the old endpoint flipped that address to verified and signed the person straight in. allauth instead
wipes the local account's password (``wipe_password``) and still requires the address to be
confirmed. That is the better behaviour and the reason not to hand-roll this: the unconfirmed local
account may have been opened by someone who typed in a stranger's address and knows its password,
waiting for the real owner to arrive. Auto-verifying leaves both of them with access; allauth locks
the squatter out and costs the real owner one confirmation email. It is also what the website has
always done, so the app and the web now agree.
"""

from __future__ import annotations

import hashlib
import logging
import secrets

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# allauth's own provider ids, so the native and web flows write identical SocialAccount.provider
# values and converge on one account with no mapping table.
PROVIDER_APPLE = "apple"
PROVIDER_GOOGLE = "google"
PROVIDER_FACEBOOK = "facebook"
SUPPORTED_PROVIDERS = (PROVIDER_APPLE, PROVIDER_GOOGLE, PROVIDER_FACEBOOK)

# Where the web continuation sends the browser when it's finished. The app watches for this exact
# path to know the flow is over (AllauthWebScreen.defaultSocialCompletionPath), so changing it needs
# an app release — don't.
SOCIAL_DONE_PATH = "/api/mobile/auth/social/done/"

# Long enough to fill in a signup form and read a confirmation email in another app, short enough
# that an unused one isn't left lying around. Single-use either way.
PENDING_TTL_SECONDS = 15 * 60

_PENDING_PREFIX = "mobile_social_pending:"
_CONTINUE_PREFIX = "mobile_social_continue:"

# Holds the pending token in the WebView's session while the user finishes on the web, so the done
# view knows which record to bind the resulting user to.
PENDING_TOKEN_SESSION_KEY = "mobile_social_pending_token"


class SocialAuthError(Exception):
    """The credential could not be verified, or isn't one we accept. Surfaces as a 401."""


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _check_nonce(raw_nonce: str, claims: dict) -> None:
    """Reject unless the token was minted for the nonce this request carries.

    The app sends the provider ``sha256(raw)`` and sends us ``raw``; a token captured from another
    session or another app was minted against a different nonce and fails here. Apple and Facebook
    Limited Login both echo the hashed value back in the ``nonce`` claim.

    A token carrying no nonce claim at all is rejected just as firmly — accepting it would let a
    caller opt out of replay protection simply by stripping the claim.
    """
    token_nonce = claims.get("nonce")
    if not token_nonce:
        msg = "Token is missing its nonce."
        raise SocialAuthError(msg)
    if not raw_nonce:
        msg = "A nonce is required for this provider."
        raise SocialAuthError(msg)
    # Neither side is a secret, so constant time isn't required here; it costs nothing and keeps the
    # habit intact for the places where it does matter.
    if not secrets.compare_digest(str(token_nonce), _sha256_hex(raw_nonce)):
        msg = "Nonce mismatch."
        raise SocialAuthError(msg)


def _get_provider(request, provider_id: str):
    """The allauth provider instance (with its configured app), or a 401-shaped error."""
    from allauth.socialaccount.adapter import get_adapter

    try:
        return get_adapter().get_provider(request, provider_id)
    except Exception as exc:
        # Almost always SocialApp.DoesNotExist: this deployment hasn't configured the provider.
        logger.warning("Social provider %s is not configured on this deployment.", provider_id, exc_info=exc)
        msg = f"{provider_id} sign-in is not configured."
        raise SocialAuthError(msg) from exc


# ---------------------------------------------------------------------------
# Per-provider verification
# ---------------------------------------------------------------------------


def _verify_google(request, data: dict):
    """Verify a Google ID token exactly the way the long-standing ``/auth/google/`` path does.

    Same library, same audience, same "reject an unverified email" rule — only what happens *after*
    verification changed (allauth now owns it). Google binds the token to us by audience, so there
    is no nonce in this flow.
    """
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

    The audience is the *app's bundle id*, not the web Services ID — the single most common way a
    native Apple integration fails, because the two are different strings and the web flow uses the
    other one. Both are accepted (``APPLE_ALLOWED_AUDIENCES``, wired into the provider app's
    comma-separated ``client_id``), so one deployment serves both flows.
    """
    from allauth.socialaccount.providers.apple.views import AppleOAuth2Adapter

    id_token = data.get("id_token")
    if not id_token:
        msg = "id_token is required for Apple."
        raise SocialAuthError(msg)

    provider = _get_provider(request, PROVIDER_APPLE)
    try:
        # Signature against Apple's JWKS, plus issuer, audience and expiry, plus jti replay
        # blacklisting. Raises OAuth2Error (or a requests error) on anything it doesn't like.
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
    """Fold Apple's one-time name/email into the provider response, without ever trusting them.

    Apple sends ``email``, ``given_name`` and ``family_name`` only on the very first authorization
    for a given Apple ID + app, and outside the identity token. Every later sign-in carries the
    ``sub`` and nothing else, so if they aren't stored now they're unrecoverable short of the user
    revoking the app in their Apple Account settings. The app forwards them precisely so they can
    be stored.

    The name is free text either way and safe to keep. The email is the dangerous one, so:

    * it is used **only** when the verified token carries no email of its own, and
    * it is never marked verified, whatever the request says.

    A hint address therefore goes through allauth's ordinary confirmation: unique and unclaimed →
    an account that cannot sign in until the address is confirmed; already claimed → allauth's
    enumeration-prevention path, which creates nothing and signs nobody in. That is exactly what a
    caller-supplied address should be worth.
    """
    first_name = (data.get("first_name") or "").strip()
    last_name = (data.get("last_name") or "").strip()
    if first_name or last_name:
        # The shape AppleProvider.extract_common_fields reads, and the shape allauth's own web flow
        # builds from Apple's `user` form field.
        response["name"] = {"firstName": first_name, "lastName": last_name}

    if response.get("email"):
        return
    hint_email = (data.get("email") or "").strip()
    if not hint_email:
        return
    response["email"] = hint_email
    # Belt and braces: the token carried no email, so nothing here is attested. Force the claim off
    # rather than merely leaving it absent, in case a provider default ever flips.
    response["email_verified"] = False


def _verify_facebook(request, data: dict):
    """Verify a Facebook credential, in either of the two shapes the app can produce.

    * ``id_token`` — Limited Login, which iOS uses when App Tracking Transparency consent is denied.
      An OIDC JWT: signature against Facebook's JWKS, issuer, and audience = our app id. Nonce-bound.
    * ``access_token`` — the classic flow (Android). Verified through Facebook's ``debug_token``
      endpoint by allauth's ``inspect_token``; the part that matters is that it checks
      ``data.app_id`` against ours. Skipping that check accepts a token minted for *any* Facebook
      app, which is a complete authentication bypass rather than a missing nicety.

    Neither shape produces a trusted email: Facebook does not attest that a profile address is
    confirmed, and frequently supplies none at all.
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
        # allauth's own verify_limited_login_token drops the nonce when it maps claims onto a fake
        # Graph response, so the check happens here, against the raw claims, before that mapping.
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

    The returned login's ``state`` points at the mobile completion path, so that when the flow has
    to detour through the web (signup form, email confirmation) it comes back somewhere the app is
    watching for. Raises :class:`SocialAuthError` for anything that doesn't verify.
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
    """A social login allauth couldn't finish unattended, parked so the web can finish it.

    Rather than reimplement the signup form and the email-confirmation gate natively, the app hands
    the user to the real web flow and picks the result back up. Three server-side pieces:

    1. :meth:`create` stores the unfinished state (allauth's own serialized ``SocialLogin``, plus
       the user it already resolved to, if any) under an opaque ``pending_token``, and mints a
       *second* single-use token for the URL the WebView loads. The URL needs its own credential
       because the WebView has neither a JWT nor a session yet.
    2. :meth:`consume_continue_token` burns that second token, so the continue view can rebuild the
       flow in the WebView's own session.
    3. :meth:`bind_user` records who the web flow signed in, and :func:`resolve_completed_user`
       hands that back to the app — after re-checking from scratch that the user is active, has a
       verified email, and really is connected to the provider account this record was made for.
       The record carries the flow between requests; it is never what authorizes the JWT.
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
            # Set when allauth already resolved the provider account to a real user but couldn't
            # sign them in yet (almost always: their address still needs confirming). Lets the app
            # finish with a plain retry once they've clicked the link in their inbox, with no
            # WebView round trip at all.
            "user_pk": user_pk,
            "completed_user_pk": None,
        }
        cache.set(_PENDING_PREFIX + pending_token, record, timeout=PENDING_TTL_SECONDS)
        cache.set(_CONTINUE_PREFIX + continue_token, pending_token, timeout=PENDING_TTL_SECONDS)
        return pending_token, continue_token

    @staticmethod
    def consume_continue_token(continue_token: str) -> tuple[str, dict] | None:
        """Atomically burn a continue token, returning ``(pending_token, record)`` or ``None``.

        Single-use is enforced by the delete, not the read: only one caller wins the delete, so two
        requests racing on the same URL can't both start the flow.
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
    """The user a finished continuation belongs to, or ``None`` if it isn't finished (or isn't safe).

    Identity comes from the ``SocialAccount`` row for the ``(provider, uid)`` the record was created
    for — the pair that was cryptographically verified when the flow started. Not from the cached
    primary key: the record says *which flow*, the database says *who*. If that row doesn't exist,
    the flow never completed and there is nobody to sign in.

    That also makes the common case work without any WebView round trip at all. A user who signs up
    through allauth's form, closes the app and later clicks the confirmation link in their inbox has
    completed everything the site needs; the next retry finds the account and hands over tokens.

    The gates below are the same ones the web and password logins apply, re-checked here from
    scratch so this endpoint can't become the weakest door into an account.
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
    # If the flow already named a user, it has to still be the same one. Nothing should be able to
    # move a SocialAccount between users mid-flow; refusing is the right answer if something did.
    expected_pk = record.get("completed_user_pk") or record.get("user_pk")
    if expected_pk and expected_pk != user.pk:
        logger.warning("Pending social login %s resolved to an unexpected user; refusing.", record["provider"])
        return None
    if not user.is_active:
        return None
    # An unconfirmed address can't sign in on the web, so it can't sign in here either.
    if not MobileAuthService.email_verification_satisfied(user):
        return None
    return user
