"""Query-count guards: the N+1s that were fixed, and stay fixed.

Every optimization in ``OPTIMIZATION.md`` that removed a per-row query has a test here, because a
``select_related`` or a ``@cached_property`` is invisible: delete it and every test still passes,
the page just costs ten times as much. That is the same failure mode as ``SuiteStaysFastTests`` in
``auctions/tests.py``, and this file is the same answer to it.

**These assert growth, not totals.** A page's fixed cost (session, userdata, the nav, feature
flags) moves whenever anything else changes and is nobody's bug; what must not move is the cost of
*one more row*. So each test renders the same page against two different row counts and asserts the
difference. A test that fails here names a real N+1 -- find what the new row touched.
"""

import datetime

from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from auctions.models import Bid, Lot, LotImage
from auctions.tests import StandardTestCase


class QueryGrowthMixin:
    """``queries_per_extra_row`` -- render a page twice, with N and then N+extra rows."""

    def queries_per_extra_row(self, url, params, make_rows, extra=4):
        """Return (queries added, rows added) for `extra` more rows on `url`.

        `extra` rows are created *before* the first measurement as well, so that anything the page
        pays once for having any rows at all -- every prefetch_related is one query whether the page
        holds one row or fifty -- is already paid in the baseline and does not read as growth.

        The page is also fetched once before either measurement, so anything cached per process
        (the template loader, the site row, a form's choices) is warm for both.
        """
        client = self.client
        make_rows(extra)
        client.get(url, params)
        with CaptureQueriesContext(connection) as before:
            response = client.get(url, params)
        self.assertEqual(response.status_code, 200)
        rows_before = len(response.context["object_list"])
        make_rows(extra)
        with CaptureQueriesContext(connection) as after:
            response = client.get(url, params)
        self.assertEqual(response.status_code, 200)
        rows_after = len(response.context["object_list"])
        added_rows = rows_after - rows_before
        self.assertEqual(
            added_rows,
            extra,
            f"the extra lots did not reach the page ({rows_before} -> {rows_after}), so this measures nothing",
        )
        return len(after.captured_queries) - len(before.captured_queries), added_rows


class LotListQueryCountTests(QueryGrowthMixin, StandardTestCase):
    """The lot list is the most-viewed page on the site and renders ~50 lots at a time.

    Before this was measured, one more lot on the page cost about ten more queries: the auction,
    the category, the seller, the winner and that winner's userdata, the shipping locations, the
    thumbnail (twice), and three or four passes over the lot's bids. All but the bids are now paid
    once for the whole page.
    """

    # One per row is what is left, and it is `Lot.auto_image`: a lot with no picture of its own
    # borrows one from another lot with the same name, and "the same name" is per row -- there is
    # nothing to prefetch. A lot that has its own image costs nothing here.
    MAX_QUERIES_PER_LOT = 1

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.user)
        self._next_lot = 0

    def _make_lots(self, count):
        the_future = timezone.now() + datetime.timedelta(days=3)
        for _ in range(count):
            self._next_lot += 1
            lot = Lot.objects.create(
                lot_name=f"query count lot {self._next_lot}",
                auction=self.online_auction,
                auctiontos_seller=self.online_tos,
                user=self.user,
                quantity=1,
                reserve_price=2,
                date_end=the_future,
                active=True,
            )
            # A bid, a winner and a userdata behind the winner: the row has something to render in
            # every column, so a missing select_related shows up as growth rather than as nothing.
            Bid.objects.create(user=self.user_with_no_lots, lot_number=lot, amount=5, was_high_bid=True)

    def test_tile_view_does_not_query_per_lot(self):
        self.user.userdata.use_list_view = False
        self.user.userdata.save()
        added, rows = self.queries_per_extra_row(
            reverse("allLots"), {"auction": self.online_auction.slug, "status": "all"}, self._make_lots
        )
        self.assertLessEqual(
            added,
            rows * self.MAX_QUERIES_PER_LOT,
            f"{added} queries for {rows} more lots in the tile view -- something is N+1 per lot again",
        )

    def test_list_view_does_not_query_per_lot(self):
        self.user.userdata.use_list_view = True
        self.user.userdata.save()
        added, rows = self.queries_per_extra_row(
            reverse("allLots"), {"auction": self.online_auction.slug, "status": "all"}, self._make_lots
        )
        self.assertLessEqual(
            added,
            rows * self.MAX_QUERIES_PER_LOT,
            f"{added} queries for {rows} more lots in the list view -- something is N+1 per lot again",
        )


class LotCachedPropertyTests(StandardTestCase):
    """``Lot``'s read properties are cached on the instance, and a write drops the cache.

    The caching is the optimization; the invalidation is what keeps it correct, and it is the half
    that breaks silently -- a stale ``high_bidder`` after a bid is a wrong page, not an error.
    """

    def _open_lot(self, name, reserve_price=2):
        """A lot that is still running.

        Lot.save() takes date_end from the auction for an online auction, and this fixture's online
        auction ended two days ago -- so the end time has to be forced past the save, or every bid
        below is filtered out for arriving after the lot ended.
        """
        lot = Lot.objects.create(
            lot_name=name,
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            reserve_price=reserve_price,
            active=True,
        )
        Lot.objects.filter(pk=lot.pk).update(date_end=timezone.now() + datetime.timedelta(days=3))
        return Lot.objects.get(pk=lot.pk)

    def test_bids_is_read_once_per_instance(self):
        lot = Lot.objects.get(pk=self.unsoldLot.pk)
        with CaptureQueriesContext(connection) as queries:
            lot.bids
            lot.bids
            lot.high_bid
            lot.high_bidder
        bid_queries = [q for q in queries.captured_queries if "auctions_bid" in q["sql"]]
        self.assertEqual(len(bid_queries), 1, "lot.bids should be one query however many times it is read")

    def test_images_is_read_once_per_instance(self):
        LotImage.objects.create(lot_number=self.unsoldLot, url="https://example.com/a.png", is_primary=True)
        lot = Lot.objects.get(pk=self.unsoldLot.pk)
        with CaptureQueriesContext(connection) as queries:
            lot.images
            lot.images
            lot.image_count
            lot.thumbnail
            lot.thumbnail
        image_queries = [q for q in queries.captured_queries if "auctions_lotimage" in q["sql"]]
        self.assertEqual(len(image_queries), 1, "lot.images should be one query however many times it is read")

    def test_thumbnail_is_the_primary_image(self):
        """The one behaviour the images rewrite had to keep: primary first, whatever the pk order."""
        LotImage.objects.create(lot_number=self.unsoldLot, url="https://example.com/other.png")
        primary = LotImage.objects.create(
            lot_number=self.unsoldLot, url="https://example.com/primary.png", is_primary=True
        )
        lot = Lot.objects.get(pk=self.unsoldLot.pk)
        self.assertEqual(lot.thumbnail, primary)
        self.assertEqual(lot.images[0], primary)
        self.assertEqual(lot.image_count, 2)

    def test_saving_the_lot_drops_the_cache(self):
        lot = Lot.objects.get(pk=self.unsoldLot.pk)
        self.assertEqual(lot.winner_as_str, "")
        lot.auctiontos_winner = self.tosB
        lot.winning_price = 5
        lot.save()
        self.assertEqual(lot.winner_as_str, str(self.tosB))

    def test_invalidate_named_properties_only(self):
        lot = Lot.objects.get(pk=self.unsoldLot.pk)
        lot.bids
        lot.images
        self.assertIn("bids", lot.__dict__)
        lot.invalidate_cached_properties("bids")
        self.assertNotIn("bids", lot.__dict__)
        self.assertIn("images", lot.__dict__)

    def test_saving_a_bid_drops_the_lots_cache(self):
        """What ``bid_on_lot`` relies on: it reads high_bidder, writes a Bid, and reads it again.

        No ``Lot.save()`` happens in between, so ``Bid.save()`` is what has to drop the cache.
        Without this, a proxy bid is judged against the bid before it and every bidder after the
        first is told they placed the opening bid.
        """
        lot = self._open_lot("open lot")
        self.assertFalse(lot.high_bidder)
        Bid.objects.create(user=self.user_with_no_lots, lot_number=lot, amount=50, was_high_bid=True)
        self.assertEqual(lot.high_bidder, self.user_with_no_lots)

    # The end-to-end version of the above is test_bidding.DecimalBidValidationTests, which places
    # two bids on one Lot instance: without Bid.save() invalidating, the second bidder is told they
    # placed the opening bid and the increment is checked against nothing.

    def test_bids_keeps_only_each_users_latest_bid(self):
        """The dedupe rule Lot.bids used to express as a correlated subquery, now applied in Python."""
        lot = self._open_lot("dedupe lot", reserve_price=5)
        early = Bid.objects.create(user=self.user_with_no_lots, lot_number=lot, amount=9)
        Bid.objects.filter(pk=early.pk).update(
            bid_time=timezone.now() - datetime.timedelta(hours=2),
            last_bid_time=timezone.now() - datetime.timedelta(hours=2),
        )
        latest = Bid.objects.create(user=self.user_with_no_lots, lot_number=lot, amount=20)
        Bid.objects.create(user=self.userB, lot_number=lot, amount=15)
        # a deleted bid, and one under the reserve, are both out
        deleted = Bid.objects.create(user=self.admin_user, lot_number=lot, amount=99)
        deleted.delete()
        Bid.objects.create(user=self.user_who_does_not_join, lot_number=lot, amount=1)
        self.assertEqual(len(lot.bids), 2, "one bid per user, deleted and under-reserve bids dropped")
        self.assertEqual(lot.bids[0].pk, latest.pk, "the user's earlier bid is not the one that counts")
        self.assertEqual(lot.bids[0].amount, 20)
        self.assertEqual(lot.bids[1].user, self.userB)
