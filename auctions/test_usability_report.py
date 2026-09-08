"""Tests for the usability dashboard's three panels, and for the URL classifier behind the first.

The classifier is the part with teeth: ``PageView.url`` holds a path, the reach question is about a
*route*, and the previous attempt at that mapping shipped without a test and was wrong. This one
asks Django's resolver, so the test's job is to prove that it does -- including on the shapes that
break a hand-written pattern list.
"""

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from auctions import usability_report
from auctions.models import FormFailure, PageView
from auctions.tests import StandardTestCase
from auctions.usability_report import UNROUTED, friction_by_form, reach_by_route, route_name


class RouteNameTests(TestCase):
    def setUp(self):
        route_name.cache_clear()

    def test_a_singleton_page_resolves_to_its_url_name(self):
        self.assertEqual(route_name("/lots/"), "allLots")

    def test_a_path_with_a_slug_in_it_folds_onto_one_route(self):
        """The whole point: a hundred auctions must not be a hundred rows."""
        first = route_name("/auctions/springfield-2026/")
        second = route_name("/auctions/shelbyville-2027/")
        self.assertEqual(first, second)
        self.assertNotEqual(first, UNROUTED)

    def test_a_path_that_matches_nothing_is_bucketed_rather_than_dropped(self):
        self.assertEqual(route_name("/this-is-not-a-page-on-this-site-at-all/"), UNROUTED)

    def test_junk_does_not_raise(self):
        for path in ("", None, "not-a-path", "https://example.com/lots/", "//evil.example/", "/lots/%%%"):
            self.assertIsInstance(route_name(path), str)

    def test_an_absolute_url_is_not_classified_as_one_of_our_routes(self):
        """PageView held whole URLs before migration 0425, and old rows can still.

        Filing https://example.com/lots/ as `allLots` would put somebody else's traffic in our
        reach numbers under a route we own.
        """
        self.assertEqual(route_name("https://example.com/lots/"), UNROUTED)

    def test_the_classifier_follows_urls_py_rather_than_a_copy_of_it(self):
        """A route renamed in urls.py must not leave this reporting under the old name."""
        path = reverse("allLots")
        self.assertEqual(route_name(path), "allLots")


class ReachTests(StandardTestCase):
    def _view(self, url, days_ago=1):
        view = PageView.objects.create(url=url, title="t")
        PageView.objects.filter(pk=view.pk).update(date_start=timezone.now() - timedelta(days=days_ago))
        return view

    def test_paths_are_folded_onto_routes_and_counted(self):
        self._view("/auctions/first-auction/")
        self._view("/auctions/second-auction/")
        self._view("/auctions/second-auction/")
        rows = {row["route"]: row for row in reach_by_route(days=30)}
        route = route_name("/auctions/first-auction/")
        self.assertEqual(rows[route]["views"], 3)
        self.assertEqual(rows[route]["pages"], 2)

    def test_views_outside_the_window_are_not_counted(self):
        self._view("/lots/", days_ago=90)
        rows = {row["route"]: row for row in reach_by_route(days=7)}
        self.assertNotIn("allLots", rows)

    def test_rows_with_no_url_are_skipped(self):
        self._view("")
        self.assertEqual(reach_by_route(days=30), [])

    def test_the_caveats_travel_with_the_numbers(self):
        """Every reach number on this site is biased toward pages nobody struggled with."""
        self.assertEqual(len(usability_report.REACH_CAVEATS), 2)
        for caveat in usability_report.REACH_CAVEATS:
            self.assertGreater(len(caveat), 40)


class FrictionReportTests(StandardTestCase):
    def _failure(self, form_name="AuctionEditForm", resolved=False, attempt=1, fields=None, user=None):
        row = FormFailure.objects.create(
            form_name=form_name,
            url="/auctions/x/edit/",
            user=user,
            session_id="s1",
            field_errors=fields if fields is not None else {"tax": ["invalid"]},
            attempt=attempt,
            resolved=resolved,
        )
        return row

    def test_a_form_nobody_finishes_sorts_above_a_busier_one_that_everybody_finishes(self):
        for _ in range(20):
            self._failure(form_name="BusyButFine", resolved=True)
        for _ in range(3):
            self._failure(form_name="NobodyFinishes", resolved=False)
        rows = friction_by_form(days=30)
        self.assertEqual(rows[0]["form_name"], "NobodyFinishes")

    def test_the_worst_field_is_named_with_its_error_code(self):
        for _ in range(5):
            self._failure(fields={"date_end": ["required"]})
        self._failure(fields={"tax": ["invalid"]})
        row = next(row for row in friction_by_form(days=30) if row["form_name"] == "AuctionEditForm")
        self.assertEqual(row["fields"][0]["field"], "date_end")
        self.assertEqual(row["fields"][0]["code"], "required")
        self.assertEqual(row["fields"][0]["count"], 5)

    def test_completion_is_reported_as_a_percentage(self):
        for _ in range(3):
            self._failure(resolved=True)
        self._failure(resolved=False)
        row = friction_by_form(days=30)[0]
        self.assertEqual(row["completion"], 75.0)

    def test_giving_up_after_one_rejection_is_counted_separately(self):
        self._failure(resolved=False, attempt=1)
        self._failure(resolved=False, attempt=4)
        row = friction_by_form(days=30)[0]
        self.assertEqual(row["gave_up_on_the_first_try"], 1)
        self.assertEqual(row["unresolved"], 2)

    def test_a_row_with_no_field_errors_does_not_break_the_report(self):
        self._failure(fields={})
        row = friction_by_form(days=30)[0]
        self.assertEqual(row["fields"], [])

    def test_time_before_leaving_is_a_median_not_a_mean(self):
        """The tail is tabs somebody left open over lunch, and one of those moves a mean by minutes."""
        for seconds in (10, 12, 14, 16, 20_000):
            row = self._failure()
            FormFailure.objects.filter(pk=row.pk).update(kind="abandoned", seconds_on_page=seconds)
        reported = friction_by_form(days=30)[0]["seconds_before_leaving"]
        self.assertEqual(reported, 14)

    def test_a_form_with_no_abandonments_reports_no_duration(self):
        self._failure()
        self.assertIsNone(friction_by_form(days=30)[0]["seconds_before_leaving"])

    def test_abandonments_and_rejections_are_counted_separately(self):
        self._failure()
        row = self._failure()
        FormFailure.objects.filter(pk=row.pk).update(kind="abandoned")
        reported = friction_by_form(days=30)[0]
        self.assertEqual(reported["bounces"], 1)
        self.assertEqual(reported["abandoned"], 1)

    def test_failures_outside_the_window_are_left_out(self):
        row = self._failure()
        FormFailure.objects.filter(pk=row.pk).update(timestamp=timezone.now() - timedelta(days=90))
        self.assertEqual(friction_by_form(days=7), [])


class DashboardTests(StandardTestCase):
    def test_a_superuser_sees_all_three_panels(self):
        self.admin_user.is_superuser = True
        self.admin_user.save()
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(reverse("admin_usability"))
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        for heading in ("Failure", "Adoption", "Reach"):
            self.assertIn(heading, page)
        for caveat in usability_report.REACH_CAVEATS:
            self.assertIn(caveat[:40], page)

    def test_an_ordinary_user_cannot_open_it(self):
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.get(reverse("admin_usability"))
        self.assertNotEqual(response.status_code, 200)

    def test_a_nonsense_days_parameter_does_not_500(self):
        self.admin_user.is_superuser = True
        self.admin_user.save()
        self.client.login(username="admin_user", password="testpassword")
        for value in ("abc", "-5", "999999", ""):
            response = self.client.get(reverse("admin_usability"), {"days": value})
            self.assertEqual(response.status_code, 200, value)
