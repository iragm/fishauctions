"""Tests for the mobile-app web-side features.

Part 1 — label printing: the mismatch-warning matrix, the ThermalPrinterProfile command-program
validator + seed data, and the mobile printer/label API.
Part 8 — printer identification: the device-info match patterns served to the app, and the
observed-printer feed that tells us which printers need a profile.
Part 9 — binary label endpoints must accept an honest Accept header (application/pdf, image/png).
Part 2 — push notifications: the push-routing decision (user_prefers_push / notify_user), the
send_push_to_user fan-out + token pruning, device register/unregister, and the promo push job.
Part T/X — the TSPL printer profile and the v2 command-program schema it needs.
Part U/Y — adding a printer without an app release: what the app can capture about an unknown
printer, and drafting a profile from it.
Part W — bulk Bluetooth printing: the lot-set deep link and marking labels printed natively.
"""

import datetime
import io
import json
import uuid
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from auctions import notifications
from auctions.mobile.services.devices import DeviceService
from auctions.models import (
    Auction,
    Lot,
    MobileDevice,
    ObservedPrinter,
    PickupLocation,
    PushNotificationSent,
    ThermalPrinterProfile,
    UserLabelPrefs,
)
from auctions.printer_drafts import (
    DraftError,
    draft_profile_from_observation,
    pick_gatt_ids,
    profile_matches_observation,
)
from auctions.printer_programs import (
    LANGUAGE_TEMPLATES,
    PROGRAM_SCHEMA_VERSION,
    ProgramValidationError,
    validate_match_patterns,
    validate_profile_programs,
)
from auctions.printing import label_prefs_warnings, warning_matrix
from auctions.tests import StandardTestCase

# A plausible-looking inline service-account JSON; push_configured() only checks it's non-empty and
# no real FCM call is made (send_push_to_user.delay / send_fcm_message are mocked where needed).
FAKE_FIREBASE = '{"type": "service_account", "project_id": "x"}'


def _bearer(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


# ---------------------------------------------------------------------------
# Part 1 — label-prefs mismatch warnings
# ---------------------------------------------------------------------------


class LabelPrefsWarningsTests(TestCase):
    def _prefs(self, **kwargs):
        user = User.objects.create_user(username=f"warn{User.objects.count()}", password="x")
        prefs, _ = UserLabelPrefs.objects.get_or_create(user=user)
        for key, value in kwargs.items():
            setattr(prefs, key, value)
        prefs.save()
        return prefs

    def test_pdf_with_thermal_size_warns(self):
        prefs = self._prefs(print_method="pdf", preset="thermal_sm")
        self.assertTrue(any("thermal roll" in w for w in label_prefs_warnings(prefs)))

    def test_system_with_thermal_size_warns(self):
        prefs = self._prefs(print_method="system", preset="thermal_very_sm")
        self.assertTrue(label_prefs_warnings(prefs))

    def test_bluetooth_with_sheet_size_warns(self):
        prefs = self._prefs(print_method="bluetooth", preset="sm")
        self.assertTrue(any("thermal" in w.lower() for w in label_prefs_warnings(prefs)))

    def test_pdf_with_sheet_size_is_fine(self):
        prefs = self._prefs(print_method="pdf", preset="lg")
        self.assertEqual(label_prefs_warnings(prefs), [])

    def test_bluetooth_with_thermal_size_is_fine(self):
        prefs = self._prefs(print_method="bluetooth", preset="thermal_sm")
        self.assertEqual(label_prefs_warnings(prefs), [])

    def test_bluetooth_custom_too_large_warns(self):
        ThermalPrinterProfile.objects.create(
            slug="tiny",
            name="Tiny",
            print_program=[{"tx": "1d 0c"}],
            max_label_width_mm=50,
            max_label_height_mm=50,
        )
        prefs = self._prefs(print_method="bluetooth", preset="custom", unit="in", label_width=10, label_height=10)
        self.assertTrue(any("large" in w.lower() for w in label_prefs_warnings(prefs)))

    def test_warning_matrix_shape(self):
        matrix = warning_matrix()
        self.assertTrue(matrix["pdf|thermal_sm"])
        self.assertTrue(matrix["bluetooth|sm"])
        self.assertEqual(matrix["pdf|lg"], [])
        self.assertEqual(matrix["bluetooth|thermal_sm"], [])


# ---------------------------------------------------------------------------
# Part 1 — printer command-program validation + seed data
# ---------------------------------------------------------------------------


class PrinterProgramValidationTests(TestCase):
    def test_seed_programs_are_valid(self):
        for slug in ("d11s-aiyin", "d11s-lujiang", "escpos-raster"):
            profile = ThermalPrinterProfile.objects.get(slug=slug)
            validate_profile_programs(
                print_program=profile.print_program,
                status_program=profile.status_program,
                label_size_program=profile.label_size_program,
                status_flags=profile.status_flags,
                label_size_parse=profile.label_size_parse,
            )

    def test_print_program_required(self):
        with self.assertRaises(ProgramValidationError):
            validate_profile_programs(print_program=None)

    def test_bad_hex_rejected(self):
        with self.assertRaises(ProgramValidationError):
            validate_profile_programs(print_program=[{"tx": "zz"}])

    def test_odd_length_hex_rejected(self):
        with self.assertRaises(ProgramValidationError):
            validate_profile_programs(print_program=[{"tx": "1d 0"}])

    def test_unknown_placeholder_rejected(self):
        with self.assertRaises(ProgramValidationError):
            validate_profile_programs(print_program=[{"tx": "1d {nope}"}])

    def test_u16le_placeholder_allowed(self):
        validate_profile_programs(
            print_program=[{"tx": "1d 76 30 00 {u16le:width_bytes} {u16le:height_px}"}, {"tx_raster": True}]
        )

    def test_nested_repeat_rejected(self):
        with self.assertRaises(ProgramValidationError):
            validate_profile_programs(print_program=[{"repeat_per_copy": [{"repeat_per_copy": [{"tx": "00"}]}]}])

    def test_two_actions_in_one_step_rejected(self):
        with self.assertRaises(ProgramValidationError):
            validate_profile_programs(print_program=[{"tx": "00", "delay_ms": 5}])

    def test_tx_raster_must_be_true(self):
        with self.assertRaises(ProgramValidationError):
            validate_profile_programs(print_program=[{"tx_raster": False}])

    def test_negative_delay_rejected(self):
        with self.assertRaises(ProgramValidationError):
            validate_profile_programs(print_program=[{"delay_ms": -1}])

    def test_await_on_timeout_validated(self):
        with self.assertRaises(ProgramValidationError):
            validate_profile_programs(print_program=[{"await": {"any_hex_prefix": ["AA"], "on_timeout": "explode"}}])

    def test_model_clean_wraps_validation_error(self):
        profile = ThermalPrinterProfile(slug="bad", name="Bad", print_program=[{"tx": "zzz"}])
        with self.assertRaises(ValidationError):
            profile.clean()

    def test_model_clean_accepts_valid(self):
        profile = ThermalPrinterProfile(slug="ok", name="OK", print_program=[{"tx": "1d 0c"}])
        profile.clean()  # must not raise


# ---------------------------------------------------------------------------
# Part 8.1 — matching on what the printer reports over GATT 0x180A
# ---------------------------------------------------------------------------


class PrinterMatchPatternTests(TestCase):
    def test_seeded_d11s_rows_have_device_info_patterns(self):
        for slug in ("d11s-aiyin", "d11s-lujiang"):
            profile = ThermalPrinterProfile.objects.get(slug=slug)
            self.assertTrue(profile.model_patterns, slug)
            self.assertTrue(profile.manufacturer_patterns, slug)

    def test_empty_patterns_allowed(self):
        validate_match_patterns([], "model_patterns")
        validate_match_patterns(None, "model_patterns")

    def test_bad_regex_rejected(self):
        with self.assertRaises(ProgramValidationError):
            validate_match_patterns(["^d11("], "model_patterns")

    def test_non_string_entry_rejected(self):
        with self.assertRaises(ProgramValidationError):
            validate_match_patterns([7], "manufacturer_patterns")

    def test_non_list_rejected(self):
        with self.assertRaises(ProgramValidationError):
            validate_match_patterns("^d11", "model_patterns")

    def test_model_clean_rejects_bad_pattern(self):
        profile = ThermalPrinterProfile(
            slug="bad-pattern", name="Bad pattern", print_program=[{"tx": "1d 0c"}], model_patterns=["*nope"]
        )
        with self.assertRaises(ValidationError) as caught:
            profile.clean()
        self.assertIn("model_patterns", caught.exception.message_dict)


# ---------------------------------------------------------------------------
# Part 1 — mobile printer profiles API
# ---------------------------------------------------------------------------


class MobilePrinterProfilesApiTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("mobile-printer-profiles")

    def test_lists_enabled_profiles_in_priority_order(self):
        resp = self.client.get(self.url, **_bearer(self.user))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("schema_version_max", data)
        slugs = [p["slug"] for p in data["profiles"]]
        self.assertEqual(slugs[:2], ["d11s-aiyin", "d11s-lujiang"])

    def test_disabled_profiles_excluded(self):
        ThermalPrinterProfile.objects.filter(slug="d11s-aiyin").update(enabled=False)
        data = self.client.get(self.url, **_bearer(self.user)).json()
        self.assertNotIn("d11s-aiyin", [p["slug"] for p in data["profiles"]])

    def test_etag_returns_304(self):
        resp = self.client.get(self.url, **_bearer(self.user))
        etag = resp["ETag"]
        resp2 = self.client.get(self.url, HTTP_IF_NONE_MATCH=etag, **_bearer(self.user))
        self.assertEqual(resp2.status_code, 304)

    def test_match_section_carries_device_info_patterns(self):
        data = self.client.get(self.url, **_bearer(self.user)).json()
        match = next(p for p in data["profiles"] if p["slug"] == "d11s-aiyin")["match"]
        self.assertEqual(match["model_patterns"], ["^d11"])
        self.assertIn("aiyin", match["manufacturer_patterns"])
        self.assertIn("ble_name_patterns", match)

    def test_requires_jwt(self):
        self.assertIn(self.client.get(self.url).status_code, (401, 403))


# ---------------------------------------------------------------------------
# Part 8.2 — POST /api/mobile/printers/observed/
# ---------------------------------------------------------------------------


class MobilePrinterObservedApiTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("mobile-printer-observed")

    def _post(self, user=None, **overrides):
        payload = {
            "ble_name": "D11-4C21",
            "manufacturer": "AiYin",
            "model": "D11S",
            "firmware": "1.0.3",
            "hardware": "V2",
            "service_uuids": ["18F0", "180A", "18f0"],
            "profile_slug": "d11s-aiyin",
            "matched_by": "deviceInfo",
        }
        payload.update(overrides)
        return self.client.post(self.url, payload, content_type="application/json", **_bearer(user or self.user))

    def test_records_a_pairing(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 201)
        observed = ObservedPrinter.objects.get(user=self.user)
        self.assertEqual(observed.model, "D11S")
        self.assertEqual(observed.manufacturer, "AiYin")
        self.assertEqual(observed.profile_slug, "d11s-aiyin")
        self.assertEqual(observed.matched_by, "deviceInfo")
        self.assertEqual(observed.times_seen, 1)

    def test_service_uuids_lowercased_and_deduped(self):
        self._post()
        self.assertEqual(ObservedPrinter.objects.get(user=self.user).service_uuids, ["18f0", "180a"])

    def test_repeat_pairing_bumps_count_not_rows(self):
        self._post()
        resp = self._post(firmware="1.0.4")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ObservedPrinter.objects.filter(user=self.user).count(), 1)
        observed = ObservedPrinter.objects.get(user=self.user)
        self.assertEqual(observed.times_seen, 2)
        self.assertEqual(observed.firmware, "1.0.4")  # refreshed to current truth

    def test_manual_row_without_profile_is_kept(self):
        # The work queue: a printer nobody had a profile for, and the user cancelled the dialog.
        resp = self._post(profile_slug=None, matched_by="manual", model="", manufacturer="")
        self.assertEqual(resp.status_code, 201)
        observed = ObservedPrinter.objects.get(user=self.user)
        self.assertEqual(observed.profile_slug, "")
        self.assertEqual(observed.matched_by, "manual")

    def test_different_printer_is_a_new_row(self):
        self._post()
        self._post(ble_name="Fichero-99", model="D11")
        self.assertEqual(ObservedPrinter.objects.filter(user=self.user).count(), 2)

    def test_each_user_gets_their_own_row(self):
        self._post()
        self._post(user=self.userB)
        self.assertEqual(ObservedPrinter.objects.count(), 2)

    def test_printed_ok_latches_true(self):
        self._post(printed_ok=True)
        self._post()  # a later pairing that didn't print must not unsay it
        self.assertTrue(ObservedPrinter.objects.get(user=self.user).printed_ok)

    def test_over_long_strings_are_truncated_not_rejected(self):
        resp = self._post(model="M" * 400)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(len(ObservedPrinter.objects.get(user=self.user).model), 100)

    def test_nulls_are_tolerated(self):
        # Dart omits nothing: an unset field arrives as an explicit null.
        resp = self._post(ble_name=None, manufacturer=None, model=None, firmware=None, service_uuids=None)
        self.assertEqual(resp.status_code, 201)
        observed = ObservedPrinter.objects.get(user=self.user)
        self.assertEqual(observed.manufacturer, "")
        self.assertEqual(observed.service_uuids, [])

    def test_unknown_matched_by_rejected(self):
        self.assertEqual(self._post(matched_by="telepathy").status_code, 400)

    def test_matched_by_required(self):
        resp = self.client.post(self.url, {"ble_name": "D11"}, content_type="application/json", **_bearer(self.user))
        self.assertEqual(resp.status_code, 400)

    def test_requires_jwt(self):
        self.assertIn(self.client.post(self.url, {}, content_type="application/json").status_code, (401, 403))


# ---------------------------------------------------------------------------
# Part 1 — mobile label prefs API + PDF renderer
# ---------------------------------------------------------------------------


class MobileLabelPrefsApiTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("mobile-label-prefs")

    def test_get_creates_and_returns_prefs_with_warnings(self):
        resp = self.client.get(self.url, **_bearer(self.user))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("print_method", body)
        self.assertIn("warnings", body)

    def test_patch_updates_writable_subset(self):
        resp = self.client.patch(
            self.url,
            {"print_method": "bluetooth", "preset": "thermal_sm"},
            content_type="application/json",
            **_bearer(self.user),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(UserLabelPrefs.objects.get(user=self.user).print_method, "bluetooth")

    def test_patch_returns_recomputed_warnings(self):
        resp = self.client.patch(
            self.url,
            {"print_method": "pdf", "preset": "thermal_sm"},
            content_type="application/json",
            **_bearer(self.user),
        )
        self.assertTrue(resp.json()["warnings"])

    def test_requires_jwt(self):
        self.assertIn(self.client.get(self.url).status_code, (401, 403))


class MobileLabelPdfTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("mobile-label-lot", kwargs={"pk": self.lot.pk})

    def test_pdf_format_returns_pdf(self):
        resp = self.client.get(self.url, {"fmt": "pdf"}, **_bearer(self.user))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertEqual(resp.content[:4], b"%PDF")

    def test_pdf_forbidden_for_non_owner(self):
        resp = self.client.get(self.url, {"fmt": "pdf"}, **_bearer(self.userB))
        self.assertEqual(resp.status_code, 403)


class MobileLabelAcceptHeaderTests(StandardTestCase):
    """Part 9 — an honest Accept header must not be a 406.

    DRF negotiates content before authentication, against the view's renderers; with the default
    JSON-only set, ``Accept: application/pdf`` / ``image/png`` 406'd before the view ran and *all*
    label fetching broke in production.
    """

    def setUp(self):
        super().setUp()
        self.url = reverse("mobile-label-lot", kwargs={"pk": self.lot.pk})

    def test_accept_pdf_returns_pdf(self):
        resp = self.client.get(self.url, {"fmt": "pdf"}, HTTP_ACCEPT="application/pdf", **_bearer(self.user))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")
        self.assertEqual(resp.content[:4], b"%PDF")

    def test_accept_png_returns_png(self):
        resp = self.client.get(self.url, HTTP_ACCEPT="image/png", **_bearer(self.user))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/png")

    def test_accept_any_still_works(self):
        resp = self.client.get(self.url, HTTP_ACCEPT="*/*", **_bearer(self.user))
        self.assertEqual(resp.status_code, 200)

    def test_error_body_is_json_even_for_a_binary_accept(self):
        # The app reads `detail` off DRF errors; a binary-only Accept must not corrupt it.
        resp = self.client.get(self.url, {"fmt": "pdf"}, HTTP_ACCEPT="application/pdf", **_bearer(self.userB))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp["Content-Type"], "application/json")
        self.assertIn("detail", resp.json())


# ---------------------------------------------------------------------------
# Part 2 — push routing decision
# ---------------------------------------------------------------------------


class PushConfiguredTests(TestCase):
    # Pinned empty rather than relying on the ambient env: a dev box / staging with real
    # FIREBASE_CREDENTIALS_JSON exported would otherwise make "unconfigured" tests fail.
    @override_settings(FIREBASE_CREDENTIALS_JSON="")
    def test_default_is_disabled(self):
        self.assertFalse(notifications.push_configured())

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_enabled_when_credentials_present(self):
        self.assertTrue(notifications.push_configured())


class UserPrefersPushTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="pushpref", password="x")
        self.ud = self.user.userdata

    def _device(self, token="tok", push_enabled=True):
        return MobileDevice.objects.create(
            user=self.user, device_uuid=uuid.uuid4(), fcm_token=token, push_enabled=push_enabled
        )

    def test_has_push_device(self):
        self.assertFalse(self.user.userdata.has_push_device)
        self._device()
        self.assertTrue(self.user.userdata.has_push_device)

    def test_blank_token_is_not_a_push_device(self):
        self._device(token="")
        self.assertFalse(self.user.userdata.has_push_device)

    def test_disabled_device_is_not_a_push_device(self):
        self._device(push_enabled=False)
        self.assertFalse(self.user.userdata.has_push_device)

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_requires_optin_and_device(self):
        self.assertFalse(self.user.userdata.user_prefers_push())  # no opt-in, no device
        self.ud.push_notifications_instead_of_email = True
        self.ud.save()
        self.assertFalse(self.user.userdata.user_prefers_push())  # opted in but no device
        self._device()
        self.assertTrue(self.user.userdata.user_prefers_push())

    @override_settings(FIREBASE_CREDENTIALS_JSON="")
    def test_false_when_push_not_configured_globally(self):
        self.ud.push_notifications_instead_of_email = True
        self.ud.save()
        self._device()
        self.assertFalse(self.user.userdata.user_prefers_push())  # FIREBASE unset


class NotifyUserTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="notify", password="x")
        self.ud = self.user.userdata
        self.ud.push_notifications_instead_of_email = True
        self.ud.save()
        MobileDevice.objects.create(user=self.user, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True)

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_account_category_never_pushes(self):
        sent = []
        with patch("auctions.tasks.send_push_to_user.delay") as delay:
            pushed = notifications.notify_user(
                self.user, category="account", title="t", body="b", url="u", send_email=lambda: sent.append(1)
            )
        self.assertFalse(pushed)
        self.assertEqual(sent, [1])
        delay.assert_not_called()

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_pushes_when_user_prefers_push(self):
        sent = []
        with patch("auctions.tasks.send_push_to_user.delay") as delay:
            pushed = notifications.notify_user(
                self.user, category="invoice", title="t", body="b", url="u", send_email=lambda: sent.append(1)
            )
        self.assertTrue(pushed)
        self.assertEqual(sent, [])
        delay.assert_called_once()

    @override_settings(FIREBASE_CREDENTIALS_JSON="")
    def test_falls_back_to_email_when_unconfigured(self):
        sent = []
        with patch("auctions.tasks.send_push_to_user.delay") as delay:
            pushed = notifications.notify_user(
                self.user, category="invoice", title="t", body="b", url="u", send_email=lambda: sent.append(1)
            )
        self.assertFalse(pushed)
        self.assertEqual(sent, [1])
        delay.assert_not_called()

    def test_none_user_emails(self):
        sent = []
        pushed = notifications.notify_user(
            None, category="invoice", title="t", body="b", url="u", send_email=lambda: sent.append(1)
        )
        self.assertFalse(pushed)
        self.assertEqual(sent, [1])


# ---------------------------------------------------------------------------
# Part 2 — send_push_to_user fan-out + token pruning
# ---------------------------------------------------------------------------


class SendPushToUserTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="fanout", password="x")
        self.device = MobileDevice.objects.create(
            user=self.user, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True
        )

    def test_logs_row_on_success(self):
        from auctions.tasks import send_push_to_user

        with patch("auctions.notifications.send_fcm_message", return_value=notifications.SEND_OK):
            count = send_push_to_user(self.user.pk, title="t", body="b", url="u", category="invoice")
        self.assertEqual(count, 1)
        self.assertEqual(PushNotificationSent.objects.filter(user=self.user, category="invoice").count(), 1)

    def test_prunes_dead_token(self):
        from auctions.tasks import send_push_to_user

        with patch("auctions.notifications.send_fcm_message", return_value=notifications.SEND_INVALID_TOKEN):
            count = send_push_to_user(self.user.pk, title="t", body="b", url="u", category="invoice")
        self.assertEqual(count, 0)
        self.device.refresh_from_db()
        self.assertEqual(self.device.fcm_token, "")
        self.assertEqual(PushNotificationSent.objects.count(), 0)

    def test_transient_error_keeps_token(self):
        from auctions.tasks import send_push_to_user

        with patch("auctions.notifications.send_fcm_message", return_value=notifications.SEND_ERROR):
            count = send_push_to_user(self.user.pk, title="t", body="b", url="u", category="invoice")
        self.assertEqual(count, 0)
        self.device.refresh_from_db()
        self.assertEqual(self.device.fcm_token, "tok")

    def test_skips_disabled_and_tokenless_devices(self):
        MobileDevice.objects.create(user=self.user, device_uuid=uuid.uuid4(), fcm_token="", push_enabled=True)
        self.device.push_enabled = False
        self.device.save()
        from auctions.tasks import send_push_to_user

        with patch("auctions.notifications.send_fcm_message", return_value=notifications.SEND_OK) as send:
            count = send_push_to_user(self.user.pk, title="t", body="b", url="u", category="invoice")
        self.assertEqual(count, 0)
        send.assert_not_called()


# ---------------------------------------------------------------------------
# Part 2 — device register/unregister (token lifecycle)
# ---------------------------------------------------------------------------


class DeviceServiceTokenTests(TestCase):
    def setUp(self):
        self.u1 = User.objects.create_user(username="dev1", password="x")
        self.u2 = User.objects.create_user(username="dev2", password="x")

    def test_register_sets_token_and_timestamp(self):
        device, created = DeviceService.register_or_update(self.u1, uuid.uuid4(), fcm_token="tokA")
        self.assertTrue(created)
        self.assertEqual(device.fcm_token, "tokA")
        self.assertIsNotNone(device.fcm_token_updated_at)

    def test_register_without_token_preserves_existing(self):
        uid = uuid.uuid4()
        DeviceService.register_or_update(self.u1, uid, fcm_token="tokA")
        DeviceService.register_or_update(self.u1, uid)  # no token passed
        self.assertEqual(MobileDevice.objects.get(device_uuid=uid).fcm_token, "tokA")

    def test_token_moves_off_other_device(self):
        old, new = uuid.uuid4(), uuid.uuid4()
        DeviceService.register_or_update(self.u1, old, fcm_token="shared")
        DeviceService.register_or_update(self.u2, new, fcm_token="shared")
        self.assertEqual(MobileDevice.objects.get(device_uuid=old).fcm_token, "")
        self.assertEqual(MobileDevice.objects.get(device_uuid=new).fcm_token, "shared")

    def test_unregister_clears_token_keeps_row(self):
        uid = uuid.uuid4()
        DeviceService.register_or_update(self.u1, uid, fcm_token="tokA")
        self.assertTrue(DeviceService.unregister(self.u1, uid))
        device = MobileDevice.objects.get(device_uuid=uid)
        self.assertEqual(device.fcm_token, "")

    def test_unregister_scoped_to_user(self):
        uid = uuid.uuid4()
        DeviceService.register_or_update(self.u1, uid, fcm_token="tokA")
        self.assertFalse(DeviceService.unregister(self.u2, uid))
        self.assertEqual(MobileDevice.objects.get(device_uuid=uid).fcm_token, "tokA")


class MobileDeviceApiTests(StandardTestCase):
    def test_register_with_fcm_token(self):
        uid = str(uuid.uuid4())
        resp = self.client.post(
            reverse("mobile-device-register"),
            {"device_uuid": uid, "fcm_token": "tokX"},
            content_type="application/json",
            **_bearer(self.user),
        )
        self.assertIn(resp.status_code, (200, 201))
        self.assertEqual(MobileDevice.objects.get(device_uuid=uid).fcm_token, "tokX")

    def test_unregister_endpoint_clears_token(self):
        uid = uuid.uuid4()
        MobileDevice.objects.create(user=self.user, device_uuid=uid, fcm_token="tokX", push_enabled=True)
        resp = self.client.post(
            reverse("mobile-device-unregister"),
            {"device_uuid": str(uid)},
            content_type="application/json",
            **_bearer(self.user),
        )
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(MobileDevice.objects.get(device_uuid=uid).fcm_token, "")

    def test_unregister_unknown_device_404(self):
        resp = self.client.post(
            reverse("mobile-device-unregister"),
            {"device_uuid": str(uuid.uuid4())},
            content_type="application/json",
            **_bearer(self.user),
        )
        self.assertEqual(resp.status_code, 404)


# ---------------------------------------------------------------------------
# Part 2 — preferences form push toggle (disabled without a device)
# ---------------------------------------------------------------------------


class PreferencesPushToggleTests(TestCase):
    def test_toggle_disabled_without_device(self):
        from auctions.forms import ChangeUserPreferencesForm

        user = User.objects.create_user(username="pref1", password="x")
        form = ChangeUserPreferencesForm(user, instance=user.userdata)
        self.assertTrue(form.fields["push_notifications_instead_of_email"].disabled)

    def test_toggle_enabled_with_device(self):
        from auctions.forms import ChangeUserPreferencesForm

        user = User.objects.create_user(username="pref2", password="x")
        MobileDevice.objects.create(user=user, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True)
        form = ChangeUserPreferencesForm(user, instance=user.userdata)
        self.assertFalse(form.fields["push_notifications_instead_of_email"].disabled)


# ---------------------------------------------------------------------------
# Part 2 — promo push job + weekly_promo skip
# ---------------------------------------------------------------------------


class PromoPushCommandTests(TestCase):
    def setUp(self):
        now = timezone.now()
        self.seller = User.objects.create_user(username="promo_seller", password="x")
        self.auction = Auction.objects.create(
            created_by=self.seller,
            title="Promo Auction",
            is_online=True,
            promote_this_auction=True,
            use_categories=True,
            date_start=now + datetime.timedelta(days=1),
            date_end=now + datetime.timedelta(days=3),
        )
        # date_posted is auto_now_add; backdate it so the auction is past its 24h "settle" window.
        Auction.objects.filter(pk=self.auction.pk).update(date_posted=now - datetime.timedelta(days=2))
        PickupLocation.objects.create(
            name="loc",
            auction=self.auction,
            latitude=40.0,
            longitude=-80.0,
            pickup_time=now + datetime.timedelta(days=1),
        )
        self.user = User.objects.create_user(username="promo_fan", password="x")
        ud = self.user.userdata
        ud.push_notifications_instead_of_email = True
        ud.email_me_about_new_auctions = True
        ud.email_me_about_new_auctions_distance = 1000
        ud.latitude = 40.1
        ud.longitude = -80.1
        ud.has_unsubscribed = False
        ud.save()
        MobileDevice.objects.create(user=self.user, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True)

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_notifies_nearby_opted_in_user(self):
        with patch("auctions.tasks.send_push_to_user.delay") as delay:
            call_command("promo_push_notifications")
        delay.assert_called_once()
        self.auction.refresh_from_db()
        self.assertEqual(self.auction.promo_push_notifications_sent, 1)

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_dedupes_via_ledger(self):
        PushNotificationSent.objects.create(user=self.user, category="promo", auction=self.auction)
        with patch("auctions.tasks.send_push_to_user.delay") as delay:
            call_command("promo_push_notifications")
        delay.assert_not_called()

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_skips_user_who_does_not_want_online_auctions(self):
        ud = self.user.userdata
        ud.email_me_about_new_auctions = False
        ud.save()
        with patch("auctions.tasks.send_push_to_user.delay") as delay:
            call_command("promo_push_notifications")
        delay.assert_not_called()

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_does_not_promote_freshly_posted_auction(self):
        Auction.objects.filter(pk=self.auction.pk).update(date_posted=timezone.now())
        with patch("auctions.tasks.send_push_to_user.delay") as delay:
            call_command("promo_push_notifications")
        delay.assert_not_called()


class WeeklyPromoSkipsPushUsersTests(TestCase):
    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_push_user_is_skipped(self):
        user = User.objects.create_user(username="wp_push", password="x")
        ud = user.userdata
        ud.push_notifications_instead_of_email = True
        ud.email_me_about_new_auctions = True
        ud.latitude = 40.0
        ud.longitude = -80.0
        # Make the user genuinely eligible for the weekly promo (active 30 days ago, not in the last
        # 6 days) so the push-skip is actually exercised rather than filtered out beforehand.
        ud.last_activity = timezone.now() - datetime.timedelta(days=30)
        ud.save()
        MobileDevice.objects.create(user=user, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True)

        # This user genuinely prefers push (opted in + live device + FCM configured), so weekly_promo
        # must skip them rather than email.
        self.assertTrue(user.userdata.user_prefers_push())
        with patch("auctions.management.commands.weekly_promo.mail.send") as send:
            call_command("weekly_promo")
        emailed = [call.args[0] for call in send.call_args_list if call.args]
        self.assertNotIn(user.email, emailed)


# ---------------------------------------------------------------------------
# Part X — command-program schema v2
# ---------------------------------------------------------------------------


class ProgramSchemaV2Tests(TestCase):
    """The additive v2 constructs, and the guard that makes the arithmetic one safe to author."""

    def test_total_bytes_in_tx_text(self):
        validate_profile_programs(
            print_program=[{"tx_text": "^GFA,{total_bytes},{total_bytes},{width_bytes},"}, {"tx_raster": True}]
        )

    def test_u32le_placeholder_allowed(self):
        validate_profile_programs(print_program=[{"tx": "1d {u32le:total_bytes}"}, {"tx_raster": True}])

    def test_unknown_width_function_rejected(self):
        with self.assertRaises(ProgramValidationError):
            validate_profile_programs(print_program=[{"tx": "1d {u64le:total_bytes}"}])

    def test_u16le_rejects_total_bytes(self):
        """total_bytes has no 16-bit form — a real raster overflows it."""
        with self.assertRaises(ProgramValidationError):
            validate_profile_programs(print_program=[{"tx": "1d {u16le:total_bytes}"}])

    def test_bare_size_placeholder_in_tx_rejected_unconditionally(self):
        """A bare {name} in a hex tx renders as ONE byte.

        Rejected whatever the value would be at render time: a profile authored against a small
        test label would otherwise validate and then silently truncate a length field on the first
        4x6, printing half a label for a reason nobody can see.
        """
        for name in ("total_bytes", "width_bytes", "height_px", "width_px"):
            with self.assertRaises(ProgramValidationError, msg=name):
                validate_profile_programs(print_program=[{"tx": f"1d 76 {{{name}}}"}])

    def test_bare_byte_placeholders_still_allowed_in_tx(self):
        # The D11s rows depend on these, and they really are one byte.
        validate_profile_programs(print_program=[{"tx": "10 ff 10 00 {density}"}, {"tx": "10 ff 84 {paper_type}"}])

    def test_size_placeholders_are_fine_in_tx_text(self):
        # tx_text renders ASCII decimal, so there is no one-byte limit to overflow.
        validate_profile_programs(print_program=[{"tx_text": "BITMAP 0,0,{width_bytes},{height_px},0,"}])

    def test_tx_raster_encodings(self):
        validate_profile_programs(print_program=[{"tx_raster": {"encoding": "binary"}}])
        validate_profile_programs(print_program=[{"tx_raster": {"encoding": "hex"}}])

    def test_tx_raster_false_still_rejected(self):
        """A step that does nothing is a typo, not an instruction to omit the label body."""
        with self.assertRaises(ProgramValidationError):
            validate_profile_programs(print_program=[{"tx_raster": False}])

    def test_tx_raster_unknown_encoding_rejected(self):
        with self.assertRaises(ProgramValidationError):
            validate_profile_programs(print_program=[{"tx_raster": {"encoding": "base64"}}])

    def test_tx_raster_unknown_key_rejected(self):
        with self.assertRaises(ProgramValidationError):
            validate_profile_programs(print_program=[{"tx_raster": {"encodign": "hex"}}])

    def test_status_flags_values_accepted(self):
        validate_profile_programs(
            print_program=[{"tx": "1d 0c"}],
            status_flags={"byte": 0, "values": {"00": [], "07": ["no_ribbon", "cover_open"]}},
        )

    def test_status_flags_values_reject_unknown_condition(self):
        with self.assertRaises(ProgramValidationError):
            validate_profile_programs(print_program=[{"tx": "1d 0c"}], status_flags={"values": {"01": ["lid_ajar"]}})

    def test_status_flags_values_reject_non_list(self):
        with self.assertRaises(ProgramValidationError):
            validate_profile_programs(print_program=[{"tx": "1d 0c"}], status_flags={"values": {"01": "cover_open"}})

    def test_status_flags_values_reject_multibyte_key(self):
        with self.assertRaises(ProgramValidationError):
            validate_profile_programs(print_program=[{"tx": "1d 0c"}], status_flags={"values": {"0107": []}})

    def test_status_flags_rejects_unknown_flag_name(self):
        with self.assertRaises(ProgramValidationError):
            validate_profile_programs(print_program=[{"tx": "1d 0c"}], status_flags={"flags": {"lid_ajar": "01"}})

    def test_status_flags_rejects_unknown_key(self):
        with self.assertRaises(ProgramValidationError):
            validate_profile_programs(print_program=[{"tx": "1d 0c"}], status_flags={"kind": "value_map"})

    def test_schema_version_max_is_two(self):
        self.assertEqual(PROGRAM_SCHEMA_VERSION, 2)

    def test_language_templates_are_valid_programs(self):
        """Every drafting template has to be something a printer could actually be driven with."""
        for language, template in LANGUAGE_TEMPLATES.items():
            with self.subTest(language=language):
                validate_profile_programs(
                    print_program=template["print_program"], status_program=template["status_program"]
                )


# ---------------------------------------------------------------------------
# Part T — the TSPL profile
# ---------------------------------------------------------------------------


class TsplPrinterProfileTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.profile = ThermalPrinterProfile.objects.get(slug="tspl-raster")

    def test_seed_row_validates(self):
        self.profile.full_clean(exclude=["slug"])

    def test_verified_gatt_ids_are_pinned(self):
        """Never blank these: the service's first *writable* characteristic is the radio module's
        control channel, so discovery-by-guessing writes label rasters into the radio's config."""
        self.assertEqual(self.profile.service_uuid, "49535343-fe7d-4ae5-8fa9-9fafd205e455")
        self.assertEqual(self.profile.write_characteristic_uuid, "49535343-8841-43f4-a8d4-ecbe34729bb3")
        self.assertEqual(self.profile.notify_characteristic_uuid, "49535343-1e4d-4bd9-ba61-23c647249616")

    def test_manufacturer_patterns_stay_empty(self):
        """The DIS reports "Feasycom" / "FSC-BT986" — the radio module, which ships in dozens of
        unrelated products. Matching on it would claim other vendors' hardware."""
        self.assertEqual(self.profile.manufacturer_patterns, [])

    def test_raster_is_inverted(self):
        """TSPL BITMAP paints on a 0 bit. Without this every label comes out solid black."""
        self.assertTrue(self.profile.invert_raster)

    def test_program_has_no_await_step(self):
        """TSPL has no print-completion ack. An await here resurrects exactly the "couldn't confirm
        the print finished" warning this profile exists to fix."""
        self.assertNotIn("await", [key for step in self.profile.print_program for key in step])

    def test_status_values_disambiguate_the_lid_open_reading(self):
        """A Y486BT with nothing but its lid open answers 0x07. Read as a bitmask that is
        out-of-paper AND jammed AND open — which told the user to load labels already in the
        printer."""
        self.assertEqual(self.profile.status_flags["values"]["07"], ["no_ribbon", "cover_open"])
        self.assertEqual(self.profile.status_flags["values"]["00"], [])
        self.assertNotIn("out_of_paper", self.profile.status_flags["values"]["07"])

    def test_uses_schema_v2(self):
        """It carries a values map, so an older app build must ignore it rather than mis-decode."""
        self.assertEqual(self.profile.schema_version, 2)

    def test_priority_sits_between_the_d11s_rows_and_the_escpos_fallback(self):
        priorities = dict(ThermalPrinterProfile.objects.values_list("slug", "priority"))
        self.assertLess(priorities["d11s-aiyin"], priorities["tspl-raster"])
        self.assertLess(priorities["tspl-raster"], priorities["escpos-raster"])

    def test_profile_names_read_as_printers(self):
        """The app shows profile.name to a volunteer looking at a box on a table."""
        self.assertEqual(
            ThermalPrinterProfile.objects.get(slug="escpos-raster").name, "Other thermal printer (ESC/POS)"
        )

    def test_api_serializes_the_command_language(self):
        data = self.client.get(reverse("mobile-printer-profiles"), **_bearer(self.user)).json()
        languages = {p["slug"]: p["command_language"] for p in data["profiles"]}
        self.assertEqual(languages["tspl-raster"], "tspl")
        self.assertEqual(languages["escpos-raster"], "escpos")
        self.assertEqual(languages["d11s-aiyin"], "d11s")

    def test_exactly_one_enabled_profile_speaks_tspl(self):
        """Uniqueness is what lets the app auto-select without asking. Knowing a printer speaks
        TSPL doesn't tell you its printhead width or GATT ids, so two candidates is a real
        question; one candidate is nothing to get wrong."""
        tspl = ThermalPrinterProfile.objects.filter(enabled=True, command_language="tspl")
        self.assertEqual(tspl.count(), 1)


# ---------------------------------------------------------------------------
# Part U1/U2 + Y1 — capturing what an unknown printer is
# ---------------------------------------------------------------------------


class ObservedPrinterProbeCaptureTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("mobile-printer-observed")

    def _post(self, **overrides):
        payload = {"ble_name": "Y486BT_AB10-BLE", "model": "FSC-BT986", "matched_by": "probe"}
        payload.update(overrides)
        return self.client.post(self.url, payload, content_type="application/json", **_bearer(self.user))

    def test_probe_match_is_no_longer_reported_as_device_info(self):
        """A strict ChoiceField 400s an unknown value, so without this the app had to lie and call
        a probe-derived match "deviceInfo" — making the two indistinguishable in the admin."""
        self.assertEqual(self._post(matched_by="probe").status_code, 201)
        self.assertEqual(ObservedPrinter.objects.get(user=self.user).matched_by, "probe")

    def test_probe_replies_and_language_are_recorded(self):
        replies = {"tspl_status": {"hex": "00", "ascii": "."}}
        self.assertEqual(self._post(probe_replies=replies, probed_language="TSPL").status_code, 201)
        observed = ObservedPrinter.objects.get(user=self.user)
        self.assertEqual(observed.probe_replies, replies)
        self.assertEqual(observed.probed_language, "tspl")

    def test_gatt_tree_is_recorded(self):
        gatt = [{"uuid": "49535343-fe7d-4ae5-8fa9-9fafd205e455", "characteristics": [{"uuid": "x", "properties": []}]}]
        self._post(gatt=gatt)
        self.assertEqual(ObservedPrinter.objects.get(user=self.user).gatt, gatt)

    def test_characterization_sets_the_work_queue_flag(self):
        self._post(
            status_captures={"ready": {"tspl_status": {"hex": "00"}}},
            derived_status_values={"00": [], "01": ["cover_open"]},
            status_ambiguities=["01: cover_open and no_labels_cover_open are indistinguishable"],
        )
        observed = ObservedPrinter.objects.get(user=self.user)
        self.assertTrue(observed.characterized)
        self.assertEqual(observed.derived_status_values["01"], ["cover_open"])
        self.assertEqual(len(observed.status_ambiguities), 1)

    def test_a_plain_repairing_does_not_wipe_captured_evidence(self):
        """Most pairings carry no probe data. Overwriting with the empty default would throw away
        the one report that was worth having."""
        self._post(
            probed_language="tspl",
            probe_replies={"tspl_status": {"hex": "00"}},
            gatt=[{"uuid": "abc", "characteristics": []}],
            status_captures={"ready": {"tspl_status": {"hex": "00"}}},
            derived_status_values={"00": []},
        )
        self._post()  # a later ordinary pairing
        observed = ObservedPrinter.objects.get(user=self.user)
        self.assertEqual(observed.probed_language, "tspl")
        self.assertTrue(observed.probe_replies)
        self.assertTrue(observed.gatt)
        self.assertTrue(observed.status_captures)
        self.assertTrue(observed.characterized)
        self.assertEqual(observed.times_seen, 2)

    def test_a_fresh_characterization_supersedes_the_old_one(self):
        self._post(
            status_captures={"ready": {"tspl_status": {"hex": "00"}}},
            derived_status_values={"00": []},
            status_ambiguities=["something"],
        )
        self._post(
            status_captures={"ready": {"tspl_status": {"hex": "20"}}},
            derived_status_values={"20": ["printing"]},
            status_ambiguities=[],
        )
        observed = ObservedPrinter.objects.get(user=self.user)
        self.assertEqual(observed.derived_status_values, {"20": ["printing"]})
        # An empty ambiguity list is a real result ("tells every state apart"), not a missing one.
        self.assertEqual(observed.status_ambiguities, [])

    def test_absent_fields_are_still_a_valid_report(self):
        """Older app builds send none of this."""
        self.assertEqual(self._post(matched_by="bleName").status_code, 201)
        observed = ObservedPrinter.objects.get(user=self.user)
        self.assertEqual(observed.probe_replies, {})
        self.assertEqual(observed.gatt, [])
        self.assertFalse(observed.characterized)

    def test_oversized_json_is_dropped_not_rejected(self):
        """Same leniency as the rest of the endpoint: the app ignores the response, so a row we
        refuse is a row we simply never see."""
        resp = self._post(probe_replies={"k": "v" * 50000})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(ObservedPrinter.objects.get(user=self.user).probe_replies, {})

    def test_wrong_json_type_is_dropped_not_rejected(self):
        resp = self._post(gatt={"not": "a list"}, probe_replies=["not a dict"])
        self.assertEqual(resp.status_code, 201)
        observed = ObservedPrinter.objects.get(user=self.user)
        self.assertEqual(observed.gatt, [])
        self.assertEqual(observed.probe_replies, {})

    def test_nulls_are_tolerated(self):
        resp = self._post(probe_replies=None, probed_language=None, gatt=None, status_captures=None)
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(ObservedPrinter.objects.get(user=self.user).characterized)


# ---------------------------------------------------------------------------
# Part Y2 — drafting a profile from an observation
# ---------------------------------------------------------------------------


class DraftProfileFromObservationTests(StandardTestCase):
    # The real Y486BT shape: the module's CONTROL channel (…6daa…) is writable and comes first.
    GATT = [
        {"uuid": "1800", "characteristics": [{"uuid": "2a00", "properties": ["read", "write"]}]},
        {"uuid": "0000180a-0000-1000-8000-00805f9b34fb", "characteristics": [{"uuid": "2a24", "properties": ["read"]}]},
        {
            "uuid": "49535343-fe7d-4ae5-8fa9-9fafd205e455",
            "characteristics": [
                {"uuid": "49535343-6daa-4d02-abf6-19569aca69fe", "properties": ["read", "write"]},
                {"uuid": "49535343-8841-43f4-a8d4-ecbe34729bb3", "properties": ["write", "writeNR"]},
                {"uuid": "49535343-1e4d-4bd9-ba61-23c647249616", "properties": ["notify"]},
            ],
        },
    ]

    def _observation(self, **overrides):
        fields = {
            "user": self.user,
            "ble_name": "Y486BT_AB10-BLE",
            "manufacturer": "Feasycom",
            "model": "ITPP941",
            "matched_by": "manual",
            "probed_language": "tspl",
            "probe_replies": {"tspl_status": {"hex": "00", "ascii": "."}},
            "gatt": self.GATT,
            "status_captures": {"ready": {"tspl_status": {"hex": "00"}}},
            "derived_status_values": {"00": [], "01": ["cover_open"], "04": ["out_of_paper"]},
            "status_ambiguities": ["01: cover_open and no_labels_cover_open are indistinguishable"],
            "characterized": True,
        }
        fields.update(overrides)
        return ObservedPrinter.objects.create(**fields)

    def test_drafts_a_disabled_profile(self):
        """Disabled because a drafted profile is a hypothesis: the person who submitted it is the
        one holding the printer, and "Print test label" is what confirms it."""
        profile, created = draft_profile_from_observation(self._observation())
        self.assertTrue(created)
        self.assertFalse(profile.enabled)
        self.assertEqual(profile.slug, "itpp941")
        self.assertEqual(profile.command_language, "tspl")

    def test_draft_validates_as_a_real_profile(self):
        profile, _ = draft_profile_from_observation(self._observation())
        profile.full_clean(exclude=["slug"])

    def test_skips_the_radio_control_channel(self):
        """Picking the write characteristic wrong is silent: labels go into the radio module's
        configuration, nothing prints, and nothing errors."""
        profile, _ = draft_profile_from_observation(self._observation())
        self.assertEqual(profile.service_uuid, "49535343-fe7d-4ae5-8fa9-9fafd205e455")
        self.assertEqual(profile.write_characteristic_uuid, "49535343-8841-43f4-a8d4-ecbe34729bb3")
        self.assertEqual(profile.notify_characteristic_uuid, "49535343-1e4d-4bd9-ba61-23c647249616")

    def test_skips_generic_services(self):
        self.assertEqual(pick_gatt_ids(self.GATT[:2]), ("", "", ""))

    def test_carries_the_derived_status_map(self):
        profile, _ = draft_profile_from_observation(self._observation())
        self.assertEqual(profile.status_flags["values"]["01"], ["cover_open"])
        self.assertEqual(profile.schema_version, 2)

    def test_notes_carry_the_raw_evidence_verbatim(self):
        profile, _ = draft_profile_from_observation(self._observation())
        self.assertIn("tspl_status", profile.notes)
        self.assertIn("indistinguishable", profile.notes)
        self.assertIn("print_width_px", profile.notes)  # the bit that still needs a human

    def test_notes_flag_a_manufacturer_that_may_be_the_radio_module(self):
        """Feasycom is the Y486BT's Bluetooth module, not its maker — a pattern on it would claim
        unrelated hardware. Drafted (it is what the printer said) but flagged for the reviewer."""
        profile, _ = draft_profile_from_observation(self._observation())
        self.assertIn("CHECK manufacturer_patterns", profile.notes)
        self.assertIn("Feasycom", profile.notes)

    def test_model_and_manufacturer_become_escaped_patterns(self):
        profile, _ = draft_profile_from_observation(self._observation(model="D11-S+"))
        self.assertEqual(profile.model_patterns, ["^D11\\-S\\+"])
        self.assertEqual(profile.manufacturer_patterns, ["Feasycom"])

    def test_redrafting_refreshes_rather_than_duplicating(self):
        observation = self._observation()
        first, _ = draft_profile_from_observation(observation)
        observation.derived_status_values = {"00": [], "02": ["paper_jam"]}
        observation.save()
        second, created = draft_profile_from_observation(observation)
        self.assertFalse(created)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.status_flags["values"]["02"], ["paper_jam"])

    def test_never_overwrites_an_enabled_profile(self):
        """An enabled row is one a human has taken ownership of — including every seeded row."""
        # A model name that happens to slugify onto a seeded profile must not clobber it.
        with self.assertRaises(DraftError):
            draft_profile_from_observation(self._observation(model="TSPL raster"))
        self.assertEqual(
            ThermalPrinterProfile.objects.get(slug="tspl-raster").name,
            "TSPL label printer (VEVOR Y486BT, TSC-compatible)",
        )
        # Nor may a redraft clobber a draft somebody has since confirmed and enabled.
        observation = self._observation()
        first, _ = draft_profile_from_observation(observation)
        ThermalPrinterProfile.objects.filter(pk=first.pk).update(enabled=True)
        with self.assertRaises(DraftError):
            draft_profile_from_observation(observation)

    def test_no_probed_language_cannot_be_drafted(self):
        """The print program is the one part no probe can discover, so there is nothing to write."""
        with self.assertRaises(DraftError):
            draft_profile_from_observation(self._observation(probed_language=""))

    def test_zpl_draft_uses_schema_v2_constructs(self):
        profile, _ = draft_profile_from_observation(self._observation(probed_language="zpl", model="ZD421"))
        program = json.dumps(profile.print_program)
        self.assertIn("{total_bytes}", program)
        self.assertIn('"encoding": "hex"', program)
        self.assertEqual(profile.schema_version, 2)
        profile.full_clean(exclude=["slug"])


# ---------------------------------------------------------------------------
# Part W2 — POST /api/mobile/labels/printed/
# ---------------------------------------------------------------------------


class MobileLabelsPrintedApiTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("mobile-labels-printed")

    def _post(self, lots, user=None):
        return self.client.post(self.url, {"lots": lots}, content_type="application/json", **_bearer(user or self.user))

    def test_marks_labels_printed(self):
        resp = self._post([self.lot.pk, self.lotB.pk])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"marked": 2})
        self.lot.refresh_from_db()
        self.assertTrue(self.lot.label_printed)
        self.assertFalse(self.lot.label_needs_reprinting)

    def test_clears_needs_reprinting_like_the_pdf_views_do(self):
        Lot.objects.filter(pk=self.lot.pk).update(label_printed=True, label_needs_reprinting=True)
        self._post([self.lot.pk])
        self.lot.refresh_from_db()
        self.assertFalse(self.lot.label_needs_reprinting)

    def test_is_idempotent(self):
        self._post([self.lot.pk])
        self.assertEqual(self._post([self.lot.pk]).json(), {"marked": 1})

    def test_shrinks_the_unprinted_queryset(self):
        """The whole point: without this, "print unprinted labels" never shrinks for a Bluetooth
        user, because only the PDF views set label_printed."""
        before = self.online_tos.unprinted_label_count
        self._post([self.lot.pk])
        self.assertEqual(self.online_tos.unprinted_label_count, before - 1)

    def test_lots_the_caller_cannot_touch_are_skipped_not_refused(self):
        """A batch of forty is one print run and most of it printed fine — failing the whole
        report over one lot would lose the record of the thirty-nine that worked."""
        stranger = User.objects.create_user(username="stranger", password="x")
        resp = self._post([self.lot.pk], user=stranger)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"marked": 0})
        self.lot.refresh_from_db()
        self.assertFalse(self.lot.label_printed)

    def test_a_mixed_batch_marks_what_it_may(self):
        stranger_lot = Lot.objects.create(lot_name="not yours", user=self.userB, quantity=1)
        resp = self._post([self.lot.pk, stranger_lot.pk])
        self.assertEqual(resp.json(), {"marked": 1})
        stranger_lot.refresh_from_db()
        self.assertFalse(stranger_lot.label_printed)

    def test_auction_admin_may_mark_a_sellers_labels(self):
        resp = self._post([self.lot.pk], user=self.admin_user)
        self.assertEqual(resp.json(), {"marked": 1})

    def test_unknown_and_deleted_pks_are_ignored(self):
        self.lotB.is_deleted = True
        self.lotB.save()
        resp = self._post([self.lot.pk, self.lotB.pk, 99999999])
        self.assertEqual(resp.json(), {"marked": 1})

    def test_empty_batch_is_fine(self):
        self.assertEqual(self._post([]).json(), {"marked": 0})

    def test_malformed_body_is_a_400(self):
        resp = self.client.post(self.url, {"lots": ["nope"]}, content_type="application/json", **_bearer(self.user))
        self.assertEqual(resp.status_code, 400)

    def test_requires_jwt(self):
        self.assertIn(self.client.post(self.url, {"lots": []}, content_type="application/json").status_code, (401, 403))


# ---------------------------------------------------------------------------
# Part W1 — bulk label printing hands a Bluetooth app user the lot set
# ---------------------------------------------------------------------------


class BulkBluetoothPrintLinkTests(StandardTestCase):
    """A Bluetooth user tapping a *bulk* label button used to get a PDF sheet they can't feed to a
    thermal printer.

    Gated in LotLabelView rather than in the templates that build the links: every bulk entry point
    -- the users-table anchors, ?printredirect=, the command palette, print-after-bulk-add, a
    bookmarked URL -- funnels through that one view, and gating templates one at a time leaves
    entry points behind (it would also put label printing back inside the mobile-app UA
    conditionals that MobileAppLabelPrintingVisibilityTests exists to keep out).
    """

    APP_UA = "FishAuctionsApp/1.0 (iOS)"
    WEB_UA = "Mozilla/5.0"

    def setUp(self):
        super().setUp()
        self.in_person_auction.date_end = timezone.now() - datetime.timedelta(days=1)
        self.in_person_auction.save()
        self.lots = [
            Lot.objects.create(
                lot_name=f"bt label {i}",
                auction=self.in_person_auction,
                auctiontos_seller=self.in_person_tos,
                quantity=1,
            )
            for i in range(3)
        ]
        self.url = reverse("print_my_labels", kwargs={"slug": self.in_person_auction.slug})
        self.prefs, _ = UserLabelPrefs.objects.get_or_create(user=self.user)

    def _get(self, user_agent, url=None):
        self.client.force_login(self.user)
        return self.client.get(url or self.url, HTTP_USER_AGENT=user_agent)

    def _set_method(self, method):
        self.prefs.print_method = method
        self.prefs.save()

    def test_bluetooth_in_the_app_gets_the_lot_set(self):
        self._set_method("bluetooth")
        html = self._get(self.APP_UA).content.decode()
        expected = "fishauctions://print/?lots=" + ",".join(str(lot.pk) for lot in self.lots)
        self.assertIn(expected, html)

    def test_lot_order_matches_the_order_the_pdf_prints_them(self):
        """That's the order the labels come out of the printer."""
        self._set_method("bluetooth")
        html = self._get(self.APP_UA).content.decode()
        pks = [str(pk) for pk in self.in_person_tos.print_labels_qs.values_list("pk", flat=True)]
        self.assertIn("fishauctions://print/?lots=" + ",".join(pks), html)

    def test_bluetooth_on_the_web_still_gets_the_pdf(self):
        """The scheme has no handler in a browser, so a deep link there is a dead end."""
        self._set_method("bluetooth")
        resp = self._get(self.WEB_UA)
        self.assertEqual(resp["Content-Type"], "application/pdf")

    def test_pdf_method_in_the_app_still_gets_the_pdf(self):
        """Everyone else is unchanged -- the PDF and System-printer methods are the default."""
        self._set_method("pdf")
        self.assertEqual(self._get(self.APP_UA)["Content-Type"], "application/pdf")

    def test_system_method_in_the_app_still_gets_the_pdf(self):
        self._set_method("system")
        self.assertEqual(self._get(self.APP_UA)["Content-Type"], "application/pdf")

    def test_the_unprinted_variant_is_gated_too(self):
        self._set_method("bluetooth")
        Lot.objects.filter(pk=self.lots[0].pk).update(label_printed=True)
        url = reverse("print_my_unprinted_labels", kwargs={"slug": self.in_person_auction.slug})
        html = self._get(self.APP_UA, url=url).content.decode()
        self.assertIn(f"lots={self.lots[1].pk},{self.lots[2].pk}", html)

    def test_admin_printing_for_a_bidder_number_is_gated_too(self):
        self._set_method("bluetooth")
        url = reverse(
            "print_labels_by_bidder_number",
            kwargs={"slug": self.in_person_auction.slug, "bidder_number": self.in_person_tos.bidder_number},
        )
        self.assertIn("fishauctions://print/?lots=", self._get(self.APP_UA, url=url).content.decode())

    def test_the_handoff_does_not_mark_anything_printed(self):
        """Nothing has printed yet. The app posts labels/printed/ for what actually comes out."""
        self._set_method("bluetooth")
        self._get(self.APP_UA)
        self.assertEqual(self.in_person_tos.unprinted_label_count, len(self.lots))

    def test_the_pdf_still_marks_labels_printed(self):
        self._set_method("pdf")
        self._get(self.APP_UA)
        self.assertEqual(self.in_person_tos.unprinted_label_count, 0)

    def test_long_lot_sets_are_capped_and_say_so(self):
        """Platform URL handling varies, so keep the link near 2000 characters. The app itself has
        no cap -- it prints serially and cancellably."""
        Lot.objects.bulk_create(
            [
                Lot(
                    lot_name=f"many {i}",
                    auction=self.in_person_auction,
                    auctiontos_seller=self.in_person_tos,
                    quantity=1,
                )
                for i in range(320)
            ]
        )
        self._set_method("bluetooth")
        html = self._get(self.APP_UA).content.decode()
        link = html.split("fishauctions://print/?lots=")[1].split('"')[0]
        self.assertEqual(len(link.split(",")), 300)
        self.assertLess(len("fishauctions://print/?lots=" + link), 2100)
        self.assertIn("Print only unprinted labels", html)


# ---------------------------------------------------------------------------
# The Bluetooth PNG must be the PDF
# ---------------------------------------------------------------------------


class LabelPngMatchesPdfTests(StandardTestCase):
    """The PNG sent to a Bluetooth printer used to be drawn independently in Pillow -- a Code128
    barcode where the PDF puts a QR code, different fields, different typography, and no knowledge
    of Auction.label_print_fields or the user's UserLabelPrefs. There is one layout now: WeasyPrint
    renders the same label_template.html and pdfium rasterizes page one.
    """

    def setUp(self):
        super().setUp()
        self.url = reverse("mobile-label-lot", kwargs={"pk": self.lot.pk})
        self.prefs, _ = UserLabelPrefs.objects.get_or_create(user=self.user)
        self.prefs.preset = "thermal_sm"
        self.prefs.save()

    def _png(self, **params):
        resp = self.client.get(self.url, params, **_bearer(self.user))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/png")
        return resp.content

    @staticmethod
    def _image(content):
        from PIL import Image

        return Image.open(io.BytesIO(content))

    def test_png_is_the_rasterized_pdf(self):
        """Not "looks similar": the same bytes go through the same template, so rasterizing the
        PDF ourselves must reproduce the endpoint's PNG exactly."""
        from auctions.mobile.services.label_pdf import render_single_lot_pdf
        from auctions.mobile.services.label_raster import rasterize_pdf

        content = self._png(resolution="600x400")

        request = self.client.get(self.url, **_bearer(self.user)).wsgi_request
        request.user = self.user
        pdf_bytes, _ = render_single_lot_pdf(self.lot, request, single_label_page=True, mark_printed=False)
        self.assertEqual(content, rasterize_pdf(pdf_bytes, width=600, height=400, dpi=203))

    def test_requested_resolution_is_exact(self):
        """The app asks for its printhead's pixel grid; anything else prints scaled and smeared."""
        for resolution, size in (("600x400", (600, 400)), ("832x1248", (832, 1248)), ("96x200", (96, 200))):
            with self.subTest(resolution=resolution):
                self.assertEqual(self._image(self._png(resolution=resolution)).size, size)

    def test_label_is_not_distorted(self):
        """A label whose aspect ratio doesn't match the user's page setup gets even white margins,
        not stretched text."""
        wide = self._image(self._png(resolution="800x200")).convert("L")
        # Scaled to fit a 3x2 page into 800x200 => 300x200 of content, centred: the far edges are
        # white padding.
        self.assertEqual(wide.getpixel((2, 100)), 255)
        self.assertEqual(wide.getpixel((797, 100)), 255)

    def test_rendering_a_png_does_not_mark_the_label_printed(self):
        """Rendering a PDF sheet marks it printed; drawing a raster must not. Nothing has printed
        until the app says so via labels/printed/."""
        self._png()
        self.lot.refresh_from_db()
        self.assertFalse(self.lot.label_printed)

    def test_a_sheet_preset_renders_one_label_not_a_sheet(self):
        """An Avery page is 8.5x11 with the label in a corner. Rasterizing that would be a label in
        the corner of a mostly blank image -- and on a roll, a lot of wasted label."""
        self.prefs.preset = "lg"
        self.prefs.save()
        image = self._image(self._png(resolution="780x243")).convert("L")
        dark = sum(count for value, count in enumerate(image.histogram()) if value < 128)
        self.assertGreater(dark, 500, "the label did not fill the frame")

    def test_the_label_respects_auction_print_fields(self):
        """Proof it is the real pipeline: turning a field off in the auction changes the raster.
        The standalone renderer never knew label_print_fields existed."""
        self.online_auction.label_print_fields = "lot_name,seller_name"
        self.online_auction.save()
        without_qr = self._png(resolution="600x400")
        self.online_auction.label_print_fields = "lot_name,seller_name,qr_code"
        self.online_auction.save()
        self.assertNotEqual(without_qr, self._png(resolution="600x400"))

    def test_falls_back_when_there_is_no_pdf_to_rasterize(self):
        """A lot with no auction has no label configuration to render against, and an approximate
        label still beats no label at a check-in table."""
        orphan = Lot.objects.create(lot_name="no auction here", user=self.user, quantity=1)
        url = reverse("mobile-label-lot", kwargs={"pk": orphan.pk})
        resp = self.client.get(url, {"resolution": "600x400"}, **_bearer(self.user))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertEqual(self._image(resp.content).size, (600, 400))

    def test_bad_resolution_is_still_a_400(self):
        self.assertEqual(self.client.get(self.url, {"resolution": "wide"}, **_bearer(self.user)).status_code, 400)

    def test_the_web_png_endpoint_matches_the_mobile_one(self):
        """SingleLotLabelView?fmt=png exists so the web endpoint matches the app; it has to keep
        matching."""
        self.client.force_login(self.user)
        web = self.client.get(
            reverse("single_lot_label", kwargs={"pk": self.lot.pk}), {"fmt": "png", "resolution": "600x400"}
        )
        self.assertEqual(web.status_code, 200)
        self.assertEqual(web["Content-Type"], "image/png")
        self.assertEqual(web.content, self._png(resolution="600x400"))


# ---------------------------------------------------------------------------
# Part Y3 — telling a user their printer is supported now
# ---------------------------------------------------------------------------


class PrinterSupportedNotificationTests(StandardTestCase):
    """Enabling a profile that claims a hand-identified printer must not be silent.

    Their next connect just starts matching properly, which from where the user is standing looks
    exactly like nothing happened.
    """

    def setUp(self):
        super().setUp()
        self.observation = ObservedPrinter.objects.create(
            user=self.user,
            ble_name="ITPP941-3C",
            model="ITPP941",
            manufacturer="MUNBYN",
            matched_by="manual",
            profile_slug="",
        )
        userdata = self.user.userdata
        userdata.push_notifications_instead_of_email = True
        userdata.save()
        MobileDevice.objects.create(user=self.user, device_uuid=str(uuid.uuid4()), fcm_token="t", push_enabled=True)

    def _enable_matching_profile(self, **overrides):
        fields = {
            "slug": "munbyn-itpp941",
            "name": "MUNBYN ITPP941",
            "enabled": True,
            "model_patterns": ["^ITPP941"],
            "print_program": [{"tx": "1d 0c"}],
        }
        fields.update(overrides)
        return ThermalPrinterProfile.objects.create(**fields)

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    @patch("auctions.tasks.send_push_to_user.delay")
    def test_enabling_a_matching_profile_notifies_the_owner(self, delay):
        # The enqueue is on_commit, so a rolled-back save can't push about a profile that was
        # never enabled -- which means the test has to let the commit hooks run.
        with self.captureOnCommitCallbacks(execute=True):
            self._enable_matching_profile()
        delay.assert_called_once()
        self.assertEqual(delay.call_args.args[0], self.user.pk)
        self.assertEqual(delay.call_args.kwargs["url"], "/printing/")
        self.assertIn("ITPP941", delay.call_args.kwargs["body"])
        self.observation.refresh_from_db()
        self.assertTrue(self.observation.support_notified)

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    @patch("auctions.tasks.send_push_to_user.delay")
    def test_nobody_is_told_twice(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            profile = self._enable_matching_profile()
            profile.priority = 42  # a later edit while the row is being tuned
            profile.save()
        delay.assert_called_once()

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    @patch("auctions.tasks.send_push_to_user.delay")
    def test_a_disabled_profile_says_nothing(self, delay):
        """A drafted profile is a hypothesis until a human enables it."""
        with self.captureOnCommitCallbacks(execute=True):
            self._enable_matching_profile(enabled=False)
        delay.assert_not_called()

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    @patch("auctions.tasks.send_push_to_user.delay")
    def test_a_non_matching_profile_says_nothing(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            self._enable_matching_profile(slug="other", model_patterns=["^ZD421"])
        delay.assert_not_called()

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    @patch("auctions.tasks.send_push_to_user.delay")
    def test_printers_that_already_matched_are_not_news(self, delay):
        ObservedPrinter.objects.filter(pk=self.observation.pk).update(matched_by="bleName", profile_slug="something")
        with self.captureOnCommitCallbacks(execute=True):
            self._enable_matching_profile()
        delay.assert_not_called()

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    @patch("auctions.tasks.send_push_to_user.delay")
    def test_a_user_without_push_stays_unnotified_so_the_news_can_still_reach_them(self, delay):
        userdata = self.user.userdata
        userdata.push_notifications_instead_of_email = False
        userdata.save()
        with self.captureOnCommitCallbacks(execute=True):
            self._enable_matching_profile()
        delay.assert_not_called()
        self.observation.refresh_from_db()
        self.assertFalse(self.observation.support_notified)

    def test_matcher_handles_a_bad_pattern_without_blowing_up_the_save(self):
        """A regex saved before clean() validated them must not break every later profile edit."""
        profile = ThermalPrinterProfile(slug="x", name="X", print_program=[{"tx": "1d 0c"}], model_patterns=["^ITPP("])
        self.assertFalse(profile_matches_observation(profile, self.observation))

    def test_matcher_ignores_blank_observation_fields(self):
        blank = ObservedPrinter(ble_name="", model="", manufacturer="")
        profile = ThermalPrinterProfile(slug="y", name="Y", print_program=[{"tx": "1d 0c"}], model_patterns=[".*"])
        self.assertFalse(profile_matches_observation(profile, blank))
