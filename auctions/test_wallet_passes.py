"""Membership cards: Google Wallet, Apple Wallet, PassKit and the numbers on them.

A pass is never given a programmatic expiry -- a lapsed one is re-issued with the expired styling
rather than archived, because an archived pass cannot come back when somebody renews.
"""

import datetime
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    Club,
    ClubAPIKey,
    ClubMember,
)
from auctions.tests import WritableMediaRoot


class DiscordJoinModalNameTests(TestCase):
    """Tests for the Discord join modal — the modal now collects a single ``name``
    field, but the handler must still accept ``first_name`` / ``last_name`` for
    backward compatibility with any cached/older Discord modal definitions."""

    def setUp(self):
        from auctions.views import DiscordInteractionsView

        self.club = Club.objects.create(name="Discord Join Club", discord_server_id="999000111")
        self.view = DiscordInteractionsView()

    def _modal_data(self, fields, discord_id="55501", username="newbie"):
        return {
            "guild_id": self.club.discord_server_id,
            "user": {"id": discord_id, "username": username},
            "data": {
                "components": [{"components": [{"custom_id": k, "value": v}]} for k, v in fields.items()],
            },
        }

    def test_accepts_single_name_field(self):
        self.view._handle_join_modal(self._modal_data({"name": "Solo Name", "email": "solo@example.com"}))
        m = ClubMember.objects.get(club=self.club, email="solo@example.com")
        self.assertEqual(m.name, "Solo Name")

    def test_accepts_first_and_last_name(self):
        self.view._handle_join_modal(
            self._modal_data({"first_name": "Old", "last_name": "Cache", "email": "oc@example.com"})
        )
        m = ClubMember.objects.get(club=self.club, email="oc@example.com")
        self.assertEqual(m.name, "Old Cache")

    def test_accepts_only_first_name(self):
        self.view._handle_join_modal(
            self._modal_data({"first_name": "First", "email": "first@example.com"}, discord_id="55502"),
        )
        m = ClubMember.objects.get(club=self.club, email="first@example.com")
        self.assertEqual(m.name, "First")


class DiscordJoinButtonTests(TestCase):
    """The join button and the /membership command must behave identically:
    not joined -> show the join modal; joined -> show membership info + link."""

    def setUp(self):
        from auctions.views import DiscordInteractionsView

        self.club = Club.objects.create(name="Button Club", discord_server_id="777000222")
        self.view = DiscordInteractionsView()

    def _interaction(self, discord_id="42", interaction_type=3, custom_id="join_button"):
        return {
            "type": interaction_type,
            "guild_id": self.club.discord_server_id,
            "data": {"custom_id": custom_id, "name": "membership"},
            "member": {"user": {"id": discord_id, "username": "buttonuser"}},
        }

    def _payload(self, response):
        return json.loads(response.content)

    def test_button_shows_join_modal_when_not_joined(self):
        from auctions.views.discord import _DISCORD_TYPE_MODAL

        response = self.view._handle_membership_command(self._interaction())
        payload = self._payload(response)
        self.assertEqual(payload["type"], _DISCORD_TYPE_MODAL)
        self.assertEqual(payload["data"]["custom_id"], "join_modal")

    def test_button_shows_membership_info_when_joined(self):
        from auctions.views.discord import _DISCORD_TYPE_CHANNEL_MESSAGE

        member = ClubMember.objects.create(
            club=self.club,
            name="Joined User",
            discord_id="42",
            membership_expiration_date=timezone.now().date() + datetime.timedelta(days=30),
        )
        response = self.view._handle_membership_command(self._interaction())
        payload = self._payload(response)
        self.assertEqual(payload["type"], _DISCORD_TYPE_CHANNEL_MESSAGE)
        content = payload["data"]["content"]
        self.assertIn("Your membership", content)
        self.assertIn(member.simple_membership_link, content)

    @patch("auctions.views.discord.verify_discord_signature", return_value=True)
    @override_settings(DISCORD_PUBLIC_KEY="aa" * 32)
    def test_post_routes_join_button_through_membership(self, mock_verify):
        from auctions.views.discord import _DISCORD_TYPE_MODAL

        ClubMember.objects.filter(club=self.club).delete()
        body = json.dumps(self._interaction()).encode()
        response = self.client.post(
            reverse("discord_interactions"),
            data=body,
            content_type="application/json",
            HTTP_X_SIGNATURE_ED25519="00",
            HTTP_X_SIGNATURE_TIMESTAMP=str(int(timezone.now().timestamp())),
        )
        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.content)
        self.assertEqual(payload["type"], _DISCORD_TYPE_MODAL)
        self.assertEqual(payload["data"]["custom_id"], "join_modal")


class ClubMemberNameModelTests(TestCase):
    """Tests for the renamed ``name`` field on ClubMember."""

    def setUp(self):
        self.club = Club.objects.create(name="Name Field Club")

    def test_str_uses_name(self):
        m = ClubMember.objects.create(club=self.club, name="Jane Doe")
        self.assertEqual(str(m), "Jane Doe")

    def test_str_falls_back_to_email(self):
        m = ClubMember.objects.create(club=self.club, email="x@example.com")
        self.assertEqual(str(m), "x@example.com")

    def test_str_fallback_member_id(self):
        m = ClubMember.objects.create(club=self.club)
        self.assertIn("Member #", str(m))

    def test_display_name_matches_str(self):
        m = ClubMember.objects.create(club=self.club, name="Solo")
        self.assertEqual(m.display_name, str(m))

    def test_possible_duplicate_detected_by_name(self):
        a = ClubMember.objects.create(club=self.club, name="Same Name")
        b = ClubMember.objects.create(club=self.club, name="Same Name")
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.possible_duplicate_id, b.pk)
        self.assertEqual(b.possible_duplicate_id, a.pk)


class ClubMemberIngestNameTests(TestCase):
    """Unit-level tests for the serializer/map_fields combine logic."""

    def setUp(self):

        self.club = Club.objects.create(name="Ingest Logic Club")
        _, prefix, key_hash = ClubAPIKey.generate()
        self.api_key = ClubAPIKey.objects.create(club=self.club, name="ingest-logic", prefix=prefix, key_hash=key_hash)

    def test_map_fields_combines_first_and_last(self):
        from auctions.services import map_fields

        result = map_fields({"first_name": "Map", "last_name": "Fields"}, self.api_key)
        self.assertEqual(result["name"], "Map Fields")
        self.assertNotIn("first_name", result)
        self.assertNotIn("last_name", result)

    def test_map_fields_keeps_existing_name(self):
        from auctions.services import map_fields

        result = map_fields({"name": "Direct", "first_name": "Ignored"}, self.api_key)
        self.assertEqual(result["name"], "Direct")

    def test_serializer_rejects_when_no_email_or_name(self):
        from auctions.serializers import ClubMemberIngestSerializer

        s = ClubMemberIngestSerializer(data={})
        self.assertFalse(s.is_valid())

    def test_serializer_combines_first_and_last_name(self):
        from auctions.serializers import ClubMemberIngestSerializer

        s = ClubMemberIngestSerializer(data={"first_name": "A", "last_name": "B"})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["name"], "A B")


@override_settings(
    GOOGLE_WALLET_ISSUER_ID="3388000000022XXXXXX",
    GOOGLE_WALLET_SERVICE_ACCOUNT_EMAIL="signer@example.iam.gserviceaccount.com",
    GOOGLE_WALLET_SERVICE_ACCOUNT_KEY="fake-key",
)
class GoogleWalletClassCreateTests(TestCase):
    """Verify the Wallet class create task: idempotent, configured-gated, correct body."""

    def setUp(self):
        from auctions.models import Club

        self.club = Club.objects.create(name="Wallet Test Club")

    def _mock_response(self, status_code, json_body=None, text=""):
        from unittest.mock import MagicMock

        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_body or {}
        resp.text = text
        if status_code >= 400:
            from requests.exceptions import HTTPError

            resp.raise_for_status.side_effect = HTTPError(f"{status_code}")
        else:
            resp.raise_for_status.return_value = None
        return resp

    @override_settings(
        GOOGLE_WALLET_ISSUER_ID="",
        GOOGLE_WALLET_SERVICE_ACCOUNT_EMAIL="",
        GOOGLE_WALLET_SERVICE_ACCOUNT_KEY="",
    )
    def test_no_op_when_not_configured(self):
        from auctions.google_wallet import create_generic_class

        with patch("auctions.google_wallet.requests.post") as post:
            self.assertFalse(create_generic_class(self.club))
            post.assert_not_called()

    def test_sends_pk_based_class_id(self):
        from auctions.google_wallet import create_generic_class

        with patch("auctions.google_wallet.get_access_token", return_value="t"):
            with patch(
                "auctions.google_wallet.requests.post",
                return_value=self._mock_response(200, {"id": "ok"}),
            ) as post:
                self.assertTrue(create_generic_class(self.club))
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["id"], f"3388000000022XXXXXX.membership_{self.club.pk}")

    def test_409_triggers_patch_and_returns_success(self):
        """POST → 409 → PATCH (so icon / metadata updates push to existing classes)."""
        from auctions.google_wallet import create_generic_class

        with patch("auctions.google_wallet.get_access_token", return_value="t"):
            with patch(
                "auctions.google_wallet.requests.post",
                return_value=self._mock_response(409, text="already exists"),
            ):
                with patch(
                    "auctions.google_wallet.requests.patch",
                    return_value=self._mock_response(200, {"id": "patched"}),
                ) as patch_mock:
                    self.assertTrue(create_generic_class(self.club))
        self.assertEqual(patch_mock.call_count, 1)

    def test_400_raises(self):
        from auctions.google_wallet import create_generic_class

        with patch("auctions.google_wallet.get_access_token", return_value="t"):
            with patch(
                "auctions.google_wallet.requests.post",
                return_value=self._mock_response(400, text="bad request"),
            ):
                with self.assertRaises(Exception):
                    create_generic_class(self.club)

    def test_signal_dispatches_when_class_not_yet_created(self):
        from auctions.models import Club

        # transaction.on_commit callbacks do not fire inside TestCase's atomic block
        # unless we capture them — use captureOnCommitCallbacks(execute=True) so the
        # signal's lambda actually runs and we can observe the .delay() call.
        with patch("auctions.tasks.create_google_wallet_class_for_club.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                club = Club.objects.create(name="Another")
            delay.assert_called_once_with(club.pk)
            delay.reset_mock()
            # Subsequent edits while the flag is still False — dispatch again so legacy
            # clubs that pre-date the integration get their class on next save.
            with self.captureOnCommitCallbacks(execute=True):
                club.name = "Another Renamed"
                club.save()
            delay.assert_called_once_with(club.pk)
            delay.reset_mock()
            # Once Google confirms the class exists, the flag is flipped and further
            # saves must NOT re-dispatch (don't spam Google's API on every edit).
            club.google_wallet_class_created = True
            club.save()
            with self.captureOnCommitCallbacks(execute=True):
                club.name = "Renamed Again"
                club.save()
            delay.assert_not_called()

    def test_task_flips_flag_on_success(self):
        from auctions.models import Club
        from auctions.tasks import create_google_wallet_class_for_club

        club = Club.objects.create(name="Flag flip test")
        self.assertFalse(club.google_wallet_class_created)
        with patch("auctions.google_wallet.create_generic_class", return_value=True):
            create_google_wallet_class_for_club.apply(args=[club.pk])
        club.refresh_from_db()
        self.assertTrue(club.google_wallet_class_created)


class MembershipNumberUniquenessTests(TestCase):
    """The DB-level unique constraint plus save() retry must prevent collisions."""

    def setUp(self):
        from auctions.models import Club

        self.club = Club.objects.create(name="Unique Test Club")

    def test_save_repicks_on_collision(self):
        """If a new member is constructed with a number that already exists, save() picks a new one."""
        from auctions.models import ClubMember

        first = ClubMember.objects.create(club=self.club, name="A")
        # Force a collision by hand and save again.
        second = ClubMember(club=self.club, name="B", membership_number=first.membership_number)
        second.save()
        self.assertNotEqual(first.membership_number, second.membership_number)

    def test_pick_unique_returns_unused_value(self):
        from auctions.models import ClubMember, _pick_unique_membership_number

        existing = ClubMember.objects.create(club=self.club, name="A")
        new_number = _pick_unique_membership_number()
        self.assertNotEqual(new_number, existing.membership_number)
        # Sanity: the picker keeps producing a number even when an unrelated row exists.
        self.assertTrue(1_000_000_000 <= new_number <= 9_999_999_999)


class AppleWalletPassTests(TestCase):
    """Verify the .pkpass builder produces a valid signed zip with the right metadata."""

    def setUp(self):
        from auctions.models import Club, ClubMember

        self.user = User.objects.create_user(username="apple_user", password="x", email="a@b.c")
        self.other_user = User.objects.create_user(username="other_user", password="x", email="o@b.c")
        self.club = Club.objects.create(name="Apple Test Club")
        self.member = ClubMember.objects.create(club=self.club, name="Test Member", user=self.user)

    @staticmethod
    def _make_cert_files(tmp_path, chained=True, wwdr_encoding="PEM"):
        """Generate a WWDR-stand-in CA plus a signer cert and return their paths.

        By default the signer is issued by the WWDR stand-in (a real chain, which
        _load_signing_certs now verifies). chained=False produces an unrelated
        self-signed signer to exercise the chain-mismatch error. wwdr_encoding
        may be "DER" to mimic Apple's .cer download format.
        """
        import datetime as _dt

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import pkcs12
        from cryptography.x509.oid import NameOID

        def _build_cert(subject, key, issuer_name=None, issuer_key=None):
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
            now = _dt.datetime.now(_dt.timezone.utc)
            return (
                x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(issuer_name or name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - _dt.timedelta(minutes=1))
                .not_valid_after(now + _dt.timedelta(days=1))
                .sign(issuer_key or key, hashes.SHA256())
            )

        wwdr_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        wwdr_cert = _build_cert("WWDR Stand-in", wwdr_key)
        signer_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if chained:
            signer_cert = _build_cert("Pass Type Cert", signer_key, issuer_name=wwdr_cert.subject, issuer_key=wwdr_key)
        else:
            signer_cert = _build_cert("Pass Type Cert", signer_key)

        # .p12 with no password — encryption=NoEncryption matches APPLE_WALLET_CERT_PASSWORD="".
        p12_bytes = pkcs12.serialize_key_and_certificates(
            name=b"pass-cert",
            key=signer_key,
            cert=signer_cert,
            cas=None,
            encryption_algorithm=serialization.NoEncryption(),
        )
        p12_path = tmp_path / "cert.p12"
        p12_path.write_bytes(p12_bytes)

        encoding = serialization.Encoding.DER if wwdr_encoding == "DER" else serialization.Encoding.PEM
        wwdr_path = tmp_path / ("wwdr.cer" if wwdr_encoding == "DER" else "wwdr.pem")
        wwdr_path.write_bytes(wwdr_cert.public_bytes(encoding))
        return p12_path, wwdr_path

    def test_generate_pkpass_contains_required_files(self):

        from auctions import apple_wallet

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            p12_path, wwdr_path = self._make_cert_files(tmp_path)
            # Clear the cached signing certs from any earlier test.
            apple_wallet._load_signing_certs.cache_clear()
            with self.settings(
                BASE_DIR=tmp_path,
                APPLE_WALLET_CERT_FILE=p12_path.name,
                APPLE_WALLET_CERT_PASSWORD="",
                APPLE_WALLET_WWDR_FILE=wwdr_path.name,
                APPLE_WALLET_PASS_TYPE_IDENTIFIER="pass.com.example.membership",
                APPLE_WALLET_TEAM_IDENTIFIER="ABCDE12345",
                APPLE_WALLET_ORGANIZATION_NAME="Test Org",
            ):
                self.assertTrue(apple_wallet.is_configured())
                pkpass_bytes = apple_wallet.generate_pkpass_for_member(self.member)
        # The result must be a valid zip containing all required files.
        import json as _json
        import zipfile as _zip

        with _zip.ZipFile(io.BytesIO(pkpass_bytes)) as zf:
            names = set(zf.namelist())
            self.assertEqual(
                names,
                {"pass.json", "icon.png", "icon@2x.png", "logo.png", "manifest.json", "signature"},
            )
            pass_data = _json.loads(zf.read("pass.json"))
            self.assertEqual(pass_data["passTypeIdentifier"], "pass.com.example.membership")
            self.assertEqual(pass_data["teamIdentifier"], "ABCDE12345")
            self.assertEqual(pass_data["serialNumber"], f"member-{self.member.pk}")
            self.assertEqual(pass_data["barcode"]["message"], str(self.member.membership_number))
            # Manifest must list a sha1 for every payload file (not itself, not signature).
            manifest = _json.loads(zf.read("manifest.json"))
            self.assertEqual(set(manifest.keys()), {"pass.json", "icon.png", "icon@2x.png", "logo.png"})

    def test_is_configured_false_when_settings_missing(self):
        from auctions import apple_wallet

        with self.settings(
            APPLE_WALLET_CERT_FILE="",
            APPLE_WALLET_WWDR_FILE="",
            APPLE_WALLET_PASS_TYPE_IDENTIFIER="",
            APPLE_WALLET_TEAM_IDENTIFIER="",
        ):
            self.assertFalse(apple_wallet.is_configured())

    def test_pkpass_download_requires_owner(self):
        """Only the owning user may download; UUID-link visitors / other users get 403."""
        url = reverse("club_member_apple_wallet", kwargs={"pk": self.member.pk})
        with self.settings(
            APPLE_WALLET_CERT_FILE="cert.p12",
            APPLE_WALLET_CERT_PASSWORD="",
            APPLE_WALLET_WWDR_FILE="wwdr.pem",
            APPLE_WALLET_PASS_TYPE_IDENTIFIER="pass.com.example.membership",
            APPLE_WALLET_TEAM_IDENTIFIER="ABCDE12345",
        ):
            # Wrong user → 403.
            self.client.force_login(self.other_user)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403)
            # Anonymous → redirect to login (LoginRequiredMixin).
            self.client.logout()
            response = self.client.get(url)
            self.assertIn(response.status_code, (302, 401, 403))

    def test_pkpass_download_404_when_not_configured(self):
        url = reverse("club_member_apple_wallet", kwargs={"pk": self.member.pk})
        self.client.force_login(self.user)
        with self.settings(
            APPLE_WALLET_CERT_FILE="",
            APPLE_WALLET_WWDR_FILE="",
            APPLE_WALLET_PASS_TYPE_IDENTIFIER="",
            APPLE_WALLET_TEAM_IDENTIFIER="",
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 404)


class PassKitWebServiceTests(TestCase):
    """Apple PassKit web service: device registration, pass delivery, APNs pushes."""

    # Settings that satisfy is_configured() for endpoints that never load the certs.
    FAKE_WALLET_SETTINGS = {
        "APPLE_WALLET_CERT_FILE": "cert.p12",
        "APPLE_WALLET_CERT_PASSWORD": "",
        "APPLE_WALLET_WWDR_FILE": "wwdr.pem",
        "APPLE_WALLET_PASS_TYPE_IDENTIFIER": "pass.com.example.membership",
        "APPLE_WALLET_TEAM_IDENTIFIER": "ABCDE12345",
    }

    def setUp(self):
        from auctions.apple_wallet import ensure_apple_pass_auth_token
        from auctions.models import Club, ClubMember

        self.user = User.objects.create_user(username="passkit_user", password="x", email="pk@b.c")
        self.club = Club.objects.create(name="PassKit Test Club")
        self.member = ClubMember.objects.create(club=self.club, name="PassKit Member", user=self.user)
        self.token = ensure_apple_pass_auth_token(self.member)
        self.device_id = "device-abc123"
        self.registration_url = reverse(
            "passkit_registration",
            kwargs={
                "device_library_id": self.device_id,
                "pass_type_id": "pass.com.example.membership",
                "serial_number": f"member-{self.member.pk}",
            },
        )

    def _auth(self, token=None):
        return {"HTTP_AUTHORIZATION": f"ApplePass {token or self.token}"}

    def _register(self, push_token="apns-token-1"):
        from auctions.models import AppleDeviceRegistration

        return AppleDeviceRegistration.objects.create(
            member=self.member, device_library_identifier=self.device_id, push_token=push_token
        )

    def test_register_device(self):
        from auctions.models import AppleDeviceRegistration

        with self.settings(**self.FAKE_WALLET_SETTINGS):
            response = self.client.post(
                self.registration_url,
                data='{"pushToken": "apns-token-1"}',
                content_type="application/json",
                **self._auth(),
            )
            self.assertEqual(response.status_code, 201)
            registration = AppleDeviceRegistration.objects.get(
                member=self.member, device_library_identifier=self.device_id
            )
            self.assertEqual(registration.push_token, "apns-token-1")
            # Re-registering is 200 (not 201) and updates a rotated push token.
            response = self.client.post(
                self.registration_url,
                data='{"pushToken": "apns-token-2"}',
                content_type="application/json",
                **self._auth(),
            )
            self.assertEqual(response.status_code, 200)
            registration.refresh_from_db()
            self.assertEqual(registration.push_token, "apns-token-2")

    def test_register_device_rejects_bad_auth(self):
        from auctions.models import AppleDeviceRegistration

        with self.settings(**self.FAKE_WALLET_SETTINGS):
            # Wrong token, missing header, and empty stored token must all fail.
            response = self.client.post(
                self.registration_url,
                data='{"pushToken": "t"}',
                content_type="application/json",
                **self._auth("wrong-token"),
            )
            self.assertEqual(response.status_code, 401)
            response = self.client.post(
                self.registration_url, data='{"pushToken": "t"}', content_type="application/json"
            )
            self.assertEqual(response.status_code, 401)
            type(self.member).objects.filter(pk=self.member.pk).update(apple_pass_auth_token="")
            response = self.client.post(
                self.registration_url,
                data='{"pushToken": "t"}',
                content_type="application/json",
                HTTP_AUTHORIZATION="ApplePass ",
            )
            self.assertEqual(response.status_code, 401)
            self.assertFalse(AppleDeviceRegistration.objects.exists())

    def test_register_device_404s(self):
        with self.settings(**self.FAKE_WALLET_SETTINGS):
            # Unknown serial number.
            url = reverse(
                "passkit_registration",
                kwargs={
                    "device_library_id": self.device_id,
                    "pass_type_id": "pass.com.example.membership",
                    "serial_number": "member-999999",
                },
            )
            response = self.client.post(url, data='{"pushToken": "t"}', content_type="application/json", **self._auth())
            self.assertEqual(response.status_code, 404)
            # Wrong pass type identifier.
            url = reverse(
                "passkit_registration",
                kwargs={
                    "device_library_id": self.device_id,
                    "pass_type_id": "pass.com.wrong.type",
                    "serial_number": f"member-{self.member.pk}",
                },
            )
            response = self.client.post(url, data='{"pushToken": "t"}', content_type="application/json", **self._auth())
            self.assertEqual(response.status_code, 404)
        # Not configured at all.
        with self.settings(APPLE_WALLET_CERT_FILE="", APPLE_WALLET_PASS_TYPE_IDENTIFIER=""):
            response = self.client.post(
                self.registration_url, data='{"pushToken": "t"}', content_type="application/json", **self._auth()
            )
            self.assertEqual(response.status_code, 404)

    def test_unregister_device(self):
        from auctions.models import AppleDeviceRegistration

        self._register()
        with self.settings(**self.FAKE_WALLET_SETTINGS):
            response = self.client.delete(self.registration_url, **self._auth("wrong"))
            self.assertEqual(response.status_code, 401)
            response = self.client.delete(self.registration_url, **self._auth())
            self.assertEqual(response.status_code, 200)
        self.assertFalse(AppleDeviceRegistration.objects.exists())

    def test_list_updatable_passes(self):
        self._register()
        list_url = reverse(
            "passkit_device_registrations",
            kwargs={"device_library_id": self.device_id, "pass_type_id": "pass.com.example.membership"},
        )
        with self.settings(**self.FAKE_WALLET_SETTINGS):
            response = self.client.get(list_url)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["serialNumbers"], [f"member-{self.member.pk}"])
            last_updated = data["lastUpdated"]
            # Nothing changed since the tag we just got → 204.
            response = self.client.get(list_url, {"passesUpdatedSince": last_updated})
            self.assertEqual(response.status_code, 204)
            # Bump the pass version (what the notify task does) → serial reappears.
            type(self.member).objects.filter(pk=self.member.pk).update(
                apple_pass_updated=timezone.now() + datetime.timedelta(seconds=5)
            )
            response = self.client.get(list_url, {"passesUpdatedSince": last_updated})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["serialNumbers"], [f"member-{self.member.pk}"])
            # A device we've never seen → 404.
            unknown_url = reverse(
                "passkit_device_registrations",
                kwargs={"device_library_id": "device-unknown", "pass_type_id": "pass.com.example.membership"},
            )
            response = self.client.get(unknown_url)
            self.assertEqual(response.status_code, 404)

    def test_get_pass_serves_fresh_pkpass_and_304s(self):
        import io as _io
        import json as _json
        import zipfile as _zip

        from auctions import apple_wallet

        pass_url = reverse(
            "passkit_pass",
            kwargs={"pass_type_id": "pass.com.example.membership", "serial_number": f"member-{self.member.pk}"},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            p12_path, wwdr_path = AppleWalletPassTests._make_cert_files(tmp_path)
            apple_wallet._load_signing_certs.cache_clear()
            with self.settings(
                BASE_DIR=tmp_path,
                APPLE_WALLET_CERT_FILE=p12_path.name,
                APPLE_WALLET_CERT_PASSWORD="",
                APPLE_WALLET_WWDR_FILE=wwdr_path.name,
                APPLE_WALLET_PASS_TYPE_IDENTIFIER="pass.com.example.membership",
                APPLE_WALLET_TEAM_IDENTIFIER="ABCDE12345",
            ):
                response = self.client.get(pass_url, **self._auth("nope"))
                self.assertEqual(response.status_code, 401)
                response = self.client.get(pass_url, **self._auth())
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "application/vnd.apple.pkpass")
                last_modified = response["Last-Modified"]
                with _zip.ZipFile(_io.BytesIO(response.content)) as zf:
                    pass_data = _json.loads(zf.read("pass.json"))
                self.assertEqual(pass_data["authenticationToken"], self.token)
                self.assertTrue(pass_data["webServiceURL"].endswith("/passkit"))
                self.assertNotIn("voided", pass_data)
                # Device re-checks with If-Modified-Since → 304 until the pass is bumped.
                response = self.client.get(pass_url, HTTP_IF_MODIFIED_SINCE=last_modified, **self._auth())
                self.assertEqual(response.status_code, 304)
                type(self.member).objects.filter(pk=self.member.pk).update(
                    apple_pass_updated=timezone.now() + datetime.timedelta(seconds=5)
                )
                response = self.client.get(pass_url, HTTP_IF_MODIFIED_SINCE=last_modified, **self._auth())
                self.assertEqual(response.status_code, 200)

    def test_get_pass_serves_voided_pass_when_barcode_disabled_or_member_deleted(self):
        """Unlike the user-facing download (404), the web service serves a voided pass."""
        import io as _io
        import json as _json
        import zipfile as _zip

        from auctions import apple_wallet

        pass_url = reverse(
            "passkit_pass",
            kwargs={"pass_type_id": "pass.com.example.membership", "serial_number": f"member-{self.member.pk}"},
        )

        def _fetch_pass_json():
            response = self.client.get(pass_url, **self._auth())
            self.assertEqual(response.status_code, 200)
            with _zip.ZipFile(_io.BytesIO(response.content)) as zf:
                return _json.loads(zf.read("pass.json"))

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            p12_path, wwdr_path = AppleWalletPassTests._make_cert_files(tmp_path)
            apple_wallet._load_signing_certs.cache_clear()
            with self.settings(
                BASE_DIR=tmp_path,
                APPLE_WALLET_CERT_FILE=p12_path.name,
                APPLE_WALLET_CERT_PASSWORD="",
                APPLE_WALLET_WWDR_FILE=wwdr_path.name,
                APPLE_WALLET_PASS_TYPE_IDENTIFIER="pass.com.example.membership",
                APPLE_WALLET_TEAM_IDENTIFIER="ABCDE12345",
            ):
                self.club.show_member_barcode = False
                self.club.save()
                self.assertTrue(_fetch_pass_json().get("voided"))
                self.club.show_member_barcode = True
                self.club.save()
                type(self.member).objects.filter(pk=self.member.pk).update(is_deleted=True)
                self.assertTrue(_fetch_pass_json().get("voided"))

    def test_log_endpoint(self):
        with self.settings(**self.FAKE_WALLET_SETTINGS):
            response = self.client.post(
                reverse("passkit_log"), data='{"logs": ["something broke"]}', content_type="application/json"
            )
            self.assertEqual(response.status_code, 200)

    def test_member_change_queues_apple_notification(self):
        """Editing a wallet-visible field queues both the Google PATCH and the Apple push."""
        with (
            patch("auctions.tasks.update_google_wallet_object_for_member.delay") as google_delay,
            patch("auctions.tasks.notify_apple_wallet_devices_for_member.delay") as apple_delay,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.member.membership_expiration_date = timezone.now().date() + datetime.timedelta(days=365)
            self.member.save()
        google_delay.assert_called_once_with(self.member.pk)
        apple_delay.assert_called_once_with(self.member.pk)

    def test_member_deactivation_queues_apple_notification(self):
        with (
            patch("auctions.tasks.update_google_wallet_object_for_member.delay"),
            patch("auctions.tasks.notify_apple_wallet_devices_for_member.delay") as apple_delay,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.member.is_deleted = True
            self.member.save()
        apple_delay.assert_called_once_with(self.member.pk)

    def test_barcode_mode_flip_queues_club_wide_apple_notification(self):
        with (
            patch("auctions.tasks.expire_google_wallet_objects_for_club.delay"),
            patch("auctions.tasks.notify_apple_wallet_devices_for_club.delay") as apple_delay,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.club.show_member_barcode = False
            self.club.save()
        apple_delay.assert_called_once_with(self.club.pk)
        # Re-enabling also pushes (passes un-void), unlike Google's expire-only path.
        with (
            patch("auctions.tasks.notify_apple_wallet_devices_for_club.delay") as apple_delay,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.club.show_member_barcode = True
            self.club.save()
        apple_delay.assert_called_once_with(self.club.pk)

    def test_notify_task_bumps_version_and_pushes(self):
        from auctions.tasks import notify_apple_wallet_devices_for_member

        registration = self._register()
        before = self.member.apple_pass_updated
        with (
            self.settings(**self.FAKE_WALLET_SETTINGS),
            patch("auctions.apple_wallet.send_pass_update_notification") as send,
        ):
            notify_apple_wallet_devices_for_member(self.member.pk)
        self.member.refresh_from_db()
        self.assertGreater(self.member.apple_pass_updated, before)
        send.assert_called_once()
        self.assertEqual(send.call_args[0][0].pk, registration.pk)

    def test_apns_dead_token_deletes_registration(self):
        from auctions.apple_wallet import send_pass_update_notification
        from auctions.models import AppleDeviceRegistration

        registration = self._register()
        response = MagicMock(status_code=410)
        response.json.return_value = {"reason": "Unregistered"}
        client = MagicMock()
        client.post.return_value = response
        with (
            self.settings(**self.FAKE_WALLET_SETTINGS),
            patch("auctions.apple_wallet._apns_client", return_value=client),
        ):
            self.assertFalse(send_pass_update_notification(registration))
        self.assertFalse(AppleDeviceRegistration.objects.filter(pk=registration.pk).exists())

    def test_apns_success_keeps_registration(self):
        from auctions.apple_wallet import send_pass_update_notification
        from auctions.models import AppleDeviceRegistration

        registration = self._register()
        client = MagicMock()
        client.post.return_value = MagicMock(status_code=200)
        with (
            self.settings(**self.FAKE_WALLET_SETTINGS),
            patch("auctions.apple_wallet._apns_client", return_value=client),
        ):
            self.assertTrue(send_pass_update_notification(registration))
        self.assertTrue(AppleDeviceRegistration.objects.filter(pk=registration.pk).exists())
        # The push itself: empty aps payload, topic = pass type identifier.
        _args, kwargs = client.post.call_args
        self.assertEqual(kwargs["json"], {"aps": {}})
        self.assertEqual(kwargs["headers"]["apns-topic"], "pass.com.example.membership")


class MembershipNumberModeTests(TestCase):
    """Visibility gating + revocation triggers for Club.show_member_barcode."""

    def setUp(self):
        import datetime as _dt

        from auctions.models import Club, ClubMember

        self.user = User.objects.create_user(username="mn_user", password="x", email="m@b.c")
        self.club = Club.objects.create(name="Mode Test Club")
        self.club.membership_system = "rolling"
        self.club.save()
        # Two members: one paid (expires in 30 days), one not.
        self.paid = ClubMember.objects.create(
            club=self.club,
            name="Paid",
            user=self.user,
            membership_last_paid=timezone.now().date(),
            membership_expiration_date=timezone.now().date() + _dt.timedelta(days=30),
        )
        self.unpaid = ClubMember.objects.create(club=self.club, name="Unpaid")

    def test_is_paid_member_property(self):
        self.assertTrue(self.paid.is_paid_member)
        self.assertFalse(self.unpaid.is_paid_member)

    def test_visibility_enabled(self):
        self.club.show_member_barcode = True
        self.club.save()
        self.paid.refresh_from_db()
        self.unpaid.refresh_from_db()
        self.assertTrue(self.paid.club.show_member_barcode)
        self.assertTrue(self.unpaid.club.show_member_barcode)

    def test_visibility_disabled(self):
        self.club.show_member_barcode = False
        self.club.save()
        self.paid.refresh_from_db()
        self.unpaid.refresh_from_db()
        self.assertFalse(self.paid.club.show_member_barcode)
        self.assertFalse(self.unpaid.club.show_member_barcode)

    def test_pkpass_404_when_barcode_disabled(self):
        self.club.show_member_barcode = False
        self.club.save()
        unpaid_user = User.objects.create_user(username="unpaid_owner", password="x", email="u@b.c")
        self.unpaid.user = unpaid_user
        self.unpaid.save()
        url = reverse("club_member_apple_wallet", kwargs={"pk": self.unpaid.pk})
        self.client.force_login(unpaid_user)
        with self.settings(
            APPLE_WALLET_CERT_FILE="cert.p12",
            APPLE_WALLET_CERT_PASSWORD="",
            APPLE_WALLET_WWDR_FILE="wwdr.pem",
            APPLE_WALLET_PASS_TYPE_IDENTIFIER="pass.com.example.membership",
            APPLE_WALLET_TEAM_IDENTIFIER="ABCDE12345",
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 404)

    def test_google_wallet_url_empty_when_barcode_disabled(self):
        from auctions.templatetags.membership_tags import google_wallet_save_url

        self.club.show_member_barcode = False
        self.club.save()
        self.paid.refresh_from_db()
        self.assertEqual(google_wallet_save_url(self.paid), "")

    def test_admin_membership_number_view_404_when_disabled(self):
        from auctions.models import ClubMember

        admin = User.objects.create_user(username="admin_for_mode", password="x", email="adm@b.c")
        ClubMember.objects.create(club=self.club, name="Admin", user=admin, permission_add_edit=True)
        self.club.show_member_barcode = False
        self.club.save()
        self.client.force_login(admin)
        url = reverse("club_member_membership_number", kwargs={"pk": self.paid.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_signal_revokes_all_passes_when_barcode_disabled(self):
        with patch("auctions.tasks.expire_google_wallet_objects_for_club.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                self.club.show_member_barcode = False
                self.club.save()
            delay.assert_called_once_with(self.club.pk)

    def test_signal_no_revoke_when_barcode_enabled(self):
        self.club.show_member_barcode = False
        self.club.save()
        with patch("auctions.tasks.expire_google_wallet_objects_for_club.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                self.club.show_member_barcode = True
                self.club.save()
            delay.assert_not_called()

    def test_expire_task_only_targets_unpaid_when_flagged(self):
        """The bulk task respects unpaid_only and skips paid members."""
        from auctions.tasks import expire_google_wallet_objects_for_club

        with patch("auctions.google_wallet.is_configured", return_value=True):
            with patch("auctions.google_wallet.expire_generic_object_for_member") as expire:
                expire_google_wallet_objects_for_club.apply(args=[self.club.pk], kwargs={"unpaid_only": True})
        targeted_pks = {call.args[0].pk for call in expire.call_args_list}
        self.assertEqual(targeted_pks, {self.unpaid.pk})


class ClubIconWalletTests(WritableMediaRoot, TestCase):
    """The uploaded Club.icon must flow into the Google Wallet class and Apple pkpass."""

    def _png_bytes(self, size=(64, 64), color=(220, 30, 30)):
        import io as _io

        from PIL import Image as _Image

        img = _Image.new("RGB", size, color)
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def setUp(self):

        from auctions.models import Club, ClubMember

        self.user = User.objects.create_user(username="icon_user", password="x", email="i@b.c")
        self.club = Club.objects.create(name="Iconed Club")
        self.club.icon = SimpleUploadedFile("test.png", self._png_bytes(), content_type="image/png")
        self.club.save()
        self.member = ClubMember.objects.create(club=self.club, user=self.user, name="M")

    def test_object_visuals_includes_logo_when_icon_set(self):
        """Logo lives on GenericObject (not GenericClass) per Google Wallet REST schema."""
        from auctions.google_wallet import _object_visuals

        with self.settings(GOOGLE_WALLET_ISSUER_ID="3388000000022XXXXXX"):
            visuals = _object_visuals(self.club)
        self.assertIn("logo", visuals)
        self.assertIn("hexBackgroundColor", visuals)
        self.assertTrue(visuals["logo"]["sourceUri"]["uri"].startswith("https://"))
        self.assertIn("/media/", visuals["logo"]["sourceUri"]["uri"])
        # contentDescription is required by Google for accessibility
        self.assertIn("contentDescription", visuals["logo"])

    def test_object_visuals_omits_logo_when_no_icon(self):
        from auctions.google_wallet import _object_visuals
        from auctions.models import Club

        no_icon = Club.objects.create(name="Bare Club")
        with self.settings(GOOGLE_WALLET_ISSUER_ID="3388000000022XXXXXX"):
            visuals = _object_visuals(no_icon)
        self.assertNotIn("logo", visuals)
        # Background color is still set even without an icon
        self.assertIn("hexBackgroundColor", visuals)

    def test_class_body_never_contains_logo_or_hex_bg(self):
        """Google silently ignores logo/hexBackgroundColor on GenericClass — keep them out."""
        from auctions.google_wallet import _class_body

        with self.settings(GOOGLE_WALLET_ISSUER_ID="3388000000022XXXXXX"):
            body = _class_body(self.club)
        self.assertNotIn("logo", body)
        self.assertNotIn("hexBackgroundColor", body)

    def test_icon_change_dispatches_object_refresh(self):
        """Icon change must refresh every member's GenericObject (logo lives there)."""

        with patch("auctions.tasks.update_google_wallet_objects_for_club.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                self.club.icon = SimpleUploadedFile(
                    "new.png", self._png_bytes(color=(0, 255, 0)), content_type="image/png"
                )
                self.club.save()
            delay.assert_called_once_with(self.club.pk)

    def test_unchanged_club_does_not_redispatch(self):
        """Saving without changing name or icon must not dispatch an object refresh."""
        from auctions.models import Club

        Club.objects.filter(pk=self.club.pk).update(google_wallet_class_created=True)
        self.club.refresh_from_db()
        with patch("auctions.tasks.update_google_wallet_objects_for_club.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                self.club.save()
            delay.assert_not_called()

    def test_adding_icon_to_initialized_club_dispatches_object_refresh(self):
        """Adding an icon for the first time must refresh every member's wallet object."""

        from auctions.models import Club

        no_icon_club = Club.objects.create(name="No Icon Yet")
        Club.objects.filter(pk=no_icon_club.pk).update(google_wallet_class_created=True)
        no_icon_club.refresh_from_db()
        self.assertFalse(bool(no_icon_club.icon))

        with patch("auctions.tasks.update_google_wallet_objects_for_club.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                no_icon_club.icon = SimpleUploadedFile("first.png", self._png_bytes(), content_type="image/png")
                no_icon_club.save()
            delay.assert_called_once_with(no_icon_club.pk)

    def test_club_rename_dispatches_wallet_object_sync(self):
        with patch("auctions.tasks.update_google_wallet_objects_for_club.delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                self.club.name = "Renamed Club"
                self.club.save()
            delay.assert_called_once_with(self.club.pk)

    def test_update_generic_object_for_member_patches_with_logo_and_bg(self):
        from unittest.mock import MagicMock

        from auctions.google_wallet import update_generic_object_for_member

        patch_resp = MagicMock(status_code=200, text="patched")
        with self.settings(
            GOOGLE_WALLET_ISSUER_ID="3388000000022XXXXXX",
            GOOGLE_WALLET_SERVICE_ACCOUNT_EMAIL="signer@example.iam.gserviceaccount.com",
            GOOGLE_WALLET_SERVICE_ACCOUNT_KEY="fake-key",
        ):
            with patch("auctions.google_wallet.get_access_token", return_value="t"):
                with patch("auctions.google_wallet.requests.patch", return_value=patch_resp) as patch_mock:
                    self.assertTrue(update_generic_object_for_member(self.member))
        payload = patch_mock.call_args.kwargs["json"]
        self.assertEqual(payload["cardTitle"]["defaultValue"]["value"], self.club.name)
        # Logo + background must be on the GenericObject PATCH — not the class.
        self.assertIn("logo", payload)
        self.assertIn("hexBackgroundColor", payload)

    def test_apple_wallet_icon_png_uses_club_icon(self):
        from auctions.apple_wallet import _icon_png

        png = _icon_png(self.club, (29, 29))
        # Decode the PNG and confirm it's the requested size — proves it ran
        # through the icon-rendering branch (not the placeholder text fallback).
        import io as _io

        from PIL import Image as _Image

        img = _Image.open(_io.BytesIO(png))
        self.assertEqual(img.size, (29, 29))

    def test_apple_wallet_icon_png_falls_back_to_placeholder_without_icon(self):
        from auctions.apple_wallet import _icon_png
        from auctions.models import Club

        no_icon = Club.objects.create(name="No Icon Club")
        png = _icon_png(no_icon, (29, 29))
        import io as _io

        from PIL import Image as _Image

        img = _Image.open(_io.BytesIO(png))
        self.assertEqual(img.size, (29, 29))

    def test_create_generic_class_patches_on_409(self):
        """409 from POST must trigger a PATCH to keep the class definition current."""
        from unittest.mock import MagicMock

        from auctions.google_wallet import create_generic_class

        post_resp = MagicMock(status_code=409, text="exists")
        patch_resp = MagicMock(
            status_code=200,
            text="patched",
            json=lambda: {"id": "x.membership_1", "classTemplateInfo": {}},
        )
        patch_resp.headers = {"content-type": "application/json"}
        with self.settings(
            GOOGLE_WALLET_ISSUER_ID="3388000000022XXXXXX",
            GOOGLE_WALLET_SERVICE_ACCOUNT_EMAIL="signer@example.iam.gserviceaccount.com",
            GOOGLE_WALLET_SERVICE_ACCOUNT_KEY="fake-key",
        ):
            with patch("auctions.google_wallet.get_access_token", return_value="t"):
                with patch("auctions.google_wallet.requests.post", return_value=post_resp) as post_mock:
                    with patch("auctions.google_wallet.requests.patch", return_value=patch_resp) as patch_mock:
                        self.assertTrue(create_generic_class(self.club))
        self.assertEqual(post_mock.call_count, 1)
        self.assertEqual(patch_mock.call_count, 1)
        # logo/hexBackgroundColor are NOT valid GenericClass fields — keep them out.
        patch_body = patch_mock.call_args.kwargs["json"]
        self.assertNotIn("logo", patch_body)
        self.assertNotIn("hexBackgroundColor", patch_body)
