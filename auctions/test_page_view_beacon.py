"""One view per page, recorded on every page, with no timer in front of it.

The beacon used to be opt-in -- 38 templates out of 247 called ``pageView()`` -- and fired two
seconds after load.  Both biases ran the same way: a page nobody instrumented read as *absent*
rather than unvisited, and a page somebody bounced off in a second recorded nothing at all, which
deletes exactly the confused mis-clicks a funnel is about.  ``base_page_view.html`` now fires one
view at ``DOMContentLoaded`` on every page that extends ``base.html``.

Which auction (if any) that view is *about* comes from the view, in ``page_view_auction`` and
``page_view_lot``.  Three pages set it, and the list is not an accident of which templates have an
``auction`` in context: it is the definition of ``Auction.unique_views`` -- "distinct visitors who
viewed this auction's rules page or any of its lots" -- which organizers read on their own stats
page.  The bidder list, the stats page and the auction edit form all have an ``auction`` in scope
and must not tag, or an organizer's traffic number fills up with their own admin visits.  The
class below that opens all six pages is what holds that line.
"""

from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from auctions.models import Lot, PageView
from auctions.tests import StandardTestCase

TEMPLATES = Path(__file__).resolve().parent / "templates"
BEACON = TEMPLATES / "base_page_view.html"


class BeaconSourceTests(TestCase):
    def test_the_beacon_does_not_wait(self):
        """The two-second timer deleted the fast bounces, which are the interesting ones."""
        self.assertNotIn("setTimeout", BEACON.read_text())

    def test_it_fires_itself_on_every_page(self):
        source = BEACON.read_text()
        self.assertIn("DOMContentLoaded", source)
        self.assertIn("pageView(pageViewSubject)", source)

    def test_one_row_per_page(self):
        source = BEACON.read_text()
        self.assertIn("if (pageViewSent) { return; }", source)
        self.assertIn("pageViewSent = true;", source)

    def test_no_template_calls_the_beacon_itself(self):
        """The ratchet, and the reason there is nothing left to get in the wrong order.

        34 templates used to call ``pageView()`` with no arguments, which by then was exactly what
        base.html already did, and three called it with a pk from a parse-time script -- where they
        had to be, because whichever call ran first won.  A template calling it again brings that
        back: run early it silently replaces the tagged view, run late it is dropped.
        """
        callers = [
            path.name
            for path in sorted(TEMPLATES.rglob("*.html"))
            if path != BEACON and "pageView(" in path.read_text()
        ]
        self.assertEqual(
            callers,
            [],
            f"{callers} call the beacon directly; set page_view_auction/page_view_lot in the view instead.",
        )


class WhatCountsAsViewingAnAuctionTests(StandardTestCase):
    """The three pages that tag, and three with an auction in context that must not."""

    def _subject(self, url):
        """The lot and auction the beacon on this page will send, as the browser would read them."""
        page = self.client.get(url).content.decode()
        lot = page.split('lot: "', 1)[1].split('"', 1)[0]
        auction = page.split('auction: "', 1)[1].split('"', 1)[0]
        return lot, auction

    def test_the_lot_page_sends_its_lot_and_its_auction(self):
        """Both, so a reader can match the auction on one indexed column."""
        self.assertEqual(
            self._subject(self.lot.lot_link),
            (str(self.lot.pk), str(self.online_auction.pk)),
        )

    def test_the_rules_page_sends_the_auction(self):
        url = reverse("auction_main", kwargs={"slug": self.online_auction.slug})
        self.assertEqual(self._subject(url), ("", str(self.online_auction.pk)))

    def test_the_auctions_lot_list_sends_the_auction(self):
        url = reverse("allLots") + f"?auction={self.online_auction.slug}"
        self.assertEqual(self._subject(url), ("", str(self.online_auction.pk)))

    def test_the_lot_list_with_no_auction_sends_nothing(self):
        self.assertEqual(self._subject(reverse("allLots")), ("", ""))

    def test_a_lot_with_no_auction_sends_only_the_lot(self):
        lot = Lot.objects.create(lot_name="No auction here", user=self.user, quantity=1)
        self.assertEqual(self._subject(lot.lot_link), (str(lot.pk), ""))

    def test_the_organizers_own_pages_do_not_tag_the_auction(self):
        """The one that protects unique_views. All three have an ``auction`` in context."""
        self.client.login(username="admin_user", password="testpassword")
        for name in ("auction_tos_list", "auction_stats", "edit_auction"):
            url = reverse(name, kwargs={"slug": self.online_auction.slug})
            with self.subTest(page=name):
                self.assertEqual(self._subject(url), ("", ""))


class OneViewPerPageTests(StandardTestCase):
    def test_a_page_that_never_opted_in_records_one(self):
        """105 pages gained the beacon this way; login and signup are the two this phase is for."""
        self.client.login(username="my_lot", password="testpassword")
        page = self.client.get(reverse("user_api_keys")).content.decode()
        self.assertEqual(page.count("pageView(pageViewSubject)"), 1)


class RowsFromTheBeaconTests(TestCase):
    """What the endpoint does with what the beacon posts."""

    def _beacon(self, path="/lots/", **extra):
        return self.client.post(
            reverse("pageview"),
            {"url": path, "title": "t", "referrer": "", "first_view": "true", **extra},
        )

    def test_an_anonymous_visitor_records_one_row_under_their_session(self):
        self._beacon()
        rows = PageView.objects.filter(url="/lots/")
        self.assertEqual(rows.count(), 1)
        self.assertTrue(rows.first().session_id)
        self.assertIsNone(rows.first().user)

    def test_a_signed_in_visitor_records_one_row(self):
        User.objects.create_user("beaconuser", "beacon@example.com", "x")
        self.client.login(username="beaconuser", password="x")
        self._beacon("/account/")
        self.assertEqual(PageView.objects.filter(url="/account/").count(), 1)

    def test_an_untagged_page_is_invisible_to_an_organizer(self):
        """Why no admin-only flag is needed on the write side: the separation is in the data.
        Every organizer-facing read of PageView filters on one of these two FKs."""
        for path in ("/account/", "/clubs/", "/auctions/x/edit/"):
            self._beacon(path)
        self.assertEqual(PageView.objects.count(), 3)
        self.assertEqual(PageView.objects.filter(lot_number__isnull=False).count(), 0)
        self.assertEqual(PageView.objects.filter(auction__isnull=False).count(), 0)

    def test_an_empty_subject_is_not_a_pk(self):
        """Every page posts both keys; most post them empty. An empty string is 'not given'."""
        self._beacon("/account/", lot="", auction="")
        row = PageView.objects.get(url="/account/")
        self.assertIsNone(row.lot_number)
        self.assertIsNone(row.auction)

    def test_a_junk_subject_does_not_500(self):
        """The endpoint is AllowAny, so anyone at all can post whatever they like to it."""
        response = self._beacon("/account/", lot="abc", auction="../1")
        self.assertEqual(response.status_code, 200)
        row = PageView.objects.get(url="/account/")
        self.assertIsNone(row.lot_number)
        self.assertIsNone(row.auction)
