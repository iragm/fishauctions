"""Tests for the mobile-app web-side features."""

import base64
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

from auctions import notifications, tasks
from auctions.mobile.services.devices import DeviceService
from auctions.models import (
    Auction,
    AuctionTOS,
    Lot,
    MobileDevice,
    ObservedPrinter,
    PickupLocation,
    PushNotificationSent,
    ThermalPrinterProfile,
    UserData,
    UserLabelPrefs,
    Watch,
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
    STATUS_CONDITIONS,
    ProgramValidationError,
    validate_match_patterns,
    validate_profile_programs,
)
from auctions.printing import label_prefs_warnings, warning_matrix
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase, patch_views

# push_configured() only checks it's non-empty; FCM calls are mocked.
FAKE_FIREBASE = '{"type": "service_account", "project_id": "x"}'


def _bearer(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


# Part 1 — label-prefs mismatch warnings


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


# Part 1 — printer command-program validation + seed data


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


# Part 8.1 — matching on what the printer reports over GATT 0x180A


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


# Part 1 — mobile printer profiles API


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


# Part 8.2 — POST /api/mobile/printers/observed/


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
        # A printer with no profile, and the user cancelled the dialog.
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


# Part 1 — mobile label prefs API + PDF renderer


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


class PrintMethodDropdownOnTheWebTests(StandardTestCase):
    """Saving /printing/ from a computer with an app-only print method selected.

    Disabled ``<option>``s aren't submitted, so the first save failed and the second reset to PDF.
    """

    WEB_UA = "Mozilla/5.0"

    def setUp(self):
        super().setUp()
        self.prefs, _ = UserLabelPrefs.objects.get_or_create(user=self.user)
        self.prefs.print_method = "bluetooth"
        self.prefs.save()
        MobileDevice.objects.create(user=self.user, device_uuid=uuid.uuid4())
        self.client.force_login(self.user)

    def _options(self, html):
        """The print-method ``<option>`` tags, one string each."""
        select = html.split('id="id_print_method"')[1].split("</select>")[0]
        return ["<option" + one.split(">")[0] + ">" for one in select.split("<option")[1:]]

    def _option(self, html, value):
        return [one for one in self._options(html) if f'value="{value}"' in one][0]

    def _form_data(self, **overrides):
        """Every field the page renders, as a browser would post it (the custom-geometry ones are required)."""
        data = {}
        for field in UserLabelPrefs._meta.get_fields():
            if not hasattr(field, "attname") or field.name in ("id", "user"):
                continue
            value = getattr(self.prefs, field.name)
            if isinstance(value, bool):
                if value:
                    data[field.name] = "on"
            elif value is not None:
                data[field.name] = value
        data.update(overrides)
        return {name: value for name, value in data.items() if value is not None}

    def _save(self, **overrides):
        return self.client.post(
            reverse("printing") + "?next=/", self._form_data(**overrides), HTTP_USER_AGENT=self.WEB_UA
        )

    def test_the_selected_app_only_option_is_not_rendered_disabled(self):
        html = self.client.get(reverse("printing"), HTTP_USER_AGENT=self.WEB_UA).content.decode()
        bluetooth = self._option(html, "bluetooth")
        self.assertIn("selected", bluetooth)
        self.assertNotIn("disabled", bluetooth)

    def test_the_app_only_options_you_have_not_chosen_are_still_disabled(self):
        html = self.client.get(reverse("printing"), HTTP_USER_AGENT=self.WEB_UA).content.decode()
        self.assertIn("disabled", self._option(html, "system"))

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_enabling_print_from_computer_saves_first_time(self):
        """Pins push config, which ``_show_print_from_computer`` needs to offer the checkbox."""
        MobileDevice.objects.filter(user=self.user).update(ever_print_ready=True)
        response = self._save(print_from_computer="on")
        self.assertEqual(
            response.status_code, 302, getattr(response, "context", None) and response.context["form"].errors
        )
        self.prefs.refresh_from_db()
        self.assertTrue(self.prefs.print_from_computer)
        self.assertEqual(self.prefs.print_method, "bluetooth")

    def test_a_post_with_no_print_method_keeps_the_stored_one(self):
        data = self._form_data()
        data.pop("print_method")
        response = self.client.post(reverse("printing") + "?next=/", data, HTTP_USER_AGENT=self.WEB_UA)
        self.assertEqual(response.status_code, 302)
        self.prefs.refresh_from_db()
        self.assertEqual(self.prefs.print_method, "bluetooth")

    def test_pdf_is_still_selectable_from_the_web(self):
        response = self._save(print_method="pdf")
        self.assertEqual(response.status_code, 302)
        self.prefs.refresh_from_db()
        self.assertEqual(self.prefs.print_method, "pdf")


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


class _FakeLot:
    """Just a pk: the batch loop's cap and time budget don't need real renders."""

    def __init__(self, pk):
        self.pk = pk


@isolated_cache("label-batch")
class MobileLabelBatchTests(StandardTestCase):
    """POST labels/batch/ — a whole print run in one request."""

    def setUp(self):
        super().setUp()
        self.url = reverse("mobile-labels-batch")
        UserLabelPrefs.objects.get_or_create(user=self.user)
        self.lots = [self.lot] + [
            Lot.objects.create(
                lot_name=f"batch label {i}",
                auction=self.lot.auction,
                auctiontos_seller=self.lot.auctiontos_seller,
                user=self.lot.user,
                quantity=1,
            )
            for i in range(2)
        ]

    def _post(self, body, user=None):
        return self.client.post(self.url, body, content_type="application/json", **_bearer(user or self.user))

    def test_one_request_returns_every_label_in_the_order_asked_for(self):
        pks = [lot.pk for lot in self.lots]
        body = self._post({"lots": pks}).json()
        self.assertEqual([entry["lot"] for entry in body["labels"]], pks)
        self.assertEqual(body["remaining"], [])

    def test_each_entry_is_a_png(self):
        entry = self._post({"lots": [self.lot.pk]}).json()["labels"][0]
        self.assertEqual(entry["content_type"], "image/png")
        self.assertEqual(base64.b64decode(entry["png"])[:8], b"\x89PNG\r\n\x1a\n")

    def test_a_lot_you_cannot_print_is_skipped_rather_than_failing_the_run(self):
        other = Lot.objects.create(lot_name="not yours", user=self.userB, quantity=1)
        body = self._post({"lots": [self.lot.pk, other.pk, 9999999]}).json()
        self.assertEqual([entry["lot"] for entry in body["labels"]], [self.lot.pk])
        self.assertEqual(sorted(entry["lot"] for entry in body["skipped"]), sorted([other.pk, 9999999]))

    def test_a_lot_that_does_not_exist_is_skipped_not_left_pending(self):
        body = self._post({"lots": [self.lot.pk, 9000001]}).json()
        self.assertEqual(body["remaining"], [])
        self.assertEqual([entry["lot"] for entry in body["skipped"]], [9000001])

    def test_a_run_longer_than_a_chunk_hands_the_rest_back(self):
        """A run longer than a chunk hands the rest back; the server picks the chunk size."""
        from auctions.mobile.services import label_raster

        lots = [_FakeLot(pk) for pk in range(1, label_raster.MAX_LABELS_PER_BATCH + 3)]
        with patch.object(label_raster, "render_lot_label_png", return_value=b"png"):
            rendered, remaining = label_raster.render_lot_labels_png(lots, None, width=600, height=400, dpi=203)
        self.assertEqual(len(rendered), label_raster.MAX_LABELS_PER_BATCH)
        self.assertEqual([lot.pk for lot in remaining], [lot.pk for lot in lots[label_raster.MAX_LABELS_PER_BATCH :]])

    def test_a_slow_render_stops_at_the_time_budget_but_never_returns_nothing(self):
        from auctions.mobile.services import label_raster

        lots = [_FakeLot(pk) for pk in range(1, 6)]
        slow = iter([0.0] + [label_raster.BATCH_TIME_BUDGET_SECONDS + 1] * 10)
        with (
            patch.object(label_raster, "render_lot_label_png", return_value=b"png"),
            patch.object(label_raster.time, "monotonic", lambda: next(slow)),
        ):
            rendered, remaining = label_raster.render_lot_labels_png(lots, None, width=600, height=400, dpi=203)
        self.assertEqual(len(rendered), 1)
        self.assertEqual(len(remaining), 4)

    def test_rendering_a_label_does_not_mark_it_printed(self):
        self._post({"lots": [self.lot.pk]})
        self.lot.refresh_from_db()
        self.assertFalse(self.lot.label_printed)

    def test_the_same_label_twice_is_rendered_once(self):
        with patch(
            "auctions.mobile.services.label_raster.rasterize_pdf", return_value=b"\x89PNG\r\n\x1a\nfake"
        ) as raster:
            self._post({"lots": [self.lot.pk]})
            self._post({"lots": [self.lot.pk]})
        self.assertEqual(raster.call_count, 1)

    def test_editing_the_lot_renders_it_again(self):
        with patch(
            "auctions.mobile.services.label_raster.rasterize_pdf", return_value=b"\x89PNG\r\n\x1a\nfake"
        ) as raster:
            self._post({"lots": [self.lot.pk]})
            self.lot.lot_name = "a different name on the label"
            self.lot.save()
            self._post({"lots": [self.lot.pk]})
        self.assertEqual(raster.call_count, 2)

    def test_a_bad_resolution_is_a_400(self):
        self.assertEqual(self._post({"lots": [self.lot.pk], "resolution": "huge"}).status_code, 400)

    def test_requires_jwt(self):
        self.assertIn(self.client.post(self.url, {"lots": [self.lot.pk]}).status_code, (401, 403))


class MobileLabelAcceptHeaderTests(StandardTestCase):
    """An image or PDF Accept header must not 406."""

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
        resp = self.client.get(self.url, {"fmt": "pdf"}, HTTP_ACCEPT="application/pdf", **_bearer(self.userB))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp["Content-Type"], "application/json")
        self.assertIn("detail", resp.json())


# Part 2 — push routing decision


class PushConfiguredTests(TestCase):
    # Pinned: a box with real FIREBASE_CREDENTIALS_JSON would fail this.
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


# Part 2 — send_push_to_user fan-out + token pruning


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


# Part 2 — device register/unregister (token lifecycle)


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


# Part 2 — preferences form push toggle (disabled without a device)


class PreferencesPushToggleTests(TestCase):
    def test_toggle_disabled_without_device(self):
        from auctions.forms import ChangeUserNotificationsForm

        user = User.objects.create_user(username="pref1", password="x")
        form = ChangeUserNotificationsForm(user, instance=user.userdata)
        self.assertTrue(form.fields["push_notifications_instead_of_email"].disabled)

    def test_toggle_enabled_with_device(self):
        from auctions.forms import ChangeUserNotificationsForm

        user = User.objects.create_user(username="pref2", password="x")
        MobileDevice.objects.create(user=user, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True)
        form = ChangeUserNotificationsForm(user, instance=user.userdata)
        self.assertFalse(form.fields["push_notifications_instead_of_email"].disabled)


# Part 2 — promo push job + weekly_promo skip


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
        # Backdate past the auction's 24h settle window.
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
    def test_title_is_short_and_the_body_carries_name_and_distance(self):
        with patch("auctions.tasks.send_push_to_user.delay") as delay:
            call_command("promo_push_notifications")
        self.assertEqual(delay.call_args.kwargs["title"], "New auction")
        body = delay.call_args.kwargs["body"]
        self.assertIn(self.auction.title, body)
        self.assertIn("miles away", body)

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_distance_uses_the_users_own_unit(self):
        userdata = self.user.userdata
        userdata.distance_unit = "km"
        userdata.save()
        with patch("auctions.tasks.send_push_to_user.delay") as delay:
            call_command("promo_push_notifications")
        self.assertIn("km away", delay.call_args.kwargs["body"])

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
        # Eligible for the weekly promo, so the push-skip is really exercised.
        ud.last_activity = timezone.now() - datetime.timedelta(days=30)
        ud.save()
        MobileDevice.objects.create(user=user, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True)

        self.assertTrue(user.userdata.user_prefers_push())
        with patch("auctions.management.commands.weekly_promo.mail.send") as send:
            call_command("weekly_promo")
        emailed = [call.args[0] for call in send.call_args_list if call.args]
        self.assertNotIn(user.email, emailed)


# Part X — command-program schema v2


class ProgramSchemaV2Tests(TestCase):
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
        with self.assertRaises(ProgramValidationError):
            validate_profile_programs(print_program=[{"tx": "1d {u16le:total_bytes}"}])

    def test_bare_size_placeholder_in_tx_rejected_unconditionally(self):
        """A bare size placeholder in a hex tx renders as one byte, so it's always rejected."""
        for name in ("total_bytes", "width_bytes", "height_px", "width_px"):
            with self.assertRaises(ProgramValidationError, msg=name):
                validate_profile_programs(print_program=[{"tx": f"1d 76 {{{name}}}"}])

    def test_bare_byte_placeholders_still_allowed_in_tx(self):
        # The D11s rows depend on these, and they really are one byte.
        validate_profile_programs(print_program=[{"tx": "10 ff 10 00 {density}"}, {"tx": "10 ff 84 {paper_type}"}])

    def test_size_placeholders_are_fine_in_tx_text(self):
        validate_profile_programs(print_program=[{"tx_text": "BITMAP 0,0,{width_bytes},{height_px},0,"}])

    def test_tx_raster_encodings(self):
        validate_profile_programs(print_program=[{"tx_raster": {"encoding": "binary"}}])
        validate_profile_programs(print_program=[{"tx_raster": {"encoding": "hex"}}])

    def test_tx_raster_false_still_rejected(self):
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
        for language, template in LANGUAGE_TEMPLATES.items():
            with self.subTest(language=language):
                validate_profile_programs(
                    print_program=template["print_program"], status_program=template["status_program"]
                )


# Part T — the TSPL profile


class TsplPrinterProfileTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.profile = ThermalPrinterProfile.objects.get(slug="tspl-raster")

    def test_seed_row_validates(self):
        self.profile.full_clean(exclude=["slug"])

    def test_verified_gatt_ids_are_pinned(self):
        """The GATT ids are pinned: the first writable characteristic is the radio's control channel."""
        self.assertEqual(self.profile.service_uuid, "49535343-fe7d-4ae5-8fa9-9fafd205e455")
        self.assertEqual(self.profile.write_characteristic_uuid, "49535343-8841-43f4-a8d4-ecbe34729bb3")
        self.assertEqual(self.profile.notify_characteristic_uuid, "49535343-1e4d-4bd9-ba61-23c647249616")

    def test_manufacturer_patterns_stay_empty(self):
        """No manufacturer patterns: "Feasycom" is the radio module, used in unrelated products."""
        self.assertEqual(self.profile.manufacturer_patterns, [])

    def test_raster_is_inverted(self):
        self.assertTrue(self.profile.invert_raster)

    def test_program_has_no_await_step(self):
        """TSPL has no print-completion ack, so the program must not await one."""
        self.assertNotIn("await", [key for step in self.profile.print_program for key in step])

    def test_status_values_disambiguate_the_lid_open_reading(self):
        """0x07 means lid open, not out of paper and jammed."""
        self.assertEqual(self.profile.status_flags["values"]["07"], ["no_ribbon", "cover_open"])
        self.assertEqual(self.profile.status_flags["values"]["00"], [])
        self.assertNotIn("out_of_paper", self.profile.status_flags["values"]["07"])

    def test_uses_schema_v2(self):
        self.assertEqual(self.profile.schema_version, 2)

    def test_priority_sits_between_the_d11s_rows_and_the_escpos_fallback(self):
        priorities = dict(ThermalPrinterProfile.objects.values_list("slug", "priority"))
        self.assertLess(priorities["d11s-aiyin"], priorities["tspl-raster"])
        self.assertLess(priorities["tspl-raster"], priorities["escpos-raster"])

    def test_profile_names_read_as_printers(self):
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
        """Exactly one enabled TSPL profile, so the app can auto-select it."""
        tspl = ThermalPrinterProfile.objects.filter(enabled=True, command_language="tspl")
        self.assertEqual(tspl.count(), 1)


# Part U1/U2 + Y1 — capturing what an unknown printer is


class ObservedPrinterProbeCaptureTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("mobile-printer-observed")

    def _post(self, **overrides):
        payload = {"ble_name": "Y486BT_AB10-BLE", "model": "FSC-BT986", "matched_by": "probe"}
        payload.update(overrides)
        return self.client.post(self.url, payload, content_type="application/json", **_bearer(self.user))

    def test_probe_match_is_no_longer_reported_as_device_info(self):
        """matched_by="probe" is accepted and stored as such."""
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
        """A plain re-pairing doesn't wipe stored probe evidence."""
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
        self.assertEqual(observed.status_ambiguities, [])

    def test_absent_fields_are_still_a_valid_report(self):
        self.assertEqual(self._post(matched_by="bleName").status_code, 201)
        observed = ObservedPrinter.objects.get(user=self.user)
        self.assertEqual(observed.probe_replies, {})
        self.assertEqual(observed.gatt, [])
        self.assertFalse(observed.characterized)

    def test_oversized_json_is_dropped_not_rejected(self):
        """Oversized JSON is dropped, not rejected."""
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


# Part Y2 — drafting a profile from an observation


class DraftProfileFromObservationTests(StandardTestCase):
    # The real Y486BT: the writable control channel comes first.
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
        """Drafted profiles start disabled until someone prints a test label."""
        profile, created = draft_profile_from_observation(self._observation())
        self.assertTrue(created)
        self.assertFalse(profile.enabled)
        self.assertEqual(profile.slug, "itpp941")
        self.assertEqual(profile.command_language, "tspl")

    def test_draft_validates_as_a_real_profile(self):
        profile, _ = draft_profile_from_observation(self._observation())
        profile.full_clean(exclude=["slug"])

    def test_skips_the_radio_control_channel(self):
        """The draft skips the radio control channel."""
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
        """A manufacturer that may be the radio module is flagged in the notes."""
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
        # A model name slugifying onto a seeded profile must not clobber it.
        with self.assertRaises(DraftError):
            draft_profile_from_observation(self._observation(model="TSPL raster"))
        self.assertEqual(
            ThermalPrinterProfile.objects.get(slug="tspl-raster").name,
            "TSPL label printer (VEVOR Y486BT, TSC-compatible)",
        )
        # Nor may a redraft clobber a confirmed draft.
        observation = self._observation()
        first, _ = draft_profile_from_observation(observation)
        ThermalPrinterProfile.objects.filter(pk=first.pk).update(enabled=True)
        with self.assertRaises(DraftError):
            draft_profile_from_observation(observation)

    def test_no_probed_language_cannot_be_drafted(self):
        with self.assertRaises(DraftError):
            draft_profile_from_observation(self._observation(probed_language=""))

    def test_zpl_draft_uses_schema_v2_constructs(self):
        profile, _ = draft_profile_from_observation(self._observation(probed_language="zpl", model="ZD421"))
        program = json.dumps(profile.print_program)
        self.assertIn("{total_bytes}", program)
        self.assertIn('"encoding": "hex"', program)
        self.assertEqual(profile.schema_version, 2)
        profile.full_clean(exclude=["slug"])


# Part W2 — POST /api/mobile/labels/printed/


class MobileLabelsPrintedApiTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("mobile-labels-printed")

    def _post(self, lots, user=None):
        return self.client.post(self.url, {"lots": lots}, content_type="application/json", **_bearer(user or self.user))

    def test_marks_labels_printed(self):
        resp = self._post([self.lot.pk, self.lotB.pk])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"marked": 2, "failed": 0})
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
        self.assertEqual(self._post([self.lot.pk]).json(), {"marked": 1, "failed": 0})

    def test_shrinks_the_unprinted_queryset(self):
        """Reported labels leave the unprinted queryset."""
        before = self.online_tos.unprinted_label_count
        self._post([self.lot.pk])
        # unprinted_label_count is cached on the instance.
        after = AuctionTOS.objects.get(pk=self.online_tos.pk).unprinted_label_count
        self.assertEqual(after, before - 1)

    def test_lots_the_caller_cannot_touch_are_skipped_not_refused(self):
        """Lots the caller can't touch are skipped, not the whole report refused."""
        stranger = User.objects.create_user(username="stranger", password="x")
        resp = self._post([self.lot.pk], user=stranger)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"marked": 0, "failed": 0})
        self.lot.refresh_from_db()
        self.assertFalse(self.lot.label_printed)

    def test_a_mixed_batch_marks_what_it_may(self):
        stranger_lot = Lot.objects.create(lot_name="not yours", user=self.userB, quantity=1)
        resp = self._post([self.lot.pk, stranger_lot.pk])
        self.assertEqual(resp.json(), {"marked": 1, "failed": 0})
        stranger_lot.refresh_from_db()
        self.assertFalse(stranger_lot.label_printed)

    def test_auction_admin_may_mark_a_sellers_labels(self):
        resp = self._post([self.lot.pk], user=self.admin_user)
        self.assertEqual(resp.json(), {"marked": 1, "failed": 0})

    def test_unknown_and_deleted_pks_are_ignored(self):
        self.lotB.is_deleted = True
        self.lotB.save()
        resp = self._post([self.lot.pk, self.lotB.pk, 99999999])
        self.assertEqual(resp.json(), {"marked": 1, "failed": 0})

    def test_empty_batch_is_fine(self):
        self.assertEqual(self._post([]).json(), {"marked": 0, "failed": 0})

    def test_malformed_body_is_a_400(self):
        resp = self.client.post(self.url, {"lots": ["nope"]}, content_type="application/json", **_bearer(self.user))
        self.assertEqual(resp.status_code, 400)

    def test_requires_jwt(self):
        self.assertIn(self.client.post(self.url, {"lots": []}, content_type="application/json").status_code, (401, 403))


class LabelPrintFailureReportTests(StandardTestCase):
    """The app reports which labels didn't come out, using the profiles' status_flags vocabulary."""

    def setUp(self):
        super().setUp()
        self.url = reverse("mobile-labels-printed")

    def _post(self, body, user=None):
        return self.client.post(self.url, body, content_type="application/json", **_bearer(user or self.user))

    def test_a_failed_label_stays_unprinted_and_is_flagged_for_reprinting(self):
        resp = self._post({"lots": [self.lotB.pk], "failed": [self.lot.pk], "conditions": ["paper_jam"]})
        self.assertEqual(resp.json(), {"marked": 1, "failed": 1})
        self.lot.refresh_from_db()
        self.assertFalse(self.lot.label_printed)
        self.assertTrue(self.lot.label_needs_reprinting)

    def test_a_jam_on_a_reprint_puts_the_label_back_to_unprinted(self):
        Lot.objects.filter(pk=self.lot.pk).update(label_printed=True, label_needs_reprinting=False)
        self._post({"lots": [], "failed": [self.lot.pk], "conditions": ["out_of_paper"]})
        self.lot.refresh_from_db()
        self.assertFalse(self.lot.label_printed)

    def test_the_failed_label_comes_back_in_print_unprinted_labels(self):
        self._post({"lots": [self.lot.pk]})
        self.assertNotIn(self.lot.pk, self.online_tos.unprinted_labels_qs.values_list("pk", flat=True))
        self._post({"lots": [], "failed": [self.lot.pk], "conditions": ["paper_jam"]})
        self.assertIn(self.lot.pk, self.online_tos.unprinted_labels_qs.values_list("pk", flat=True))

    def test_a_lot_in_both_lists_counts_as_failed(self):
        self._post({"lots": [self.lot.pk], "failed": [self.lot.pk]})
        self.lot.refresh_from_db()
        self.assertFalse(self.lot.label_printed)

    def test_a_condition_outside_the_profiles_vocabulary_is_rejected(self):
        resp = self._post({"lots": [], "failed": [self.lot.pk], "conditions": ["printer_on_fire"]})
        self.assertEqual(resp.status_code, 400)
        self.lot.refresh_from_db()
        self.assertFalse(self.lot.label_needs_reprinting)

    def test_every_condition_a_profile_can_decode_is_accepted(self):
        for condition in sorted(STATUS_CONDITIONS):
            resp = self._post({"lots": [], "failed": [self.lot.pk], "conditions": [condition]})
            self.assertEqual(resp.status_code, 200, condition)

    def test_an_app_that_reports_no_failures_behaves_exactly_as_before(self):
        resp = self._post({"lots": [self.lot.pk]})
        self.assertEqual(resp.json(), {"marked": 1, "failed": 0})
        self.lot.refresh_from_db()
        self.assertTrue(self.lot.label_printed)

    def test_a_failed_lot_the_caller_cannot_touch_is_skipped(self):
        stranger_lot = Lot.objects.create(lot_name="not yours", user=self.userB, quantity=1)
        resp = self._post({"lots": [], "failed": [stranger_lot.pk]})
        self.assertEqual(resp.json(), {"marked": 0, "failed": 0})


class BulkBluetoothPrintLinkTests(StandardTestCase):
    """A Bluetooth user's bulk label button hands the lots to the app instead of a PDF sheet.

    Gated in LotLabelView, which every bulk entry point goes through.
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
        self._set_method("bluetooth")
        html = self._get(self.APP_UA).content.decode()
        pks = [str(pk) for pk in self.in_person_tos.print_labels_qs.values_list("pk", flat=True)]
        self.assertIn("fishauctions://print/?lots=" + ",".join(pks), html)

    def test_bluetooth_on_the_web_still_gets_the_pdf(self):
        self._set_method("bluetooth")
        resp = self._get(self.WEB_UA)
        self.assertEqual(resp["Content-Type"], "application/pdf")

    def test_pdf_method_in_the_app_still_gets_the_pdf(self):
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
        self._set_method("bluetooth")
        self._get(self.APP_UA)
        self.assertEqual(self.in_person_tos.unprinted_label_count, len(self.lots))

    def test_the_pdf_still_marks_labels_printed(self):
        self._set_method("pdf")
        self._get(self.APP_UA)
        self.assertEqual(self.in_person_tos.unprinted_label_count, 0)

    def test_long_lot_sets_are_capped_and_say_so(self):
        """Long lot sets are capped near 2000 characters and say so."""
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


# The Bluetooth PNG must be the PDF


class LabelPngMatchesPdfTests(StandardTestCase):
    """The Bluetooth PNG is page one of the WeasyPrint label PDF, rasterized."""

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
        """The PNG matches our own rasterization of the PDF exactly."""
        from auctions.mobile.services.label_pdf import render_single_lot_pdf
        from auctions.mobile.services.label_raster import rasterize_pdf

        content = self._png(resolution="600x400")

        request = self.client.get(self.url, **_bearer(self.user)).wsgi_request
        request.user = self.user
        pdf_bytes, _ = render_single_lot_pdf(self.lot, request, single_label_page=True, mark_printed=False)
        self.assertEqual(content, rasterize_pdf(pdf_bytes, width=600, height=400, dpi=203))

    def test_requested_resolution_is_exact(self):
        for resolution, size in (("600x400", (600, 400)), ("832x1248", (832, 1248)), ("96x200", (96, 200))):
            with self.subTest(resolution=resolution):
                self.assertEqual(self._image(self._png(resolution=resolution)).size, size)

    def test_label_is_not_distorted(self):
        """A mismatched aspect ratio gets white margins, not stretched text."""
        wide = self._image(self._png(resolution="800x200")).convert("L")
        # 3x2 into 800x200 is 300x200 of content, centred.
        self.assertEqual(wide.getpixel((2, 100)), 255)
        self.assertEqual(wide.getpixel((797, 100)), 255)

    def test_rendering_a_png_does_not_mark_the_label_printed(self):
        """Rendering a PNG doesn't mark the label printed."""
        self._png()
        self.lot.refresh_from_db()
        self.assertFalse(self.lot.label_printed)

    def test_a_sheet_preset_renders_one_label_not_a_sheet(self):
        """A sheet preset renders one label, not a sheet."""
        self.prefs.preset = "lg"
        self.prefs.save()
        image = self._image(self._png(resolution="780x243")).convert("L")
        dark = sum(count for value, count in enumerate(image.histogram()) if value < 128)
        self.assertGreater(dark, 500, "the label did not fill the frame")

    def test_the_label_respects_auction_print_fields(self):
        """Auction label_print_fields change the raster."""
        self.online_auction.label_print_fields = "lot_name,seller_name"
        self.online_auction.save()
        without_qr = self._png(resolution="600x400")
        self.online_auction.label_print_fields = "lot_name,seller_name,qr_code"
        self.online_auction.save()
        self.assertNotEqual(without_qr, self._png(resolution="600x400"))

    def test_falls_back_when_there_is_no_pdf_to_rasterize(self):
        """A lot with no auction falls back to the approximate renderer."""
        orphan = Lot.objects.create(lot_name="no auction here", user=self.user, quantity=1)
        url = reverse("mobile-label-lot", kwargs={"pk": orphan.pk})
        resp = self.client.get(url, {"resolution": "600x400"}, **_bearer(self.user))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "image/png")
        self.assertEqual(self._image(resp.content).size, (600, 400))

    def test_bad_resolution_is_still_a_400(self):
        self.assertEqual(self.client.get(self.url, {"resolution": "wide"}, **_bearer(self.user)).status_code, 400)

    def test_the_web_png_endpoint_matches_the_mobile_one(self):
        """The web PNG endpoint matches the mobile one."""
        self.client.force_login(self.user)
        web = self.client.get(
            reverse("single_lot_label", kwargs={"pk": self.lot.pk}), {"fmt": "png", "resolution": "600x400"}
        )
        self.assertEqual(web.status_code, 200)
        self.assertEqual(web["Content-Type"], "image/png")
        self.assertEqual(web.content, self._png(resolution="600x400"))


# Part Y3 — telling a user their printer is supported now


class PrinterSupportedNotificationTests(StandardTestCase):
    """Enabling a profile that matches a hand-identified printer notifies its owner."""

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
        # The enqueue is on_commit.
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
        profile = ThermalPrinterProfile(slug="x", name="X", print_program=[{"tx": "1d 0c"}], model_patterns=["^ITPP("])
        self.assertFalse(profile_matches_observation(profile, self.observation))

    def test_matcher_ignores_blank_observation_fields(self):
        blank = ObservedPrinter(ble_name="", model="", manufacturer="")
        profile = ThermalPrinterProfile(slug="y", name="Y", print_program=[{"tx": "1d 0c"}], model_patterns=[".*"])
        self.assertFalse(profile_matches_observation(profile, blank))


class WatchedLotPushRoutingTests(StandardTestCase):
    """A watcher reachable in the app is notified only there, not by browser push too."""

    def setUp(self):
        super().setUp()
        self.watcher = self.user_with_no_lots
        userdata = UserData.objects.get(user=self.watcher)
        userdata.push_notifications_when_lots_sell = True
        userdata.save()
        Watch.objects.create(lot_number=self.in_person_lot, user=self.watcher)

    def _web_subscription(self):
        from webpush.models import PushInformation, SubscriptionInfo

        subscription = SubscriptionInfo.objects.create(
            browser="Chrome",
            endpoint="https://fcm.googleapis.com/push/example_token",
            auth="auth_secret",
            p256dh="p256dh_key",
        )
        return PushInformation.objects.create(user=self.watcher, subscription=subscription)

    def _app_device(self, token="tok", push_enabled=True):
        return MobileDevice.objects.create(
            user=self.watcher, device_uuid=uuid.uuid4(), fcm_token=token, push_enabled=push_enabled
        )

    def _notify(self, **kwargs):
        from auctions.views import notify_watchers_lot_selling_soon

        with (
            patch_views("send_push_to_user.delay") as app_push,
            patch_views("send_user_notification") as web_push,
        ):
            notify_watchers_lot_selling_soon(self.in_person_lot, **kwargs)
        return app_push, web_push

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_app_user_gets_the_app_push_and_no_browser_push(self):
        self._web_subscription()
        self._app_device()
        app_push, web_push = self._notify()
        web_push.assert_not_called()
        app_push.assert_called_once()
        self.assertEqual(app_push.call_args.args[0], self.watcher.pk)
        self.assertEqual(app_push.call_args.kwargs["category"], notifications.CATEGORY_LOT_SELLING)
        # Same tag as the browser payload, so the newer alert replaces the older.
        self.assertEqual(app_push.call_args.kwargs["collapse_key"], f"lot_sell_notification_{self.in_person_lot.pk}")

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_the_email_toggle_does_not_govern_this_category(self):
        self._app_device()
        self.assertFalse(self.watcher.userdata.push_notifications_instead_of_email)
        app_push, web_push = self._notify()
        app_push.assert_called_once()
        web_push.assert_not_called()

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_browser_only_watcher_is_unaffected(self):
        self._web_subscription()
        app_push, web_push = self._notify()
        app_push.assert_not_called()
        web_push.assert_called_once()

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_device_with_push_switched_off_falls_back_to_the_browser(self):
        self._web_subscription()
        self._app_device(push_enabled=False)
        app_push, web_push = self._notify()
        app_push.assert_not_called()
        web_push.assert_called_once()

    @override_settings(FIREBASE_CREDENTIALS_JSON="")
    def test_browser_still_used_when_fcm_is_not_configured(self):
        self._web_subscription()
        self._app_device()
        app_push, web_push = self._notify()
        app_push.assert_not_called()
        web_push.assert_called_once()

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_coming_up_soon_pushes_to_the_app_too(self):
        self._app_device()
        app_push, _ = self._notify(position=3)
        app_push.assert_called_once()
        self.assertIn("coming up soon", app_push.call_args.kwargs["title"])

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_test_notification_button_uses_the_app_channel(self):
        self._app_device()
        self.client.login(username=self.watcher.username, password="testpassword")
        with (
            patch_views("send_push_to_user.delay") as app_push,
            patch_views("send_user_notification") as web_push,
        ):
            response = self.client.post(reverse("lot_push_test", kwargs={"pk": self.in_person_lot.pk}))
        self.assertEqual(response.status_code, 200)
        app_push.assert_called_once()
        web_push.assert_not_called()

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_lot_page_points_an_app_user_at_their_phone(self):
        self._app_device()
        self.client.login(username=self.watcher.username, password="testpassword")
        response = self.client.get(reverse("lot_by_pk", kwargs={"pk": self.in_person_lot.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "notification in the app on your phone")
        self.assertNotContains(response, "webpush-subscribe-button")

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_lot_page_offers_an_app_user_a_way_to_turn_the_alerts_on(self):
        userdata = UserData.objects.get(user=self.watcher)
        userdata.push_notifications_when_lots_sell = False
        userdata.save()
        self._app_device()
        self.client.login(username=self.watcher.username, password="testpassword")
        response = self.client.get(reverse("lot_by_pk", kwargs={"pk": self.in_person_lot.pk}))
        self.assertContains(response, 'id="enable-app-notifications"')

    def test_lot_page_keeps_the_browser_button_for_everyone_else(self):
        self.client.login(username=self.watcher.username, password="testpassword")
        response = self.client.get(reverse("lot_by_pk", kwargs={"pk": self.in_person_lot.pk}))
        self.assertContains(response, "webpush-subscribe-button")


class PreferencesWebpushVisibilityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="prefs_push", password="x")

    def _form(self, **kwargs):
        from auctions.forms import ChangeUserNotificationsForm

        return ChangeUserNotificationsForm(self.user, instance=self.user.userdata, **kwargs)

    def test_offered_on_the_web_without_the_app(self):
        self.assertTrue(self._form().can_subscribe_to_webpush)

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_hidden_for_an_app_user_with_an_explanation(self):
        MobileDevice.objects.create(user=self.user, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True)
        form = self._form()
        self.assertFalse(form.can_subscribe_to_webpush)
        self.assertIn("app on your phone", form.fields["push_notifications_when_lots_sell"].help_text)

    def test_hidden_inside_the_app_webview(self):
        # No Push API in a WebView.
        self.assertFalse(self._form(is_mobile_app=True).can_subscribe_to_webpush)


@override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
class RunningTotalNotificationTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.buyer = self.user_with_no_lots  # the account behind self.in_person_buyer
        MobileDevice.objects.create(user=self.buyer, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True)
        self._sell(self.in_person_lot, 12)

    def _sell(self, lot, price):
        from auctions.models import Invoice

        lot.auctiontos_winner = self.in_person_buyer
        lot.winning_price = price
        lot.active = False
        lot.save()
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.in_person_buyer, auction=self.in_person_auction)
        invoice.recalculate()
        return lot

    def _notify(self, lot=None):
        """Run the helper and return the mocked task; the patch outlives captureOnCommitCallbacks."""
        with patch("auctions.tasks.send_push_to_user.delay") as push:
            with self.captureOnCommitCallbacks(execute=True):
                sent = notifications.notify_running_total(lot or self.in_person_lot)
        return sent, push

    def test_the_winner_is_told_what_they_have_spent(self):
        sent, push = self._notify()
        self.assertTrue(sent)
        # The first sale also sends a one-time tip.
        total_call = push.call_args_list[0]
        self.assertEqual(total_call.args[0], self.buyer.pk)
        self.assertEqual(total_call.kwargs["category"], notifications.CATEGORY_RUNNING_TOTAL)
        self.assertIn(self.in_person_lot.lot_name, total_call.kwargs["title"])
        self.assertIn("12", total_call.kwargs["title"])
        self.assertIn("12.00", total_call.kwargs["body"])
        # Tapping it opens their invoice for this auction.
        self.assertIn(
            reverse("my_auction_invoice", kwargs={"slug": self.in_person_auction.slug}),
            total_call.kwargs["url"],
        )

    def test_one_notification_per_auction_rather_than_one_per_lot(self):
        _, first = self._notify()
        second_lot = Lot.objects.create(
            lot_name="a second test lot",
            auction=self.in_person_auction,
            auctiontos_seller=self.admin_in_person_tos,
            quantity=1,
            custom_lot_number="101-2",
        )
        self._sell(second_lot, 8)
        _, second = self._notify(second_lot)
        key = f"running_total_{self.in_person_auction.pk}"
        self.assertEqual(first.call_args_list[0].kwargs["collapse_key"], key)
        self.assertEqual(second.call_args_list[0].kwargs["collapse_key"], key)
        self.assertIn("20.00", second.call_args_list[0].kwargs["body"])

    def test_the_tip_is_sent_once_and_only_once(self):
        _, first = self._notify()
        self.assertEqual(len(first.call_args_list), 2)
        tip = first.call_args_list[1]
        self.assertEqual(tip.kwargs["category"], notifications.CATEGORY_RUNNING_TOTAL_TIP)
        self.assertEqual(tip.kwargs["title"], "Notifications as you win lots")
        # Tapping the tip lands on the page carrying the setting it names.
        self.assertIn(reverse("notification_preferences"), tip.kwargs["url"])
        self.buyer.userdata.refresh_from_db()
        self.assertTrue(self.buyer.userdata.running_total_tip_sent)
        # The next lot gets the running total alone.
        _, second = self._notify()
        self.assertEqual(len(second.call_args_list), 1)
        self.assertEqual(second.call_args_list[0].kwargs["category"], notifications.CATEGORY_RUNNING_TOTAL)

    def test_the_preference_turns_it_off(self):
        userdata = self.buyer.userdata
        userdata.show_running_total_notification = False
        userdata.save()
        sent, push = self._notify()
        self.assertFalse(sent)
        push.assert_not_called()

    def test_it_is_on_by_default(self):
        self.assertTrue(UserData.objects.get(user=self.buyer).show_running_total_notification)

    def test_nothing_is_sent_without_the_app(self):
        MobileDevice.objects.filter(user=self.buyer).delete()
        sent, push = self._notify()
        self.assertFalse(sent)
        push.assert_not_called()

    def test_online_auctions_are_left_alone(self):
        sent, push = self._notify(self.lot)  # self.lot belongs to the online auction
        self.assertFalse(sent)
        push.assert_not_called()

    def test_a_bidder_with_no_account_is_skipped(self):
        self.in_person_buyer.user = None
        self.in_person_buyer.save()
        sent, push = self._notify()
        self.assertFalse(sent)
        push.assert_not_called()

    def test_setting_a_winner_sends_it(self):
        from auctions.views import DynamicSetLotWinner

        view = DynamicSetLotWinner()
        view.request = type("R", (), {"user": self.admin_user})()
        view.auction = self.in_person_auction
        with patch_views("notify_running_total") as notify:
            view.set_winner(self.in_person_lot, self.in_person_buyer, 12)
        notify.assert_called_once_with(self.in_person_lot)

    def test_the_toggle_is_greyed_out_without_a_device(self):
        from auctions.forms import ChangeUserNotificationsForm

        MobileDevice.objects.filter(user=self.buyer).delete()
        form = ChangeUserNotificationsForm(self.buyer, instance=self.buyer.userdata)
        self.assertTrue(form.fields["show_running_total_notification"].disabled)

    def test_the_toggle_is_usable_with_a_device(self):
        from auctions.forms import ChangeUserNotificationsForm

        form = ChangeUserNotificationsForm(self.buyer, instance=self.buyer.userdata)
        self.assertFalse(form.fields["show_running_total_notification"].disabled)


class QueueRespectsTheAuctionNotificationSettingTests(StandardTestCase):
    """The lot queue honours message_users_when_lots_sell, like the set-winners screen."""

    def setUp(self):
        super().setUp()
        userdata = UserData.objects.get(user=self.user_with_no_lots)
        userdata.push_notifications_when_lots_sell = True
        userdata.save()
        Watch.objects.create(lot_number=self.in_person_lot, user=self.user_with_no_lots)
        from auctions.models import LotQueueEntry

        LotQueueEntry.objects.create(auction=self.in_person_auction, lot=self.in_person_lot, order=1)

    def _process(self):
        from auctions.views import process_queue_notifications

        with patch_views("notify_watchers_lot_selling_soon") as notify:
            process_queue_notifications(self.in_person_auction)
        return notify

    def test_notifies_when_the_setting_is_on(self):
        self.in_person_auction.message_users_when_lots_sell = True
        self.in_person_auction.save()
        self._process().assert_called_once()

    def test_silent_when_the_setting_is_off(self):
        self.in_person_auction.message_users_when_lots_sell = False
        self.in_person_auction.save()
        self._process().assert_not_called()

    def test_kiosk_still_refreshes_when_the_setting_is_off(self):
        self.in_person_auction.message_users_when_lots_sell = False
        self.in_person_auction.save()
        from auctions.views import process_queue_notifications

        with patch("auctions.views.selling.broadcast_queue_update") as broadcast:
            process_queue_notifications(self.in_person_auction)
        broadcast.assert_called_once()


# Part 2 — which categories are allowed to leave the inbox at all


class PushExemptCategoryTests(TestCase):
    """Some mail stays mail no matter how much the recipient prefers push."""

    def setUp(self):
        self.user = User.objects.create_user(username="exempt", password="x")
        userdata = self.user.userdata
        userdata.push_notifications_instead_of_email = True
        userdata.save()
        MobileDevice.objects.create(user=self.user, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True)

    def _notify(self, category):
        sent = []
        with patch("auctions.tasks.send_push_to_user.delay") as delay:
            pushed = notifications.notify_user(
                self.user, category=category, title="t", body="b", url="u", send_email=lambda: sent.append(1)
            )
        return pushed, sent, delay

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_club_membership_is_always_emailed(self):
        pushed, sent, delay = self._notify(notifications.CATEGORY_MEMBERSHIP)
        self.assertFalse(pushed)
        self.assertEqual(sent, [1])
        delay.assert_not_called()

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_auction_admin_mail_is_always_emailed(self):
        pushed, sent, delay = self._notify(notifications.CATEGORY_AUCTION_ADMIN)
        self.assertFalse(pushed)
        self.assertEqual(sent, [1])
        delay.assert_not_called()

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_the_join_reminder_is_a_good_push(self):
        pushed, sent, delay = self._notify(notifications.CATEGORY_AUCTION_REMINDER)
        self.assertTrue(pushed)
        self.assertEqual(sent, [])
        delay.assert_called_once()


class JoinReminderPushTests(TestCase):
    """The "you looked but never joined" nudge goes to the app when it can."""

    def setUp(self):
        now = timezone.now()
        self.seller = User.objects.create_user(username="jr_seller", password="x")
        self.auction = Auction.objects.create(
            created_by=self.seller,
            title="Reminder Auction",
            is_online=True,
            date_start=now + datetime.timedelta(days=1),
            date_end=now + datetime.timedelta(days=3),
        )
        PickupLocation.objects.create(
            name="loc",
            auction=self.auction,
            latitude=40.0,
            longitude=-80.0,
            pickup_time=now + datetime.timedelta(days=1),
        )
        self.user = User.objects.create_user(username="jr_viewer", password="x", email="jr@example.com")
        userdata = self.user.userdata
        userdata.push_notifications_instead_of_email = True
        userdata.send_reminder_emails_about_joining_auctions = True
        userdata.email_me_about_new_auctions_distance = 1000
        userdata.latitude = 40.1
        userdata.longitude = -80.1
        userdata.save()
        MobileDevice.objects.create(user=self.user, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True)

        from auctions.models import AuctionCampaign

        self.campaign = AuctionCampaign.objects.create(auction=self.auction, user=self.user, email=self.user.email)
        AuctionCampaign.objects.filter(pk=self.campaign.pk).update(timestamp=now - datetime.timedelta(hours=48))

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_pushes_instead_of_emailing(self):
        with (
            patch("auctions.tasks.send_push_to_user.delay") as delay,
            patch("auctions.management.commands.auctiontos_notifications.mail.send") as send,
        ):
            call_command("auctiontos_notifications")
        delay.assert_called_once()
        self.assertEqual(delay.call_args.kwargs["title"], "Don't miss this auction")
        self.assertIn(self.auction.title, delay.call_args.kwargs["body"])
        self.assertIn(str(self.campaign.uuid), delay.call_args.kwargs["url"])
        emailed = [call.args[0] for call in send.call_args_list if call.args]
        self.assertNotIn(self.user.email, emailed)

    @override_settings(FIREBASE_CREDENTIALS_JSON="")
    def test_falls_back_to_email_without_push(self):
        with (
            patch("auctions.tasks.send_push_to_user.delay") as delay,
            patch("auctions.management.commands.auctiontos_notifications.mail.send") as send,
        ):
            call_command("auctiontos_notifications")
        delay.assert_not_called()
        emailed = [call.args[0] for call in send.call_args_list if call.args]
        self.assertIn(self.user.email, emailed)


class UninstallFallbackTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="gone", password="x", email="gone@example.com")
        userdata = self.user.userdata
        userdata.push_notifications_instead_of_email = True
        userdata.save()
        self.device = MobileDevice.objects.create(
            user=self.user, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True
        )

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_dead_token_is_pruned_and_the_notification_is_emailed_not_lost(self):
        with (
            patch("auctions.notifications.send_fcm_message", return_value=notifications.SEND_INVALID_TOKEN),
            patch("auctions.tasks.mail.send") as send,
        ):
            sent = tasks.send_push_to_user(
                self.user.pk, title="Your invoice is ready", body="b", url="https://x/y", category="invoice"
            )
        self.assertEqual(sent, 0)
        self.device.refresh_from_db()
        self.assertEqual(self.device.fcm_token, "")
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], self.user.email)
        self.assertEqual(send.call_args.kwargs["subject"], "Your invoice is ready")

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_transient_fcm_failure_also_emails_rather_than_dropping(self):
        with (
            patch("auctions.notifications.send_fcm_message", return_value=notifications.SEND_ERROR),
            patch("auctions.tasks.mail.send") as send,
        ):
            tasks.send_push_to_user(self.user.pk, title="t", body="b", url="u", category="invoice")
        send.assert_called_once()
        # A transient error doesn't kill the token.
        self.device.refresh_from_db()
        self.assertEqual(self.device.fcm_token, "tok")

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_push_only_categories_are_dropped_rather_than_emailed(self):
        for category in ("volunteer", "lot_selling", "promo", "printer"):
            with (
                patch("auctions.notifications.send_fcm_message", return_value=notifications.SEND_INVALID_TOKEN),
                patch("auctions.tasks.mail.send") as send,
            ):
                tasks.send_push_to_user(self.user.pk, title="t", body="b", url="u", category=category)
            send.assert_not_called()

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_a_successful_send_does_not_also_email(self):
        with (
            patch("auctions.notifications.send_fcm_message", return_value=notifications.SEND_OK),
            patch("auctions.tasks.mail.send") as send,
        ):
            sent = tasks.send_push_to_user(self.user.pk, title="t", body="b", url="u", category="invoice")
        self.assertEqual(sent, 1)
        send.assert_not_called()

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_later_notifications_route_to_email_on_their_own(self):
        self.assertTrue(self.user.userdata.user_prefers_push())
        MobileDevice.objects.filter(pk=self.device.pk).update(fcm_token="")
        self.assertFalse(self.user.userdata.user_prefers_push())
        emailed = []
        with patch("auctions.tasks.send_push_to_user.delay") as delay:
            pushed = notifications.notify_user(
                self.user,
                category="invoice",
                title="t",
                body="b",
                url="u",
                send_email=lambda: emailed.append(1),
            )
        self.assertFalse(pushed)
        self.assertEqual(emailed, [1])
        delay.assert_not_called()

    def test_preferences_explain_a_phone_that_has_gone_quiet(self):
        from auctions.forms import ChangeUserNotificationsForm

        MobileDevice.objects.filter(pk=self.device.pk).update(fcm_token="")
        form = ChangeUserNotificationsForm(self.user, instance=self.user.userdata)
        field = form.fields["push_notifications_instead_of_email"]
        self.assertTrue(field.disabled)
        self.assertIn("isn't receiving notifications right now", field.help_text)

    def test_preferences_still_pitch_the_app_to_someone_who_never_had_it(self):
        from auctions.forms import ChangeUserNotificationsForm

        newcomer = User.objects.create_user(username="newbie", password="x")
        form = ChangeUserNotificationsForm(newcomer, instance=newcomer.userdata)
        self.assertIn("Install the app", form.fields["push_notifications_instead_of_email"].help_text)

    def test_the_stored_choice_survives_so_reinstalling_resumes_push(self):
        from auctions.forms import ChangeUserNotificationsForm

        MobileDevice.objects.filter(pk=self.device.pk).update(fcm_token="")
        form = ChangeUserNotificationsForm(self.user, data={}, instance=self.user.userdata)
        form.is_valid()
        # A disabled field keeps the stored value.
        self.assertTrue(form.cleaned_data["push_notifications_instead_of_email"])


# Part N — the app's notification opt-in


class MobileNotificationPrefsApiTests(TestCase):
    """/api/mobile/notifications/prefs/, written after the app gets OS permission and registers."""

    def setUp(self):
        self.user = User.objects.create_user(username="prefs_api", password="x")
        self.url = reverse("mobile-notification-prefs")

    def test_get_returns_every_toggle(self):
        response = self.client.get(self.url, **_bearer(self.user))
        self.assertEqual(response.status_code, 200)
        # running_total defaults on.
        self.assertEqual(
            response.json(),
            {"push_instead_of_email": False, "push_when_lots_sell": False, "running_total": True},
        )

    def test_patch_writes_them(self):
        response = self.client.patch(
            self.url,
            data=json.dumps({"push_instead_of_email": True, "push_when_lots_sell": True}),
            content_type="application/json",
            **_bearer(self.user),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"push_instead_of_email": True, "push_when_lots_sell": True, "running_total": True},
        )
        userdata = UserData.objects.get(user=self.user)
        self.assertTrue(userdata.push_notifications_instead_of_email)
        self.assertTrue(userdata.push_notifications_when_lots_sell)

    def test_patch_can_turn_the_running_total_off(self):
        response = self.client.patch(
            self.url,
            data=json.dumps({"running_total": False}),
            content_type="application/json",
            **_bearer(self.user),
        )
        self.assertEqual(response.status_code, 200)
        self.user.userdata.refresh_from_db()
        self.assertFalse(self.user.userdata.show_running_total_notification)

    def test_patch_is_partial(self):
        UserData.objects.filter(user=self.user).update(push_notifications_when_lots_sell=True)
        response = self.client.patch(
            self.url,
            data=json.dumps({"push_instead_of_email": True}),
            content_type="application/json",
            **_bearer(self.user),
        )
        self.assertEqual(response.status_code, 200)
        userdata = UserData.objects.get(user=self.user)
        self.assertTrue(userdata.push_notifications_instead_of_email)
        self.assertTrue(userdata.push_notifications_when_lots_sell)

    def test_stores_intent_without_a_device_or_push_config(self):
        self.assertFalse(self.user.userdata.has_push_device)
        response = self.client.patch(
            self.url,
            data=json.dumps({"push_instead_of_email": True}),
            content_type="application/json",
            **_bearer(self.user),
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(UserData.objects.get(user=self.user).push_notifications_instead_of_email)

    def test_rejects_a_non_boolean(self):
        response = self.client.patch(
            self.url,
            data=json.dumps({"push_instead_of_email": "banana"}),
            content_type="application/json",
            **_bearer(self.user),
        )
        self.assertEqual(response.status_code, 400)

    def test_requires_a_token(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_only_touches_the_callers_own_prefs(self):
        other = User.objects.create_user(username="prefs_other", password="x")
        self.client.patch(
            self.url,
            data=json.dumps({"push_when_lots_sell": True}),
            content_type="application/json",
            **_bearer(self.user),
        )
        self.assertFalse(UserData.objects.get(user=other).push_notifications_when_lots_sell)


class LotPagePushPromptOfferTests(StandardTestCase):
    """The lot page tells the app when to offer notifications."""

    APP_UA = "FishAuctionsApp/1.0 (Flutter; iOS)"

    def setUp(self):
        super().setUp()
        # Put the fixture's in-person auction back on the calendar.
        self.in_person_auction.date_start = timezone.now() - datetime.timedelta(hours=1)
        self.in_person_auction.date_end = timezone.now() + datetime.timedelta(days=1)
        self.in_person_auction.message_users_when_lots_sell = True
        self.in_person_auction.save()
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        self.url = reverse("lot_by_pk", kwargs={"pk": self.in_person_lot.pk})

    def test_offered_in_the_app_on_an_in_person_lot(self):
        response = self.client.get(self.url, HTTP_USER_AGENT=self.APP_UA)
        self.assertContains(response, "pushPromptOffer")
        self.assertContains(response, "lot_selling_soon")

    def test_not_offered_on_the_web(self):
        self.assertNotContains(self.client.get(self.url), "pushPromptOffer")

    def test_not_offered_for_an_online_auction(self):
        response = self.client.get(reverse("lot_by_pk", kwargs={"pk": self.lot.pk}), HTTP_USER_AGENT=self.APP_UA)
        self.assertNotContains(response, "pushPromptOffer")

    def test_not_offered_once_the_auction_is_over(self):
        self.in_person_auction.date_start = timezone.now() - datetime.timedelta(days=5)
        self.in_person_auction.date_end = timezone.now() - datetime.timedelta(days=4)
        self.in_person_auction.save()
        self.assertTrue(self.in_person_auction.pretty_much_over)
        response = self.client.get(self.url, HTTP_USER_AGENT=self.APP_UA)
        self.assertNotContains(response, "pushPromptOffer")

    @override_settings(FIREBASE_CREDENTIALS_JSON=FAKE_FIREBASE)
    def test_not_offered_to_someone_already_set_up(self):
        MobileDevice.objects.create(
            user=self.user_with_no_lots, device_uuid=uuid.uuid4(), fcm_token="tok", push_enabled=True
        )
        UserData.objects.filter(user=self.user_with_no_lots).update(push_notifications_when_lots_sell=True)
        response = self.client.get(self.url, HTTP_USER_AGENT=self.APP_UA)
        self.assertNotContains(response, "pushPromptOffer")

    def test_the_auctions_own_setting_still_wins(self):
        self.in_person_auction.message_users_when_lots_sell = False
        self.in_person_auction.save()
        response = self.client.get(self.url, HTTP_USER_AGENT=self.APP_UA)
        self.assertNotContains(response, "pushPromptOffer")


class PreferencesPushBridgeTests(TestCase):
    """/notifications/ asks the app about this phone rather than showing a grey checkbox."""

    APP_UA = "FishAuctionsApp/1.0 (Flutter; Android)"

    def setUp(self):
        self.user = User.objects.create_user(username="prefs_bridge", password="x")
        self.client.force_login(self.user)
        self.url = reverse("notification_preferences")

    def test_controls_rendered_in_the_app(self):
        response = self.client.get(self.url, HTTP_USER_AGENT=self.APP_UA)
        self.assertContains(response, "app-push-controls")
        self.assertContains(response, "pushGetState")
        self.assertContains(response, "pushEnable")

    def test_nothing_on_the_web(self):
        response = self.client.get(self.url)
        self.assertNotContains(response, "app-push-controls")
        self.assertNotContains(response, "pushGetState")


# Part L — terms and privacy policy, linked from sign-up


def _ensure_privacy_post():
    """Ensure the privacy post exists; an earlier TransactionTestCase may have truncated the seed row."""
    from auctions.models import PRIVACY_POLICY_SLUG, BlogPost

    BlogPost.objects.get_or_create(
        slug=PRIVACY_POLICY_SLUG,
        defaults={"title": "Privacy", "body_rendered": "<h3>Deleting your account</h3>"},
    )


class MobileConfigLegalUrlsTests(TestCase):
    """The public config carries terms and privacy policy paths for the app's sign-up (Apple requires them)."""

    def setUp(self):
        _ensure_privacy_post()

    def test_config_carries_both(self):
        response = self.client.get(reverse("mobile-config"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["terms_url"], "/tos/")
        self.assertEqual(data["privacy_policy_url"], "/privacy/")

    def test_privacy_omitted_when_the_page_is_missing(self):
        from auctions.models import BlogPost

        BlogPost.objects.filter(slug="privacy").delete()
        data = self.client.get(reverse("mobile-config")).json()
        self.assertNotIn("privacy_policy_url", data)

    def test_no_secrets_leaked_alongside_them(self):
        data = self.client.get(reverse("mobile-config")).json()
        for key in data:
            self.assertNotIn("secret", key.lower())


class PrivacyPolicyPageTests(TestCase):
    def setUp(self):
        _ensure_privacy_post()

    def test_privacy_page_renders_in_place(self):
        response = self.client.get(reverse("privacy_policy"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Deleting your account")

    def test_no_redirect_out_of_the_signup_webview(self):
        # A redirect would leave the app's allow-list.
        self.assertEqual(self.client.get("/privacy/").status_code, 200)

    def test_the_blog_url_still_works(self):
        self.assertEqual(self.client.get("/blog/privacy/").status_code, 200)

    def test_missing_page_is_a_404_not_a_500(self):
        from auctions.models import BlogPost

        BlogPost.objects.filter(slug="privacy").delete()
        self.assertEqual(self.client.get(reverse("privacy_policy")).status_code, 404)


class SignupLegalLinksTests(TestCase):
    """Both links are on the sign-up page itself, so the web form carries them too."""

    def test_signup_links_terms_and_privacy(self):
        response = self.client.get(reverse("account_signup"))
        self.assertContains(response, reverse("tos"))
        self.assertContains(response, reverse("privacy_policy"))


class CookieBannerInTheAppTests(StandardTestCase):
    """The cookie banner doesn't render in the app."""

    APP_UA = "FishAuctionsApp/1.0 (Flutter; iOS)"
    BANNER = "By using this site"

    def _home(self, user_agent="", login=True):
        if login:
            self.client.force_login(self.user)
        return self.client.get(reverse("home"), follow=True, HTTP_USER_AGENT=user_agent)

    def test_shown_on_the_web(self):
        self.assertContains(self._home(), self.BANNER)

    def test_hidden_in_the_app(self):
        response = self._home(self.APP_UA)
        self.assertNotContains(response, self.BANNER)
        self.assertNotContains(response, "agreeTos")  # and the dismiss button's script with it

    def test_hidden_in_the_app_for_a_signed_out_visitor(self):
        # Including the app's WebView login screen.
        self.assertNotContains(self._home(self.APP_UA, login=False), self.BANNER)

    def test_the_web_dismiss_cookie_still_works(self):
        self.client.cookies["hide_tos_banner"] = "true"
        self.assertNotContains(self._home(), self.BANNER)


# Part A — wallet buttons, one per platform


class MobileAppPlatformMiddlewareTests(TestCase):
    def _platform(self, user_agent):
        from auctions.middleware import MobileAppMiddleware

        request = self.client.request().wsgi_request
        request.META["HTTP_USER_AGENT"] = user_agent
        MobileAppMiddleware(lambda r: None)(request)
        return request.mobile_app_platform

    def test_ios(self):
        self.assertEqual(self._platform("FishAuctionsApp/1.0 (Flutter; iOS)"), "ios")

    def test_android(self):
        self.assertEqual(self._platform("FishAuctionsApp/1.0 (Flutter; Android)"), "android")

    def test_blank_for_a_browser(self):
        self.assertEqual(self._platform("Mozilla/5.0 (Linux; Android 13) Chrome/120"), "")


class MembershipCardWalletButtonsTests(TestCase):
    """Offer only the wallet the phone has; the web offers both."""

    @classmethod
    def setUpTestData(cls):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.private_key = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()

    def setUp(self):
        from auctions.models import Club, ClubMember

        self.club = Club.objects.create(name="Wallet club", show_member_barcode=True)
        self.member = ClubMember.objects.create(club=self.club, name="A Member", email="member@example.com")
        self.url = reverse("club_member_by_uuid", kwargs={"slug": self.club.slug, "uuid": self.member.uuid})

    def _get(self, user_agent="", apple=True):
        settings_kwargs = {
            "GOOGLE_WALLET_ISSUER_ID": "1234",
            "GOOGLE_WALLET_SERVICE_ACCOUNT_EMAIL": "wallet@example.com",
            "GOOGLE_WALLET_SERVICE_ACCOUNT_KEY": self.private_key,
        }
        with override_settings(**settings_kwargs), patch("auctions.apple_wallet.is_configured", return_value=apple):
            return self.client.get(self.url, HTTP_USER_AGENT=user_agent)

    def test_web_offers_both(self):
        response = self._get()
        self.assertContains(response, "Add to Google Wallet")
        self.assertContains(response, "Add to Apple Wallet")

    def test_ios_app_hides_google_wallet(self):
        response = self._get("FishAuctionsApp/1.0 (Flutter; iOS)")
        self.assertNotContains(response, "Add to Google Wallet")
        self.assertContains(response, "Add to Apple Wallet")

    def test_android_app_hides_apple_wallet(self):
        response = self._get("FishAuctionsApp/1.0 (Flutter; Android)")
        self.assertContains(response, "Add to Google Wallet")
        self.assertNotContains(response, "Add to Apple Wallet")

    def test_the_apple_explainer_is_web_only(self):
        web = self._get(apple=False)
        self.assertContains(web, "apple-wallet-explainer")
        in_app = self._get("FishAuctionsApp/1.0 (Flutter; iOS)", apple=False)
        self.assertNotContains(in_app, "apple-wallet-explainer")
        self.assertNotContains(in_app, "take a screenshot")


# Part PALETTE — the website's command palette, inside the app


class AppCommandPaletteRenderingTests(StandardTestCase):
    """The palette modal renders in the app, which opens it instead of its native palette."""

    APP_UA = "FishAuctionsApp/1.0 (Flutter; iOS)"

    def _home(self, user_agent=""):
        self.client.force_login(self.user)
        return self.client.get(reverse("home"), follow=True, HTTP_USER_AGENT=user_agent)

    def test_the_palette_is_rendered_in_the_app(self):
        self.assertContains(self._home(self.APP_UA), "command-palette-modal")

    def test_the_palette_is_still_rendered_on_the_web(self):
        self.assertContains(self._home(), "command-palette-modal")

    def test_the_keyboard_footer_stays_hidden_on_a_phone(self):
        self.assertContains(self._home(self.APP_UA), "d-none d-md-flex")


class AppPaletteDeepLinkTests(StandardTestCase):
    """Native lot scanning and Tap to Pay as palette items with the app's URL scheme, app UA only."""

    IOS_UA = "FishAuctionsApp/1.0 (Flutter; iOS)"
    ANDROID_UA = "FishAuctionsApp/1.0 (Flutter; Android)"
    WEB_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

    def setUp(self):
        super().setUp()
        # Make the auction current, so lot scanning is offered.
        self.in_person_auction.date_start = timezone.now() - datetime.timedelta(hours=1)
        self.in_person_auction.date_end = timezone.now() + datetime.timedelta(hours=4)
        self.in_person_auction.save()
        self.user.userdata.last_auction_used = self.in_person_auction
        self.user.userdata.save()
        self.ar_link = f"fishauctions://ar/{self.in_person_auction.slug}"
        self.tap_to_pay_link = "fishauctions://tap-to-pay"

    def _palette(self, query="", user_agent=None, user=None):
        self.client.force_login(user or self.user)
        return self.client.get(
            reverse("command_palette"),
            {"q": query} if query else {},
            HTTP_USER_AGENT=self.IOS_UA if user_agent is None else user_agent,
        )

    def _urls(self, response):
        return [item["url"] for group in response.json()["groups"] for item in group["items"]]

    def _titles(self, response):
        return [item["title"] for group in response.json()["groups"] for item in group["items"]]

    def test_lot_scanning_is_offered_for_the_auction_the_user_is_at(self):
        self.assertIn(self.ar_link, self._urls(self._palette()))

    def test_lot_scanning_is_not_offered_on_the_web(self):
        self.assertNotIn(self.ar_link, self._urls(self._palette(user_agent=self.WEB_UA)))

    def test_lot_scanning_is_not_offered_for_an_online_auction(self):
        self.user.userdata.last_auction_used = self.online_auction
        self.user.userdata.save()
        self.assertFalse([url for url in self._urls(self._palette()) if url.startswith("fishauctions://ar/")])

    def test_lot_scanning_stops_once_the_room_is_packed_up(self):
        self.in_person_auction.date_start = timezone.now() - datetime.timedelta(days=4)
        self.in_person_auction.date_end = timezone.now() - datetime.timedelta(days=3)
        self.in_person_auction.save()
        self.assertTrue(self.in_person_auction.pretty_much_over)
        self.assertNotIn(self.ar_link, self._urls(self._palette()))

    def test_a_query_about_scanning_finds_it(self):
        for query in ("scan", "scanning", "augmented reality", "find lot"):
            self.assertIn(self.ar_link, self._urls(self._palette(query)), query)

    def test_an_unrelated_query_does_not_drag_scanning_in(self):
        for query in ("car", "starting price", "search my lots", "find my lots"):
            self.assertNotIn(self.ar_link, self._urls(self._palette(query)), query)

    def test_tap_to_pay_is_offered_to_someone_who_can_take_a_payment(self):
        self.assertIn(self.tap_to_pay_link, self._urls(self._palette()))

    def test_tap_to_pay_is_not_offered_to_a_buyer(self):
        self.assertNotIn(self.tap_to_pay_link, self._urls(self._palette(user=self.userB)))

    def test_tap_to_pay_is_not_offered_on_the_web(self):
        self.assertNotIn(self.tap_to_pay_link, self._urls(self._palette(user_agent=self.WEB_UA)))

    def test_the_label_is_the_one_apples_review_guide_allows(self):
        """ "Tap to Pay on iPhone" with no auction suffix (Apple 5.4)."""
        titles = self._titles(self._palette())
        self.assertIn("Tap to Pay on iPhone", titles)
        self.assertFalse([title for title in titles if title.startswith("Tap to Pay on iPhone —")], titles)

    def test_tap_to_pay_is_not_offered_on_android(self):
        """Tap to Pay is not offered on Android."""
        for query in ("", "tap", "card", "payment"):
            urls = self._urls(self._palette(query, self.ANDROID_UA))
            self.assertNotIn(self.tap_to_pay_link, urls, query)
        self.assertIn(self.ar_link, self._urls(self._palette(user_agent=self.ANDROID_UA)))

    def test_the_row_carries_no_payment_iconography(self):
        """No payment icon on the row (Apple 5.5)."""
        items = [
            item
            for group in self._palette().json()["groups"]
            for item in group["items"]
            if item["url"] == self.tap_to_pay_link
        ]
        self.assertEqual([item["icon"] for item in items], ["bi-arrow-right-short"])

    def test_a_query_about_payments_finds_it(self):
        for query in ("tap", "card", "payment"):
            self.assertIn(self.tap_to_pay_link, self._urls(self._palette(query)), query)

    def test_the_native_palette_is_not_sent_rows_it_injects_itself(self):
        """The native palette isn't sent rows it adds itself."""
        response = self.client.get(reverse("mobile-command-palette"), HTTP_USER_AGENT=self.IOS_UA, **_bearer(self.user))
        self.assertEqual(response.status_code, 200)
        items = [item for group in response.json()["groups"] for item in group["items"]]
        self.assertFalse([item["url"] for item in items if item["url"].startswith("fishauctions://")], items)
        # …but the rest of the palette is untouched.
        self.assertTrue(any("View lots" in item["title"] for item in items), items)


class AppPaletteNavigationTests(StandardTestCase):
    IOS_UA = "FishAuctionsApp/1.0 (Flutter; iOS)"
    ANDROID_UA = "FishAuctionsApp/1.0 (Flutter; Android)"

    def setUp(self):
        super().setUp()
        self.in_person_auction.date_start = timezone.now() - datetime.timedelta(hours=1)
        self.in_person_auction.date_end = timezone.now() + datetime.timedelta(hours=4)
        self.in_person_auction.save()
        self.user.userdata.last_auction_used = self.in_person_auction
        self.user.userdata.save()

    def _go(self, page, user_agent=None, user=None):
        from auctions import palette_actions

        request = self.client.request(HTTP_USER_AGENT=user_agent or "").wsgi_request
        request.user = user or self.user
        request.palette_page = {}
        return palette_actions.run_action(request, "go_to_page", {"page": page})

    def test_tap_to_pay_opens_the_card_reader_rather_than_the_payout_settings(self):
        self.assertEqual(self._go("tap to pay", self.IOS_UA)["url"], "fishauctions://tap-to-pay")

    def test_lot_scanning_opens_the_camera(self):
        self.assertEqual(
            self._go("lot scanning", self.IOS_UA)["url"], f"fishauctions://ar/{self.in_person_auction.slug}"
        )

    def test_on_the_web_the_same_question_still_finds_a_real_page(self):
        result = self._go("tap to pay")
        self.assertTrue(result["url"].startswith("/"), result)

    def test_a_page_the_catalog_owns_is_not_hijacked_in_the_app(self):
        self.assertEqual(self._go("my_invoices", self.IOS_UA)["url"], reverse("my_invoices"))

    def _request(self, user_agent=""):
        request = self.client.request(HTTP_USER_AGENT=user_agent).wsgi_request
        request.user = self.user
        request.palette_page = {}
        return request

    def test_the_assistant_is_told_about_the_native_screens(self):
        """The assistant is told about the native screens."""
        from auctions import command_palette, palette_assist

        destinations = command_palette.app_destinations_for_prompt(self._request(self.IOS_UA))
        self.assertEqual([name for name, _ in destinations], ["lot scanning", "tap to pay"])
        prompt = palette_assist.build_system_prompt(self.user, {}, destinations)
        self.assertIn("lot scanning", prompt)
        self.assertIn("tap to pay", prompt)

    def test_android_hears_about_lot_scanning_but_never_about_tap_to_pay(self):
        """Android hears about lot scanning but never Tap to Pay."""
        from auctions import command_palette

        request = self._request(self.ANDROID_UA)
        destinations = command_palette.app_destinations_for_prompt(request)
        self.assertEqual([name for name, _ in destinations], ["lot scanning"])
        self.assertIsNone(command_palette.app_deep_link_by_name(request, "tap to pay"))
        # Still answered with the nearest real page.
        self.assertTrue(self._go("tap to pay", self.ANDROID_UA)["url"].startswith("/"))

    def test_the_web_prompt_says_nothing_about_screens_the_browser_cannot_open(self):
        from auctions import command_palette, palette_assist

        self.assertEqual(command_palette.app_destinations_for_prompt(self._request()), [])
        self.assertNotIn("native screens", palette_assist.build_system_prompt(self.user, {}))

    def test_every_name_the_prompt_offers_is_one_the_navigation_skill_accepts(self):
        from auctions import command_palette

        request = self._request(self.IOS_UA)
        offered = command_palette.app_destinations_for_prompt(request)
        self.assertTrue(offered)
        for name, _ in offered:
            self.assertIsNotNone(command_palette.app_deep_link_by_name(request, name), name)
