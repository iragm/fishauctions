"""One view per page, recorded on every page, with no timer in front of it.

The beacon used to be opt-in -- 38 templates out of 247 called ``pageView()`` -- and fired two
seconds after load.  Both biases ran the same way: a page nobody instrumented read as *absent*
rather than unvisited, and a page somebody bounced off in a second recorded nothing at all, which
deletes exactly the confused mis-clicks a funnel is about.  ``base_page_view.html`` now fires one
view at ``DOMContentLoaded`` on every page that extends ``base.html``.

Two of these tests read the template source rather than exercising behaviour, because the invariant
they protect is a JavaScript one and this suite runs no JavaScript.  They are ratchets: the first
``pageView()`` call of a page wins, so a call that carries a lot or an auction has to happen while
the page is parsing.  Made from a load handler it arrives *after* the automatic view has gone, and
the row lands with both FKs null -- which is invisible rather than wrong, since every
organizer-facing read of ``PageView`` filters on one of those two columns.
"""

import re
from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from auctions.models import PageView
from auctions.tests import StandardTestCase

TEMPLATES = Path(__file__).resolve().parent / "templates"
BEACON = TEMPLATES / "base_page_view.html"

# Anything that defers a call past parse time. An enriched pageView() call inside one of these is
# the bug this module exists to prevent.
DEFERRED = (
    "window.onload",
    "$(document).ready(",
    "$(function(",
    "addEventListener('load'",
    'addEventListener("load"',
)


COMMENT = re.compile(r"{% comment %}.*?{% endcomment %}|{#.*?#}", re.DOTALL)


def _templates_calling_page_view():
    """Every template that calls the beacon, with its comments stripped.

    The comments have to go: this module's whole subject is where in a file a call sits, and the
    notes explaining that rule name ``window.onload`` themselves.
    """
    for path in sorted(TEMPLATES.rglob("*.html")):
        if path == BEACON:
            continue
        text = COMMENT.sub("", path.read_text())
        if "pageView(" in text:
            yield path, text


class BeaconSourceTests(TestCase):
    def test_the_beacon_does_not_wait(self):
        """The two-second timer deleted the fast bounces, which are the interesting ones."""
        source = BEACON.read_text()
        self.assertNotIn("setTimeout", source)

    def test_it_fires_itself_on_every_page(self):
        source = BEACON.read_text()
        self.assertIn("DOMContentLoaded", source)
        self.assertIn("pageView();", source)

    def test_the_first_call_wins(self):
        """An enriched call replaces the automatic view rather than adding a second row."""
        source = BEACON.read_text()
        self.assertIn("if (pageViewSent) { return; }", source)
        self.assertIn("pageViewSent = true;", source)

    def test_a_call_naming_a_lot_or_an_auction_runs_while_the_page_parses(self):
        """The ratchet. Moving one of these into a load handler loses the FK it exists to carry."""
        enriched = re.compile(r"pageView\(\s*\{|pageView\(\{%")
        checked = 0
        for path, text in _templates_calling_page_view():
            match = enriched.search(text)
            if not match:
                continue
            checked += 1
            for opener in DEFERRED:
                position = text.find(opener)
                if position == -1:
                    continue
                self.assertLess(
                    match.start(),
                    position,
                    f"{path.name} calls pageView with data after {opener}; the automatic view in "
                    "base_page_view.html has already fired by then and the lot/auction FK is lost.",
                )
        self.assertGreaterEqual(checked, 3, "expected the lot, auction and all-lots pages to name their FK")


class OneViewPerPageTests(StandardTestCase):
    def test_a_page_that_never_opted_in_now_records_one(self):
        """105 pages gained the beacon this way; login and signup are the two this phase is for."""
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.get(reverse("user_api_keys"))
        page = response.content.decode()
        self.assertNotIn("pageView({", page)
        self.assertIn("DOMContentLoaded', function () { pageView(); }", page)

    def test_the_lot_page_names_its_lot_exactly_once(self):
        response = self.client.get(self.lot.lot_link)
        page = response.content.decode()
        self.assertEqual(page.count(f"pageView({{'lot':{self.lot.pk} }})"), 1)


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

    def test_a_view_that_names_no_lot_and_no_auction_is_invisible_to_an_organizer(self):
        """Why no admin-only flag is needed on the write side: the separation is already in the
        data. Every organizer-facing read filters on one of these two FKs."""
        for path in ("/account/", "/clubs/", "/auctions/x/edit/"):
            self._beacon(path)
        self.assertEqual(PageView.objects.count(), 3)
        self.assertEqual(PageView.objects.filter(lot_number__isnull=False).count(), 0)
        self.assertEqual(PageView.objects.filter(auction__isnull=False).count(), 0)
