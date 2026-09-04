"""The two page-view history modals: one lot's, and every lot on the selling dashboard.

Both are the same helper (``views.page_view_history``) and the same partial at two scopes, so the
tests are grouped the same way: the counting rules once, then the gate on each surface.

Four things here are worth stating out loud, because getting any of them wrong is silent:

* **The window is the bound.** ``PageView`` is the largest table on the site, so every query the
  helper makes carries a date *and* an owner. A test that only checked the numbers were right would
  still pass if the window quietly stopped being applied, which is why there is a test for a view
  just outside it.
* **``PageView.date_start`` is ``auto_now_add``**, so a row cannot be created with a date in the
  past -- ``_view_on`` writes it with ``update()`` afterwards.
* **The chart is the only day-by-day output**, and the template hands it straight to
  ``json_script``, so anything in it that is not a string or a number is a 500 on the modal rather
  than a wrong-looking label -- which is what ``test_the_chart_survives_json`` is for.
* **The seller of a fixture lot is the seller *TOS*, not ``Lot.user``.** ``StandardTestCase``'s lots
  have ``user=None`` and ``auctiontos_seller=online_tos``, which is exactly the case
  ``Lot.is_owned_by`` exists for -- so these tests exercise the TOS path for free, and the
  standalone lot below covers the ``Lot.user`` one.
"""

import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.template.defaultfilters import date as date_format
from django.urls import reverse
from django.utils import timezone

from auctions.models import Lot, PageView
from auctions.tests import StandardTestCase
from auctions.views import (
    PAGE_VIEW_HISTORY_CHART_SOURCES,
    PAGE_VIEW_HISTORY_DAYS,
    PAGE_VIEW_HISTORY_MIN_Y,
    page_view_history,
)


def _view_on(lot, days_ago=0, *, source="", user=None, session_id=None, referrer=None):
    """One PageView on ``lot``, ``days_ago`` days back.

    ``date_start`` is ``auto_now_add``, so it has to be written after the insert.
    """
    row = PageView.objects.create(lot_number=lot, source=source, user=user, session_id=session_id, referrer=referrer)
    when = timezone.now() - timedelta(days=days_ago)
    PageView.objects.filter(pk=row.pk).update(date_start=when, date_end=when)
    return row


class PageViewHistoryHelperTests(StandardTestCase):
    """``views.page_view_history`` -- the counting, and the 15-day bound on it."""

    def _history(self, lot=None):
        lot = lot or self.lot
        return page_view_history(PageView.objects.filter(lot_number=lot))

    def test_totals_count_views_and_people_separately(self):
        _view_on(self.lot, 1, user=self.user)
        _view_on(self.lot, 1, user=self.user)  # same person twice: 2 views, 1 person
        _view_on(self.lot, 2, user=self.user_with_no_lots)
        history = self._history()
        self.assertEqual(history["total_views"], 3)
        self.assertEqual(history["unique_viewers"], 2)

    def test_anonymous_sessions_count_as_people(self):
        _view_on(self.lot, 0, session_id="anon-1")
        _view_on(self.lot, 0, session_id="anon-1")  # one session, one person
        _view_on(self.lot, 0, session_id="anon-2")
        history = self._history()
        self.assertEqual((history["total_views"], history["unique_viewers"]), (3, 2))

    def test_anything_older_than_the_window_is_left_out(self):
        """The bound that keeps this cheap. Without it the modal reads the whole table."""
        _view_on(self.lot, 1, user=self.user)
        _view_on(self.lot, PAGE_VIEW_HISTORY_DAYS + 5, user=self.user)
        history = self._history()
        self.assertEqual(history["total_views"], 1)
        self.assertEqual(sum(sum(band) for band in history["chart"]["data"]), 1)
        self.assertEqual(sum(row["views"] for row in history["sources"]), 1)

    def test_views_on_another_lot_are_not_counted(self):
        _view_on(self.lot, 0, user=self.user)
        _view_on(self.lotB, 0, user=self.user)
        self.assertEqual(self._history()["total_views"], 1)

    def test_sources_are_labelled_and_ordered_by_views(self):
        _view_on(self.lot, 0, source="qr", user=self.user)
        for _ in range(3):
            _view_on(self.lot, 0, source="lot_list", user=self.user)
        rows = self._history()["sources"]
        self.assertEqual(rows[0]["source"], "lot_list")
        self.assertEqual(rows[0]["label"], "From a lot list")
        self.assertEqual(rows[1]["label"], "Scanned the printed QR code")

    def test_blank_and_null_sources_are_one_row(self):
        _view_on(self.lot, 0, source="", user=self.user)
        _view_on(self.lot, 0, source=None, session_id="anon-1")
        rows = self._history()["sources"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "Opened the lot page directly")
        self.assertEqual(rows[0]["views"], 2)

    def test_an_unknown_source_is_shown_as_it_was_written(self):
        """A campaign uuid from the weekly promo email, or a club API key's name."""
        _view_on(self.lot, 0, source="my-club-website", user=self.user)
        self.assertEqual(self._history()["sources"][0]["label"], "my-club-website")

    def test_there_is_a_column_for_every_day_including_the_quiet_ones(self):
        """A chart with the empty days left out would read as a busier lot than it was."""
        _view_on(self.lot, 0, user=self.user)
        chart = self._history()["chart"]
        self.assertEqual(len(chart["labels"]), PAGE_VIEW_HISTORY_DAYS)
        self.assertEqual(chart["labels"][-1], date_format(timezone.localdate(), "M j"))
        self.assertEqual(len(chart["data"][0]), PAGE_VIEW_HISTORY_DAYS)
        self.assertEqual(chart["data"][0][0], 0)
        self.assertEqual(chart["data"][0][-1], 1)

    def test_the_chart_is_one_band_per_source_stacked_inside_each_day(self):
        """The source split the table used to show is what the chart draws, for everybody."""
        _view_on(self.lot, 0, source="lot_list", user=self.user)
        _view_on(self.lot, 0, source="lot_list", user=self.user_with_no_lots)
        _view_on(self.lot, 1, source="qr", session_id="anon-1")
        chart = self._history()["chart"]
        self.assertEqual(chart["series"], ["From a lot list", "Scanned the printed QR code"])
        self.assertEqual(chart["data"][0][-1], 2)
        self.assertEqual(chart["data"][1][-2], 1)
        self.assertEqual(chart["busiest"], 2)

    def test_the_long_tail_of_sources_is_one_band(self):
        """``?src=`` is not a closed vocabulary -- a club API key writes its own name into it."""
        for number in range(PAGE_VIEW_HISTORY_CHART_SOURCES + 3):
            _view_on(self.lot, 0, source=f"club-website-{number}", user=self.user)
        chart = self._history()["chart"]
        self.assertEqual(len(chart["series"]), PAGE_VIEW_HISTORY_CHART_SOURCES + 1)
        self.assertEqual(chart["series"][-1], "Everything else")
        self.assertEqual(chart["data"][-1][-1], 3)
        self.assertEqual(sum(band[-1] for band in chart["data"]), PAGE_VIEW_HISTORY_CHART_SOURCES + 3)

    def test_one_view_does_not_fill_the_chart(self):
        """Most lots get five to fifteen views in the whole fortnight; the axis has to suit that."""
        _view_on(self.lot, 0, user=self.user)
        chart = self._history()["chart"]
        self.assertEqual(chart["y_max"], PAGE_VIEW_HISTORY_MIN_Y)
        self.assertEqual(chart["y_step"], 1)

    def test_the_axis_climbs_in_whole_views_when_there_are_more(self):
        """The selling dashboard totals every lot somebody sells, so it does get real numbers."""
        for _ in range(13):
            _view_on(self.lot, 0, user=self.user)
        chart = self._history()["chart"]
        self.assertEqual((chart["y_step"], chart["y_max"]), (4, 16))
        self.assertGreaterEqual(chart["y_max"], chart["busiest"])

    def test_the_chart_survives_json(self):
        """The template hands this to json_script: a date in here is a 500 on the modal."""
        _view_on(self.lot, 0, source="qr", user=self.user)
        payload = json.loads(json.dumps(self._history()["chart"]))
        self.assertEqual(payload["labels"][-1], date_format(timezone.localdate(), "M j"))
        self.assertEqual(payload["series"], ["Scanned the printed QR code"])

    def test_referrers_list_other_sites_and_not_our_own_pages(self):
        domain = Site.objects.get_current().domain
        _view_on(self.lot, 0, user=self.user, referrer=f"https://{domain}/lots/")
        _view_on(self.lot, 0, user=self.user, referrer=domain + "/auctions/")
        _view_on(self.lot, 0, user=self.user, referrer="")
        _view_on(self.lot, 0, user=self.user, referrer="https://example.org/fishclub")
        rows = self._history()["referrers"]
        self.assertEqual([row["referrer"] for row in rows], ["https://example.org/fishclub"])

    def test_the_referrer_list_is_capped(self):
        for number in range(9):
            _view_on(self.lot, 0, user=self.user, referrer=f"https://example.org/{number}")
        self.assertEqual(len(self._history()["referrers"]), 5)

    def test_nothing_at_all_is_a_clean_zero(self):
        history = self._history()
        self.assertEqual(history["total_views"], 0)
        self.assertEqual(history["unique_viewers"], 0)
        self.assertEqual(history["sources"], [])
        self.assertEqual(history["referrers"], [])
        self.assertEqual(history["chart"]["series"], [])
        self.assertEqual(history["chart"]["data"], [])
        self.assertEqual(len(history["chart"]["labels"]), PAGE_VIEW_HISTORY_DAYS)
        self.assertEqual(history["chart"]["y_max"], PAGE_VIEW_HISTORY_MIN_Y)


class LotPageViewHistoryViewTests(StandardTestCase):
    """The button on the lot page and the modal behind it.

    The permission lives in the view, not in the template: the URL is guessable, so every
    "who cannot see this" test asks for the modal itself rather than only checking the page.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Auction.permission_check passes any superuser, so this one reaches the modal without
        # being the seller or an admin of the auction -- which is the point of it here.
        cls.site_admin = User.objects.create_superuser(
            username="site_admin_lot_history", password="testpassword", email="site_admin_lot@example.com"
        )

    def setUp(self):
        super().setUp()
        self.url = reverse("lot_page_view_history", kwargs={"pk": self.lot.pk})
        # A lot with no auction at all, and with Lot.user set rather than a seller TOS.
        self.standalone_lot = Lot.objects.create(lot_name="No auction lot", quantity=1, user=self.user)

    def test_the_seller_sees_the_button_on_an_online_lot(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("lot_by_pk", kwargs={"pk": self.lot.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["show_page_view_history"])
        self.assertContains(response, "15 day history")
        self.assertContains(response, self.url)

    def test_the_seller_gets_the_modal(self):
        _view_on(self.lot, 0, source="lot_list", user=self.user_with_no_lots)
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "How people found this lot")
        self.assertContains(response, "From a lot list")
        self.assertEqual(response.context["history"]["total_views"], 1)

    def test_an_auction_admin_gets_it_too(self):
        self.client.force_login(self.admin_user)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_another_bidder_in_the_same_auction_is_refused(self):
        self.client.force_login(self.user_with_no_lots)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_the_button_is_not_drawn_for_other_people(self):
        self.client.force_login(self.user_with_no_lots)
        response = self.client.get(reverse("lot_by_pk", kwargs={"pk": self.lot.pk}))
        self.assertFalse(response.context["show_page_view_history"])
        self.assertNotContains(response, "15 day history")

    def test_a_signed_out_visitor_is_sent_to_log_in(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_a_lot_with_no_auction_gets_it(self):
        _view_on(self.standalone_lot, 0, source="userpage", user=self.user_with_no_lots)
        self.client.force_login(self.user)
        url = reverse("lot_page_view_history", kwargs={"pk": self.standalone_lot.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # The seller has no source table, so the label arrives as a chart band in the json_script
        # block -- which escapes < > and & and leaves the apostrophe alone.
        self.assertContains(response, "From a user's page")

    def test_an_in_person_lot_keeps_the_collapse_and_does_not_get_the_modal(self):
        """The in-person breakdown already exists below the same line; two of them is worse."""
        self.client.force_login(self.admin_user)
        page = self.client.get(reverse("lot_by_pk", kwargs={"pk": self.in_person_lot.pk}))
        self.assertFalse(page.context["show_page_view_history"])
        self.assertNotContains(page, "15 day history")
        refused = self.client.get(reverse("lot_page_view_history", kwargs={"pk": self.in_person_lot.pk}))
        self.assertEqual(refused.status_code, 403)

    def test_a_sealed_bid_lot_publishes_no_view_count_and_no_history(self):
        self.online_auction.sealed_bid = True
        self.online_auction.save()
        self.client.force_login(self.user)
        page = self.client.get(reverse("lot_by_pk", kwargs={"pk": self.lot.pk}))
        self.assertFalse(page.context["show_page_view_history"])
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_the_seller_does_not_get_the_source_table_but_does_get_the_chart(self):
        """Which of our own pages sent somebody is detail a seller has no use for -- but it is
        still the thing the chart is split by, and the referrer list is still theirs."""
        _view_on(self.lot, 0, source="lot_list", user=self.user_with_no_lots)
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertFalse(response.context["show_source_table"])
        self.assertNotContains(response, "How they got here")
        self.assertContains(response, "From a lot list")  # the chart band, in the json_script
        self.assertContains(response, "Links from other sites")

    def test_a_superuser_gets_the_source_table(self):
        _view_on(self.lot, 0, source="lot_list", user=self.user_with_no_lots)
        self.client.force_login(self.site_admin)
        response = self.client.get(self.url)
        self.assertTrue(response.context["show_source_table"])
        self.assertContains(response, "How they got here")

    def test_an_auction_admin_is_not_a_superuser(self):
        """The gate is Django's is_superuser, not "can administer this auction"."""
        self.client.force_login(self.admin_user)
        response = self.client.get(self.url)
        self.assertFalse(response.context["show_source_table"])
        self.assertNotContains(response, "How they got here")

    def test_a_deleted_lot_is_a_404(self):
        self.lot.is_deleted = True
        self.lot.save()
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 404)


class SellingDashboardPageViewHistoryTests(StandardTestCase):
    """The same modal on /selling/, totalled over everything the reader is selling."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.site_admin = User.objects.create_superuser(
            username="site_admin_selling_history", password="testpassword", email="site_admin_selling@example.com"
        )

    def setUp(self):
        super().setUp()
        self.url = reverse("my_lots_page_view_history")

    def test_the_button_is_on_the_selling_dashboard(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("selling"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "15 day view history")
        self.assertContains(response, self.url)

    def test_it_adds_up_every_lot_this_person_is_selling(self):
        _view_on(self.lot, 0, source="lot_list", user=self.user_with_no_lots)
        _view_on(self.lotB, 1, source="lot_list", user=self.user_with_no_lots)
        _view_on(self.lotC, 2, source="qr", session_id="anon-1")
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        history = response.context["history"]
        self.assertEqual(history["total_views"], 3)
        self.assertEqual({row["source"]: row["views"] for row in history["sources"]}, {"lot_list": 2, "qr": 1})

    def test_somebody_elses_lots_are_not_in_it(self):
        """in_person_lot belongs to admin_user; self.user must never see its views here."""
        _view_on(self.in_person_lot, 0, source="qr", user=self.user_with_no_lots)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).context["history"]["total_views"], 0)
        self.client.force_login(self.admin_user)
        self.assertEqual(self.client.get(self.url).context["history"]["total_views"], 1)

    def test_the_window_applies_here_too(self):
        _view_on(self.lot, 0, user=self.user_with_no_lots)
        _view_on(self.lotB, PAGE_VIEW_HISTORY_DAYS + 1, user=self.user_with_no_lots)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).context["history"]["total_views"], 1)

    def test_a_signed_out_visitor_is_sent_to_log_in(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_the_source_table_is_superuser_only_here_too(self):
        """Both modals go through views.page_view_history_context, so both gate the same way."""
        _view_on(self.lot, 0, source="lot_list", user=self.user_with_no_lots)
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get(self.url), "How they got here")
        self.client.force_login(self.site_admin)
        self.assertContains(self.client.get(self.url), "How they got here")

    def test_a_user_with_no_lots_gets_an_empty_history_rather_than_an_error(self):
        self.client.force_login(self.user_who_does_not_join)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["history"]["total_views"], 0)
        self.assertContains(response, "How people found your lots")
