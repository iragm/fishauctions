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
    """PayPal order creation with partially refunded lots: item amounts must sum to item_total."""

    def _buyer_invoice(self, lots_spec):
        """Create a buyer, their lots (list of (winning_price, refund_percent)) and an invoice."""
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
        discount = Decimal(breakdown.get("discount", {}).get("value", "0.00"))
        self.assertEqual(Decimal(unit["amount"]["value"]), item_total + tax_total - discount)

    def test_partial_refund_uses_refund_adjusted_price(self):
        # 25% tax; no invoice rounding so amounts are exact.
        self.online_auction.invoice_rounding = False
        self.online_auction.save()
        # A: $20 -> item $20, tax $5. B: $20 with 50% refund -> item $10, tax $2.50.
        invoice = self._buyer_invoice([(20, 0), (20, 50)])
        payload, approval_url = self._order_payload(invoice)
        self.assertEqual(approval_url, "https://www.paypal.com/checkoutnow?token=ORDER-TEST")

        items = payload["purchase_units"][0]["items"]
        self.assertEqual(len(items), 2)
        unit_values = sorted(Decimal(i["unit_amount"]["value"]) for i in items)
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
        unit_values = sorted(Decimal(i["unit_amount"]["value"]) for i in items)
        self.assertEqual(unit_values, [Decimal("15.00"), Decimal("20.00")])

        breakdown = payload["purchase_units"][0]["amount"]["breakdown"]
        self.assertEqual(breakdown["item_total"]["value"], "35.00")
        self.assertEqual(breakdown["tax_total"]["value"], "8.75")
        self.assertEqual(payload["purchase_units"][0]["amount"]["value"], "43.75")
        self._assert_breakdown_valid(payload)

    def test_fractional_cent_refund_items_sum_matches_item_total(self):
        # A 50% refund on odd cents: item_total is summed from the same quantized values sent.
        self.online_auction.invoice_rounding = False
        self.online_auction.save()
        invoice = self._buyer_invoice([(Decimal("10.03"), 50), (Decimal("10.03"), 50)])
        payload, _ = self._order_payload(invoice)

        self._assert_breakdown_valid(payload)

        items = payload["purchase_units"][0]["items"]
        item_sum = sum((Decimal(i["unit_amount"]["value"]) for i in items), Decimal("0.00"))
        self.assertEqual(item_sum, Decimal(payload["purchase_units"][0]["amount"]["breakdown"]["item_total"]["value"]))


class MedianLotValueTests(TestCase):
    """Auction.median_lot_price and median_value(): true middle element, mean for even counts, no banned lots."""

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
        auction = self._auction()
        seller = self._tos(auction)
        for price in (10, 30, 20):  # insertion order deliberately not sorted
            self._lot(auction, seller, price)
        self.assertEqual(auction.median_lot_price, Decimal(20))

    def test_odd_count_five_lots_returns_true_middle(self):
        auction = self._auction()
        seller = self._tos(auction)
        for price in (25, 5, 20, 10, 15):
            self._lot(auction, seller, price)
        self.assertEqual(auction.median_lot_price, Decimal(15))

    def test_odd_count_seven_lots_returns_true_middle(self):
        auction = self._auction()
        seller = self._tos(auction)
        for price in (7, 6, 5, 4, 3, 2, 1):
            self._lot(auction, seller, price)
        self.assertEqual(auction.median_lot_price, Decimal(4))

    def test_even_count_four_lots_returns_mean_of_two_middle(self):
        # Even counts return the mean of the two middle values.
        auction = self._auction()
        seller = self._tos(auction)
        for price in (40, 10, 30, 20):
            self._lot(auction, seller, price)
        self.assertEqual(auction.median_lot_price, Decimal(25))

    def test_even_count_mean_can_be_fractional(self):
        auction = self._auction()
        seller = self._tos(auction)
        for price in (10, 15):
            self._lot(auction, seller, price)
        self.assertEqual(auction.median_lot_price, Decimal("12.5"))

    def test_banned_lots_excluded_from_median(self):
        # A banned $1000 lot must not shift the median of 10, 20, 30.
        auction = self._auction()
        seller = self._tos(auction)
        for price in (10, 20, 30):
            self._lot(auction, seller, price)
        self._lot(auction, seller, 1000, banned=True)
        self.assertEqual(auction.median_lot_price, Decimal(20))

    def test_banned_lot_removal_changes_result(self):
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
        auction = self._auction()
        seller = self._tos(auction)
        for price in (10, 20, 30):
            self._lot(auction, seller, price)
        self._lot(auction, seller, 0, sold=False)
        self.assertEqual(auction.median_lot_price, Decimal(20))

    def test_empty_auction_returns_zero_without_crashing(self):
        auction = self._auction()
        self._tos(auction)  # a registrant but no sold lots
        self.assertEqual(auction.median_lot_price, 0)

    def test_median_value_helper_raises_on_empty_queryset(self):
        from auctions.models import median_value

        with self.assertRaises(IndexError):
            median_value(Lot.objects.none(), "winning_price")


class SellPriceChartBinTests(TestCase):
    """Sell-price histogram: labels and bins describe the same buckets, and banned lots are excluded.

    The model and the view fallback share ``_lot_sell_price_bins``.
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
        """A view instance for the fallback branch (no cached_stats)."""
        from auctions.views import AuctionStatsLotSellPricesJSONView

        view = AuctionStatsLotSellPricesJSONView()
        view.auction = auction
        view.compare_auction = None
        return view

    @staticmethod
    def _bar_range(label):
        """(lower, upper) for a priced label like "$3-5", or None."""
        nums = re.findall(r"\d+", label)
        if len(nums) < 2:
            return None
        return int(nums[0]), int(nums[1])

    def _assert_price_in_labeled_bar(self, labels, row, price):
        """The bar holding a single ``price`` is the one whose label contains it (left-inclusive)."""
        self.assertEqual(len(labels), len(row), "labels and data must have one entry per bar")
        hit_index = next(i for i, count in enumerate(row) if count == 1)
        rng = self._bar_range(labels[hit_index])
        self.assertIsNotNone(rng, f"price {price} landed on a non-priced bar '{labels[hit_index]}'")
        lower, upper = rng
        self.assertTrue(
            lower <= price < upper,
            f"price {price} counted in bar '{labels[hit_index]}' ({lower}-{upper}), which does not contain it",
        )

    def test_model_labels_and_data_have_equal_length(self):
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
        # For a $25 top price the old fallback produced 16 labels against 17 data points.
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, 25)
        view = self._fallback_view(auction)
        self.assertIsNone(auction.cached_stats)  # confirm we are exercising the fallback branch
        labels = view.get_labels()
        row = view.get_data()[0]
        self.assertEqual(len(labels), len(row))

    def test_fallback_priced_lot_lands_in_labeled_bar(self):
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, 25)
        view = self._fallback_view(auction)
        self.assertIsNone(auction.cached_stats)
        self._assert_price_in_labeled_bar(view.get_labels(), view.get_data()[0], 25)

    def test_cached_priced_lot_lands_in_labeled_bar(self):
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, 25)
        auction.cached_stats = {"lot_sell_prices": auction.set_stat_lot_sell_prices()}
        auction.save()
        view = self._fallback_view(auction)
        self._assert_price_in_labeled_bar(view.get_labels(), view.get_data()[0], 25)

    def test_view_fallback_matches_cached_stats(self):
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
        # A $25 top keeps bins width 2; a lot priced exactly 3 belongs to "3-5".
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
        # A $1000 top caps at 30 width-2 bins, so 1000 lands in the overflow bar.
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, 5)
        self._lot(auction, seller, 1000)
        stats = auction.set_stat_lot_sell_prices()
        labels, row = stats["labels"], stats["data"][0]
        self.assertTrue(labels[-1].endswith("+"), "last bar should be the high-overflow bucket")
        self.assertGreaterEqual(row[-1], 1, "the $1000 lot must be counted in the overflow bar")
        self.assertEqual(sum(row[1:]), 2)

    def test_banned_lots_excluded_from_priced_bars(self):
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, 5)
        self._lot(auction, seller, 5, banned=True)
        stats = auction.set_stat_lot_sell_prices()
        row = stats["data"][0]
        self.assertEqual(sum(row[1:]), 1, "banned sold lot must not be counted among priced bars")

    def test_banned_unsold_lot_excluded_from_not_sold_bar(self):
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, 0, sold=False)  # a genuine unsold lot
        self._lot(auction, seller, 0, sold=False, banned=True)  # removed, must not count
        stats = auction.set_stat_lot_sell_prices()
        self.assertEqual(stats["labels"][0], "Not sold")
        self.assertEqual(stats["data"][0][0], 1)

    def test_banned_exclusion_is_observable(self):
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, 25)
        before = sum(auction.set_stat_lot_sell_prices()["data"][0][1:])
        self.assertEqual(before, 1)
        self._lot(auction, seller, 25, banned=True)
        after = sum(auction.set_stat_lot_sell_prices()["data"][0][1:])
        self.assertEqual(after, 1, "adding a banned lot must not change the priced-bar totals")


class ParticipantCountTests(TestCase):
    """Seller, buyer and participant counts ignore banned and deleted lots, and count people once."""

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
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, banned=True)
        self._lot(auction, seller, price=10)
        self.assertEqual(auction.number_of_sellers, 1)

    def test_multiple_lots_same_seller_counted_once(self):
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, price=5)
        self._lot(auction, seller, price=10)
        self._lot(auction, seller, price=15)
        self.assertEqual(auction.number_of_sellers, 1)

    def test_buyer_with_only_banned_won_lot_not_counted(self):
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
        # sellers: seller1, seller2 (2); buyers: buyer1 (1); sellers who didn't buy: 2; participants: 3
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
        auction = self._auction()
        seller1 = self._tos(auction)
        seller2 = self._tos(auction)
        buyer1 = self._tos(auction)
        buyer2 = self._tos(auction)
        # seller1 -> live sale to buyer2 (both stay)
        self._lot(auction, seller1, winner=buyer2, price=10)
        # seller2's only lot is removed -> seller2 drops
        self._lot(auction, seller2, banned=True)
        self._lot(auction, seller1, winner=buyer1, price=15, banned=True)
        self.assertEqual(auction.number_of_sellers, 1)  # seller1 only
        self.assertEqual(auction.number_of_buyers, 1)  # buyer2 only
        self.assertEqual(auction.number_of_participants, 2)  # seller1, buyer2


class ViewsAndWinnersStatsTests(TestCase):
    """Unique views don't double-count an anonymous visitor who logs in; total_winners counts admin-declared winners."""

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
        return PageView.objects.create(auction=auction, user=user, session_id=session_id)

    def test_anonymous_then_logged_in_same_session_counted_once(self):
        # Browsed anonymously, then logged in on the same session: counted once.
        auction = self._auction()
        user = User.objects.create_user("stats19_visitor", "v@example.com", "pw")
        self._view(auction, user=None, session_id="sessA")
        self._view(auction, user=user, session_id="sessA")
        result = auction.unique_views
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["logged_in"], 1)
        self.assertEqual(result["anonymous"], 0)

    def test_two_different_visitors_counted_twice(self):
        # The old formula returned 4: each side's NULL counted as a bucket.
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
        auction = self._auction()
        seller = self._tos(auction)
        lot = self._lot(auction, seller, price=10)
        user = User.objects.create_user("stats19_lotviewer", "lv@example.com", "pw")
        PageView.objects.create(lot_number=lot, user=user, session_id=None)
        PageView.objects.create(lot_number=lot, user=None, session_id="lotsess")
        self.assertEqual(auction.unique_views["total"], 2)

    def test_admin_declared_winner_counted(self):
        # auctiontos_winner and winning_price, but no winner FK: check-in's shape.
        auction = self._auction()
        seller = self._tos(auction)
        buyer = self._tos(auction, user=None)  # in-person buyer, no account
        self._lot(auction, seller, winner=buyer, winner_user=None, price=10)
        # Old (buggy) path would have returned 0:
        self.assertEqual(User.objects.filter(winner__auction=auction).distinct().count(), 0)
        self.assertEqual(auction.buyer_tos_qs.count(), 1)
        self.assertEqual(auction.set_stat_misc()["total_winners"], 1)

    def test_bid_flow_winner_still_counted(self):
        auction = self._auction()
        seller = self._tos(auction)
        buyer_user = User.objects.create_user("stats19_bidwinner", "bw@example.com", "pw")
        buyer = self._tos(auction, user=buyer_user)
        self._lot(auction, seller, winner=buyer, winner_user=buyer_user, price=20)
        self.assertEqual(auction.set_stat_misc()["total_winners"], 1)

    def test_admin_and_bid_winners_together_not_double_counted(self):
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
    """Auction.percent_unsold_lots is 0, not 100, with no lots."""

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
    """PayPal bulk-invoice CSV chunks and the export loop both count ``paypal_invoices_to_export``."""

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
        # net == 0: not a payout, but owes nothing, so takes no chunk slot.
        user = self._tos(auction)
        return Invoice.objects.create(auctiontos_user=user, auction=auction, status="UNPAID")

    def test_settled_invoice_excluded_from_export_set(self):
        auction = self._auction()
        owing = self._owing_invoice(auction)
        settled = self._settled_invoice(auction)
        export_pks = {inv.pk for inv in auction.paypal_invoices_to_export}
        self.assertIn(owing.pk, export_pks, "An invoice that owes the club is billed")
        self.assertNotIn(settled.pk, export_pks, "A settled $0 invoice is not billed")
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
        self.assertEqual(auction.paypal_invoice_chunks, [1])
        old_counter_set = [inv for inv in auction.paypal_invoices if not inv.user_should_be_paid]
        self.assertGreater(
            len(old_counter_set),
            len(export),
            "Old counter advanced on more invoices than were billable (the tail-drop drift)",
        )

    def test_every_exported_invoice_lands_in_an_offered_chunk(self):
        # Every exported invoice's chunk number is one the UI offers.
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
    """invoice_summary_short says "is settled up" at $0; user_should_be_paid only when the club owes them."""

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
        InvoiceAdjustment.objects.create(invoice=invoice, adjustment_type="DISCOUNT", amount=5)
        self.assertGreater(invoice.net, 0)
        self.assertTrue(invoice.user_should_be_paid, "Positive net means the club owes the user")
        self.assertEqual(invoice.invoice_summary_short, "needs to be paid $5.00")


class LedgerPercentAdjustmentBaseTests(TestCase):
    """The ledger's percent adjustment uses ``Invoice.manual_adjustment_amount``, the same base as ``net``."""

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
        # subtotal -100, first_bid +10, flat -20, 20%: base -110, adjustment -42, net -132.
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
        self.assertEqual(booked.get(ClubMoney.CATEGORY_ROUNDING, Decimal("0.00")), Decimal("0.00"))
        # The whole ledger reconciles to the rounded invoice total.
        self.assertEqual(sum(booked.values()), -invoice.rounded_net)

    def test_rounding_only_holds_subcent_after_fix(self):
        # base -111, adjustment -43.2, net -133.2 -> -133: rounding is only $0.20.
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
    """Image stats, total_donations and the auctioneer-speed/attrition scatters exclude banned and deleted lots."""

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
        # Only $10 and $30 count: median $20, not $265.
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
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, 10, donation=True)
        self._lot(auction, seller, 20, donation=True)
        self._lot(auction, seller, 100, donation=True, banned=True)
        self.assertEqual(auction.total_donations, Decimal(30))

    def test_auctioneer_speed_excludes_banned(self):
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
    """AuctionLotBiddersChartData: 7+ bidders bucketed, bidless sold lots not "Not sold", distinct users."""

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
        auction = self._auction()
        seller = self._tos(auction)
        self._lot(auction, seller, sold=True)
        data = self._chart_data(auction)
        self.assertEqual(data[0], 0, "a sold lot must never be counted as 'Not sold'")
        self.assertEqual(data[1], 1, "a sold lot with no recorded bids floors at the 1-bidder bucket")

    def test_distinct_bidders_counted_not_raw_bid_rows(self):
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
    """An invalid ?compare= slug doesn't 500 the stats page."""

    def test_invalid_compare_slug_does_not_crash(self):
        self.client.login(username=self.user.username, password="testpassword")
        url = f"/auctions/{self.online_auction.slug}/stats/?compare=this-slug-does-not-exist"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, "a bad ?compare= slug must not 500")
        self.assertIsNone(response.context.get("compare_auction"), "a bad compare slug must not set compare_auction")
