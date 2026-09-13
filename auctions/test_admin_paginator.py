"""``EstimatedCountPaginator``: what it counts exactly and what it guesses at.

The whole point is that the *unfiltered* case never issues a ``COUNT``, because that is the one
that scans the biggest table on the site.  Everything else -- a filter, a search, an engine with no
estimate to offer -- has to keep giving the real answer, so those are what the tests below pin.
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from auctions.admin_paginator import EstimatedCountPaginator
from auctions.models import PageView


class EstimatedCountPaginatorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for i in range(5):
            PageView.objects.create(url=f"/lots/{i}/", title="t", ip_address="10.0.0.1")

    def test_a_filtered_queryset_is_counted_exactly(self):
        """A filter bounds the scan, so there is no reason to estimate -- and the number shown
        under a search box has to be the number of rows the search found."""
        paginator = EstimatedCountPaginator(PageView.objects.filter(url="/lots/1/").order_by("pk"), 20)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(paginator.count, 1)
        self.assertTrue(any("COUNT(" in q["sql"].upper() for q in queries.captured_queries))

    def test_an_unfiltered_queryset_uses_the_engine_estimate(self):
        """No COUNT at all: this is the query that gets slower every month."""
        paginator = EstimatedCountPaginator(PageView.objects.order_by("pk"), 20)
        with patch.object(EstimatedCountPaginator, "_table_row_estimate", return_value=4000):
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(paginator.count, 4000)
        self.assertFalse(any("COUNT(" in q["sql"].upper() for q in queries.captured_queries))

    def test_no_estimate_falls_back_to_the_real_count(self):
        """A fresh table, a non-MySQL backend or an unreadable information_schema all report
        nothing.  Reporting nothing must not read as "there are no page views"."""
        paginator = EstimatedCountPaginator(PageView.objects.order_by("pk"), 20)
        with patch.object(EstimatedCountPaginator, "_table_row_estimate", return_value=0):
            self.assertEqual(paginator.count, 5)

    def test_the_estimate_query_itself_does_not_raise(self):
        """It runs against the real database rather than a mock, because a typo in the SQL is
        exactly the kind of thing that would otherwise only show up in the admin."""
        paginator = EstimatedCountPaginator(PageView.objects.order_by("pk"), 20)
        self.assertGreaterEqual(paginator._table_row_estimate(), 0)


class PageViewChangelistTests(TestCase):
    """The wiring, not the paginator: an unfiltered changelist must issue no ``COUNT`` at all.

    ``show_full_result_count`` removes one of the two and the paginator removes the other, so
    dropping either attribute puts a full scan of the biggest table back on every load of this
    page -- which is the regression worth a test rather than a comment.
    """

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("pvadmin", "pvadmin@example.com", "x")
        PageView.objects.create(url="/lots/1/", title="t")

    def test_changelist_renders_without_counting_the_table(self):
        self.client.force_login(self.admin)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("admin:auctions_pageview_changelist"))
        self.assertEqual(response.status_code, 200)
        counts = [
            q["sql"]
            for q in queries.captured_queries
            if "auctions_pageview" in q["sql"] and "COUNT(" in q["sql"].upper()
        ]
        self.assertEqual(counts, [])
