"""Tests for native social sign-in (Apple, Google, Facebook).

Mostly about the ways it must refuse: no takeover by claiming somebody else's address (Apple's
unauthenticated hint, Facebook's unverified profile email), no account without a verified email, no
replayed provider token (the nonce), and no Facebook token minted for another app.

Only the provider calls are mocked; the nonce check, the hint rules and the allauth pipeline run
for real.
"""

import datetime
from types import SimpleNamespace
from unittest.mock import patch

from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount, SocialToken
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from auctions.mobile.services.social_auth import (
    PendingSocialLogin,
    _sha256_hex,
    resolve_completed_user,
)
from auctions.test_support import isolated_cache

APPLE_BUNDLE_ID = "com.fishauctions.app"
APPLE_SERVICES_ID = "fish.auction.signin"
FACEBOOK_APP_ID = "1234567890"

SOCIAL_PROVIDERS = {
    "google": {"SCOPE": ["profile", "email"]},
    "apple": {
        "APP": {
            "client_id": f"{APPLE_SERVICES_ID},{APPLE_BUNDLE_ID}",
            "secret": "KEYID12345",
            "key": "TEAMID1234",
            "settings": {"certificate_key": "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----"},
        }
    },
    "facebook": {
        "APP": {
            "client_id": FACEBOOK_APP_ID,
            "secret": "fb-secret",
            "settings": {"email_authentication": False, "verified_email": False},
        },
        "METHOD": "oauth2",
        "SCOPE": ["email", "public_profile"],
    },
}

RAW_NONCE = "a-32-byte-random-value-from-the-app"
HASHED_NONCE = _sha256_hex(RAW_NONCE)


@isolated_cache("mobile-social-auth")
@override_settings(
    SOCIALACCOUNT_PROVIDERS=SOCIAL_PROVIDERS,
    GOOGLE_OAUTH_CLIENT_ID="test-client.apps.googleusercontent.com",
    APPLE_SIGN_IN_SERVICES_ID=APPLE_SERVICES_ID,
    APPLE_SIGN_IN_BUNDLE_ID=APPLE_BUNDLE_ID,
    APPLE_ALLOWED_AUDIENCES=[APPLE_SERVICES_ID, APPLE_BUNDLE_ID],
    FACEBOOK_APP_ID=FACEBOOK_APP_ID,
    FACEBOOK_APP_SECRET="fb-secret",
)
class SocialAuthTestCase(TestCase):
    """Shared plumbing: the endpoint URLs and one mocked call per provider."""

    def setUp(self):
        # The throttle and pending-token records live in the shared cache.
        cache.clear()
        self.url = reverse("mobile-auth-social")
        self.complete_url = reverse("mobile-auth-social-complete")
        self.continue_url = reverse("mobile-auth-social-continue")
        self.done_url = reverse("mobile-auth-social-done")

    # -- provider stubs ----------------------------------------------------
    # Each patches only the call that talks to the provider.

    def apple_claims(self, sub="apple-sub-1", email=None, email_verified=True, nonce=HASHED_NONCE):
        claims = {"sub": sub, "aud": APPLE_BUNDLE_ID, "iss": "https://appleid.apple.com"}
        if nonce is not None:
            claims["nonce"] = nonce
        if email:
            claims["email"] = email
            claims["email_verified"] = email_verified
        return claims

    def post_apple(self, claims, **body):
        payload = {"provider": "apple", "id_token": "stub", "nonce": RAW_NONCE, **body}
        with patch(
            "allauth.socialaccount.providers.apple.views.AppleOAuth2Adapter.get_verified_identity_data",
            return_value=claims,
        ):
            return self.client.post(self.url, payload, content_type="application/json")

    def post_google(self, claims, **body):
        payload = {"provider": "google", "id_token": "stub", **body}
        with patch("google.oauth2.id_token.verify_oauth2_token", return_value=claims):
            return self.client.post(self.url, payload, content_type="application/json")

    def post_facebook_limited(self, claims, **body):
        payload = {"provider": "facebook", "id_token": "stub", "nonce": RAW_NONCE, **body}
        with patch("allauth.socialaccount.internal.jwtkit.verify_and_decode", return_value=claims):
            return self.client.post(self.url, payload, content_type="application/json")

    def post_facebook_classic(self, graph_me, token_app_id=FACEBOOK_APP_ID, **body):
        """The Android path: a classic access token verified through Facebook's Graph API.

        Only the HTTP responses are faked, so allauth's real ``inspect_token`` runs -- the app-id check
        inside it is the property being tested. ``token_app_id`` is the app Facebook says minted the token.
        """
        payload = {"provider": "facebook", "access_token": "stub", **body}
        responses = {
            "oauth/access_token": {"access_token": "app-token"},
            "debug_token": {"data": {"is_valid": True, "app_id": token_app_id}},
            "/me": graph_me,
        }

        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get(self, url, params=None, **kwargs):
                body = next(v for k, v in responses.items() if k in url)
                return SimpleNamespace(json=lambda: body, raise_for_status=lambda: None)

        with patch(
            "allauth.socialaccount.adapter.DefaultSocialAccountAdapter.get_requests_session",
            return_value=FakeSession(),
        ):
            return self.client.post(self.url, payload, content_type="application/json")


class AppleSignInTests(SocialAuthTestCase):
    def test_new_user_with_verified_apple_email_is_signed_in(self):
        resp = self.post_apple(self.apple_claims(email="new@example.com"))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("access", resp.json())
        user = User.objects.get(email="new@example.com")
        self.assertTrue(EmailAddress.objects.get(user=user, email="new@example.com").verified)
        self.assertTrue(SocialAccount.objects.filter(user=user, provider="apple", uid="apple-sub-1").exists())

    def test_returning_user_is_matched_by_sub_not_by_anything_the_request_says(self):
        first = self.post_apple(self.apple_claims(email="repeat@example.com"))
        self.assertEqual(first.status_code, 200, first.content)
        user = User.objects.get(email="repeat@example.com")
        # Later sign-ins carry only the sub.
        again = self.post_apple(self.apple_claims())
        self.assertEqual(again.status_code, 200, again.content)
        self.assertEqual(User.objects.filter(pk=user.pk).count(), 1)
        self.assertEqual(SocialAccount.objects.filter(provider="apple").count(), 1)

    def test_first_authorization_name_is_stored(self):
        # Apple sends the name once, outside the token.
        resp = self.post_apple(
            self.apple_claims(email="named@example.com"),
            first_name="Ada",
            last_name="Lovelace",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        user = User.objects.get(email="named@example.com")
        self.assertEqual(user.first_name, "Ada")
        self.assertEqual(user.last_name, "Lovelace")

    def test_hint_email_cannot_take_over_an_existing_account(self):
        """A token with no email plus somebody else's address in the body must sign nobody in and leave the
        victim's account untouched: Apple's ``email`` request field is unauthenticated.
        """
        victim = User.objects.create_user("victim", "victim@example.com", "pw")
        EmailAddress.objects.create(user=victim, email="victim@example.com", verified=True, primary=True)

        resp = self.post_apple(self.apple_claims(sub="attacker-sub"), email="victim@example.com")

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertNotIn("access", resp.json())
        self.assertNotIn("refresh", resp.json())
        # No link to the victim, and no second account holding their address.
        self.assertFalse(SocialAccount.objects.filter(user=victim).exists())
        self.assertEqual(User.objects.filter(email="victim@example.com").count(), 1)
        # And the pending record can never be completed into their account.
        self.assertIsNone(resolve_completed_user(resp.json()["pending_token"]))

    def test_hint_email_is_never_marked_verified(self):
        """An unclaimed hint address may seed an account, but only an unconfirmed one."""
        resp = self.post_apple(self.apple_claims(sub="hint-sub"), email="hinted@example.com")
        self.assertEqual(resp.status_code, 200, resp.content)
        # Not signed in: mandatory verification blocks it.
        self.assertNotIn("access", resp.json())
        address = EmailAddress.objects.filter(email="hinted@example.com").first()
        if address is not None:
            self.assertFalse(address.verified)

    def test_token_email_wins_over_a_conflicting_hint(self):
        resp = self.post_apple(self.apple_claims(sub="both-sub", email="real@example.com"), email="fake@example.com")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("access", resp.json())
        self.assertTrue(User.objects.filter(email="real@example.com").exists())
        self.assertFalse(EmailAddress.objects.filter(email="fake@example.com").exists())

    def test_nonce_mismatch_is_rejected(self):
        with patch(
            "allauth.socialaccount.providers.apple.views.AppleOAuth2Adapter.get_verified_identity_data",
            return_value=self.apple_claims(email="nonce@example.com", nonce=_sha256_hex("a-different-nonce")),
        ):
            resp = self.client.post(
                self.url,
                {"provider": "apple", "id_token": "stub", "nonce": RAW_NONCE},
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(User.objects.filter(email="nonce@example.com").exists())

    def test_token_without_a_nonce_claim_is_rejected(self):
        # Otherwise stripping the claim would opt out of replay protection.
        resp = self.post_apple(self.apple_claims(email="nononce@example.com", nonce=None))
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(User.objects.filter(email="nononce@example.com").exists())

    def test_request_without_a_nonce_is_rejected(self):
        with patch(
            "allauth.socialaccount.providers.apple.views.AppleOAuth2Adapter.get_verified_identity_data",
            return_value=self.apple_claims(email="x@example.com"),
        ):
            resp = self.client.post(
                self.url, {"provider": "apple", "id_token": "stub"}, content_type="application/json"
            )
        self.assertEqual(resp.status_code, 401)

    def test_unverifiable_token_is_rejected(self):
        with patch(
            "allauth.socialaccount.providers.apple.views.AppleOAuth2Adapter.get_verified_identity_data",
            side_effect=ValueError("bad signature"),
        ):
            resp = self.client.post(
                self.url,
                {"provider": "apple", "id_token": "stub", "nonce": RAW_NONCE},
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 401)

    def test_private_relay_address_is_accepted_as_verified(self):
        relay = "abc123@privaterelay.appleid.com"
        resp = self.post_apple(self.apple_claims(sub="relay-sub", email=relay))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("access", resp.json())
        self.assertTrue(EmailAddress.objects.get(email=relay).verified)

    def test_apple_email_verified_matches_an_existing_verified_local_account(self):
        """An address Apple did attest matches an existing verified account
        (SOCIALACCOUNT_EMAIL_AUTHENTICATION).
        """
        existing = User.objects.create_user("existing", "existing@example.com", "pw")
        EmailAddress.objects.create(user=existing, email="existing@example.com", verified=True, primary=True)

        resp = self.post_apple(self.apple_claims(sub="match-sub", email="existing@example.com"))

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("access", resp.json())
        self.assertTrue(SocialAccount.objects.filter(user=existing, provider="apple", uid="match-sub").exists())


class GoogleSignInTests(SocialAuthTestCase):
    """Google, which already worked: regression guards."""

    def google_claims(self, sub="google-sub-1", email="g@example.com", email_verified=True):
        return {
            "sub": sub,
            "email": email,
            "email_verified": email_verified,
            "given_name": "Grace",
            "family_name": "Hopper",
        }

    def test_new_google_user_is_signed_in(self):
        resp = self.post_google(self.google_claims())
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("access", resp.json())
        user = User.objects.get(email="g@example.com")
        self.assertTrue(EmailAddress.objects.get(user=user, email="g@example.com").verified)

    def test_existing_google_socialaccount_signs_in_to_the_same_account(self):
        """Accounts created by the old /auth/google/ endpoint keep working."""
        user = User.objects.create_user("oldgoogle", "old@example.com", "pw")
        EmailAddress.objects.create(user=user, email="old@example.com", verified=True, primary=True)
        SocialAccount.objects.create(user=user, provider="google", uid="google-sub-legacy")

        resp = self.post_google(self.google_claims(sub="google-sub-legacy", email="old@example.com"))

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("access", resp.json())
        self.assertEqual(User.objects.filter(username="oldgoogle").count(), 1)
        self.assertEqual(SocialAccount.objects.filter(provider="google").count(), 1)

    def test_squatted_unconfirmed_account_locks_out_the_squatter(self):
        """An unconfirmed account squatting on the owner's address: allauth wipes the password and still
        requires confirmation, so the squatter loses access and the owner confirms.
        """
        squatter_account = User.objects.create_user("squatter", "owner@example.com", "squatterpassword")
        EmailAddress.objects.create(user=squatter_account, email="owner@example.com", verified=False, primary=True)

        resp = self.post_google(self.google_claims(sub="owner-sub", email="owner@example.com"))

        self.assertEqual(resp.status_code, 200, resp.content)
        # Not signed in yet — the address still has to be confirmed.
        self.assertNotIn("access", resp.json())
        squatter_account.refresh_from_db()
        self.assertFalse(squatter_account.has_usable_password())
        self.assertFalse(
            self.client.login(username="squatter", password="squatterpassword"),
            "the squatter's password must no longer work",
        )

    def test_unverified_google_email_is_rejected(self):
        resp = self.post_google(self.google_claims(email_verified=False))
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(User.objects.filter(email="g@example.com").exists())

    def test_works_without_a_socialapp_row(self):
        """The mobile Google flow needs only GOOGLE_OAUTH_CLIENT_ID, with no SocialApp row
        (auctions.social_adapter supplies the fallback).
        """
        self.assertFalse(
            SocialAccount.objects.filter(provider="google").exists()
        )  # no fixtures; the app comes from settings/adapter
        resp = self.post_google(self.google_claims(sub="nofixture", email="nofixture@example.com"))
        self.assertEqual(resp.status_code, 200, resp.content)


class FacebookSignInTests(SocialAuthTestCase):
    def limited_claims(self, sub="fb-sub-1", email=None, nonce=HASHED_NONCE):
        claims = {"sub": sub, "name": "Fred Book", "given_name": "Fred", "family_name": "Book"}
        if nonce is not None:
            claims["nonce"] = nonce
        if email:
            claims["email"] = email
        return claims

    def test_facebook_email_is_never_verified_so_login_is_not_completed(self):
        """Facebook never attests an address, so no verified EmailAddress is created and allauth's
        confirmation email is the gate.
        """
        resp = self.post_facebook_limited(self.limited_claims(email="fb@example.com"))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertNotIn("access", resp.json())
        self.assertIn("pending_token", resp.json())
        address = EmailAddress.objects.filter(email="fb@example.com").first()
        if address is not None:
            self.assertFalse(address.verified)

    def test_facebook_cannot_claim_an_existing_account_by_email(self):
        victim = User.objects.create_user("fbvictim", "fbvictim@example.com", "pw")
        EmailAddress.objects.create(user=victim, email="fbvictim@example.com", verified=True, primary=True)

        resp = self.post_facebook_limited(self.limited_claims(sub="fb-attacker", email="fbvictim@example.com"))

        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertNotIn("access", resp.json())
        self.assertFalse(SocialAccount.objects.filter(user=victim).exists())
        self.assertIsNone(resolve_completed_user(resp.json()["pending_token"]))

    def test_no_email_at_all_goes_to_the_web_signup_form(self):
        """With no email at all, allauth asks for one on the web signup form."""
        resp = self.post_facebook_limited(self.limited_claims(sub="fb-noemail"))
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertNotIn("access", body)
        self.assertIn("continue_url", body)
        self.assertIn("pending_token", body)
        self.assertFalse(SocialAccount.objects.filter(provider="facebook", uid="fb-noemail").exists())

    def test_limited_login_nonce_is_checked(self):
        resp = self.post_facebook_limited(self.limited_claims(nonce=_sha256_hex("other")))
        self.assertEqual(resp.status_code, 401)

    def test_classic_token_for_another_facebook_app_is_rejected(self):
        """``debug_token`` must confirm the token was minted for our app id, or any app's token would work."""
        resp = self.post_facebook_classic(
            {"id": "fb-other", "email": "other@example.com"},
            token_app_id="9999999999",
        )
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(SocialAccount.objects.filter(provider="facebook", uid="fb-other").exists())

    def test_classic_token_for_our_app_is_accepted(self):
        resp = self.post_facebook_classic({"id": "fb-ours", "email": "ours@example.com"})
        self.assertEqual(resp.status_code, 200, resp.content)
        # Still not signed in: the address is unconfirmed.
        self.assertNotIn("access", resp.json())

    def test_missing_credential_is_rejected(self):
        resp = self.client.post(self.url, {"provider": "facebook"}, content_type="application/json")
        self.assertEqual(resp.status_code, 401)


class DeactivatedAccountTests(SocialAuthTestCase):
    """A disabled account is refused here as on the web."""

    def test_inactive_user_cannot_sign_in_with_a_linked_provider(self):
        user = User.objects.create_user("banned", "banned@example.com", "pw", is_active=False)
        EmailAddress.objects.create(user=user, email="banned@example.com", verified=True, primary=True)
        SocialAccount.objects.create(user=user, provider="apple", uid="banned-sub")

        resp = self.post_apple(self.apple_claims(sub="banned-sub"))

        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("access", resp.json())


class SocialContinuationTests(SocialAuthTestCase):
    """The web continuation: continue → allauth's signup form → done → complete."""

    def _pending_facebook_login(self):
        resp = self.post_facebook_limited({"sub": "fb-flow", "name": "Flo", "nonce": HASHED_NONCE})
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def test_full_flow_signs_the_user_in(self):
        body = self._pending_facebook_login()
        self.assertIn("/api/mobile/auth/social/continue/", body["continue_url"])

        # 1. The WebView loads continue_url; its token is the only credential.
        token = body["continue_url"].split("t=")[1]
        resp = self.client.get(self.continue_url, {"t": token})
        self.assertEqual(resp.status_code, 302)
        # /social/signup/, not allauth's /3rdparty/signup/, for the WebView allowlist.
        self.assertEqual(resp["Location"], reverse("mobile_socialaccount_signup"))

        # 2. allauth's signup form, with the email Facebook couldn't give us.
        resp = self.client.post(
            reverse("mobile_socialaccount_signup"),
            {
                "email": "flo@example.com",
                "username": "flo",
                "first_name": "Flo",
                "last_name": "Book",
            },
        )
        self.assertIn(resp.status_code, (302, 200), getattr(resp, "content", b""))
        user = User.objects.get(username="flo")
        self.assertTrue(SocialAccount.objects.filter(user=user, provider="facebook", uid="fb-flow").exists())

        # Not finished yet: the address still has to be confirmed.
        self.assertEqual(
            self.client.post(
                self.complete_url, {"pending_token": body["pending_token"]}, content_type="application/json"
            ).status_code,
            400,
        )

        # 3. They confirm it (in whatever browser their mail app opened).
        EmailAddress.objects.filter(user=user).update(verified=True)

        resp = self.client.post(
            self.complete_url, {"pending_token": body["pending_token"]}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("access", resp.json())

    def test_continue_token_is_single_use(self):
        body = self._pending_facebook_login()
        token = body["continue_url"].split("t=")[1]
        self.assertEqual(
            self.client.get(self.continue_url, {"t": token})["Location"], reverse("mobile_socialaccount_signup")
        )
        # A replay establishes nothing and lands on the login page.
        self.assertEqual(self.client.get(self.continue_url, {"t": token})["Location"], reverse("account_login"))

    def test_continue_signs_out_whoever_was_already_signed_in(self):
        """Continue signs out whoever was signed in, so the done view can't bind a bystander's account."""
        bystander = User.objects.create_user("bystander", "bystander@example.com", "pw")
        EmailAddress.objects.create(user=bystander, email="bystander@example.com", verified=True, primary=True)
        self.client.force_login(bystander)

        body = self._pending_facebook_login()
        token = body["continue_url"].split("t=")[1]
        self.client.get(self.continue_url, {"t": token})

        # The done view sees nobody...
        self.assertIn(b"isn't finished", self.client.get(self.done_url).content)
        # ...and the pending token can't be exchanged for their tokens.
        resp = self.client.post(
            self.complete_url, {"pending_token": body["pending_token"]}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIsNone(resolve_completed_user(body["pending_token"]))

    def test_complete_refuses_an_unfinished_flow(self):
        body = self._pending_facebook_login()
        resp = self.client.post(
            self.complete_url, {"pending_token": body["pending_token"]}, content_type="application/json"
        )
        self.assertEqual(resp.status_code, 400)

    def test_complete_refuses_an_unknown_token(self):
        resp = self.client.post(self.complete_url, {"pending_token": "nope"}, content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_complete_is_single_use(self):
        user = User.objects.create_user("once", "once@example.com", "pw")
        EmailAddress.objects.create(user=user, email="once@example.com", verified=True, primary=True)
        SocialAccount.objects.create(user=user, provider="apple", uid="once-sub")
        pending_token, _ = PendingSocialLogin.create(
            provider="apple", uid="once-sub", serialized_login=None, user_pk=user.pk
        )
        first = self.client.post(self.complete_url, {"pending_token": pending_token}, content_type="application/json")
        self.assertEqual(first.status_code, 200, first.content)
        second = self.client.post(self.complete_url, {"pending_token": pending_token}, content_type="application/json")
        self.assertEqual(second.status_code, 400)

    def test_complete_refuses_a_user_whose_email_is_unverified(self):
        user = User.objects.create_user("unconfirmed", "unconfirmed@example.com", "pw")
        EmailAddress.objects.create(user=user, email="unconfirmed@example.com", verified=False, primary=True)
        SocialAccount.objects.create(user=user, provider="apple", uid="unconfirmed-sub")
        pending_token, _ = PendingSocialLogin.create(
            provider="apple", uid="unconfirmed-sub", serialized_login=None, user_pk=user.pk
        )
        resp = self.client.post(self.complete_url, {"pending_token": pending_token}, content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_complete_refuses_an_inactive_user(self):
        user = User.objects.create_user("gone", "gone@example.com", "pw", is_active=False)
        EmailAddress.objects.create(user=user, email="gone@example.com", verified=True, primary=True)
        SocialAccount.objects.create(user=user, provider="apple", uid="gone-sub")
        pending_token, _ = PendingSocialLogin.create(
            provider="apple", uid="gone-sub", serialized_login=None, user_pk=user.pk
        )
        resp = self.client.post(self.complete_url, {"pending_token": pending_token}, content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_complete_refuses_when_the_social_account_belongs_to_someone_else(self):
        """Complete refuses when the SocialAccount belongs to someone else: the record names the flow, the
        SocialAccount names the user.
        """
        owner = User.objects.create_user("owner", "owner@example.com", "pw")
        EmailAddress.objects.create(user=owner, email="owner@example.com", verified=True, primary=True)
        SocialAccount.objects.create(user=owner, provider="apple", uid="shared-sub")
        someone_else = User.objects.create_user("else", "else@example.com", "pw")
        pending_token, _ = PendingSocialLogin.create(
            provider="apple", uid="shared-sub", serialized_login=None, user_pk=someone_else.pk
        )
        resp = self.client.post(self.complete_url, {"pending_token": pending_token}, content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_done_reports_unfinished_when_nobody_is_signed_in(self):
        resp = self.client.get(self.done_url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"isn't finished", resp.content)


class SocialAuthRequestValidationTests(SocialAuthTestCase):
    def test_unsupported_provider_is_rejected(self):
        resp = self.client.post(self.url, {"provider": "myspace", "id_token": "stub"}, content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_missing_provider_is_rejected(self):
        resp = self.client.post(self.url, {"id_token": "stub"}, content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_unconfigured_provider_reports_a_401_rather_than_a_500(self):
        with override_settings(SOCIALACCOUNT_PROVIDERS={"google": {}}, FACEBOOK_APP_ID="", FACEBOOK_APP_SECRET=""):
            resp = self.client.post(
                self.url,
                {"provider": "facebook", "access_token": "stub"},
                content_type="application/json",
            )
        self.assertEqual(resp.status_code, 401)


@isolated_cache("mobile-config-social")
class MobileConfigSocialTests(TestCase):
    """SOCIAL-7 — the app hides a provider's button entirely when its key is absent."""

    def setUp(self):
        cache.clear()
        self.url = reverse("mobile-config")

    @override_settings(APPLE_ALLOWED_AUDIENCES=[APPLE_BUNDLE_ID], FACEBOOK_APP_ID=FACEBOOK_APP_ID)
    def test_configured_providers_are_advertised(self):
        data = self.client.get(self.url).json()
        self.assertTrue(data["apple_sign_in_enabled"])
        self.assertEqual(data["facebook_app_id"], FACEBOOK_APP_ID)

    @override_settings(APPLE_ALLOWED_AUDIENCES=[], FACEBOOK_APP_ID="")
    def test_unconfigured_providers_are_reported_as_off(self):
        data = self.client.get(self.url).json()
        self.assertFalse(data["apple_sign_in_enabled"])
        self.assertEqual(data["facebook_app_id"], "")


@override_settings(
    APPLE_SIGN_IN_TEAM_ID="TEAMID1234",
    APPLE_SIGN_IN_KEY_ID="KEYID12345",
    APPLE_SIGN_IN_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
    APPLE_SIGN_IN_BUNDLE_ID=APPLE_BUNDLE_ID,
)
class AppleRevocationTests(TestCase):
    """SOCIAL-6 — Apple requires the grant to be revoked when the account is deleted."""

    def setUp(self):
        self.user = User.objects.create_user("appleuser", "apple@example.com", "pw")
        self.account = SocialAccount.objects.create(user=self.user, provider="apple", uid="revoke-sub")
        SocialToken.objects.create(account=self.account, token="access-tok", token_secret="refresh-tok")

    def test_deleting_an_account_revokes_the_apple_grant(self):
        from auctions.account_deletion import delete_account

        with (
            patch("auctions.apple_signin._client_secret", return_value="jwt"),
            patch("requests.post") as post,
        ):
            post.return_value.raise_for_status.return_value = None
            delete_account(self.user)

        self.assertEqual(post.call_count, 1)
        call = post.call_args
        self.assertEqual(call.args[0], "https://appleid.apple.com/auth/revoke")
        # The refresh token ends the grant; the access token is a fallback.
        self.assertEqual(call.kwargs["data"]["token"], "refresh-tok")
        self.assertEqual(call.kwargs["data"]["token_type_hint"], "refresh_token")
        self.assertFalse(SocialAccount.objects.filter(user=self.user).exists())

    def test_apple_being_unreachable_does_not_block_deletion(self):
        from auctions.account_deletion import delete_account

        with (
            patch("auctions.apple_signin._client_secret", return_value="jwt"),
            patch("requests.post", side_effect=OSError("network down")),
        ):
            delete_account(self.user)

        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertFalse(SocialAccount.objects.filter(user=self.user).exists())

    @override_settings(APPLE_SIGN_IN_TEAM_ID="", APPLE_SIGN_IN_KEY_ID="", APPLE_SIGN_IN_PRIVATE_KEY="")
    def test_unconfigured_revocation_is_a_warning_not_a_crash(self):
        from auctions.account_deletion import delete_account

        with patch("requests.post") as post:
            delete_account(self.user)
        post.assert_not_called()
        self.assertFalse(SocialAccount.objects.filter(user=self.user).exists())

    def test_revocation_runs_before_the_tokens_are_deleted(self):
        """Revocation runs before the tokens are deleted, since they're the only way to reach Apple."""
        from auctions.apple_signin import revoke_all_for_user

        seen = {}

        def capture(url, **kwargs):
            seen["token"] = kwargs["data"]["token"]

            class Response:
                @staticmethod
                def raise_for_status():
                    return None

            return Response()

        with (
            patch("auctions.apple_signin._client_secret", return_value="jwt"),
            patch("requests.post", side_effect=capture),
        ):
            self.assertEqual(revoke_all_for_user(self.user), 1)
        self.assertEqual(seen["token"], "refresh-tok")


class AppleAuthorizationCodeTests(SocialAuthTestCase):
    @override_settings(
        APPLE_SIGN_IN_TEAM_ID="TEAMID1234",
        APPLE_SIGN_IN_KEY_ID="KEYID12345",
        APPLE_SIGN_IN_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
    )
    def test_authorization_code_is_redeemed_and_the_refresh_token_stored(self):
        with (
            patch("auctions.apple_signin._client_secret", return_value="jwt"),
            patch("requests.post") as post,
        ):
            post.return_value.raise_for_status.return_value = None
            post.return_value.json.return_value = {"access_token": "at", "refresh_token": "rt"}
            resp = self.post_apple(
                self.apple_claims(sub="code-sub", email="code@example.com"),
                authorization_code="apple-code",
            )
        self.assertEqual(resp.status_code, 200, resp.content)
        account = SocialAccount.objects.get(provider="apple", uid="code-sub")
        self.assertEqual(SocialToken.objects.get(account=account).token_secret, "rt")

    def test_sign_in_still_works_when_apple_rejects_the_code(self):
        """Sign-in still works when Apple rejects the authorization code: identity came from the token."""
        with patch("auctions.apple_signin.redeem_authorization_code", return_value=None):
            resp = self.post_apple(
                self.apple_claims(sub="badcode-sub", email="badcode@example.com"),
                authorization_code="bad",
            )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("access", resp.json())


@isolated_cache("legacy-google-endpoint")
class LegacyGoogleEndpointTests(TestCase):
    """The old Google-only endpoint stays alive until older installs age out."""

    def setUp(self):
        cache.clear()

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="test-client.apps.googleusercontent.com")
    def test_still_issues_tokens(self):
        with patch(
            "google.oauth2.id_token.verify_oauth2_token",
            return_value={"sub": "legacy-sub", "email": "legacy@example.com", "email_verified": True},
        ):
            resp = self.client.post(
                reverse("mobile-auth-google"), {"id_token": "stub"}, content_type="application/json"
            )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("access", resp.json())


class SocialLoginPageTests(TestCase):
    """Web parity: a provider's button appears only when configured for the web."""

    def setUp(self):
        self.url = reverse("account_login")

    @override_settings(
        GOOGLE_OAUTH_LINK="", APPLE_SIGN_IN_SERVICES_ID="", APPLE_SIGN_IN_PRIVATE_KEY="", FACEBOOK_APP_ID=""
    )
    def test_no_providers_configured_shows_only_the_password_form(self):
        html = self.client.get(self.url).content.decode()
        self.assertNotIn("sign-in-google", html)
        self.assertNotIn("sign-in-apple", html)
        self.assertNotIn("sign-in-facebook", html)
        self.assertIn("Sign in with your account on this site", html)

    @override_settings(
        APPLE_SIGN_IN_SERVICES_ID=APPLE_SERVICES_ID,
        APPLE_SIGN_IN_KEY_ID="KEYID12345",
        APPLE_SIGN_IN_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
        FACEBOOK_APP_ID=FACEBOOK_APP_ID,
        FACEBOOK_APP_SECRET="fb-secret",
    )
    def test_configured_providers_are_offered(self):
        html = self.client.get(self.url).content.decode()
        self.assertIn("sign-in-apple", html)
        self.assertIn("sign-in-facebook", html)

    @override_settings(
        APPLE_SIGN_IN_SERVICES_ID=APPLE_SERVICES_ID,
        APPLE_SIGN_IN_KEY_ID="KEYID12345",
        APPLE_SIGN_IN_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----",
        FACEBOOK_APP_ID=FACEBOOK_APP_ID,
        FACEBOOK_APP_SECRET="fb-secret",
    )
    def test_hidden_inside_the_app_where_web_oauth_is_blocked(self):
        html = self.client.get(self.url, HTTP_USER_AGENT="FishAuctionsApp/1.0 (Flutter; iOS)").content.decode()
        self.assertNotIn("sign-in-apple", html)
        self.assertNotIn("sign-in-facebook", html)

    @override_settings(
        APPLE_SIGN_IN_SERVICES_ID=APPLE_SERVICES_ID,
        APPLE_SIGN_IN_KEY_ID="",
        APPLE_SIGN_IN_PRIVATE_KEY="",
    )
    def test_apple_needs_its_team_key_for_the_web_flow(self):
        # Without the key the redirect reaches Apple and fails there.
        html = self.client.get(self.url).content.decode()
        self.assertNotIn("sign-in-apple", html)


class SocialAdapterTests(TestCase):
    """The Google fallback app must not create the ambiguity ``get_app`` rejects."""

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="fallback.apps.googleusercontent.com")
    def test_fallback_is_used_when_no_app_is_configured(self):
        from allauth.socialaccount.adapter import get_adapter

        app = get_adapter().get_app(None, "google")
        self.assertEqual(app.client_id, "fallback.apps.googleusercontent.com")

    @override_settings(
        GOOGLE_OAUTH_CLIENT_ID="fallback.apps.googleusercontent.com",
        SOCIALACCOUNT_PROVIDERS={"google": {"APP": {"client_id": "configured", "secret": "s"}}},
    )
    def test_a_configured_app_wins_and_stays_unambiguous(self):
        from allauth.socialaccount.adapter import get_adapter

        app = get_adapter().get_app(None, "google")
        self.assertEqual(app.client_id, "configured")

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="")
    def test_no_client_id_means_no_fallback(self):
        from allauth.socialaccount.adapter import get_adapter
        from allauth.socialaccount.models import SocialApp

        with self.assertRaises(SocialApp.DoesNotExist):
            get_adapter().get_app(None, "google")


@isolated_cache("settings-configured-provider-tokens")
@override_settings(SOCIALACCOUNT_PROVIDERS=SOCIAL_PROVIDERS, FACEBOOK_APP_ID=FACEBOOK_APP_ID)
class SettingsConfiguredProviderTokenTests(TestCase):
    """A settings-configured provider must still store its tokens.

    ``SOCIALACCOUNT_STORE_TOKENS`` is on for Apple's refresh token, but Apple and Facebook are
    configured in settings, so their SocialApp has no pk and Django refuses the foreign key. Without
    the adapter's fix, every Apple and Facebook signup 500s on the web too.
    """

    def setUp(self):
        cache.clear()

    def test_signup_with_a_token_does_not_blow_up(self):
        from allauth.core import context
        from allauth.socialaccount.adapter import get_adapter
        from allauth.socialaccount.helpers import complete_social_login
        from allauth.socialaccount.models import SocialToken
        from django.contrib.auth.models import AnonymousUser

        # A hand-built request, since the point is the web entry into allauth's pipeline. The
        # session, message and request-context middleware are stubbed in.
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.session = self.client.session
        request.user = AnonymousUser()
        request._messages = FallbackStorage(request)

        provider = get_adapter().get_provider(request, "facebook")
        self.assertIsNone(provider.app.pk)  # settings-configured: no database row

        sociallogin = provider.sociallogin_from_response(
            request, {"id": "settings-app-uid", "email": "settingsapp@example.com", "first_name": "Set"}
        )
        sociallogin.token = SocialToken(app=provider.app, token="fb-access-token")

        # Without the adapter hook this raises "save() prohibited ... unsaved related object 'app'".
        with context.request_context(request):
            complete_social_login(request, sociallogin)

        account = SocialAccount.objects.get(provider="facebook", uid="settings-app-uid")
        stored = SocialToken.objects.get(account=account)
        self.assertEqual(stored.token, "fb-access-token")
        self.assertIsNone(stored.app_id)


class SocialAccountUsedAtDeletionTests(TestCase):
    """Deletion still removes every social identity, now including the new providers."""

    def test_all_providers_are_removed(self):
        from auctions.account_deletion import delete_account

        user = User.objects.create_user("multi", "multi@example.com", "pw")
        for provider, uid in (("google", "g1"), ("apple", "a1"), ("facebook", "f1")):
            SocialAccount.objects.create(user=user, provider=provider, uid=uid)
        user.userdata.account_deletion_requested = datetime.datetime.now(datetime.UTC)
        user.userdata.save()

        with patch("auctions.apple_signin.revoke_all_for_user", return_value=0):
            delete_account(user)

        self.assertFalse(SocialAccount.objects.filter(user=user).exists())
