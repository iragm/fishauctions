"""Tests for the mobile-app web-side features.

Part 1 — label printing: the mismatch-warning matrix, the ThermalPrinterProfile command-program
validator + seed data, and the mobile printer/label API.
Part 2 — push notifications: the push-routing decision (user_prefers_push / notify_user), the
send_push_to_user fan-out + token pruning, device register/unregister, and the promo push job.
"""

import datetime
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
    MobileDevice,
    PickupLocation,
    PushNotificationSent,
    ThermalPrinterProfile,
    UserLabelPrefs,
)
from auctions.printer_programs import ProgramValidationError, validate_profile_programs
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

    def test_requires_jwt(self):
        self.assertIn(self.client.get(self.url).status_code, (401, 403))


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


# ---------------------------------------------------------------------------
# Part 2 — push routing decision
# ---------------------------------------------------------------------------


class PushConfiguredTests(TestCase):
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
