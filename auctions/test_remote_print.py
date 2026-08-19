"""Part R — printing from a computer to the phone's Bluetooth label printer.

The whole feature is built around one constraint, and most of what is worth testing follows from it:
**the phone cannot be summoned.** Android forbids starting an Activity from the background and this
app's BLE connection lives in a UI-scoped provider on the shell; iOS silent pushes are best-effort
and dropped once the app is force-quit. So the server measures whether the app is already open
(a heartbeat), only offers the feature when it is, and tells the user the truth when it isn't —
rather than pushing hopefully and timing out.

That makes the interesting cases the negative ones: the phone that stopped heartbeating, the device
with no push token, the job that was pushed and never answered, and the escape hatch that has to work
from inside any of them.
"""

import datetime
import uuid
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework_simplejwt.tokens import RefreshToken

from auctions.mobile.services import remote_print
from auctions.models import Lot, MobileDevice, RemotePrintJob, UserLabelPrefs
from auctions.notifications import SEND_ERROR, SEND_OK
from auctions.tests import StandardTestCase

APP_UA = "FishAuctionsApp/1.0 (Flutter; iOS)"
WEB_UA = "Mozilla/5.0"


def _bearer(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


class RemotePrintBase(StandardTestCase):
    def setUp(self):
        super().setUp()
        self.in_person_auction.date_end = timezone.now() - datetime.timedelta(days=1)
        self.in_person_auction.save()
        self.lots = [
            Lot.objects.create(
                lot_name=f"remote label {i}",
                auction=self.in_person_auction,
                auctiontos_seller=self.in_person_tos,
                quantity=1,
            )
            for i in range(3)
        ]
        self.url = reverse("print_my_labels", kwargs={"slug": self.in_person_auction.slug})
        self.prefs, _ = UserLabelPrefs.objects.get_or_create(user=self.user)
        self.prefs.print_from_computer = True
        self.prefs.save()
        self.device = MobileDevice.objects.create(
            user=self.user,
            device_uuid=uuid.uuid4(),
            fcm_token="tok-phone",
            print_ready=True,
            ever_print_ready=True,
            printer_name="Y486BT",
            last_heartbeat=timezone.now(),
        )

    def _get(self, user_agent=WEB_UA, url=None):
        self.client.force_login(self.user)
        return self.client.get(url or self.url, HTTP_USER_AGENT=user_agent)


# ---------------------------------------------------------------------------
# R1 — presence
# ---------------------------------------------------------------------------


class HeartbeatTests(RemotePrintBase):
    def _beat(self, user=None, **body):
        payload = {"device_uuid": str(self.device.device_uuid)}
        payload.update(body)
        return self.client.post(
            reverse("mobile-device-heartbeat"),
            payload,
            content_type="application/json",
            **_bearer(user or self.user),
        )

    def test_heartbeat_returns_204_and_records_the_state(self):
        self.device.print_ready = False
        self.device.printer_name = ""
        self.device.save()
        response = self._beat(print_ready=True, printer_name="Y486BT", print_method="bluetooth")
        self.assertEqual(response.status_code, 204)
        self.device.refresh_from_db()
        self.assertTrue(self.device.print_ready)
        self.assertEqual(self.device.printer_name, "Y486BT")
        self.assertIsNotNone(self.device.last_heartbeat)

    def test_unregistered_device_404s(self):
        """The app self-disables the whole feature on a 404, so an old deployment costs it nothing."""
        response = self.client.post(
            reverse("mobile-device-heartbeat"),
            {"device_uuid": str(uuid.uuid4())},
            content_type="application/json",
            **_bearer(self.user),
        )
        self.assertEqual(response.status_code, 404)

    def test_cannot_beat_for_someone_elses_device(self):
        response = self._beat(user=self.user_with_no_lots, print_ready=True)
        self.assertEqual(response.status_code, 404)

    def test_ever_print_ready_is_sticky(self):
        # It answers "does this account have a phone that could do this at all", which is what decides
        # whether /printing/ offers the checkbox -- not a question that changes when the printer is
        # switched off for the morning.
        self._beat(print_ready=True, printer_name="Y486BT")
        self._beat(print_ready=False, printer_name="")
        self.device.refresh_from_db()
        self.assertFalse(self.device.print_ready)
        self.assertTrue(self.device.ever_print_ready)

    def test_requires_authentication(self):
        response = self.client.post(
            reverse("mobile-device-heartbeat"),
            {"device_uuid": str(self.device.device_uuid)},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)


class ReachabilityTests(RemotePrintBase):
    def test_fresh_heartbeat_with_a_printer_is_reachable(self):
        self.assertTrue(self.device.is_reachable_for_printing)
        self.assertIn(self.device, MobileDevice.reachable_printers_for(self.user))

    def test_one_missed_beat_is_still_reachable(self):
        # The app beats every 5 minutes; the window is 6, so a single dropped beat is slack rather
        # than a failure.
        self.device.last_heartbeat = timezone.now() - datetime.timedelta(minutes=5, seconds=30)
        self.device.save()
        self.assertTrue(self.device.is_reachable_for_printing)

    def test_a_stale_heartbeat_is_not_reachable(self):
        self.device.last_heartbeat = timezone.now() - datetime.timedelta(minutes=7)
        self.device.save()
        self.assertFalse(self.device.is_reachable_for_printing)

    def test_print_ready_false_is_not_reachable(self):
        # A phone can be wide awake with nothing paired to it. print_ready is the app's own answer to
        # "is a printer paired and does its profile resolve", not something derived from print_method.
        self.device.print_ready = False
        self.device.save()
        self.assertFalse(self.device.is_reachable_for_printing)

    def test_never_heartbeated_is_not_reachable(self):
        self.device.last_heartbeat = None
        self.device.save()
        self.assertFalse(self.device.is_reachable_for_printing)

    def test_presence_falls_back_to_the_last_phone_heard_from(self):
        """When nothing is reachable, "how long ago" is the useful answer, not "no device"."""
        self.device.last_heartbeat = timezone.now() - datetime.timedelta(hours=3)
        self.device.save()
        device, last_seen = MobileDevice.print_presence_for(self.user)
        self.assertEqual(device, self.device)
        self.assertIsNotNone(last_seen)


# ---------------------------------------------------------------------------
# R2 — the preference
# ---------------------------------------------------------------------------


@override_settings(FIREBASE_CREDENTIALS_JSON='{"type": "service_account"}')
class PrintFromComputerCheckboxTests(RemotePrintBase):
    def test_checkbox_is_offered_when_a_phone_has_ever_been_print_ready(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("printing"), HTTP_USER_AGENT=WEB_UA)
        self.assertContains(response, "print_from_computer")

    def test_checkbox_is_hidden_without_a_phone_that_could_print(self):
        """A switch with nothing behind it is worse than no switch."""
        MobileDevice.objects.filter(user=self.user).update(ever_print_ready=False)
        self.client.force_login(self.user)
        response = self.client.get(reverse("printing"), HTTP_USER_AGENT=WEB_UA)
        self.assertNotContains(response, "print_from_computer")

    def test_hidden_checkbox_does_not_clear_the_stored_value(self):
        # The field is dropped from the form when hidden, so a save from a browser that never saw it
        # leaves what the phone set alone.
        MobileDevice.objects.filter(user=self.user).update(ever_print_ready=False)
        self.client.force_login(self.user)
        self.client.post(reverse("printing") + "?next=/", {"preset": "lg", "unit": "in"})
        self.prefs.refresh_from_db()
        self.assertTrue(self.prefs.print_from_computer)

    def test_page_says_when_the_phone_was_last_seen(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("printing"), HTTP_USER_AGENT=WEB_UA)
        self.assertContains(response, "Your phone was last seen")

    def test_page_says_so_when_the_phone_is_missing(self):
        self.device.last_heartbeat = timezone.now() - datetime.timedelta(hours=3)
        self.device.save()
        self.client.force_login(self.user)
        response = self.client.get(reverse("printing"), HTTP_USER_AGENT=WEB_UA)
        self.assertContains(response, "open the app on it")

    @override_settings(FIREBASE_CREDENTIALS_JSON="")
    def test_checkbox_is_hidden_when_the_deployment_has_no_push(self):
        """The job reaches the phone as an FCM data message and by no other route.

        Without credentials every job would go straight to "couldn't reach your phone", which blames
        the user's phone for the server's missing config.
        """
        self.client.force_login(self.user)
        response = self.client.get(reverse("printing"), HTTP_USER_AGENT=WEB_UA)
        self.assertNotContains(response, "print_from_computer")


# ---------------------------------------------------------------------------
# R3/R4 — where the job is created, and what gets pushed
# ---------------------------------------------------------------------------


class LabelViewBranchTests(RemotePrintBase):
    def test_web_with_a_reachable_phone_gets_the_waiting_page(self):
        with patch("auctions.mobile.services.remote_print.send_fcm_data_message", return_value=SEND_OK):
            response = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "label_remote_print.html")
        job = RemotePrintJob.objects.get(user=self.user)
        self.assertEqual(job.lots, [lot.pk for lot in self.lots])
        self.assertEqual(job.total_count, 3)

    def test_lot_order_is_the_order_the_pdf_would_have_printed_them(self):
        with patch("auctions.mobile.services.remote_print.send_fcm_data_message", return_value=SEND_OK):
            self._get()
        job = RemotePrintJob.objects.get(user=self.user)
        self.assertEqual(job.lots, list(self.in_person_tos.print_labels_qs.values_list("pk", flat=True)))

    def test_preference_off_gets_the_pdf(self):
        self.prefs.print_from_computer = False
        self.prefs.save()
        self.assertEqual(self._get()["Content-Type"], "application/pdf")
        self.assertFalse(RemotePrintJob.objects.exists())

    def test_no_reachable_phone_gets_the_pdf(self):
        """The preference alone is a promise the phone may not be able to keep."""
        self.device.last_heartbeat = timezone.now() - datetime.timedelta(hours=1)
        self.device.save()
        self.assertEqual(self._get()["Content-Type"], "application/pdf")
        self.assertFalse(RemotePrintJob.objects.exists())

    def test_the_app_arm_wins_over_the_job_arm(self):
        """Printing *from* the phone prints directly, rather than routing a job back to itself."""
        self.prefs.print_method = "bluetooth"
        self.prefs.save()
        html = self._get(APP_UA).content.decode()
        self.assertIn("fishauctions://print/?lots=", html)
        self.assertFalse(RemotePrintJob.objects.exists())

    def test_pdf_param_skips_both_branches(self):
        # The escape hatch on the waiting page. The app arm has to respect it too, or "Print a PDF
        # here" would bounce a phone straight back into the deep link it was trying to get out of.
        self.assertEqual(self._get(url=self.url + "?pdf=1")["Content-Type"], "application/pdf")
        self.prefs.print_method = "bluetooth"
        self.prefs.save()
        self.assertEqual(self._get(APP_UA, url=self.url + "?pdf=1")["Content-Type"], "application/pdf")
        self.assertFalse(RemotePrintJob.objects.exists())

    def test_the_waiting_page_does_not_mark_anything_printed(self):
        """Nothing has printed yet; the job's result post is what marks them."""
        with patch("auctions.mobile.services.remote_print.send_fcm_data_message", return_value=SEND_OK):
            self._get()
        self.assertEqual(self.in_person_tos.unprinted_label_count, len(self.lots))


class DispatchTests(RemotePrintBase):
    def test_push_is_data_only_and_carries_the_lots_as_a_string(self):
        with patch("auctions.mobile.services.remote_print.send_fcm_data_message", return_value=SEND_OK) as send:
            job = remote_print.start(self.user, [lot.pk for lot in self.lots])
        token, data = send.call_args[0]
        self.assertEqual(token, "tok-phone")
        self.assertEqual(data["type"], "print_labels")
        self.assertEqual(data["job"], str(job.uuid))
        self.assertEqual(data["lots"], ",".join(str(lot.pk) for lot in self.lots))
        job.refresh_from_db()
        self.assertEqual(job.status, RemotePrintJob.STATUS_SENT)

    def test_missing_token_is_unreachable_at_once(self):
        """A failure already known must not become twenty seconds of spinner."""
        self.device.fcm_token = ""
        self.device.save()
        job = remote_print.start(self.user, [self.lots[0].pk])
        self.assertEqual(job.status, RemotePrintJob.STATUS_UNREACHABLE)

    def test_fcm_error_is_unreachable_at_once(self):
        with patch("auctions.mobile.services.remote_print.send_fcm_data_message", return_value=SEND_ERROR):
            job = remote_print.start(self.user, [self.lots[0].pk])
        self.assertEqual(job.status, RemotePrintJob.STATUS_UNREACHABLE)


# ---------------------------------------------------------------------------
# R5 — the waiting page's endpoints
# ---------------------------------------------------------------------------


class JobStatusViewTests(RemotePrintBase):
    def setUp(self):
        super().setUp()
        self.job = remote_print.create_job(self.user, [lot.pk for lot in self.lots])
        self.status_url = reverse("remote_print_job", kwargs={"job_uuid": self.job.uuid})

    def test_status_is_json(self):
        self.client.force_login(self.user)
        payload = self.client.get(self.status_url).json()
        self.assertEqual(payload, {"status": "queued", "printed": 0, "total": 3, "message": None})

    def test_another_user_cannot_watch_the_job(self):
        self.client.force_login(self.user_with_no_lots)
        self.assertEqual(self.client.get(self.status_url).status_code, 404)

    def test_signed_out_is_redirected_to_login(self):
        self.assertEqual(self.client.get(self.status_url).status_code, 302)

    def test_twenty_seconds_of_silence_after_sent_is_unreachable(self):
        # Applied on the server rather than in the page's JS so two tabs watching one job agree, and
        # so "unreachable" is a fact on the row rather than something one browser decided.
        RemotePrintJob.objects.filter(pk=self.job.pk).update(
            status=RemotePrintJob.STATUS_SENT,
            updated_at=timezone.now() - datetime.timedelta(seconds=25),
        )
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.status_url).json()["status"], "unreachable")

    def test_a_queued_job_is_never_called_unreachable(self):
        """It hasn't been pushed yet; there is nothing for the phone to have failed to answer."""
        RemotePrintJob.objects.filter(pk=self.job.pk).update(updated_at=timezone.now() - datetime.timedelta(minutes=5))
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.status_url).json()["status"], "queued")

    def test_a_job_that_has_reported_is_not_called_unreachable(self):
        RemotePrintJob.objects.filter(pk=self.job.pk).update(
            status=RemotePrintJob.STATUS_PRINTING,
            updated_at=timezone.now() - datetime.timedelta(minutes=5),
        )
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.status_url).json()["status"], "printing")


class JobRetryAndCancelTests(RemotePrintBase):
    def setUp(self):
        super().setUp()
        self.job = remote_print.create_job(self.user, [lot.pk for lot in self.lots])
        self.job.status = RemotePrintJob.STATUS_UNREACHABLE
        self.job.save()

    def test_retry_makes_a_new_job_with_the_same_lots(self):
        self.client.force_login(self.user)
        with patch("auctions.mobile.services.remote_print.send_fcm_data_message", return_value=SEND_OK):
            payload = self.client.post(reverse("remote_print_job_retry", kwargs={"job_uuid": self.job.uuid})).json()
        new_job = RemotePrintJob.objects.get(uuid=payload["job"])
        self.assertNotEqual(new_job.pk, self.job.pk)
        self.assertEqual(new_job.lots, self.job.lots)
        self.assertEqual(new_job.status, RemotePrintJob.STATUS_SENT)

    def test_retry_keeps_the_lots_even_if_one_has_since_gone(self):
        # Re-deriving the queryset would silently shorten the batch, and the person is standing at
        # the printer expecting the labels they asked for.
        Lot.objects.filter(pk=self.lots[0].pk).update(is_deleted=True)
        self.client.force_login(self.user)
        with patch("auctions.mobile.services.remote_print.send_fcm_data_message", return_value=SEND_OK):
            payload = self.client.post(reverse("remote_print_job_retry", kwargs={"job_uuid": self.job.uuid})).json()
        self.assertEqual(RemotePrintJob.objects.get(uuid=payload["job"]).lots, self.job.lots)

    def test_another_user_cannot_retry(self):
        self.client.force_login(self.user_with_no_lots)
        response = self.client.post(reverse("remote_print_job_retry", kwargs={"job_uuid": self.job.uuid}))
        self.assertEqual(response.status_code, 404)

    def test_cancel_marks_the_job_cancelled(self):
        job = remote_print.create_job(self.user, [self.lots[0].pk])
        self.client.force_login(self.user)
        response = self.client.post(reverse("remote_print_job_cancel", kwargs={"job_uuid": job.uuid}))
        self.assertEqual(response.json()["status"], "cancelled")
        job.refresh_from_db()
        self.assertEqual(job.status, RemotePrintJob.STATUS_CANCELLED)

    def test_cancel_does_not_rewrite_a_finished_job(self):
        job = remote_print.create_job(self.user, [self.lots[0].pk])
        job.status = RemotePrintJob.STATUS_PRINTED
        job.save()
        self.client.force_login(self.user)
        self.client.post(reverse("remote_print_job_cancel", kwargs={"job_uuid": job.uuid}))
        job.refresh_from_db()
        self.assertEqual(job.status, RemotePrintJob.STATUS_PRINTED)


# ---------------------------------------------------------------------------
# R6 — the app reporting back
# ---------------------------------------------------------------------------


class JobReportingTests(RemotePrintBase):
    def setUp(self):
        super().setUp()
        self.job = remote_print.create_job(self.user, [lot.pk for lot in self.lots])
        self.job.status = RemotePrintJob.STATUS_SENT
        self.job.save()

    def _progress(self, user=None, **body):
        return self.client.post(
            reverse("mobile-printjob-progress", kwargs={"job_uuid": self.job.uuid}),
            body,
            content_type="application/json",
            **_bearer(user or self.user),
        )

    def _result(self, user=None, **body):
        return self.client.post(
            reverse("mobile-printjob-result", kwargs={"job_uuid": self.job.uuid}),
            body,
            content_type="application/json",
            **_bearer(user or self.user),
        )

    def test_progress_updates_the_counts(self):
        self.assertEqual(self._progress(status="printing", printed=2, total=3).status_code, 204)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, RemotePrintJob.STATUS_PRINTING)
        self.assertEqual(self.job.printed_count, 2)

    def test_progress_never_counts_backwards(self):
        """These arrive out of order: the app drops and retries them. 7 of 12 must not become 5."""
        self._progress(status="printing", printed=2, total=3)
        self._progress(status="printing", printed=1, total=3)
        self.job.refresh_from_db()
        self.assertEqual(self.job.printed_count, 2)

    def test_progress_from_another_user_is_404(self):
        self.assertEqual(self._progress(user=self.user_with_no_lots, printed=1).status_code, 404)

    def test_result_records_the_apps_own_words_verbatim(self):
        message = "Couldn't connect to the printer. Move closer and try again."
        self.assertEqual(self._result(status="failed", printed=1, total=3, message=message).status_code, 204)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, RemotePrintJob.STATUS_FAILED)
        self.assertEqual(self.job.message, message)

    def test_result_marks_the_labels_that_printed(self):
        """So the app doesn't have to post to labels/printed/ as well."""
        self._result(status="printed", printed=3, total=3)
        for lot in self.lots:
            lot.refresh_from_db()
            self.assertTrue(lot.label_printed)

    def test_a_partial_batch_marks_only_what_came_out(self):
        self._result(status="failed", printed=2, total=3, message="Lost the connection.")
        printed = [Lot.objects.get(pk=lot.pk).label_printed for lot in self.lots]
        self.assertEqual(printed, [True, True, False])

    def test_result_from_another_user_is_404(self):
        self.assertEqual(self._result(user=self.user_with_no_lots, status="printed", printed=3).status_code, 404)

    def test_a_late_result_beats_the_pages_unreachable_guess(self):
        """The phone demonstrably was reachable; the truth is worth more than the earlier guess."""
        self.job.status = RemotePrintJob.STATUS_UNREACHABLE
        self.job.save()
        self._result(status="printed", printed=3, total=3)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, RemotePrintJob.STATUS_PRINTED)

    def test_a_cancelled_job_keeps_the_answer_the_person_chose(self):
        self.job.status = RemotePrintJob.STATUS_CANCELLED
        self.job.save()
        self._result(status="printed", printed=3, total=3)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, RemotePrintJob.STATUS_CANCELLED)

    def test_reporting_requires_authentication(self):
        response = self.client.post(
            reverse("mobile-printjob-result", kwargs={"job_uuid": self.job.uuid}),
            {"status": "printed"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)


class JobIsolationTests(RemotePrintBase):
    """A job uuid is unguessable; a 403 would only confirm that somebody else's exists."""

    def test_status_result_and_progress_all_hide_other_peoples_jobs(self):
        other = RemotePrintJob.objects.create(user=self.user_with_no_lots, lots=[], total_count=0)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("remote_print_job", kwargs={"job_uuid": other.uuid})).status_code, 404)
        self.assertEqual(
            self.client.post(
                reverse("mobile-printjob-progress", kwargs={"job_uuid": other.uuid}),
                {"printed": 1},
                content_type="application/json",
                **_bearer(self.user),
            ).status_code,
            404,
        )


class UnknownJobTests(TestCase):
    def test_a_uuid_that_never_existed_is_404(self):
        from django.contrib.auth.models import User

        user = User.objects.create_user(username="nobodys_job", password="x")
        self.client.force_login(user)
        response = self.client.get(reverse("remote_print_job", kwargs={"job_uuid": uuid.uuid4()}))
        self.assertEqual(response.status_code, 404)
