"""The numbers on an auction's stats page, and the invoice wording that quotes them."""

import datetime
import json
import re
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionTOS,
    Bid,
    Club,
    ClubMoney,
    Invoice,
    InvoiceAdjustment,
    Lot,
    PageView,
    PickupLocation,
)
from auctions.tests import StandardTestCase


class PayPalCreateOrderPartialRefundTests(StandardTestCase):
    """Regression tests for PayPal order creation with partially refunded lots.

    PayPal validates that the sum of every item's unit_amount equals item_total (and that the
    per-item taxes sum to tax_total). A partially refunded lot has a refund-adjusted final_price
    that is lower than its raw winning_price, so building the line items from winning_price while
    item_total came from the refund-adjusted invoice total made the sums disagree and PayPal
    rejected the order -- the buyer could not pay online.
    """

    def _buyer_invoice(self, lots_spec):
        """Create a buyer, their bought lots (list of (winning_price, refund_percent)) and an invoice."""
        buyer = AuctionTOS.objects.create(
            user=self.user_who_does_not_join,
            auction=self.online_auction,
            pickup_location=self.location,
        )
        for winning_price, refund_percent in lots_spec:
            Lot.objects.create(
                lot_name="paypal refund test lot",
                auction=self.online_auction,
                auctiontos_seller=self.online_tos,
                auctiontos_winner=buyer,
                quantity=1,
                winning_price=Decimal(str(winning_price)),
                partial_refund_percent=refund_percent,
                active=False,
            )
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=buyer)
        return invoice

    def _order_payload(self, invoice):
        """Call create_order with post_to_paypal mocked; return the payload it was handed."""
        from django.test import RequestFactory

        from auctions.views import CreatePayPalOrderView

        view = CreatePayPalOrderView()
        view.request = RequestFactory().get("/")
        with patch.object(CreatePayPalOrderView, "post_to_paypal") as mock_post:
            mock_post.return_value = {
                "id": "ORDER-TEST",
                "links": [{"rel": "approve", "href": "https://www.paypal.com/checkoutnow?token=ORDER-TEST"}],
            }
            approval_url = view.create_order(invoice)
        self.assertEqual(mock_post.call_count, 1)
        endpoint, payload = mock_post.call_args.args
        self.assertEqual(endpoint, "v2/checkout/orders")
        return payload, approval_url

    def _assert_breakdown_valid(self, payload):
        """Assert the PayPal breakdown satisfies PayPal's validation arithmetic."""
        unit = payload["purchase_units"][0]
        breakdown = unit["amount"]["breakdown"]
        items = unit["items"]
        item_total = Decimal(breakdown["item_total"]["value"])
        tax_total = Decimal(breakdown["tax_total"]["value"])
        item_sum = sum((Decimal(i["unit_amount"]["value"]) for i in items), Decimal("0.00"))
        tax_sum = sum((Decimal(i["tax"]["value"]) for i in items if "tax" in i), Decimal("0.00"))
        # PayPal: sum(items) == item_total and sum(item taxes) == tax_total.
        self.assertEqual(item_sum, item_total)
        self.assertEqual(tax_sum, tax_total)
        # PayPal: amount == item_total + tax_total + handling/shipping/insurance - discount.
        discount = Decimal(breakdown.get("discount", {}).get("value", "0.00"))
        self.assertEqual(Decimal(unit["amount"]["value"]), item_total + tax_total - discount)

    def test_partial_refund_uses_refund_adjusted_price(self):
        # tax is 25%; invoice rounding off so the amounts are exact and easy to read.
        self.online_auction.invoice_rounding = False
        self.online_auction.save()
        # Lot A: $20, no refund -> item $20.00, tax $5.00. Lot B: $20 with a 50% refund -> item
        # $10.00 (refund-adjusted), tax $2.50 (tax base consistent with the item price).
        invoice = self._buyer_invoice([(20, 0), (20, 50)])
        payload, approval_url = self._order_payload(invoice)
        self.assertEqual(approval_url, "https://www.paypal.com/checkoutnow?token=ORDER-TEST")

        items = payload["purchase_units"][0]["items"]
        self.assertEqual(len(items), 2)
        unit_values = sorted(Decimal(i["unit_amount"]["value"]) for i in items)
        # The refunded lot must bill the refund-adjusted $10.00, not the raw $20.00 winning price.
        self.assertEqual(unit_values, [Decimal("10.00"), Decimal("20.00")])
        refunded_item = min(items, key=lambda i: Decimal(i["unit_amount"]["value"]))
        self.assertEqual(refunded_item["unit_amount"]["value"], "10.00")
        self.assertEqual(refunded_item["tax"]["value"], "2.50")

        breakdown = payload["purchase_units"][0]["amount"]["breakdown"]
        self.assertEqual(breakdown["item_total"]["value"], "30.00")
        self.assertEqual(breakdown["tax_total"]["value"], "7.50")
        self.assertEqual(payload["purchase_units"][0]["amount"]["value"], "37.50")
        self._assert_breakdown_valid(payload)

    def test_no_refund_line_items_unchanged(self):
        self.online_auction.invoice_rounding = False
        self.online_auction.save()
        invoice = self._buyer_invoice([(20, 0), (15, 0)])
        payload, _ = self._order_payload(invoice)

        items = payload["purchase_units"][0]["items"]
        self.assertEqual(len(items), 2)
        # With no refunds the unit_amount equals the raw winning price (unchanged behavior).
        unit_values = sorted(Decimal(i["unit_amount"]["value"]) for i in items)
        self.assertEqual(unit_values, [Decimal("15.00"), Decimal("20.00")])

        breakdown = payload["purchase_units"][0]["amount"]["breakdown"]
        self.assertEqual(breakdown["item_total"]["value"], "35.00")
        self.assertEqual(breakdown["tax_total"]["value"], "8.75")
        self.assertEqual(payload["purchase_units"][0]["amount"]["value"], "43.75")
        self._assert_breakdown_valid(payload)

    def test_fractional_cent_refund_items_sum_matches_item_total(self):
        # A 50% refund on an odd-cent price yields a fractional-cent per-item value. Because
        # item_total is summed from the same quantized per-item values that are sent, the item
        # sum matches item_total exactly and PayPal accepts the order -- no rounding drift.
        self.online_auction.invoice_rounding = False
        self.online_auction.save()
        invoice = self._buyer_invoice([(Decimal("10.03"), 50), (Decimal("10.03"), 50)])
        payload, _ = self._order_payload(invoice)

        # The invariant that PayPal enforces holds regardless of how the DB rounds the aggregate.
        self._assert_breakdown_valid(payload)

        # Each per-item unit_amount is quantized to 2 dp and the breakdown item_total is exactly
        # their sum, so summing the sent line items can never disagree with item_total.
        items = payload["purchase_units"][0]["items"]
        item_sum = sum((Decimal(i["unit_amount"]["value"]) for i in items), Decimal("0.00"))
        self.assertEqual(item_sum, Decimal(payload["purchase_units"][0]["amount"]["breakdown"]["item_total"]["value"]))


class MedianLotValueTests(TestCase):
    """Auction.median_lot_price and the median_value() helper (Item 16).

    Two defects fixed here:
      * median_value() indexed the sorted values with ``int(round(count / 2))``. Python's round()
        uses banker's rounding, so for many counts it selected the wrong element -- e.g.
        round(3 / 2) == 2 picks the *last* of 3 values, round(7 / 2) == 4 picks past the middle of
        7 -- an off-by-one that shifted with the count. Even counts also returned the upper of the
        two middle values instead of their mean.
      * median_lot_price included banned (removed) lots, which are never charged and are excluded
        from every other money stat on the auction.
    """

    def setUp(self):
        self.creator = User.objects.create_user("median_creator", "median@example.com", "pw")
        self.club = Club.objects.create(name="Median Club")
        self._n = 0

    def _auction(self):
        auction = Auction.objects.create(
            created_by=self.creator,
            title="Median Auction",
            is_online=True,
            date_start=datetime.datetime(2026, 3, 15, 12, 0, tzinfo=datetime.timezone.utc),
            date_end=datetime.datetime(2026, 3, 16, 12, 0, tzinfo=datetime.timezone.utc),
            club=self.club,
            winning_bid_percent_to_club=25,
            tax=0,
            lot_entry_fee=0,
            unsold_lot_fee=0,
        )
        PickupLocation.objects.create(name="Median Pickup", auction=auction, pickup_time=timezone.now())
        return auction

    def _tos(self, auction):
        self._n += 1
        return AuctionTOS.objects.create(
            name=f"Person {self._n}",
            auction=auction,
            pickup_location=PickupLocation.objects.filter(auction=auction).first(),
        )

    def _lot(self, auction, seller, price, *, banned=False, sold=True):
        return Lot.objects.create(
            lot_name=f"Lot {price}",
            auction=auction,
            auctiontos_seller=seller,
            winning_price=Decimal(price) if sold else None,
            banned=banned,
            active=False,
            quantity=1,
        )

    def test_odd_count_three_lots_returns_true_middle(self):
        # Prices 10, 20, 30 -> median 20. The old round(3/2)==2 indexed the LAST value (30).
        auction = self._auction()
        seller = self._tos(auction)
        for price in (10, 30, 20):  # insertion order deliberately not sorted
            self._lot(auction, seller, price)
        self.assertEqual(auction.median_lot_price, Decimal(20))

    def test_odd_count_five_lots_returns_true_middle(self):
        # Prices 5, 10, 15, 20, 25 -> median 15 (index 2 of the sorted values).
        auction = self._auction()
        seller = self._tos(auction)
        for price in (25, 5, 20, 10, 15):
            self._lot(auction, seller, price)
        self.assertEqual(auction.median_lot_price, Decimal(15))

    def test_odd_count_seven_lots_returns_true_middle(self):
        # Prices 1..7 -> median 4 (index 3). The old round(7/2)==4 wrongly returned 5.
        auction = self._auction()
        seller = self._tos(auction)
        for price in (7, 6, 5, 4, 3, 2, 1):
            self._lot(auction, seller, price)
        self.assertEqual(auction.median_lot_price, Decimal(4))

    def test_even_count_four_lots_returns_mean_of_two_middle(self):
        # Prices 10, 20, 30, 40 -> mean of the two middle values (20, 30) == 25.
        # This documents the chosen convention: even counts return the mean, not a single element.
        auction = self._auction()
        seller = self._tos(auction)
        for price in (40, 10, 30, 20):
            self._lot(auction, seller, price)
        self.assertEqual(auction.median_lot_price, Decimal(25))

    def test_even_count_mean_can_be_fractional(self):
        # Prices 10, 15 -> mean 12.5, proving the even-count branch averages rather than snapping
        # to one of the two middle values (both old and naive lower/upper medians would give 10 or 15).
        auction = self._auction()
        seller = self._tos(auction)
        for price in (10, 15):
            self._lot(auction, seller, price)
        self.assertEqual(auction.median_lot_price, Decimal("12.5"))

    def test_banned_lots_excluded_from_median(self):
        # Sold non-banned prices 10, 20, 30 -> median 20. A banned lot priced far higher (1000)
        # must not shift the median; if it were counted the sorted set (10,20,30,1000) would move it.
        auction = self._auction()
        seller = self._tos(auction)
        for price in (10, 20, 30):
            self._lot(auction, seller, price)
        self._lot(auction, seller, 1000, banned=True)
        self.assertEqual(auction.median_lot_price, Decimal(20))

    def test_banned_lot_removal_changes_result(self):
        # Guard against a false pass: with the banned lot counted the set (10,20,30,1000) is even and
        # its median would be 25, so excluding it (median 20) is a genuine, observable difference.
        auction = self._auction()
        seller = self._tos(auction)
        for price in (10, 20, 30):
            self._lot(auction, seller, price)
        # Confirm the non-banned median.
        self.assertEqual(auction.median_lot_price, Decimal(20))
        # A banned lot at 1000 does not pull the median toward 25.
        self._lot(auction, seller, 1000, banned=True)
        self.assertEqual(auction.median_lot_price, Decimal(20))

    def test_unsold_lots_ignored(self):
        # Only lots with a winning_price count; an unsold lot (winning_price is NULL) is ignored.
        auction = self._auction()
        seller = self._tos(auction)
        for price in (10, 20, 30):
            self._lot(auction, seller, price)
        self._lot(auction, seller, 0, sold=False)
        self.assertEqual(auction.median_lot_price, Decimal(20))

    def test_empty_auction_returns_zero_without_crashing(self):
        # No sold lots -> the property returns 0 rather than raising (matches the codebase's
        # "no data" convention used by the other stat properties).
        auction = self._auction()
        self._tos(auction)  # a registrant but no sold lots
        self.assertEqual(auction.median_lot_price, 0)

    def test_median_value_helper_raises_on_empty_queryset(self):
        # The helper's documented contract: an empty queryset raises IndexError, which both callers
        # (median_lot_price and the image-stats charts) already guard for.
        from auctions.models import median_value

        with self.assertRaises(IndexError):
            median_value(Lot.objects.none(), "winning_price")


class SellPriceChartBinTests(TestCase):
    """Sell-price distribution histogram: labels and bins must always describe the same buckets
    (Item 20).

    Two defects fixed here:
      * AuctionStatsLotSellPricesJSONView had a *fallback* path (used when Auction.cached_stats has
        no "lot_sell_prices" entry -- e.g. a freshly ended auction whose stats haven't been baked).
        get_labels() and get_data() each rederived the bins with different arithmetic:
        get_labels() used num_bins = (max_price - 1) // 2 and end_bin = start + num_bins * 2, while
        get_data() used num_bins = max_price // 2 and end_bin = max_price - 1. For a $25 top price
        that produced 16 labels against 17 data points, and a bin_size of 1.87 under labels claiming
        width-2 buckets -- so bars were attributed to the wrong price ranges and one bar had no label.
      * The histogram counted banned (removed) lots, while the "Not sold" bar (total_unsold_lots) and
        every other money stat exclude them -- so the priced bars over-counted relative to "Not sold".

    The fix routes both the model (set_stat_lot_sell_prices) and the view fallback through a single
    source of truth (_lot_sell_price_bins), and excludes banned lots from the priced side.
    """

    def setUp(self):
        self.creator = User.objects.create_user("sellprice_creator", "sellprice@example.com", "pw")
        self.club = Club.objects.create(name="Sell Price Club")
        self._n = 0

    def _auction(self):
        auction = Auction.objects.create(
            created_by=self.creator,
            title="Sell Price Auction",
            is_online=True,
            date_start=datetime.datetime(2026, 3, 15, 12, 0, tzinfo=datetime.timezone.utc),
            date_end=datetime.datetime(2026, 3, 16, 12, 0, tzinfo=datetime.timezone.utc),
            club=self.club,
            winning_bid_percent_to_club=25,
            tax=0,
            lot_entry_fee=0,
            unsold_lot_fee=0,
        )
        PickupLocation.objects.create(name="Sell Price Pickup", auction=auction, pickup_time=timezone.now())
        return auction

    def _tos(self, auction):
        self._n += 1
        return AuctionTOS.objects.create(
            name=f"Person {self._n}",
            auction=auction,
            pickup_location=PickupLocation.objects.filter(auction=auction).first(),
        )

    def _lot(self, auction, seller, price, *, banned=False, sold=True):
        return Lot.objects.create(
            lot_name=f"Lot {price}",
            auction=auction,
            auctiontos_seller=seller,
            winning_price=Decimal(price) if sold else None,
            banned=banned,
            active=False,
            quantity=1,
        )

    def _fallback_view(self, auction):
        """A view instance wired for the *fallback* branch (cached_stats is None on a fresh auction)."""
        from auctions.views import AuctionStatsLotSellPricesJSONView

        view = AuctionStatsLotSellPricesJSONView()
        view.auction = auction
        view.compare_auction = None
        return view

    @staticmethod
    def _bar_range(label):
        """Return (lower, upper) integers for a priced label like "$3-5", or None for non-priced bars."""
        nums = re.findall(r"\d+", label)
        if len(nums) < 2:
            return None
        return int(nums[0]), int(nums[1])

    def _assert_price_in_labeled_bar(self, labels, row, price):
        """The bucket holding a single ``price`` (count 1) must be the one whose label range contains it,
        left-inclusive/right-exclusive. Returns nothing; asserts alignment."""
        self.assertEqual(len(labels), len(row), "labels and data must have one entry per bar")
        hit_index = next(i for i, count in enumerate(row) if count == 1)
        rng = self._bar_range(labels[hit_index])
        self.assertIsNotNone(rng, f"price {price} landed on a non-priced bar '{labels[hit_index]}'")
        lower, upper = rng
        self.assertTrue(
            lower <= price < upper,
            f"price {price} counted in bar '{labels[hit_index]}' ({lower}-{upper}), which does not contain it",
        )

    # ------------------------------------------------------------------ length parity

    def test_model_labels_and_data_have_equal_length(self):
        # The model's stat dict must have exactly one label per data point across a range of top prices,
        # including the small-price branch (bin_width collapses to 1) and the 30-bin cap.
        for prices in ([25], [5, 7, 9], [3], [1000], list(range(1, 40))):
            auction = self._auction()
            seller = self._tos(auction)
            for price in prices:
                self._lot(auction, seller, price)
            stats = auction.set_stat_lot_sell_prices()
            self.assertEqual(
                len(stats["labels"]),
                len(stats["data"][0]),
                f"length mismatch for prices {prices}",
            )

    def test_view_fallback_labels_and_data_have_equal_length(self):
        # The regression: for a $25 top price the old fallback produced 16 labels vs 17 data points.
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, 25)
        view = self._fallback_view(auction)
        self.assertIsNone(auction.cached_stats)  # confirm we are exercising the fallback branch
        labels = view.get_labels()
        row = view.get_data()[0]
        self.assertEqual(len(labels), len(row))

    # ------------------------------------------------------------------ bucket alignment

    def test_fallback_priced_lot_lands_in_labeled_bar(self):
        # Fallback path: a lot priced X is counted in the bar whose label range contains X.
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, 25)
        view = self._fallback_view(auction)
        self.assertIsNone(auction.cached_stats)
        self._assert_price_in_labeled_bar(view.get_labels(), view.get_data()[0], 25)

    def test_cached_priced_lot_lands_in_labeled_bar(self):
        # Non-fallback path: the view reads a baked cached_stats blob; same alignment property holds.
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, 25)
        auction.cached_stats = {"lot_sell_prices": auction.set_stat_lot_sell_prices()}
        auction.save()
        view = self._fallback_view(auction)
        self._assert_price_in_labeled_bar(view.get_labels(), view.get_data()[0], 25)

    def test_view_fallback_matches_cached_stats(self):
        # Both view branches (fallback recompute vs reading cached_stats) must agree bar-for-bar,
        # since they now share one source of truth.
        auction = self._auction()
        seller = self._tos(auction)
        for price in (5, 12, 25, 500):
            self._lot(auction, seller, price)
        fallback = self._fallback_view(auction)
        fallback_labels = fallback.get_labels()
        fallback_data = fallback.get_data()

        auction.cached_stats = {"lot_sell_prices": auction.set_stat_lot_sell_prices()}
        auction.save()
        cached = self._fallback_view(auction)
        self.assertEqual(fallback_labels, cached.get_labels())
        self.assertEqual(fallback_data, cached.get_data())

    def test_boundary_value_goes_to_upper_bin(self):
        # Documented convention: buckets are left-inclusive/right-exclusive. With width-2 bins
        # (1-3, 3-5, ...), a lot priced exactly 3 belongs to "3-5", never "1-3". A $25 lot forces
        # bin_width to stay 2 (otherwise small tops collapse to width 1).
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, 3)
        self._lot(auction, seller, 25)
        stats = auction.set_stat_lot_sell_prices()
        labels, row = stats["labels"], stats["data"][0]
        lower_index = next(i for i, lbl in enumerate(labels) if self._bar_range(lbl) == (1, 3))
        upper_index = next(i for i, lbl in enumerate(labels) if self._bar_range(lbl) == (3, 5))
        self.assertEqual(row[lower_index], 0, "price 3 must not fall in the 1-3 bucket")
        self.assertEqual(row[upper_index], 1, "price 3 must fall in the 3-5 bucket")

    def test_top_of_range_value_not_silently_dropped(self):
        # A price above the last labeled bucket lands in the "{end_bin}+" overflow bar rather than
        # vanishing. With a $1000 top the range caps at 30 width-2 bins (end_bin 61), so 1000 overflows.
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, 5)
        self._lot(auction, seller, 1000)
        stats = auction.set_stat_lot_sell_prices()
        labels, row = stats["labels"], stats["data"][0]
        self.assertTrue(labels[-1].endswith("+"), "last bar should be the high-overflow bucket")
        self.assertGreaterEqual(row[-1], 1, "the $1000 lot must be counted in the overflow bar")
        # Every sold lot is accounted for somewhere in the priced bars + overflow (index 1..end),
        # i.e. none were silently discarded.
        self.assertEqual(sum(row[1:]), 2)

    # ------------------------------------------------------------------ banned exclusion

    def test_banned_lots_excluded_from_priced_bars(self):
        # A banned (removed) sold lot must not be counted in any priced bar -- consistent with the
        # "Not sold" bar and every other money stat. With exclusion the single non-banned $5 lot is
        # the only thing counted; if banned lots leaked in, the $5 bucket would be 2.
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, 5)
        self._lot(auction, seller, 5, banned=True)
        stats = auction.set_stat_lot_sell_prices()
        row = stats["data"][0]
        self.assertEqual(sum(row[1:]), 1, "banned sold lot must not be counted among priced bars")

    def test_banned_unsold_lot_excluded_from_not_sold_bar(self):
        # The "Not sold" bar (index 0) must exclude banned unsold lots too, so both sides of the chart
        # treat removed lots the same way.
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, 0, sold=False)  # a genuine unsold lot
        self._lot(auction, seller, 0, sold=False, banned=True)  # removed, must not count
        stats = auction.set_stat_lot_sell_prices()
        self.assertEqual(stats["labels"][0], "Not sold")
        self.assertEqual(stats["data"][0][0], 1)

    def test_banned_exclusion_is_observable(self):
        # Guard against a false pass: prove the banned lot would change the result if counted, by
        # confirming the non-banned-only total, then adding a banned lot in the same bucket and
        # showing the total does not move.
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, 25)
        before = sum(auction.set_stat_lot_sell_prices()["data"][0][1:])
        self.assertEqual(before, 1)
        self._lot(auction, seller, 25, banned=True)
        after = sum(auction.set_stat_lot_sell_prices()["data"][0][1:])
        self.assertEqual(after, 1, "adding a banned lot must not change the priced-bar totals")


class ParticipantCountTests(TestCase):
    """Auction buyer/seller/participant counting properties (Item 18).

    ``number_of_sellers``, ``number_of_buyers``, ``number_of_sellers_who_didnt_buy`` and
    ``number_of_participants`` were computed with reverse joins from AuctionTOS through Lot that did
    not exclude removed (``banned``) or soft-deleted (``is_deleted``) lots -- so a person whose only
    lot was pulled from the sale still counted as a seller, and someone whose only won lot was
    removed still counted as a buyer. A "seller" now needs at least one non-banned, non-deleted lot;
    a "buyer" needs to have won (``winning_price`` set) at least one non-banned, non-deleted lot.
    ``.distinct()`` keeps people with several lots from being counted more than once.
    """

    def setUp(self):
        self.creator = User.objects.create_user("participant_creator", "participant@example.com", "pw")
        self.club = Club.objects.create(name="Participant Club")
        self._n = 0

    def _auction(self):
        auction = Auction.objects.create(
            created_by=self.creator,
            title="Participant Auction",
            is_online=True,
            date_start=datetime.datetime(2026, 3, 15, 12, 0, tzinfo=datetime.timezone.utc),
            date_end=datetime.datetime(2026, 3, 16, 12, 0, tzinfo=datetime.timezone.utc),
            club=self.club,
        )
        PickupLocation.objects.create(name="Participant Pickup", auction=auction, pickup_time=timezone.now())
        return auction

    def _tos(self, auction):
        self._n += 1
        return AuctionTOS.objects.create(
            name=f"Person {self._n}",
            auction=auction,
            pickup_location=PickupLocation.objects.filter(auction=auction).first(),
        )

    def _lot(self, auction, seller, *, winner=None, price=None, banned=False, is_deleted=False):
        return Lot.objects.create(
            lot_name=f"Lot {self._n}",
            auction=auction,
            auctiontos_seller=seller,
            auctiontos_winner=winner,
            winning_price=Decimal(price) if price is not None else None,
            banned=banned,
            is_deleted=is_deleted,
            active=False,
            quantity=1,
        )

    def test_seller_with_only_banned_lot_not_counted(self):
        # A person whose single lot was removed (banned) is not a seller.
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, banned=True)
        self.assertEqual(auction.number_of_sellers, 0)
        self.assertEqual(auction.number_of_participants, 0)

    def test_seller_with_only_deleted_lot_not_counted(self):
        # A soft-deleted lot does not make its owner a seller either.
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, is_deleted=True)
        self.assertEqual(auction.number_of_sellers, 0)
        self.assertEqual(auction.number_of_participants, 0)

    def test_seller_with_banned_and_live_lot_counted_once(self):
        # One banned + one live lot -> still exactly one seller (the live lot qualifies, distinct).
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, banned=True)
        self._lot(auction, seller, price=10)
        self.assertEqual(auction.number_of_sellers, 1)

    def test_multiple_lots_same_seller_counted_once(self):
        # Three live lots by one person -> counted once (reverse join must be distinct).
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, price=5)
        self._lot(auction, seller, price=10)
        self._lot(auction, seller, price=15)
        self.assertEqual(auction.number_of_sellers, 1)

    def test_buyer_with_only_banned_won_lot_not_counted(self):
        # Winning a lot that was later removed (banned) does not make you a buyer.
        auction = self._auction()
        seller = self._tos(auction)
        buyer = self._tos(auction)
        self._lot(auction, seller, winner=buyer, price=10, banned=True)
        self.assertEqual(auction.number_of_buyers, 0)
        # The seller still has no live lot, so nobody participated.
        self.assertEqual(auction.number_of_participants, 0)

    def test_buyer_with_only_deleted_won_lot_not_counted(self):
        auction = self._auction()
        seller = self._tos(auction)
        buyer = self._tos(auction)
        self._lot(auction, seller, winner=buyer, price=10, is_deleted=True)
        self.assertEqual(auction.number_of_buyers, 0)

    def test_buyer_without_winning_price_not_counted(self):
        # auctiontos_winner set but no winning_price (e.g. an unsold lot) is not a buyer.
        auction = self._auction()
        seller = self._tos(auction)
        buyer = self._tos(auction)
        self._lot(auction, seller, winner=buyer, price=None)
        self.assertEqual(auction.number_of_buyers, 0)

    def test_multiple_won_lots_same_buyer_counted_once(self):
        auction = self._auction()
        seller = self._tos(auction)
        buyer = self._tos(auction)
        self._lot(auction, seller, winner=buyer, price=10)
        self._lot(auction, seller, winner=buyer, price=20)
        self.assertEqual(auction.number_of_buyers, 1)

    def test_known_scenario_two_sellers_one_buyer(self):
        # seller1 sells a lot won by buyer1; seller2 has an unsold (but live) lot; buyer1 only buys.
        #   sellers: seller1, seller2                       -> 2
        #   buyers: buyer1                                  -> 1
        #   sellers who didn't buy: seller1, seller2        -> 2
        #   participants (union of buyers+sellers): 3
        auction = self._auction()
        seller1 = self._tos(auction)
        seller2 = self._tos(auction)
        buyer1 = self._tos(auction)
        self._lot(auction, seller1, winner=buyer1, price=10)
        self._lot(auction, seller2, price=None)  # unsold but live
        self.assertEqual(auction.number_of_sellers, 2)
        self.assertEqual(auction.number_of_buyers, 1)
        self.assertEqual(auction.number_of_sellers_who_didnt_buy, 2)
        self.assertEqual(auction.number_of_participants, 3)

    def test_person_who_buys_and_sells_counted_once_in_participants(self):
        # A person who both sells a live lot and wins a live lot is one participant, and is excluded
        # from sellers_who_didnt_buy.
        auction = self._auction()
        both = self._tos(auction)
        other = self._tos(auction)
        self._lot(auction, both, price=10)  # `both` sells a live lot
        self._lot(auction, other, winner=both, price=20)  # `both` also wins a live lot from `other`
        self.assertEqual(auction.number_of_sellers, 2)  # both, other
        self.assertEqual(auction.number_of_buyers, 1)  # both
        self.assertEqual(auction.number_of_sellers_who_didnt_buy, 1)  # other only
        self.assertEqual(auction.number_of_participants, 2)  # both, other (no double count)

    def test_banned_lots_removed_from_scenario_counts(self):
        # Regression for the reported defect: seller2's only lot and buyer1's only won lot are both
        # removed, so the counts drop them even though the reverse joins would otherwise include them.
        auction = self._auction()
        seller1 = self._tos(auction)
        seller2 = self._tos(auction)
        buyer1 = self._tos(auction)
        buyer2 = self._tos(auction)
        # seller1 -> live sale to buyer2 (both stay)
        self._lot(auction, seller1, winner=buyer2, price=10)
        # seller2's only lot is removed -> seller2 drops
        self._lot(auction, seller2, banned=True)
        # buyer1's only won lot is removed -> buyer1 drops (seller of it is seller1, already counted)
        self._lot(auction, seller1, winner=buyer1, price=15, banned=True)
        self.assertEqual(auction.number_of_sellers, 1)  # seller1 only
        self.assertEqual(auction.number_of_buyers, 1)  # buyer2 only
        self.assertEqual(auction.number_of_participants, 2)  # seller1, buyer2


class ViewsAndWinnersStatsTests(TestCase):
    """Auction unique-view counting and total_winners (Item 19).

    Two verified defects:

    * ``total_unique_views`` double-counted the anonymous -> logged-in transition. PageView rows
      store identity two ways (see the page-view tracking view): a logged-in visit stores
      ``user=<id>`` / ``session_id=NULL``, an anonymous visit stores ``user=NULL`` /
      ``session_id=<key>``. The old code did ``distinct(session_id) + distinct(user)``, which
      counted a person who browsed anonymously and then logged in twice and, worse, added a bogus
      NULL bucket to each side (every logged-in row has a NULL session; every anonymous row has a
      NULL user). ``Auction.unique_views`` now counts distinct users plus the anonymous sessions
      that never appear alongside a user.

    * ``total_winners`` joined ``User`` through the ``Lot.winner`` User FK, silently dropping
      admin-declared winners (check-in / set-lot-winner set ``auctiontos_winner`` + winning_price
      but never the winner User FK) and winners with no user account. It now counts via
      ``buyer_tos_qs`` (winning AuctionTOS), which captures every sold, live lot's winner.
    """

    def setUp(self):
        self.creator = User.objects.create_user("stats19_creator", "stats19@example.com", "pw")
        self._n = 0

    def _auction(self):
        auction = Auction.objects.create(
            created_by=self.creator,
            title="Stats19 Auction",
            is_online=True,
            date_start=datetime.datetime(2026, 3, 15, 12, 0, tzinfo=datetime.timezone.utc),
            date_end=datetime.datetime(2026, 3, 16, 12, 0, tzinfo=datetime.timezone.utc),
        )
        PickupLocation.objects.create(name="Stats19 Pickup", auction=auction, pickup_time=timezone.now())
        return auction

    def _tos(self, auction, user=None):
        self._n += 1
        return AuctionTOS.objects.create(
            name=f"Person {self._n}",
            user=user,
            auction=auction,
            pickup_location=PickupLocation.objects.filter(auction=auction).first(),
        )

    def _lot(self, auction, seller, *, winner=None, winner_user=None, price=None):
        self._n += 1
        return Lot.objects.create(
            lot_name=f"Lot {self._n}",
            auction=auction,
            auctiontos_seller=seller,
            auctiontos_winner=winner,
            winner=winner_user,
            winning_price=Decimal(price) if price is not None else None,
            active=False,
            quantity=1,
        )

    def _view(self, auction, *, user=None, session_id=None):
        # A page view of the auction's rules page (auction set) -- counted by unique_views.
        return PageView.objects.create(auction=auction, user=user, session_id=session_id)

    # ---- Bug A: unique view de-duplication -------------------------------------------------

    def test_anonymous_then_logged_in_same_session_counted_once(self):
        # One person: browsed anonymously (session "sessA"), then logged in on the same session.
        # When the session key is carried onto the logged-in row it maps to a user, so the person
        # is counted once (via their user), not once per identity.
        auction = self._auction()
        user = User.objects.create_user("stats19_visitor", "v@example.com", "pw")
        self._view(auction, user=None, session_id="sessA")
        self._view(auction, user=user, session_id="sessA")
        result = auction.unique_views
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["logged_in"], 1)
        self.assertEqual(result["anonymous"], 0)

    def test_two_different_visitors_counted_twice(self):
        # A logged-in visitor and a genuinely different anonymous visitor -> 2.
        # (The old distinct-session + distinct-user formula returned 4 here: the anon row's NULL
        # user and the logged-in row's NULL session each added a spurious bucket.)
        auction = self._auction()
        user = User.objects.create_user("stats19_loggedin", "li@example.com", "pw")
        self._view(auction, user=user, session_id=None)
        self._view(auction, user=None, session_id="anonB")
        result = auction.unique_views
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["logged_in"], 1)
        self.assertEqual(result["anonymous"], 1)

    def test_two_distinct_anonymous_sessions_counted_twice(self):
        auction = self._auction()
        self._view(auction, user=None, session_id="s1")
        self._view(auction, user=None, session_id="s1")  # same visitor, second page
        self._view(auction, user=None, session_id="s2")
        self.assertEqual(auction.unique_views["total"], 2)

    def test_repeat_logged_in_views_counted_once(self):
        auction = self._auction()
        user = User.objects.create_user("stats19_repeat", "r@example.com", "pw")
        self._view(auction, user=user, session_id=None)
        self._view(auction, user=user, session_id=None)
        self._view(auction, user=user, session_id=None)
        self.assertEqual(auction.unique_views["total"], 1)

    def test_lot_page_views_are_counted(self):
        # unique_views spans both auction-rules views and views of the auction's lots.
        auction = self._auction()
        seller = self._tos(auction)
        lot = self._lot(auction, seller, price=10)
        user = User.objects.create_user("stats19_lotviewer", "lv@example.com", "pw")
        PageView.objects.create(lot_number=lot, user=user, session_id=None)
        PageView.objects.create(lot_number=lot, user=None, session_id="lotsess")
        self.assertEqual(auction.unique_views["total"], 2)

    # ---- Bug B: total_winners counts admin-declared winners --------------------------------

    def test_admin_declared_winner_counted(self):
        # auctiontos_winner + winning_price set, but no winner User FK and no Bid rows -- the
        # shape produced by check-in / the set-lot-winner form. The old winner__auction join
        # returned 0; buyer_tos_qs (and therefore total_winners) must count it.
        auction = self._auction()
        seller = self._tos(auction)
        buyer = self._tos(auction, user=None)  # in-person buyer, no account
        self._lot(auction, seller, winner=buyer, winner_user=None, price=10)
        # Old (buggy) path would have returned 0:
        self.assertEqual(User.objects.filter(winner__auction=auction).distinct().count(), 0)
        self.assertEqual(auction.buyer_tos_qs.count(), 1)
        self.assertEqual(auction.set_stat_misc()["total_winners"], 1)

    def test_bid_flow_winner_still_counted(self):
        # Normal online flow sets both winner (User FK) and auctiontos_winner.
        auction = self._auction()
        seller = self._tos(auction)
        buyer_user = User.objects.create_user("stats19_bidwinner", "bw@example.com", "pw")
        buyer = self._tos(auction, user=buyer_user)
        self._lot(auction, seller, winner=buyer, winner_user=buyer_user, price=20)
        self.assertEqual(auction.set_stat_misc()["total_winners"], 1)

    def test_admin_and_bid_winners_together_not_double_counted(self):
        # Same person wins two lots: one via the bid flow (winner User FK + auctiontos_winner) and
        # one admin-declared (auctiontos_winner only). Distinct on the winning AuctionTOS -> 1.
        # A second, admin-only winner brings the total to 2.
        auction = self._auction()
        seller = self._tos(auction)
        person_user = User.objects.create_user("stats19_both", "both@example.com", "pw")
        person = self._tos(auction, user=person_user)
        self._lot(auction, seller, winner=person, winner_user=person_user, price=10)  # bid flow
        self._lot(auction, seller, winner=person, winner_user=None, price=15)  # admin declared
        other = self._tos(auction, user=None)
        self._lot(auction, seller, winner=other, winner_user=None, price=5)  # admin declared
        self.assertEqual(auction.set_stat_misc()["total_winners"], 2)


class PercentUnsoldLotsTests(TestCase):
    """Auction.percent_unsold_lots zero-lot guard (Item 21).

    ``percent_unsold_lots`` divided ``total_unsold_lots / total_lots`` inside a bare
    ``try/except`` that returned 100 on any error. For an auction with zero lots the division
    raised ZeroDivisionError and the property reported 100% unsold -- nonsensical, since a
    lotless auction has nothing unsold. It now returns 0 for an empty auction, matching the
    sibling percent properties on this model (reminder_email_clicks/_joins,
    weekly_promo_email_click_rate), which all return 0 on an empty base.
    """

    def setUp(self):
        self.creator = User.objects.create_user("pct_unsold_creator", "pctunsold@example.com", "pw")
        self._n = 0

    def _auction(self):
        auction = Auction.objects.create(
            created_by=self.creator,
            title="Percent Unsold Auction",
            is_online=True,
            date_start=datetime.datetime(2026, 3, 15, 12, 0, tzinfo=datetime.timezone.utc),
            date_end=datetime.datetime(2026, 3, 16, 12, 0, tzinfo=datetime.timezone.utc),
        )
        PickupLocation.objects.create(name="Percent Unsold Pickup", auction=auction, pickup_time=timezone.now())
        return auction

    def _tos(self, auction):
        self._n += 1
        return AuctionTOS.objects.create(
            name=f"Person {self._n}",
            auction=auction,
            pickup_location=PickupLocation.objects.filter(auction=auction).first(),
        )

    def _lot(self, auction, seller, *, price=None):
        self._n += 1
        return Lot.objects.create(
            lot_name=f"Lot {self._n}",
            auction=auction,
            auctiontos_seller=seller,
            winning_price=Decimal(price) if price is not None else None,
            active=False,
            quantity=1,
        )

    def test_zero_lots_reports_zero_not_hundred(self):
        # The reported defect: an auction with no lots must not report 100% unsold.
        auction = self._auction()
        self.assertEqual(auction.total_lots, 0)
        self.assertEqual(auction.percent_unsold_lots, 0, "A lotless auction has nothing unsold -> 0%")

    def test_all_lots_unsold_reports_hundred(self):
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, price=None)
        self._lot(auction, seller, price=None)
        self.assertEqual(auction.percent_unsold_lots, 100)

    def test_half_unsold_reports_fifty(self):
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, price=10)  # sold
        self._lot(auction, seller, price=None)  # unsold
        self.assertEqual(auction.percent_unsold_lots, 50)

    def test_all_lots_sold_reports_zero(self):
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, price=10)
        self._lot(auction, seller, price=20)
        self.assertEqual(auction.percent_unsold_lots, 0)


class PayPalInvoiceChunkTests(TestCase):
    """PayPal bulk-invoice CSV chunking agreement (Item 21).

    ``Auction.paypal_invoice_chunks`` counted only invoices with ``calculated_total < 0`` while
    the export loop advanced its per-row counter for every ``not user_should_be_paid`` invoice --
    which includes settled ($0) invoices (net not > 0 => user_should_be_paid False). With >150
    invoices the loop's counter therefore ran ahead of the chunk count the UI offered, and tail
    invoices could be assigned a chunk number the dropdown never listed, silently dropping them
    from every export.

    Both sides now derive from ``Auction.paypal_invoices_to_export`` -- the UNPAID invoices whose
    rounded balance still owes the club (``rounded_net_after_payments < 0``), i.e. exactly the
    invoices that get written -- so the counter and the offered chunks always agree.
    """

    def setUp(self):
        self.creator = User.objects.create_user("paypal_chunk_creator", "paypalchunk@example.com", "pw")
        self.club = Club.objects.create(name="PayPal Chunk Club")
        self._n = 0

    def _auction(self):
        auction = Auction.objects.create(
            created_by=self.creator,
            title="PayPal Chunk Auction",
            is_online=True,
            date_start=datetime.datetime(2026, 3, 15, 12, 0, tzinfo=datetime.timezone.utc),
            date_end=datetime.datetime(2026, 3, 16, 12, 0, tzinfo=datetime.timezone.utc),
            club=self.club,
        )
        PickupLocation.objects.create(name="PayPal Chunk Pickup", auction=auction, pickup_time=timezone.now())
        return auction

    def _tos(self, auction):
        self._n += 1
        return AuctionTOS.objects.create(
            name=f"Person {self._n}",
            email=f"person{self._n}@example.com",
            auction=auction,
            pickup_location=PickupLocation.objects.filter(auction=auction).first(),
        )

    def _owing_invoice(self, auction, *, price=10):
        # A buyer who bought a lot owes the club -> negative net, negative rounded balance.
        seller = self._tos(auction)
        buyer = self._tos(auction)
        Lot.objects.create(
            lot_name=f"Lot {self._n}",
            auction=auction,
            auctiontos_seller=seller,
            auctiontos_winner=buyer,
            winning_price=Decimal(price),
            active=False,
            quantity=1,
        )
        return Invoice.objects.create(auctiontos_user=buyer, auction=auction, status="UNPAID")

    def _settled_invoice(self, auction):
        # A bidder with no lots at all: net == 0, so user_should_be_paid is False (the old counter
        # would have advanced on it) but it owes nothing and must not consume a chunk slot.
        user = self._tos(auction)
        return Invoice.objects.create(auctiontos_user=user, auction=auction, status="UNPAID")

    def test_settled_invoice_excluded_from_export_set(self):
        auction = self._auction()
        owing = self._owing_invoice(auction)
        settled = self._settled_invoice(auction)
        export_pks = {inv.pk for inv in auction.paypal_invoices_to_export}
        self.assertIn(owing.pk, export_pks, "An invoice that owes the club is billed")
        self.assertNotIn(settled.pk, export_pks, "A settled $0 invoice is not billed")
        # Document the old defect: the settled invoice would have advanced the old counter.
        self.assertFalse(settled.user_should_be_paid)
        self.assertEqual(settled.rounded_net_after_payments, 0)

    def test_chunk_count_matches_export_set_not_counter_drift(self):
        auction = self._auction()
        for _ in range(3):
            self._owing_invoice(auction)
        for _ in range(4):
            self._settled_invoice(auction)
        export = auction.paypal_invoices_to_export
        self.assertEqual(len(export), 3, "Only the 3 owing invoices are billable")
        # The chunk count is derived from the export set (3 invoices -> a single chunk).
        self.assertEqual(auction.paypal_invoice_chunks, [1])
        # The old counter advanced on every non-payout invoice (owing + settled), so it counted
        # more invoices than the chunk math offered -- the drift that dropped tail invoices.
        old_counter_set = [inv for inv in auction.paypal_invoices if not inv.user_should_be_paid]
        self.assertGreater(
            len(old_counter_set),
            len(export),
            "Old counter advanced on more invoices than were billable (the tail-drop drift)",
        )

    def test_every_exported_invoice_lands_in_an_offered_chunk(self):
        # The core invariant the fix guarantees: iterating the export set with the same 150-per-chunk
        # math the loop uses, every invoice's chunk number is one the UI offers.
        auction = self._auction()
        for _ in range(5):
            self._owing_invoice(auction)
        offered = set(auction.paypal_invoice_chunks)
        chunk_size = 150
        for index, _invoice in enumerate(auction.paypal_invoices_to_export):
            count = index + 1
            chunk = (count - 1) // chunk_size + 1
            self.assertIn(chunk, offered, "Every billed invoice must land in a chunk the UI lists")


class InvoiceSummaryWordingTests(TestCase):
    """Invoice.invoice_summary_short zero-balance wording + user_should_be_paid semantics (Item 21).

    A fully settled ($0) invoice fell into the ``else`` branch and read "owes the club $0.00".
    The zero case now reads "is settled up". The non-zero wording is unchanged. This also pins
    ``user_should_be_paid`` (whose docstring had the sense inverted): it is True only when the
    club owes the user (positive net), False when the user owes the club or is settled.
    """

    def setUp(self):
        self.creator = User.objects.create_user("summary_creator", "summary@example.com", "pw")
        self._n = 0

    def _auction(self, *, invoice_rounding=True):
        auction = Auction.objects.create(
            created_by=self.creator,
            title="Summary Auction",
            is_online=True,
            date_start=datetime.datetime(2026, 3, 15, 12, 0, tzinfo=datetime.timezone.utc),
            date_end=datetime.datetime(2026, 3, 16, 12, 0, tzinfo=datetime.timezone.utc),
            invoice_rounding=invoice_rounding,
        )
        PickupLocation.objects.create(name="Summary Pickup", auction=auction, pickup_time=timezone.now())
        return auction

    def _tos(self, auction, name):
        return AuctionTOS.objects.create(
            name=name,
            auction=auction,
            pickup_location=PickupLocation.objects.filter(auction=auction).first(),
        )

    def _bought_lot(self, auction, buyer, price):
        self._n += 1
        seller = self._tos(auction, f"Seller {self._n}")
        return Lot.objects.create(
            lot_name=f"Lot {self._n}",
            auction=auction,
            auctiontos_seller=seller,
            auctiontos_winner=buyer,
            winning_price=Decimal(price),
            active=False,
            quantity=1,
        )

    def test_settled_invoice_reads_settled_up_not_owes_zero(self):
        auction = self._auction()
        user = self._tos(auction, "Alice")
        invoice = Invoice.objects.create(auctiontos_user=user, auction=auction, status="UNPAID")
        self.assertEqual(invoice.net, 0)
        self.assertEqual(invoice.invoice_summary_short, "is settled up")
        self.assertNotIn("owes the club", invoice.invoice_summary)
        self.assertEqual(invoice.invoice_summary, "Alice is settled up")

    def test_owing_invoice_reads_owes_the_club(self):
        auction = self._auction()
        buyer = self._tos(auction, "Bob")
        self._bought_lot(auction, buyer, price=10)
        invoice = Invoice.objects.create(auctiontos_user=buyer, auction=auction, status="UNPAID")
        self.assertEqual(invoice.invoice_summary_short, "owes the club $10.00")
        self.assertFalse(invoice.user_should_be_paid)

    def test_payout_invoice_reads_needs_to_be_paid(self):
        auction = self._auction()
        user = self._tos(auction, "Carol")
        invoice = Invoice.objects.create(auctiontos_user=user, auction=auction, status="UNPAID")
        # A flat DISCOUNT with nothing else on the invoice makes the net positive: the club owes them.
        InvoiceAdjustment.objects.create(invoice=invoice, adjustment_type="DISCOUNT", amount=5)
        self.assertGreater(invoice.net, 0)
        self.assertTrue(invoice.user_should_be_paid, "Positive net means the club owes the user")
        self.assertEqual(invoice.invoice_summary_short, "needs to be paid $5.00")


class LedgerPercentAdjustmentBaseTests(TestCase):
    """Ledger percent-adjustment base matches net's base (Item 21).

    ``net`` applies a legacy percent adjustment to the running base
    ``subtotal + first_bid_payout + club_member_discount + flat_adjustments``, but the club-ledger
    booking (``sync_club_money``) applied the percent to the bare ``subtotal`` only. The gap between
    the two bases was silently swept into the ledger's ``rounding`` category, so ``rounding`` held
    far more than genuine sub-cent rounding. Both sides now read
    ``Invoice.manual_adjustment_amount``, so the ledger books the exact figure ``net`` uses and
    ``rounding`` only ever holds true whole-dollar rounding.
    """

    def setUp(self):
        self.creator = User.objects.create_user("ledger_pct_creator", "ledgerpct@example.com", "pw")
        self.club = Club.objects.create(name="Ledger Pct Club")
        self._n = 0

    def _auction(self, *, first_bid_payout=0):
        auction = Auction.objects.create(
            created_by=self.creator,
            title="Ledger Pct Auction",
            is_online=True,
            date_start=datetime.datetime(2026, 3, 15, 12, 0, tzinfo=datetime.timezone.utc),
            date_end=datetime.datetime(2026, 3, 16, 12, 0, tzinfo=datetime.timezone.utc),
            club=self.club,
            first_bid_payout=first_bid_payout,
            tax=0,
        )
        PickupLocation.objects.create(name="Ledger Pct Pickup", auction=auction, pickup_time=timezone.now())
        return auction

    def _tos(self, auction, name):
        return AuctionTOS.objects.create(
            name=name,
            auction=auction,
            pickup_location=PickupLocation.objects.filter(auction=auction).first(),
        )

    def _buyer_invoice(self, auction, *, price):
        seller = self._tos(auction, "Seller")
        buyer = self._tos(auction, "Buyer")
        Lot.objects.create(
            lot_name="Ledger Lot",
            auction=auction,
            auctiontos_seller=seller,
            auctiontos_winner=buyer,
            winning_price=Decimal(price),
            active=False,
            quantity=1,
        )
        return Invoice.objects.create(auctiontos_user=buyer, auction=auction, status="UNPAID")

    def _booked(self, invoice):
        """Return {category: summed amount} for the invoice's ledger rows."""
        booked = {}
        for row in ClubMoney.objects.filter(invoice=invoice):
            booked[row.category] = booked.get(row.category, Decimal("0.00")) + row.amount
        return booked

    def test_percent_adjustment_booked_on_net_base_whole_dollar(self):
        # subtotal = -100, first_bid = +10, flat = -20 (an ADD of 20), percent = 20%.
        # net's percent base = -100 + 10 - 20 = -110; manual_adjustment_amount = -20 + (-110*0.2) = -42.
        # net = -100 + 10 - 42 = -132 (a whole dollar, so genuine rounding is 0).
        # The OLD ledger applied 20% to the bare subtotal (-100) -> adjustment 40, dumping the
        # remaining $2 into rounding. The fix books adjustment 42 and rounding 0.
        auction = self._auction(first_bid_payout=10)
        invoice = self._buyer_invoice(auction, price=100)
        InvoiceAdjustment.objects.create(invoice=invoice, adjustment_type="ADD", amount=20)
        InvoiceAdjustment.objects.create(invoice=invoice, adjustment_type="ADD_PERCENT", amount=20)

        self.assertEqual(invoice.manual_adjustment_amount, Decimal(-42))
        self.assertEqual(invoice.net, Decimal(-132))
        self.assertEqual(invoice.rounded_net, Decimal(-132))

        invoice.status = "PAID"
        invoice.save()
        booked = self._booked(invoice)

        self.assertEqual(
            booked[ClubMoney.CATEGORY_INVOICE_ADJUSTMENT],
            Decimal("42.00"),
            "Adjustment booked on net's base (subtotal+first_bid+flat), not the bare subtotal",
        )
        # The old subtotal-only base would have booked 40.00 here.
        self.assertNotEqual(booked[ClubMoney.CATEGORY_INVOICE_ADJUSTMENT], Decimal("40.00"))
        # No phantom rounding: net is a whole dollar, so rounding is exactly 0 (old code: $2).
        self.assertEqual(booked.get(ClubMoney.CATEGORY_ROUNDING, Decimal("0.00")), Decimal("0.00"))
        # The whole ledger reconciles to the rounded invoice total.
        self.assertEqual(sum(booked.values()), -invoice.rounded_net)

    def test_rounding_only_holds_subcent_after_fix(self):
        # subtotal = -100, first_bid = +10, flat = -21 (ADD 21), percent = 20%.
        # base = -111; manual_adjustment_amount = -21 + (-111*0.2) = -43.2; net = -133.2 -> rounded -133.
        # Genuine rounding is only $0.20. The OLD base-mismatch would have inflated rounding to ~$2.
        auction = self._auction(first_bid_payout=10)
        invoice = self._buyer_invoice(auction, price=100)
        InvoiceAdjustment.objects.create(invoice=invoice, adjustment_type="ADD", amount=21)
        InvoiceAdjustment.objects.create(invoice=invoice, adjustment_type="ADD_PERCENT", amount=20)

        self.assertEqual(invoice.manual_adjustment_amount, Decimal("-43.2"))
        self.assertEqual(invoice.net, Decimal("-133.2"))
        self.assertEqual(invoice.rounded_net, Decimal(-133))

        invoice.status = "PAID"
        invoice.save()
        booked = self._booked(invoice)

        self.assertEqual(booked[ClubMoney.CATEGORY_INVOICE_ADJUSTMENT], Decimal("43.20"))
        rounding = booked.get(ClubMoney.CATEGORY_ROUNDING, Decimal("0.00"))
        self.assertEqual(rounding, Decimal("-0.20"))
        self.assertLess(abs(rounding), Decimal("1.00"), "Rounding only ever holds genuine sub-dollar rounding")
        self.assertEqual(sum(booked.values()), -invoice.rounded_net)


class StatsBannedExclusionReviewTests(TestCase):
    """Follow-up stats-review fixes: several stats read over a lot set that still included banned
    (removed) or soft-deleted lots, unlike gross/median_lot_price and every other money figure.

      * set_stat_images ("importance of images on sell price" chart) counted banned AND soft-deleted
        sold lots, so a single removed lot could multiply the displayed median/average sell price.
      * total_donations counted banned donation lots.
      * set_stat_auctioneer_speed / set_stat_attrition plotted banned lots as extra scatter points
        (both share the identical one-line exclude fix; the auctioneer-speed point count is asserted
        here as the representative case).
    """

    def setUp(self):
        self.creator = User.objects.create_user("bannedstats_creator", "bannedstats@example.com", "pw")
        self.club = Club.objects.create(name="Banned Stats Club")
        self._n = 0

    def _auction(self):
        auction = Auction.objects.create(
            created_by=self.creator,
            title="Banned Stats Auction",
            is_online=True,
            date_start=datetime.datetime(2026, 3, 15, 12, 0, tzinfo=datetime.timezone.utc),
            date_end=datetime.datetime(2026, 3, 16, 12, 0, tzinfo=datetime.timezone.utc),
            club=self.club,
        )
        PickupLocation.objects.create(name="Banned Stats Pickup", auction=auction, pickup_time=timezone.now())
        return auction

    def _tos(self, auction):
        self._n += 1
        return AuctionTOS.objects.create(
            name=f"Person {self._n}",
            auction=auction,
            pickup_location=PickupLocation.objects.filter(auction=auction).first(),
        )

    def _lot(self, auction, seller, price, *, banned=False, is_deleted=False, donation=False, date_end=None):
        return Lot.objects.create(
            lot_name=f"Lot {price}",
            auction=auction,
            auctiontos_seller=seller,
            winning_price=Decimal(price) if price is not None else None,
            banned=banned,
            is_deleted=is_deleted,
            donation=donation,
            date_end=date_end,
            active=False,
            quantity=1,
        )

    def test_images_chart_excludes_banned_and_deleted_sold_lots(self):
        # The "No images" bucket should reflect only the two genuine sold lots ($10, $30): median
        # $20, count 2. A banned $500 lot and a soft-deleted $999 lot must not leak in -- if they
        # did, the median would jump to $265 (median of 10, 30, 500, 999).
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, 10)
        self._lot(auction, seller, 30)
        self._lot(auction, seller, 500, banned=True)
        self._lot(auction, seller, 999, is_deleted=True)

        stats = auction.set_stat_images()
        # index 0 == "No images" (none of these lots have LotImages)
        self.assertEqual(stats["data"][2][0], 2, "only the two non-banned, non-deleted sold lots should be counted")
        self.assertEqual(stats["data"][0][0], 20, "median sell price must exclude banned/deleted lots")

    def test_total_donations_excludes_banned(self):
        # Two genuine donation lots ($10 + $20 = $30); a banned $100 donation lot must not count.
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, 10, donation=True)
        self._lot(auction, seller, 20, donation=True)
        self._lot(auction, seller, 100, donation=True, banned=True)
        self.assertEqual(auction.total_donations, Decimal(30))

    def test_auctioneer_speed_excludes_banned(self):
        # Three sold lots ended one minute apart yield two inter-lot gaps (two scatter points). A
        # banned lot ended between them must not add a third point.
        auction = self._auction()
        seller = self._tos(auction)
        base = datetime.datetime(2026, 3, 15, 18, 0, tzinfo=datetime.timezone.utc)
        self._lot(auction, seller, 10, date_end=base)
        self._lot(auction, seller, 12, date_end=base - datetime.timedelta(minutes=1))
        self._lot(auction, seller, 14, date_end=base - datetime.timedelta(minutes=2))
        self._lot(auction, seller, 500, banned=True, date_end=base - datetime.timedelta(seconds=30))

        stats = auction.set_stat_auctioneer_speed()
        self.assertEqual(len(stats["data"][0]), 2, "banned lot must not add an extra auctioneer-speed point")


class StatsBiddersChartReviewTests(TestCase):
    """AuctionLotBiddersChartData follow-up fix: lots with more than six bidders were silently
    dropped (the >6 branch reassigned a local but never incremented a bucket), a sold lot with no
    recorded bids was tallied under "Not sold", and raw Bid rows were counted despite the labels
    reading "N users"."""

    def setUp(self):
        self.creator = User.objects.create_user("bidderstats_creator", "bidderstats@example.com", "pw")
        self.club = Club.objects.create(name="Bidder Stats Club")
        self._n = 0

    def _auction(self):
        auction = Auction.objects.create(
            created_by=self.creator,
            title="Bidder Stats Auction",
            is_online=True,
            date_start=datetime.datetime(2026, 3, 15, 12, 0, tzinfo=datetime.timezone.utc),
            date_end=datetime.datetime(2026, 3, 16, 12, 0, tzinfo=datetime.timezone.utc),
            club=self.club,
        )
        PickupLocation.objects.create(name="Bidder Stats Pickup", auction=auction, pickup_time=timezone.now())
        return auction

    def _tos(self, auction):
        self._n += 1
        return AuctionTOS.objects.create(
            name=f"Person {self._n}",
            auction=auction,
            pickup_location=PickupLocation.objects.filter(auction=auction).first(),
        )

    def _lot(self, auction, seller, *, sold=True):
        self._n += 1
        return Lot.objects.create(
            lot_name=f"Bidder lot {self._n}",
            auction=auction,
            auctiontos_seller=seller,
            winning_price=Decimal(10) if sold else None,
            active=False,
            quantity=1,
        )

    def _chart_data(self, auction):
        from auctions.views import AuctionLotBiddersChartData

        view = AuctionLotBiddersChartData()
        view.auction = auction
        return json.loads(view.get().content)["data"]

    def test_more_than_six_bidders_counted_in_top_bucket(self):
        # A lot with seven distinct bidders must land in the "6 or more" bucket (index 6), not vanish.
        auction = self._auction()
        seller = self._tos(auction)
        lot = self._lot(auction, seller)
        for i in range(7):
            bidder = User.objects.create_user(f"bidder7_{i}", f"bidder7_{i}@example.com", "pw")
            Bid.objects.create(user=bidder, lot_number=lot, amount=10 + i)
        data = self._chart_data(auction)
        self.assertEqual(data[6], 1, "a lot with 7 bidders must be counted in the 6+ bucket")
        self.assertEqual(sum(data), 1, "the 7-bidder lot must not be dropped")

    def test_sold_lot_with_no_bids_not_counted_as_unsold(self):
        # A sold lot with no Bid rows (buy-now / admin-declared) belongs in a sold bucket, never
        # "Not sold" (index 0).
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, sold=True)
        data = self._chart_data(auction)
        self.assertEqual(data[0], 0, "a sold lot must never be counted as 'Not sold'")
        self.assertEqual(data[1], 1, "a sold lot with no recorded bids floors at the 1-bidder bucket")

    def test_distinct_bidders_counted_not_raw_bid_rows(self):
        # One user bidding three times is one bidder (the labels say "users"), so the lot lands in
        # the 1-user bucket, not the 3-user bucket.
        auction = self._auction()
        seller = self._tos(auction)
        lot = self._lot(auction, seller)
        bidder = User.objects.create_user("repeat_bidder", "repeat_bidder@example.com", "pw")
        for amount in (10, 11, 12):
            Bid.objects.create(user=bidder, lot_number=lot, amount=amount)
        data = self._chart_data(auction)
        self.assertEqual(data[1], 1, "repeated bids from one user count as a single bidder")
        self.assertEqual(data[3], 0, "raw Bid rows must not be counted as distinct bidders")


class StatsCompareSlugGuardReviewTests(StandardTestCase):
    """AuctionStats follow-up fix: an invalid ?compare= slug returned None from .first() and then
    500'd on None.permission_check. The stats page must degrade gracefully instead."""

    def test_invalid_compare_slug_does_not_crash(self):
        self.client.login(username=self.user.username, password="testpassword")
        url = f"/auctions/{self.online_auction.slug}/stats/?compare=this-slug-does-not-exist"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, "a bad ?compare= slug must not 500")
        self.assertIsNone(response.context.get("compare_auction"), "a bad compare slug must not set compare_auction")
