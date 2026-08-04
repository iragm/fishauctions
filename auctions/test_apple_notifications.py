"""Tests for Sign in with Apple server-to-server notifications.

Apple sends each of these once, so a notification that isn't handled is not retried by anyone — it
is gone. That makes two things worth testing hard:

* **What we refuse.** The endpoint is public and unauthenticated, and one of the events it acts on
  means "delete this person's account". Nothing but Apple's signature stands between a POST and
  that, so the refusals (wrong key, wrong audience, wrong issuer, an ``alg`` the caller picked) get
  more tests than the happy path.
* **What we do with a retry.** Apple retries until it gets a 2xx, so a delivery that failed halfway
  must run again, and one that succeeded must not.

Only Apple's JWKS endpoint is faked. The notifications are real JWTs signed with a throwaway key, so
the signature check, the audience check and the claim parsing all run for real — a test that mocked
``verify_notification`` would pass just as happily with no verification at all.
"""

import datetime
import json
from unittest.mock import patch

import jwt
from allauth.account.models import EmailAddress
from allauth.socialaccount.models import SocialAccount, SocialToken
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions.account_deletion import GRACE_PERIOD_DAYS
from auctions.apple_notifications import (
    APPLE_ISSUER,
    AppleNotificationError,
    process_notification,
)
from auctions.models import AuctionTOS, Club, ClubMember, UserData
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase

BUNDLE_ID = "com.fishauctions.app"
SERVICES_ID = "fish.auction.signin"
KID = "test-key-1"

# One 2048-bit keypair for the whole module — generating one costs about a tenth of a second and
# every test needs the same thing: a key Apple's (faked) JWKS will vouch for.
SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
# A second key that the JWKS never publishes, for "signed by someone who isn't Apple".
IMPOSTOR_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks():
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(SIGNING_KEY.public_key()))
    jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
    return {"keys": [jwk]}


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@isolated_cache("apple-notifications")
@override_settings(
    APPLE_SIGN_IN_BUNDLE_ID=BUNDLE_ID,
    APPLE_SIGN_IN_SERVICES_ID=SERVICES_ID,
    APPLE_ALLOWED_AUDIENCES=[SERVICES_ID, BUNDLE_ID],
)
class AppleNotificationTestCase(TestCase):
    """Shared plumbing: a fake JWKS, and one helper that builds a real signed notification."""

    def setUp(self):
        # The JWKS cache and the processed-jti records both live in the cache, and SIGNING_KEY is
        # this process's own -- see auctions.test_support for why that has to be a private one.
        cache.clear()
        self.url = reverse("apple_server_notifications")
        self.jti_counter = 0

    def signed_notification(
        self,
        *,
        event_type="consent-revoked",
        sub="apple-sub-1",
        email=None,
        events_as_string=True,
        audience=BUNDLE_ID,
        issuer=APPLE_ISSUER,
        key=None,
        algorithm="RS256",
        kid=KID,
        jti=None,
        extra_event=None,
    ):
        self.jti_counter += 1
        event = {"type": event_type, "sub": sub, "event_time": 1700000000000}
        if email:
            event["email"] = email
            event["is_private_email"] = "true"
        if extra_event:
            event.update(extra_event)
        payload = {
            "iss": issuer,
            "aud": audience,
            "iat": int(timezone.now().timestamp()),
            "jti": jti or f"jti-{self.jti_counter}",
            # Apple ships the event as a JSON *string*, not a nested object. Both are exercised.
            "events": json.dumps(event) if events_as_string else event,
        }
        return jwt.encode(
            payload,
            key=key or SIGNING_KEY,
            algorithm=algorithm,
            headers={"kid": kid},
        )

    def post(self, signed_payload, field="payload"):
        with patch("requests.get", return_value=_FakeResponse(_jwks())):
            return self.client.post(
                self.url,
                data=json.dumps({field: signed_payload}),
                content_type="application/json",
            )

    def deliver(self, **kwargs):
        """Verify and process a notification directly, bypassing the HTTP layer."""
        with patch("requests.get", return_value=_FakeResponse(_jwks())):
            return process_notification(self.signed_notification(**kwargs))

    # -- fixtures ----------------------------------------------------------

    def make_apple_user(self, username="appleuser", sub="apple-sub-1", email="apple@example.com", password=None):
        user = User.objects.create_user(username=username, email=email)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()
        account = SocialAccount.objects.create(user=user, provider="apple", uid=sub)
        SocialToken.objects.create(account=account, app=None, token="access", token_secret="refresh")
        return user, account


class AppleNotificationVerificationTests(AppleNotificationTestCase):
    """Everything the endpoint must refuse. A 200 here would be an account-takeover bug."""

    def test_valid_notification_is_accepted(self):
        self.make_apple_user()
        response = self.post(self.signed_notification())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["handled"], ["consent-revoked"])

    def test_signature_from_a_key_apple_never_published_is_rejected(self):
        user, account = self.make_apple_user()
        response = self.post(self.signed_notification(key=IMPOSTOR_KEY))
        self.assertEqual(response.status_code, 400)
        # And nothing was acted on.
        self.assertTrue(SocialToken.objects.filter(account=account).exists())

    def test_unknown_kid_is_rejected(self):
        self.make_apple_user()
        response = self.post(self.signed_notification(kid="not-apples-key"))
        self.assertEqual(response.status_code, 400)

    def test_notification_for_another_app_is_rejected(self):
        """The audience check. Without it, anyone's Apple app could delete accounts here."""
        user, account = self.make_apple_user()
        response = self.post(self.signed_notification(audience="com.someone.else"))
        self.assertEqual(response.status_code, 400)
        self.assertTrue(SocialAccount.objects.filter(pk=account.pk).exists())

    def test_services_id_audience_is_accepted(self):
        """Apple sets `aud` to whichever identifier the endpoint was configured against."""
        self.make_apple_user()
        response = self.post(self.signed_notification(audience=SERVICES_ID))
        self.assertEqual(response.status_code, 200)

    def test_wrong_issuer_is_rejected(self):
        self.make_apple_user()
        response = self.post(self.signed_notification(issuer="https://appleid.example.com"))
        self.assertEqual(response.status_code, 400)

    def test_caller_cannot_choose_a_symmetric_algorithm(self):
        """`alg: HS256` with a known kid is the classic key-confusion attack; the header is theirs."""
        self.make_apple_user()
        forged = jwt.encode(
            {
                "iss": APPLE_ISSUER,
                "aud": BUNDLE_ID,
                "iat": int(timezone.now().timestamp()),
                "jti": "forged",
                "events": json.dumps({"type": "account-delete", "sub": "apple-sub-1"}),
            },
            key="whatever",
            algorithm="HS256",
            headers={"kid": KID},
        )
        response = self.post(forged)
        self.assertEqual(response.status_code, 400)

    def test_unsigned_token_is_rejected(self):
        self.make_apple_user()
        forged = jwt.encode(
            {"iss": APPLE_ISSUER, "aud": BUNDLE_ID, "iat": 1, "events": "{}"},
            key=None,
            algorithm="none",
            headers={"kid": KID},
        )
        response = self.post(forged)
        self.assertEqual(response.status_code, 400)

    def test_garbage_body_is_rejected_without_raising(self):
        response = self.client.post(self.url, data="not json at all", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_missing_payload_is_rejected(self):
        response = self.client.post(self.url, data=json.dumps({}), content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_signed_payload_spelling_is_also_accepted(self):
        self.make_apple_user()
        response = self.post(self.signed_notification(), field="signedPayload")
        self.assertEqual(response.status_code, 200)

    def test_form_encoded_delivery_is_accepted(self):
        self.make_apple_user()
        with patch("requests.get", return_value=_FakeResponse(_jwks())):
            response = self.client.post(self.url, data={"payload": self.signed_notification()})
        self.assertEqual(response.status_code, 200)

    @override_settings(APPLE_ALLOWED_AUDIENCES=[])
    def test_unconfigured_deployment_answers_503_so_apple_retries(self):
        response = self.post(self.signed_notification())
        self.assertEqual(response.status_code, 503)

    def test_get_is_a_liveness_check(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")

    def test_endpoint_lives_at_the_url_given_to_apple(self):
        self.assertEqual(self.url, "/apple/notifications")

    def test_trailing_slash_also_works(self):
        """APPEND_SLASH can't rescue a POST — it redirects and the body is lost."""
        self.make_apple_user()
        with patch("requests.get", return_value=_FakeResponse(_jwks())):
            response = self.client.post(
                "/apple/notifications/",
                data=json.dumps({"payload": self.signed_notification()}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)


class AppleNotificationForgeryTests(AppleNotificationTestCase):
    """Nothing reaches a handler without Apple's signature. Each of these tries to get past it.

    ``account-delete`` is the event used throughout, because it is the one with teeth: if any of
    these returned 200, an anonymous POST could unlink an account and start its deletion clock.
    """

    def setUp(self):
        super().setUp()
        self.user, self.account = self.make_apple_user()

    def assert_refused(self, signed_payload, *, status=400):
        with patch("auctions.apple_notifications.handle_event") as handler:
            response = self.post(signed_payload)
        self.assertEqual(response.status_code, status)
        # The real proof: no handler ran, whatever the status code says.
        handler.assert_not_called()
        self.assertTrue(SocialAccount.objects.filter(pk=self.account.pk).exists())
        self.assertTrue(SocialToken.objects.filter(account=self.account).exists())

    def forged(self, header, payload):
        """A token assembled by hand, so the header can say things no signer would produce."""
        import base64

        def segment(data):
            return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

        return f"{segment(header)}.{segment(payload)}.AAAA"

    @property
    def delete_claims(self):
        return {
            "iss": APPLE_ISSUER,
            "aud": BUNDLE_ID,
            "iat": int(timezone.now().timestamp()),
            "jti": "forged-1",
            "events": json.dumps({"type": "account-delete", "sub": "apple-sub-1"}),
        }

    def test_alg_none_with_a_real_kid(self):
        """The oldest JWT attack: ask the library to skip the signature check."""
        self.assert_refused(self.forged({"alg": "none", "kid": KID}, self.delete_claims))

    def test_alg_hs256_with_a_real_kid(self):
        """Key confusion: Apple's public key used as an HMAC secret, which anyone can read."""
        self.assert_refused(self.forged({"alg": "HS256", "kid": KID}, self.delete_claims))

    def test_alg_swapped_to_another_asymmetric_algorithm(self):
        """ES256 against Apple's RSA key. Raised a bare TypeError before the JWK became the source
        of the algorithm — refused either way, but as a 500 it was a way to mail the admins."""
        self.assert_refused(self.forged({"alg": "ES256", "kid": KID}, self.delete_claims))

    def test_valid_signature_from_the_wrong_signer(self):
        """A properly signed token — just not by a key Apple publishes."""
        self.assert_refused(self.signed_notification(event_type="account-delete", key=IMPOSTOR_KEY))

    def test_signature_stripped_off_a_genuine_notification(self):
        genuine = self.signed_notification(event_type="account-delete")
        header, payload, _ = genuine.split(".")
        self.assert_refused(f"{header}.{payload}.")

    def test_payload_tampered_after_signing(self):
        """The classic: take a real notification and change whose account it is about."""
        import base64

        genuine = self.signed_notification(event_type="consent-revoked", sub="somebody-else")
        header, _, signature = genuine.split(".")
        swapped = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "iss": APPLE_ISSUER,
                    "aud": BUNDLE_ID,
                    "iat": int(timezone.now().timestamp()),
                    "jti": "tampered",
                    "events": json.dumps({"type": "account-delete", "sub": "apple-sub-1"}),
                }
            ).encode()
        ).rstrip(b"=")
        self.assert_refused(f"{header}.{swapped.decode()}.{signature}")

    def test_no_kid_at_all(self):
        self.assert_refused(self.forged({"alg": "RS256"}, self.delete_claims))

    def test_kid_that_is_not_a_string(self):
        self.assert_refused(self.forged({"alg": "RS256", "kid": {"$ne": None}}, self.delete_claims))

    def test_not_a_jwt_at_all(self):
        self.assert_refused("hello")

    @override_settings(APPLE_ALLOWED_AUDIENCES=[])
    def test_an_unconfigured_deployment_still_refuses_rather_than_accepts(self):
        """The view answers 503 before verifying. This checks the layer under it fails closed too,
        because process_notification is importable and PyJWT could have read [] as 'any audience'."""
        with patch("requests.get", return_value=_FakeResponse(_jwks())):
            with self.assertRaises(AppleNotificationError):
                process_notification(self.signed_notification(event_type="account-delete"))
        self.assertTrue(SocialAccount.objects.filter(pk=self.account.pk).exists())

    def test_a_notification_for_a_different_apple_app_is_refused(self):
        self.assert_refused(self.signed_notification(event_type="account-delete", audience="com.someone.else"))

    def test_an_issuer_that_is_not_apple_is_refused(self):
        self.assert_refused(
            self.signed_notification(event_type="account-delete", issuer="https://appleid.apple.com.evil.test")
        )

    def test_the_jwks_is_only_ever_fetched_from_apple(self):
        """Nothing in a request can point key lookup at another host."""
        signed = self.signed_notification()
        # process_notification rather than self.post: post() patches requests.get itself, so an
        # outer patch here would be shadowed and never see the call it is meant to inspect.
        with patch("requests.get", return_value=_FakeResponse(_jwks())) as fetch:
            process_notification(signed)
        self.assertEqual(fetch.call_args.args[0], "https://appleid.apple.com/auth/keys")


class AppleNotificationEventParsingTests(AppleNotificationTestCase):
    def test_events_claim_is_parsed_when_it_is_a_json_string(self):
        """Apple's `events` is a string containing JSON, not an object. The easy bug to ship."""
        user, account = self.make_apple_user()
        handled = self.deliver(event_type="consent-revoked", events_as_string=True)
        self.assertEqual(handled, ["consent-revoked"])
        self.assertFalse(SocialToken.objects.filter(account=account).exists())

    def test_events_claim_is_parsed_when_it_is_an_object(self):
        user, account = self.make_apple_user()
        handled = self.deliver(event_type="consent-revoked", events_as_string=False)
        self.assertEqual(handled, ["consent-revoked"])
        self.assertFalse(SocialToken.objects.filter(account=account).exists())

    def test_unknown_event_type_is_ignored_not_failed(self):
        """A failure would make Apple retry a new event type for 24 hours."""
        self.make_apple_user()
        response = self.post(self.signed_notification(event_type="something-apple-added-later"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["handled"], ["ignored"])

    def test_event_without_a_sub_is_ignored(self):
        response = self.post(self.signed_notification(sub=""))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["handled"], ["ignored"])

    def test_event_for_an_unknown_sub_is_a_no_op(self):
        user, account = self.make_apple_user(sub="apple-sub-1")
        handled = self.deliver(event_type="account-delete", sub="somebody-else")
        self.assertEqual(handled, ["account-delete"])
        self.assertTrue(SocialAccount.objects.filter(pk=account.pk).exists())

    def test_missing_events_claim_is_rejected(self):
        signed = jwt.encode(
            {"iss": APPLE_ISSUER, "aud": BUNDLE_ID, "iat": int(timezone.now().timestamp()), "jti": "x"},
            key=SIGNING_KEY,
            algorithm="RS256",
            headers={"kid": KID},
        )
        response = self.post(signed)
        self.assertEqual(response.status_code, 400)


class AppleConsentRevokedTests(AppleNotificationTestCase):
    """The user disconnected the app. Sign them out — don't take their account away."""

    def test_tokens_are_dropped(self):
        user, account = self.make_apple_user()
        self.deliver(event_type="consent-revoked")
        self.assertFalse(SocialToken.objects.filter(account=account).exists())

    def test_the_account_link_is_kept_so_signing_in_again_finds_the_same_account(self):
        user, account = self.make_apple_user()
        self.deliver(event_type="consent-revoked")
        self.assertTrue(SocialAccount.objects.filter(pk=account.pk).exists())

    def test_the_site_account_is_not_deleted(self):
        user, _ = self.make_apple_user()
        self.deliver(event_type="consent-revoked")
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertIsNone(UserData.objects.get(user=user).account_deletion_requested)

    def test_the_app_is_signed_out(self):
        from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
        from rest_framework_simplejwt.tokens import RefreshToken

        user, _ = self.make_apple_user()
        RefreshToken.for_user(user)
        self.deliver(event_type="consent-revoked")
        self.assertTrue(BlacklistedToken.objects.filter(token__user=user).exists())


class AppleAccountDeleteTests(AppleNotificationTestCase):
    """The Apple ID is gone. The link always goes; the site account only when it's stranded."""

    def test_dead_link_is_removed(self):
        user, account = self.make_apple_user()
        self.deliver(event_type="account-delete")
        self.assertFalse(SocialAccount.objects.filter(pk=account.pk).exists())
        self.assertFalse(SocialToken.objects.filter(account_id=account.pk).exists())

    def test_account_with_a_password_is_kept(self):
        user, _ = self.make_apple_user(password="a-real-password")
        self.deliver(event_type="account-delete")
        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertIsNone(UserData.objects.get(user=user).account_deletion_requested)

    def test_account_with_another_social_login_is_kept(self):
        user, _ = self.make_apple_user()
        SocialAccount.objects.create(user=user, provider="google", uid="google-1")
        self.deliver(event_type="account-delete")
        self.assertIsNone(UserData.objects.get(user=user).account_deletion_requested)

    def test_account_with_a_real_verified_address_is_kept(self):
        """A working inbox means a password reset, which means they aren't locked out."""
        user, _ = self.make_apple_user(email="person@example.com")
        EmailAddress.objects.create(user=user, email="person@example.com", verified=True, primary=True)
        self.deliver(event_type="account-delete")
        self.assertIsNone(UserData.objects.get(user=user).account_deletion_requested)

    def test_apple_only_account_is_scheduled_for_deletion(self):
        user, _ = self.make_apple_user(email="abc@privaterelay.appleid.com")
        EmailAddress.objects.create(user=user, email="abc@privaterelay.appleid.com", verified=True, primary=True)
        self.deliver(event_type="account-delete")
        userdata = UserData.objects.get(user=user)
        self.assertIsNotNone(userdata.account_deletion_requested)
        # The site's ordinary grace period, not an immediate wipe.
        due = userdata.account_deletion_requested + datetime.timedelta(days=GRACE_PERIOD_DAYS)
        self.assertGreater(due, timezone.now())

    def test_a_relay_address_alone_does_not_count_as_a_way_back_in(self):
        """It only ever worked because Apple forwarded it, and Apple has stopped."""
        user, _ = self.make_apple_user(email="abc@privaterelay.appleid.com")
        EmailAddress.objects.create(user=user, email="ABC@PrivateRelay.AppleID.com", verified=True, primary=True)
        self.deliver(event_type="account-delete")
        self.assertIsNotNone(UserData.objects.get(user=user).account_deletion_requested)

    def test_an_unverified_address_does_not_count(self):
        user, _ = self.make_apple_user(email="person@example.com")
        EmailAddress.objects.create(user=user, email="person@example.com", verified=False, primary=True)
        self.deliver(event_type="account-delete")
        self.assertIsNotNone(UserData.objects.get(user=user).account_deletion_requested)

    def test_already_inactive_account_is_left_alone(self):
        user, _ = self.make_apple_user()
        user.is_active = False
        user.save()
        self.deliver(event_type="account-delete")
        self.assertIsNone(UserData.objects.get(user=user).account_deletion_requested)


@isolated_cache("apple-email-forwarding")
class AppleEmailForwardingTests(StandardTestCase):
    """Hide My Email forwarding going off means every later message is silently discarded."""

    RELAY = "relay-user@privaterelay.appleid.com"

    def setUp(self):
        super().setUp()
        cache.clear()
        self.jti_counter = 0
        self.club = Club.objects.create(name="Test club")
        # A user of its own: AuctionTOS.save() merges a second record for someone who already has
        # one on the same auction, and StandardTestCase has already signed most of its users up.
        self.relay_user = User.objects.create_user(username="relay_user", password="pw", email=self.RELAY)
        self.tos = AuctionTOS.objects.create(
            user=self.relay_user,
            auction=self.online_auction,
            pickup_location=self.location,
            email=self.RELAY,
        )
        self.member = ClubMember.objects.create(club=self.club, name="Relay member", email=self.RELAY)

    # Reuses the signing helpers above without inheriting the plain-TestCase fixtures.
    signed_notification = AppleNotificationTestCase.signed_notification
    deliver = AppleNotificationTestCase.deliver

    def statuses(self):
        self.tos.refresh_from_db()
        self.member.refresh_from_db()
        return self.tos.email_address_status, self.member.email_address_status

    @override_settings(APPLE_ALLOWED_AUDIENCES=[SERVICES_ID, BUNDLE_ID])
    def test_disabled_marks_the_address_unreachable(self):
        self.deliver(event_type="email-disabled", email=self.RELAY)
        self.assertEqual(self.statuses(), ("BAD", "BAD"))

    @override_settings(APPLE_ALLOWED_AUDIENCES=[SERVICES_ID, BUNDLE_ID])
    def test_enabled_undoes_it(self):
        self.deliver(event_type="email-disabled", email=self.RELAY)
        self.deliver(event_type="email-enabled", email=self.RELAY)
        self.assertEqual(self.statuses(), ("UNKNOWN", "UNKNOWN"))

    @override_settings(APPLE_ALLOWED_AUDIENCES=[SERVICES_ID, BUNDLE_ID])
    def test_enabled_does_not_downgrade_an_address_someone_actually_confirmed(self):
        AuctionTOS.objects.filter(pk=self.tos.pk).update(email_address_status="VALID")
        self.deliver(event_type="email-enabled", email=self.RELAY)
        self.tos.refresh_from_db()
        self.assertEqual(self.tos.email_address_status, "VALID")

    @override_settings(APPLE_ALLOWED_AUDIENCES=[SERVICES_ID, BUNDLE_ID])
    def test_matching_is_case_insensitive(self):
        self.deliver(event_type="email-disabled", email=self.RELAY.upper())
        self.assertEqual(self.statuses(), ("BAD", "BAD"))

    @override_settings(APPLE_ALLOWED_AUDIENCES=[SERVICES_ID, BUNDLE_ID])
    def test_sign_in_is_not_broken_by_a_disabled_address(self):
        """Marking the allauth address unverified would lock them out of a site where it's mandatory."""
        EmailAddress.objects.create(user=self.relay_user, email=self.RELAY, verified=True, primary=True)
        self.deliver(event_type="email-disabled", email=self.RELAY)
        self.assertTrue(EmailAddress.objects.get(email=self.RELAY).verified)


class AppleNotificationRetryTests(AppleNotificationTestCase):
    """Apple retries until it gets a 2xx, so 'already done' and 'not done yet' must differ."""

    def test_a_redelivered_notification_is_not_processed_twice(self):
        user, account = self.make_apple_user()
        signed = self.signed_notification(event_type="account-delete", jti="same-jti")
        with patch("requests.get", return_value=_FakeResponse(_jwks())):
            self.assertEqual(process_notification(signed), ["account-delete"])
            # Re-linked in between (they signed in with Apple again); the retry must not re-delete it.
            SocialAccount.objects.create(user=user, provider="apple", uid="apple-sub-1")
            self.assertEqual(process_notification(signed), [])
        self.assertTrue(SocialAccount.objects.filter(provider="apple", uid="apple-sub-1").exists())

    def test_a_notification_that_failed_is_processed_on_retry(self):
        """allauth's own jti blacklist would refuse this one forever — the reason it isn't used."""
        user, account = self.make_apple_user()
        signed = self.signed_notification(event_type="consent-revoked", jti="same-jti")
        boom = RuntimeError("database went away")
        with patch("requests.get", return_value=_FakeResponse(_jwks())):
            with patch("auctions.apple_notifications._handle_consent_revoked", side_effect=boom):
                with self.assertRaises(RuntimeError):
                    process_notification(signed)
            # Apple's retry, same payload.
            self.assertEqual(process_notification(signed), ["consent-revoked"])
        self.assertFalse(SocialToken.objects.filter(account=account).exists())

    def test_a_processing_failure_reaches_the_caller_as_a_500(self):
        """A 2xx would tell Apple it was delivered, and it is never sent again."""
        self.make_apple_user()
        with patch("auctions.apple_notifications._handle_consent_revoked", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.post(self.signed_notification())

    def test_the_jwks_is_not_refetched_for_every_notification(self):
        """The endpoint is public; a fetch per POST is a way to make us hammer Apple."""
        self.make_apple_user()
        with patch("requests.get", return_value=_FakeResponse(_jwks())) as fetch:
            process_notification(self.signed_notification(sub="apple-sub-1"))
            process_notification(self.signed_notification(sub="apple-sub-1"))
        self.assertEqual(fetch.call_count, 1)


class AppleNotificationErrorTypeTests(AppleNotificationTestCase):
    """The verification helpers raise something the view can tell apart from a real bug."""

    def test_verification_failure_raises_apple_notification_error(self):
        with patch("requests.get", return_value=_FakeResponse(_jwks())):
            with self.assertRaises(AppleNotificationError):
                process_notification(self.signed_notification(key=IMPOSTOR_KEY))

    def test_empty_payload_raises_apple_notification_error(self):
        with self.assertRaises(AppleNotificationError):
            process_notification("")


class AppleNotificationChecklistTests(TestCase):
    """The setup checklist is where an admin finds out this needs registering with Apple."""

    def setUp(self):
        self.admin = User.objects.create_superuser("checklist-admin", "admin@example.com", "pw")
        self.client.force_login(self.admin)

    @override_settings(
        APPLE_SIGN_IN_BUNDLE_ID=BUNDLE_ID,
        APPLE_SIGN_IN_SERVICES_ID=SERVICES_ID,
        APPLE_ALLOWED_AUDIENCES=[SERVICES_ID, BUNDLE_ID],
    )
    def test_checklist_shows_the_endpoint_url(self):
        response = self.client.get(reverse("admin_setup_checklist"))
        self.assertEqual(response.status_code, 200)
        text = response.content.decode()
        self.assertIn("Server to Server Notification Endpoint", text)
        self.assertIn("/apple/notifications", text)
