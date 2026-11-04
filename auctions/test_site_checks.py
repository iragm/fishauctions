"""The checks for the site quietly not doing its job (:mod:`auctions.site_checks`), and the log context
(:mod:`auctions.log_context`) that tells one request's lines from another's."""

import datetime
import logging
from unittest import mock

from django.contrib.auth.models import AnonymousUser, User
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase
from django.utils import timezone
from django_celery_beat.models import IntervalSchedule, PeriodicTask
from post_office.models import STATUS, Email

from auctions import log_context, site_checks
from auctions.models import Lot
from auctions.test_support import isolated_cache
from auctions.tests import StandardTestCase


@isolated_cache("site-checks")
class PeriodicTaskTests(StandardTestCase):
    def setUp(self):
        super().setUp()
        PeriodicTask.objects.all().delete()
        self.every_minute = IntervalSchedule.objects.create(every=1, period=IntervalSchedule.MINUTES)

    def task(self, name, last_run_minutes_ago, task="auctions.tasks.example"):
        return PeriodicTask.objects.create(
            name=name,
            task=task,
            interval=self.every_minute,
            last_run_at=timezone.now() - datetime.timedelta(minutes=last_run_minutes_ago),
        )

    def test_a_task_late_on_its_own_schedule_is_overdue_while_another_runs(self):
        """beat's newest last_run would say all is well: the one that runs every minute hides the dead one."""
        self.task("fresh", 0)
        self.task("dead", 120)
        found = site_checks.periodic_tasks()
        self.assertEqual(found["checked"], 2)
        self.assertEqual([row["task"] for row in found["overdue"]], ["dead"])
        self.assertGreaterEqual(found["overdue"][0]["late_minutes"], 100)

    def test_a_disabled_or_future_task_is_not_judged(self):
        self.task("off", 500).__class__.objects.filter(name="off").update(enabled=False)
        later = self.task("later", 500)
        later.start_time = timezone.now() + datetime.timedelta(days=1)
        later.save()
        self.assertEqual(site_checks.periodic_tasks()["overdue"], [])

    def test_a_task_whose_last_run_failed_is_failing_until_it_succeeds(self):
        self.task("flaky", 0, task="auctions.tasks.flaky")
        sender = mock.Mock()
        sender.name = "auctions.tasks.flaky"
        site_checks._task_failed(sender=sender, exception=ValueError("x"))
        failing = site_checks.periodic_tasks()["failing"]
        self.assertEqual(failing[0]["task"], "flaky")
        self.assertEqual(failing[0]["error"], "ValueError")
        with mock.patch(
            "auctions.site_checks.timezone.now", return_value=timezone.now() + datetime.timedelta(seconds=1)
        ):
            site_checks._task_succeeded(sender=sender)
        self.assertEqual(site_checks.periodic_tasks()["failing"], [])

    def test_site_health_names_them(self):
        self.task("dead", 120)
        text = self.client_health()
        self.assertIn("Overdue periodic tasks: dead.", text)

    def client_health(self):
        from auctions.mcp import admin

        return admin.site_health(None, {})["summary"]


class RateTests(StandardTestCase):
    def test_counts_compare_the_last_day_with_the_same_day_weeks_back(self):
        now = timezone.now()
        User.objects.filter(pk=self.user.pk).update(date_joined=now - datetime.timedelta(hours=1))
        for week in range(1, 5):
            for number in range(3):
                user = User.objects.create(username=f"week{week}_{number}")
                User.objects.filter(pk=user.pk).update(date_joined=now - datetime.timedelta(weeks=week, hours=2))
        current, usual = site_checks._counts(User.objects.all(), "date_joined", now)
        self.assertGreaterEqual(current, 1)
        self.assertEqual(usual, 3)

    def test_a_steady_count_falling_away_is_unusual(self):
        with mock.patch("auctions.site_checks._counts", return_value=(3, 200.0)):
            found = site_checks.rates()
        self.assertIn("page views 3 in 24 hours, usually 200", found["unusual"])
        # Bids and lots follow the auction calendar, so they're reported but never judged.
        self.assertNotIn("bids 3 in 24 hours, usually 200", found["unusual"])
        self.assertEqual(found["bids"], {"last_24h": 3, "usual": 200.0})

    def test_a_small_baseline_is_not_judged(self):
        with mock.patch("auctions.site_checks._counts", return_value=(0, 5.0)):
            self.assertEqual(site_checks.rates()["unusual"], [])

    def test_a_flood_is_unusual(self):
        with mock.patch("auctions.site_checks._counts", return_value=(900, 30.0)):
            self.assertIn("signups 900 in 24 hours, usually 30", site_checks.rates()["unusual"])


class InvariantTests(StandardTestCase):
    def test_an_online_lot_past_its_end_still_open_is_stuck(self):
        before = site_checks.invariants(timezone.now())["online_lots_not_closed"]
        Lot.objects.create(
            lot_name="Never closed",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            active=True,
            date_end=timezone.now() - datetime.timedelta(hours=2),
        )
        found = site_checks.invariants(timezone.now())
        self.assertEqual(found["online_lots_not_closed"], before + 1)
        self.assertIn(f"online lots not closed: {before + 1}", found["problems"])

    def test_failed_and_stuck_mail_is_counted(self):
        since = timezone.now() - datetime.timedelta(days=1)
        Email.objects.create(to=["a@example.com"], from_email="b@example.com", subject="x", status=STATUS.failed)
        stuck = Email.objects.create(
            to=["a@example.com"], from_email="b@example.com", subject="y", status=STATUS.queued
        )
        Email.objects.filter(pk=stuck.pk).update(created=timezone.now() - datetime.timedelta(hours=3))
        Email.objects.create(to=["a@example.com"], from_email="b@example.com", subject="z", status=STATUS.queued)
        found = site_checks.invariants(since)
        self.assertEqual(found["emails_failed"], 1)
        self.assertEqual(found["emails_stuck_in_queue"], 1)


class LogContextTests(SimpleTestCase):
    def record(self):
        record = logging.LogRecord("auctions.x", logging.WARNING, "x.py", 1, "hello", None, None)
        log_context.ContextFilter().filter(record)
        return record

    def test_nothing_outside_a_request_or_task(self):
        self.assertEqual(self.record().context, "")

    def test_a_request_gets_an_id_then_the_users_pk(self):
        seen = {}

        def view(request):
            seen["before"] = self.record().context
            request.user = mock.Mock(is_authenticated=True, pk=42)
            log_context.set_user(request)
            seen["after"] = self.record().context
            return HttpResponse()

        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        log_context.RequestContextMiddleware(view)(request)
        self.assertRegex(seen["before"], r"^\[r=[0-9a-f]{8}\] $")
        self.assertEqual(seen["after"], f"[r={request.log_request_id} u=42] ")
        # And it is gone once the request is over.
        self.assertEqual(self.record().context, "")

    def test_an_anonymous_visitor_has_no_user(self):
        def view(request):
            log_context.set_user(request)
            return HttpResponse(self.record().context)

        request = RequestFactory().get("/")
        request.user = AnonymousUser()
        response = log_context.RequestContextMiddleware(view)(request)
        self.assertNotIn("u=", response.content.decode())

    def test_a_task_names_itself(self):
        sender = mock.Mock()
        sender.name = "auctions.tasks.endauctions"
        log_context._task_started(sender=sender)
        try:
            self.assertEqual(self.record().context, "[t=endauctions] ")
        finally:
            log_context._task_finished()
        self.assertEqual(self.record().context, "")

    def test_the_file_format_carries_it(self):
        from django.conf import settings

        handler = settings.LOGGING["handlers"]["root_file"]
        self.assertIn("context", handler["filters"])
        self.assertIn("{context}{message}", settings.LOGGING["formatters"]["verbose"]["format"])
