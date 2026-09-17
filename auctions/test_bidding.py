"""Tests for bid values, bidding permissions, and the bid dialog.

``BiddingPermissionsHardeningTests`` covers paths the page would have refused.
"""

import datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from auctions.forms import (
    AuctionEditForm,
    CreateLotForm,
)
from auctions.models import (
    Auction,
    AuctionTOS,
    Bid,
    Invoice,
    Lot,
    LotHistory,
    PickupLocation,
    UserBan,
    add_price_info,
)
from auctions.tests import StandardTestCase


class LotPricesTests(TestCase):
    def setUp(self):
        time = timezone.now() - datetime.timedelta(days=2)
        timeStart = timezone.now() - datetime.timedelta(days=3)
        theFuture = timezone.now() + datetime.timedelta(days=3)
        self.user = User.objects.create_user(username="my_lot", password="testpassword", email="test@example.com")
        self.auction = Auction.objects.create(
            created_by=self.user,
            title="A test auction",
            date_end=time,
            date_start=timeStart,
            winning_bid_percent_to_club=25,
            lot_entry_fee=2,
            unsold_lot_fee=10,
            tax=25,
        )
        self.location = PickupLocation.objects.create(name="location", auction=self.auction, pickup_time=theFuture)
        self.userB = User.objects.create_user(username="no_tos", password="testpassword")
        self.tos = AuctionTOS.objects.create(user=self.user, auction=self.auction, pickup_location=self.location)
        self.tosB = AuctionTOS.objects.create(user=self.userB, auction=self.auction, pickup_location=self.location)
        self.lot = Lot.objects.create(
            lot_name="A test lot",
            auction=self.auction,
            auctiontos_seller=self.tos,
            quantity=1,
            winning_price=10,
            auctiontos_winner=self.tosB,
            active=False,
        )
        self.unsold_lot = Lot.objects.create(
            lot_name="Unsold lot",
            reserve_price=10,
            auction=self.auction,
            quantity=1,
            auctiontos_seller=self.tos,
            active=False,
        )
        self.sold_no_auction_lot = Lot.objects.create(
            lot_name="not in the auction",
            reserve_price=10,
            auction=None,
            quantity=1,
            user=self.user,
            active=False,
            winning_price=10,
            date_end=time,
        )
        self.unsold_no_auction_lot = Lot.objects.create(
            lot_name="unsold not in the auction",
            reserve_price=10,
            auction=None,
            quantity=1,
            user=self.user,
            active=True,
            date_end=time,
        )

    def test_lot_prices(self):
        lots = Lot.objects.all()
        lots = add_price_info(lots)

        lot = lots.filter(pk=self.lot.pk).first()
        assert lot.your_cut == 5.5
        unsold_lot = lots.filter(pk=self.unsold_lot.pk).first()
        assert unsold_lot.your_cut == -10
        sold_no_auction_lot = lots.filter(pk=self.sold_no_auction_lot.pk).first()
        assert sold_no_auction_lot.your_cut == 10
        unsold_no_auction_lot = lots.filter(pk=self.unsold_no_auction_lot.pk).first()
        assert unsold_no_auction_lot.your_cut == 0

        self.auction.winning_bid_percent_to_club = 50
        self.auction.winning_bid_percent_to_club_for_club_members = 0
        self.auction.save()
        lot = lots.filter(pk=self.lot.pk).first()
        assert lot.your_cut == 3.0
        unsold_lot = lots.filter(pk=self.unsold_lot.pk).first()
        assert unsold_lot.your_cut == -10

        self.tos.is_club_member = True
        self.tos.save()
        lot = lots.filter(pk=self.lot.pk).first()
        assert lot.your_cut == 10
        unsold_lot = lots.filter(pk=self.unsold_lot.pk).first()
        assert unsold_lot.your_cut == -10

        self.auction.winning_bid_percent_to_club_for_club_members = 50
        self.auction.pre_register_lot_discount_percent = 10
        self.auction.save()
        lot = lots.filter(pk=self.lot.pk).first()
        assert lot.your_cut == 5
        unsold_lot = lots.filter(pk=self.unsold_lot.pk).first()
        assert unsold_lot.your_cut == -10

        # lot is now pre-registered
        self.lot.user = self.user
        self.lot.added_by = self.user
        self.lot.save()
        lot = lots.filter(pk=self.lot.pk).first()
        assert lot.pre_register_discount == 10
        self.tos.is_club_member = False
        self.tos.save()
        self.tos.is_club_member = True
        self.tos.save()
        lot = lots.filter(pk=self.lot.pk).first()
        self.lot.user = None
        self.lot.added_by = None
        self.lot.save()

        self.auction.lot_entry_fee_for_club_members = 1
        self.auction.save()
        lot = lots.filter(pk=self.lot.pk).first()
        assert lot.your_cut == 4
        unsold_lot = lots.filter(pk=self.unsold_lot.pk).first()
        assert unsold_lot.your_cut == -10

        self.lot.partial_refund_percent = 25
        self.lot.save()
        self.unsold_lot.partial_refund_percent = 25
        self.unsold_lot.save()

        lot = lots.filter(pk=self.lot.pk).first()
        assert lot.your_cut == 3.0
        unsold_lot = lots.filter(pk=self.unsold_lot.pk).first()
        assert unsold_lot.your_cut == -10

        self.lot.donation = True
        self.lot.save()
        lot = lots.filter(pk=self.lot.pk).first()
        assert lot.your_cut == 0

    def test_invoice_rounding(self):
        invoice, created = Invoice.objects.get_or_create(auctiontos_user=self.tos)
        assert invoice.rounded_net == -4
        self.auction.invoice_rounding = False
        self.auction.winning_bid_percent_to_club = 12
        self.auction.save()
        invoice, created = Invoice.objects.get_or_create(auctiontos_user=self.tos)
        assert invoice.net == invoice.rounded_net
        self.assertAlmostEqual(Decimal(invoice.rounded_net), Decimal(-3.2))

    def test_decimal_price_your_cut(self):
        self.auction.only_whole_dollar_bids = False
        self.auction.save()
        self.lot.winning_price = Decimal("10.50")
        self.lot.save()
        lots = add_price_info(Lot.objects.filter(pk=self.lot.pk))
        lot = lots.first()
        # your_cut = 10.50 * (100-25)/100 - 2 = 7.875 - 2 = 5.875
        self.assertAlmostEqual(lot.your_cut, Decimal("5.875"), places=3)
        # club_cut = 10.50 - your_cut = 4.625
        self.assertAlmostEqual(lot.club_cut, Decimal("4.625"), places=3)

    def test_decimal_price_invoice_totals(self):
        self.auction.only_whole_dollar_bids = False
        self.auction.save()
        self.lot.winning_price = Decimal("10.50")
        self.lot.save()
        # Seller invoice
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.tos)
        # total_sold 5.875, unsold fee -10, net -4.125
        self.assertAlmostEqual(invoice.total_sold, Decimal("5.875") - 10, places=3)
        self.assertEqual(invoice.tax, 0)
        self.assertAlmostEqual(invoice.net, Decimal("5.875") - 10, places=3)

    def test_decimal_price_buyer_invoice_with_tax(self):
        self.auction.only_whole_dollar_bids = False
        self.auction.save()
        self.lot.winning_price = Decimal("10.50")
        self.lot.save()
        # Buyer invoice (tosB bought the lot)
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.tosB)
        # total_bought = 10.50 (final_price without partial refund)
        self.assertAlmostEqual(invoice.total_bought, Decimal("10.50"), places=2)
        # tax = 10.50 * 25% = 2.625 → rounded to 2.63 (ROUND_HALF_UP)
        self.assertEqual(invoice.tax, Decimal("2.63"))
        # net = -10.50 - 2.63 = -13.13
        self.assertAlmostEqual(invoice.net, Decimal("-13.13"), places=2)

    def test_decimal_price_invoice_rounding_seller(self):
        """rounded_net rounds in seller's favor (up) when invoice_rounding is enabled"""
        self.auction.only_whole_dollar_bids = False
        self.auction.invoice_rounding = True
        self.auction.save()
        # your_cut 10.50 * 0.75 - 2 = 5.875; unsold fee -10; net -4.125
        self.lot.winning_price = Decimal("10.50")
        self.lot.save()
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.tos)
        # Rounds in the buyer's favour: -4.125 -> -4.
        self.assertEqual(invoice.rounded_net, Decimal(-4))

    def test_decimal_price_invoice_rounding_buyer(self):
        self.auction.only_whole_dollar_bids = False
        self.auction.invoice_rounding = True
        self.auction.save()
        self.lot.winning_price = Decimal("10.50")
        self.lot.save()
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.tosB)
        # -13.13 -> -13.
        self.assertEqual(invoice.rounded_net, Decimal(-13))

    def test_decimal_price_no_invoice_rounding(self):
        self.auction.only_whole_dollar_bids = False
        self.auction.invoice_rounding = False
        self.auction.save()
        self.lot.winning_price = Decimal("10.50")
        self.lot.save()
        invoice_buyer, _ = Invoice.objects.get_or_create(auctiontos_user=self.tosB)
        self.assertEqual(invoice_buyer.rounded_net, invoice_buyer.net)
        self.assertAlmostEqual(invoice_buyer.net, Decimal("-13.13"), places=2)

    def test_recalculate_stores_exact_decimal_net(self):
        """recalculate() stores exact cents (calculated_total used to be an IntegerField)."""
        self.auction.only_whole_dollar_bids = False
        self.auction.invoice_rounding = False
        self.auction.tax = 0
        self.auction.save()
        self.lot.winning_price = Decimal("10.50")
        self.lot.save()
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.tosB)
        self.assertEqual(invoice.net, Decimal("-10.50"))
        invoice.recalculate()
        invoice.refresh_from_db()
        self.assertEqual(invoice.calculated_total, Decimal("-10.50"))

    def test_recalculate_stores_whole_dollar_when_rounding_enabled(self):
        """With invoice_rounding on, the stored total is still a whole-dollar amount."""
        self.auction.only_whole_dollar_bids = False
        self.auction.invoice_rounding = True
        self.auction.save()
        self.lot.winning_price = Decimal("10.50")
        self.lot.save()
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.tosB)
        # net = -13.13; rounded in the buyer's favor -> -13
        self.assertEqual(invoice.rounded_net, Decimal(-13))
        invoice.recalculate()
        invoice.refresh_from_db()
        self.assertEqual(invoice.calculated_total, Decimal("-13.00"))
        self.assertEqual(invoice.calculated_total, invoice.calculated_total.to_integral_value())


class DecimalBidValidationTests(TestCase):
    """bid_on_lot with only_whole_dollar_bids and decimal validation."""

    def setUp(self):
        time = timezone.now() + datetime.timedelta(days=30)
        pastTime = timezone.now() - datetime.timedelta(hours=1)
        # Valid emails so outbid notifications don't error.
        self.lotuser = User.objects.create_user(username="decimal_lotowner", password="x", email="lotowner@example.com")
        self.userA = User.objects.create_user(username="decimal_userA", password="x", email="userA@example.com")
        self.userB = User.objects.create_user(username="decimal_userB", password="x", email="userB@example.com")

        self.whole_dollar_auction = Auction.objects.create(
            title="Whole dollar auction",
            date_end=time,
            date_start=timezone.now() - datetime.timedelta(days=1),
            only_whole_dollar_bids=True,
        )
        self.decimal_auction = Auction.objects.create(
            title="Decimal auction",
            date_end=time,
            date_start=timezone.now() - datetime.timedelta(days=1),
            only_whole_dollar_bids=False,
        )
        location_whole = PickupLocation.objects.create(
            name="loc_whole", auction=self.whole_dollar_auction, pickup_time=time
        )
        location_decimal = PickupLocation.objects.create(
            name="loc_decimal", auction=self.decimal_auction, pickup_time=time
        )
        AuctionTOS.objects.create(user=self.lotuser, auction=self.whole_dollar_auction, pickup_location=location_whole)
        AuctionTOS.objects.create(user=self.userA, auction=self.whole_dollar_auction, pickup_location=location_whole)
        AuctionTOS.objects.create(user=self.userB, auction=self.whole_dollar_auction, pickup_location=location_whole)
        AuctionTOS.objects.create(user=self.lotuser, auction=self.decimal_auction, pickup_location=location_decimal)
        AuctionTOS.objects.create(user=self.userA, auction=self.decimal_auction, pickup_location=location_decimal)
        AuctionTOS.objects.create(user=self.userB, auction=self.decimal_auction, pickup_location=location_decimal)

        self.whole_dollar_lot = Lot.objects.create(
            lot_name="Whole dollar lot",
            auction=self.whole_dollar_auction,
            reserve_price=5,
            user=self.lotuser,
            quantity=1,
            date_end=time,
        )
        self.whole_dollar_lot.date_posted = pastTime
        self.whole_dollar_lot.save()

        # Decimal lot with reserve=$5.00; used for most tests
        self.decimal_lot = Lot.objects.create(
            lot_name="Decimal lot",
            auction=self.decimal_auction,
            reserve_price=Decimal("5.00"),
            user=self.lotuser,
            quantity=1,
            date_end=time,
        )
        self.decimal_lot.date_posted = pastTime
        self.decimal_lot.save()

    def test_fractional_bid_rejected_on_whole_dollar_auction(self):
        """A bid with cents is rejected when only_whole_dollar_bids=True"""
        from auctions.bidding import bid_on_lot

        result = bid_on_lot(self.whole_dollar_lot, self.userA, 10.50)
        self.assertEqual(result["type"], "ERROR")
        self.assertIn("whole dollar", result["message"].lower())

    def test_whole_dollar_bid_accepted_on_whole_dollar_auction(self):
        """A whole-dollar bid is accepted when only_whole_dollar_bids=True"""
        from auctions.bidding import bid_on_lot

        result = bid_on_lot(self.whole_dollar_lot, self.userA, 10)
        self.assertIn(result["type"], ["NEW_HIGH_BIDDER", "INFO"])

    def test_decimal_bid_accepted_on_decimal_auction(self):
        """A bid with cents is accepted when only_whole_dollar_bids=False"""
        from auctions.bidding import bid_on_lot

        result = bid_on_lot(self.decimal_lot, self.userA, Decimal("5.50"))
        self.assertIn(result["type"], ["NEW_HIGH_BIDDER", "INFO"])

    def test_more_than_two_decimal_places_rejected(self):
        """A bid with more than 2 decimal places is always rejected"""
        from auctions.bidding import bid_on_lot

        result = bid_on_lot(self.decimal_lot, self.userA, "10.555")
        self.assertEqual(result["type"], "ERROR")
        self.assertIn("2 decimal", result["message"].lower())

    def test_decimal_bid_increment_minimum(self):
        """Decimal auction: the minimum increment is 5% of the reserve, rounded down to cents, at least $0.01."""
        from auctions.bidding import bid_on_lot

        # Use a lot with reserve=$10.00 so the math is clean
        time = timezone.now() + datetime.timedelta(days=30)
        pastTime = timezone.now() - datetime.timedelta(hours=1)
        lot = Lot.objects.create(
            lot_name="Increment test lot",
            auction=self.decimal_auction,
            reserve_price=Decimal("10.00"),
            user=self.lotuser,
            quantity=1,
            date_end=time,
        )
        lot.date_posted = pastTime
        lot.save()
        bid_on_lot(lot, self.userA, Decimal("20.00"))
        # 5% of $10.00 is $0.50, so $10.49 fails.
        result = bid_on_lot(lot, self.userB, Decimal("10.49"))
        self.assertEqual(result["type"], "ERROR")
        self.assertIn("10.50", result["message"])
        result = bid_on_lot(lot, self.userB, Decimal("10.50"))
        self.assertIn(result["type"], ["NEW_HIGH_BIDDER", "NEW_HIGH_BID", "INFO"])

    def test_whole_dollar_bid_increment_minimum(self):
        """Whole-dollar auction: minimum increment is $1 even when 5% < $1"""
        from auctions.bidding import bid_on_lot

        # Reserve $5: 5% rounds down to $0, so the increment is the $1 minimum.
        time = timezone.now() + datetime.timedelta(days=30)
        pastTime = timezone.now() - datetime.timedelta(hours=1)
        lot = Lot.objects.create(
            lot_name="Whole dollar increment lot",
            auction=self.whole_dollar_auction,
            reserve_price=5,
            user=self.lotuser,
            quantity=1,
            date_end=time,
        )
        lot.date_posted = pastTime
        lot.save()
        bid_on_lot(lot, self.userA, 10)
        # Next allowed is $6.
        result = bid_on_lot(lot, self.userB, 5)
        self.assertEqual(result["type"], "ERROR")
        # bid of $6 should succeed
        result = bid_on_lot(lot, self.userB, 6)
        self.assertIn(result["type"], ["NEW_HIGH_BIDDER", "NEW_HIGH_BID", "INFO"])


class BiddingPermissionsHardeningTests(TestCase):
    """Bid-path hardening: admin-team bans, own-lot and seller-ban checks via auctiontos_seller, invoice
    gate for email-matched TOS, under-reserve bids, and CreateUserBan cleanup.
    """

    def setUp(self):
        self.future = timezone.now() + datetime.timedelta(days=30)
        self.past = timezone.now() - datetime.timedelta(hours=1)
        self.creator = User.objects.create_user(username="hard_creator", password="x", email="hardcreator@example.com")
        self.coadmin = User.objects.create_user(username="hard_coadmin", password="x", email="hardcoadmin@example.com")
        self.bidder = User.objects.create_user(username="hard_bidder", password="x", email="hardbidder@example.com")
        self.outbidder = User.objects.create_user(
            username="hard_outbidder", password="x", email="hardoutbidder@example.com"
        )
        self.auction = Auction.objects.create(
            created_by=self.creator,
            title="Hardening auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=self.future,
        )
        self.location = PickupLocation.objects.create(name="hard_loc", auction=self.auction, pickup_time=self.future)
        # note: the creator deliberately has NO AuctionTOS row of their own
        self.coadmin_tos = AuctionTOS.objects.create(
            user=self.coadmin, auction=self.auction, pickup_location=self.location, is_admin=True
        )
        self.bidder_tos = AuctionTOS.objects.create(
            user=self.bidder, auction=self.auction, pickup_location=self.location
        )
        self.outbidder_tos = AuctionTOS.objects.create(
            user=self.outbidder, auction=self.auction, pickup_location=self.location
        )
        # Created before the user exists, so save() can't auto-link it.
        self.unlinked_tos = AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.location, email="hardunlinked@example.com", name="Unlinked"
        )
        self.unlinked_user = User.objects.create_user(
            username="hard_unlinked", password="x", email="hardunlinked@example.com"
        )

    def _make_lot(self, seller_tos, reserve=5, user=None, name="hardening lot"):
        lot = Lot.objects.create(
            lot_name=name,
            auction=self.auction,
            auctiontos_seller=seller_tos,
            user=user,
            reserve_price=reserve,
            quantity=1,
            date_end=self.future,
        )
        lot.date_posted = self.past
        lot.save()
        return lot

    def test_coadmin_ban_blocks_bidding_in_auction(self):
        from auctions.consumers import check_all_permissions

        lot = self._make_lot(self.coadmin_tos, user=self.coadmin)
        UserBan.objects.create(user=self.coadmin, banned_user=self.bidder)
        self.assertEqual(check_all_permissions(lot, self.bidder), "This user has banned you from bidding on their lots")
        other_lot = self._make_lot(self.outbidder_tos, user=self.outbidder, name="other lot")
        self.assertEqual(
            check_all_permissions(other_lot, self.bidder), "You don't have permission to bid in this auction"
        )

    def test_creator_ban_blocks_even_without_creator_tos(self):
        """The creator's ban applies even without a creator TOS."""
        from auctions.consumers import check_all_permissions

        lot = self._make_lot(self.outbidder_tos, user=self.outbidder)
        UserBan.objects.create(user=self.creator, banned_user=self.bidder)
        self.assertEqual(check_all_permissions(lot, self.bidder), "You don't have permission to bid in this auction")

    def test_non_admin_ban_does_not_block_auction_bidding(self):
        from auctions.consumers import check_all_permissions

        lot = self._make_lot(self.coadmin_tos, user=self.coadmin)
        UserBan.objects.create(user=self.outbidder, banned_user=self.bidder)
        self.assertFalse(check_all_permissions(lot, self.bidder))

    def test_own_lot_blocked_via_auctiontos_seller(self):
        """Sellers can't bid on their own lot when matched via auctiontos_seller by email."""
        from auctions.bidding import check_bidding_permissions

        self.unlinked_tos.refresh_from_db()
        self.assertIsNone(self.unlinked_tos.user)
        lot = self._make_lot(self.unlinked_tos, user=None)
        self.assertEqual(check_bidding_permissions(lot, self.unlinked_user), "You can't bid on your own lot")

    def test_seller_ban_applies_when_lot_user_is_none(self):
        from auctions.consumers import check_all_permissions

        lot = self._make_lot(self.coadmin_tos, user=None)
        UserBan.objects.create(user=self.coadmin, banned_user=self.bidder)
        self.assertEqual(check_all_permissions(lot, self.bidder), "This user has banned you from bidding on their lots")

    def test_invoice_gate_applies_to_email_matched_tos(self):
        """A closed invoice blocks bidding for a TOS with no linked user."""
        from auctions.bidding import bid_on_lot

        invoice = Invoice.objects.create(auctiontos_user=self.unlinked_tos, auction=self.auction)
        Invoice.objects.filter(pk=invoice.pk).update(status="UNPAID")
        lot = self._make_lot(self.coadmin_tos, user=self.coadmin)
        result = bid_on_lot(lot, self.unlinked_user, 10)
        self.assertEqual(result["type"], "ERROR")
        self.assertIn("not open", result["message"])

    def test_first_bid_below_reserve_rejected(self):
        from auctions.bidding import bid_on_lot

        lot = self._make_lot(self.coadmin_tos, user=self.coadmin, reserve=10)
        result = bid_on_lot(lot, self.bidder, 5)
        self.assertEqual(result["type"], "ERROR")
        self.assertIn("bid at least", result["message"])
        self.assertFalse(Bid.objects.filter(lot_number=lot).exists())
        # a bid at exactly the reserve is accepted
        result = bid_on_lot(lot, self.bidder, 10)
        self.assertEqual(result["type"], "NEW_HIGH_BIDDER")

    def test_raising_reserve_above_existing_bids_does_not_break_rebidding(self):
        """Re-bidding works after the reserve is raised above existing bids (high_bidder is False)."""
        from auctions.bidding import bid_on_lot

        lot = self._make_lot(self.coadmin_tos, user=self.coadmin, reserve=5)
        bid_on_lot(lot, self.bidder, 10)
        lot.reserve_price = 20
        lot.save()
        result = bid_on_lot(lot, self.bidder, 25)
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "NEW_HIGH_BIDDER")

    def test_outbid_email_failure_does_not_fail_bid(self):

        from auctions.bidding import bid_on_lot

        lot = self._make_lot(self.coadmin_tos, user=self.coadmin)
        bid_on_lot(lot, self.bidder, 10)
        with patch("auctions.bidding.mail.send", side_effect=Exception("smtp down")):
            result = bid_on_lot(lot, self.outbidder, 20)
        self.assertEqual(result["type"], "NEW_HIGH_BIDDER")
        self.assertTrue(Bid.objects.filter(lot_number=lot, user=self.outbidder, was_high_bid=True).exists())
        # the price-change history was still written
        self.assertTrue(LotHistory.objects.filter(lot=lot, changed_price=True, bid_amount=20).exists())

    def test_place_bid_and_broadcast_places_bid_and_handles_missing_lot(self):
        from auctions.bidding import place_bid_and_broadcast

        lot = self._make_lot(self.coadmin_tos, user=self.coadmin)
        result = place_bid_and_broadcast(lot, self.bidder, 10)
        self.assertEqual(result["type"], "NEW_HIGH_BIDDER")
        lot.is_deleted = True
        lot.save()
        result = place_bid_and_broadcast(lot, self.outbidder, 20)
        self.assertEqual(result["type"], "ERROR")
        self.assertEqual(result["message"], "This lot has been removed")

    def test_tos_for_user_newest_record_wins(self):
        """tos_for_user returns the newest record; the duplicate is made with update() to bypass auto-merge."""
        AuctionTOS.objects.filter(pk=self.bidder_tos.pk).update(createdon=timezone.now() - datetime.timedelta(days=2))
        newer = AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.location, email="tempdupe@example.com", name="dupe"
        )
        AuctionTOS.objects.filter(pk=newer.pk).update(email="hardbidder@example.com")
        self.assertEqual(self.auction.tos_for_user(self.bidder).pk, newer.pk)

    def test_create_user_ban_survives_soft_deleted_lots(self):
        """Banning a user whose bids touch a soft-deleted lot doesn't 500."""
        lot = self._make_lot(self.coadmin_tos, user=self.coadmin)
        Bid.objects.create(user=self.bidder, lot_number=lot, amount=10)
        lot.is_deleted = True
        lot.save()
        live_lot = self._make_lot(self.outbidder_tos, user=self.outbidder, name="live lot")
        live_bid = Bid.objects.create(user=self.bidder, lot_number=live_lot, amount=10)
        self.client.force_login(self.creator)
        response = self.client.post(f"/api/users/ban/{self.bidder.pk}/")
        self.assertEqual(response.status_code, 302)
        live_bid.refresh_from_db()
        self.assertTrue(live_bid.is_deleted)

    def test_coadmin_ban_sweeps_administered_auction(self):
        """A co-admin's ban sweeps auctions they administer."""
        target_lot = self._make_lot(self.outbidder_tos, user=self.outbidder, name="bid target")
        bid = Bid.objects.create(user=self.bidder, lot_number=target_lot, amount=10)
        seller_lot = self._make_lot(self.bidder_tos, user=None, name="seller linked lot")
        self.client.force_login(self.coadmin)
        response = self.client.post(f"/api/users/ban/{self.bidder.pk}/")
        self.assertEqual(response.status_code, 302)
        bid.refresh_from_db()
        seller_lot.refresh_from_db()
        self.assertTrue(bid.is_deleted)
        self.assertTrue(seller_lot.banned)

    def test_banned_user_cannot_submit_lot(self):
        """A banned user can't submit lots."""
        UserBan.objects.create(user=self.creator, banned_user=self.bidder)
        userdata = self.bidder.userdata
        userdata.address = "123 Test St"
        userdata.save()
        self.bidder.first_name = "Test"
        self.bidder.last_name = "Bidder"
        self.bidder.save()
        self.client.force_login(self.bidder)
        response = self.client.post(
            f"/lots/new/?auction={self.auction.slug}",
            {
                "lot_name": "Banned user lot",
                "auction": self.auction.pk,
                "quantity": 1,
                "reserve_price": "5",
                "part_of_auction": "True",
                "run_duration": "10",
                "cloned_from": "",
                "image_url": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "banned from selling")
        self.assertFalse(Lot.objects.filter(lot_name="Banned user lot").exists())


class AuctionEditFormMinimumBidTests(TestCase):
    """Tests for AuctionEditForm minimum_bid validation with only_whole_dollar_bids"""

    def _get_form_data(self, auction, overrides=None):
        """Minimal valid AuctionEditForm data from an auction."""

        data = {
            "title": auction.title,
            "summernote_description": auction.summernote_description or "",
            "lot_entry_fee": str(auction.lot_entry_fee or "0"),
            "unsold_lot_fee": str(auction.unsold_lot_fee or "0"),
            "winning_bid_percent_to_club": str(auction.winning_bid_percent_to_club or "0"),
            "winning_bid_percent_to_club_for_club_members": str(
                auction.winning_bid_percent_to_club_for_club_members or "0"
            ),
            "lot_entry_fee_for_club_members": str(auction.lot_entry_fee_for_club_members or "0"),
            "pre_register_lot_discount_percent": str(auction.pre_register_lot_discount_percent or "0"),
            "alternate_split_mode": auction.alternate_split_mode,
            "alternative_split_label": auction.alternative_split_label or "",
            "reserve_price": auction.reserve_price,
            "buy_now": auction.buy_now,
            "tax": str(auction.tax or "0"),
            "online_bidding": auction.online_bidding,
            "custom_field_1": auction.custom_field_1,
            "date_start": auction.date_start.strftime("%Y-%m-%d %H:%M:%S"),
            "date_end": auction.date_end.strftime("%Y-%m-%d %H:%M:%S"),
            "invoice_rounding": str(auction.invoice_rounding),
            "only_whole_dollar_bids": str(auction.only_whole_dollar_bids),
            "minimum_bid": "",
        }
        if overrides:
            data.update(overrides)
        return data

    def setUp(self):
        self.user = User.objects.create_user(username="auction_form_user", password="testpassword")
        time = timezone.now() + datetime.timedelta(days=7)
        self.auction = Auction.objects.create(
            created_by=self.user,
            title="Form test auction",
            date_end=time,
            date_start=timezone.now() - datetime.timedelta(days=1),
            only_whole_dollar_bids=True,
            reserve_price="allow",
            buy_now="allow",
        )
        self.location = PickupLocation.objects.create(
            name="form test pickup",
            auction=self.auction,
            pickup_time=timezone.now() + datetime.timedelta(days=8),
        )
        self.tos = AuctionTOS.objects.create(user=self.user, auction=self.auction, pickup_location=self.location)

    def test_fractional_minimum_bid_rejected_when_whole_dollar_required(self):
        """minimum_bid with cents is invalid when only_whole_dollar_bids=True"""
        data = self._get_form_data(self.auction, {"only_whole_dollar_bids": True, "minimum_bid": "5.50"})
        form = AuctionEditForm(data=data, instance=self.auction, user=self.user, cloned_from=None, user_timezone="UTC")
        form.is_valid()
        self.assertIn("minimum_bid", form.errors)
        self.assertIn("whole dollar", str(form.errors["minimum_bid"]).lower())

    def test_whole_dollar_minimum_bid_accepted_when_whole_dollar_required(self):
        """minimum_bid as a whole dollar is valid when only_whole_dollar_bids=True"""
        data = self._get_form_data(self.auction, {"only_whole_dollar_bids": True, "minimum_bid": "5"})
        form = AuctionEditForm(data=data, instance=self.auction, user=self.user, cloned_from=None, user_timezone="UTC")
        form.is_valid()
        self.assertNotIn("minimum_bid", form.errors)

    def test_fractional_minimum_bid_allowed_when_decimal_bids_enabled(self):
        """minimum_bid with cents is valid when only_whole_dollar_bids=False"""
        self.auction.only_whole_dollar_bids = False
        self.auction.save()
        data = self._get_form_data(self.auction, {"only_whole_dollar_bids": False, "minimum_bid": "5.50"})
        form = AuctionEditForm(data=data, instance=self.auction, user=self.user, cloned_from=None, user_timezone="UTC")
        form.is_valid()
        self.assertNotIn("minimum_bid", form.errors)

    def test_toggle_to_whole_dollar_rounds_existing_prices(self):
        self.auction.only_whole_dollar_bids = False
        self.auction.minimum_bid = Decimal("5.75")
        self.auction.save()
        lot = Lot.objects.create(
            lot_name="Decimal lot",
            auction=self.auction,
            auctiontos_seller=self.tos,
            reserve_price=Decimal("6.25"),
            buy_now_price=Decimal("7.75"),
            winning_price=Decimal("8.80"),
            quantity=1,
        )

        data = self._get_form_data(self.auction, {"only_whole_dollar_bids": True, "minimum_bid": "5.75"})
        form = AuctionEditForm(data=data, instance=self.auction, user=self.user, cloned_from=None, user_timezone="UTC")
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        self.auction.refresh_from_db()
        lot.refresh_from_db()
        self.assertEqual(self.auction.minimum_bid, Decimal(6))
        self.assertEqual(lot.reserve_price, Decimal(6))
        self.assertEqual(lot.buy_now_price, Decimal(8))
        self.assertEqual(lot.winning_price, Decimal(9))

        lot_data = {
            "lot_name": lot.lot_name,
            "auction": self.auction.pk,
            "quantity": lot.quantity,
            "reserve_price": str(lot.reserve_price),
            "buy_now_price": str(lot.buy_now_price),
            "part_of_auction": "True",
            "run_duration": "10",
            "cloned_from": "",
            "image_url": "",
        }
        lot_form = CreateLotForm(data=lot_data, instance=lot, user=self.user, cloned_from=None, auction=self.auction)
        self.assertTrue(lot_form.is_valid(), lot_form.errors)
        lot_form.save()
        lot.refresh_from_db()
        self.assertEqual(lot.reserve_price, Decimal(6))
        self.assertEqual(lot.buy_now_price, Decimal(8))

        fractional_lot_data = {**lot_data, "reserve_price": "6.50", "buy_now_price": "8.25"}
        fractional_lot_form = CreateLotForm(
            data=fractional_lot_data, instance=lot, user=self.user, cloned_from=None, auction=self.auction
        )
        self.assertFalse(fractional_lot_form.is_valid())
        self.assertIn("reserve_price", fractional_lot_form.errors)
        self.assertIn("buy_now_price", fractional_lot_form.errors)


class IntegerMoneyColumnRepairTests(TransactionTestCase):
    """Repairing a money column that's still an integer while Django believes it migrated.

    Migration 0437 converts any DecimalField whose column is an integer type; unconverted, every price
    read is an ``int`` and whole-dollar bids 500'd the edit page.
    """

    TABLE = "auctions_lot"
    #: What production had: unsigned integers, and one of them NOT NULL.
    DRIFTED = {"reserve_price": "int(10) unsigned NOT NULL", "winning_price": "int(10) unsigned NULL"}
    REPAIRED = {"reserve_price": "decimal(10, 2) NOT NULL", "winning_price": "decimal(10, 2) NULL"}

    def setUp(self):
        self.user = User.objects.create_user(username="money_column_user", password="testpassword")
        self.auction = Auction.objects.create(
            created_by=self.user,
            title="Money column auction",
            date_end=timezone.now() + datetime.timedelta(days=7),
            date_start=timezone.now() - datetime.timedelta(days=1),
            only_whole_dollar_bids=False,
            reserve_price="allow",
            buy_now="allow",
        )
        self.location = PickupLocation.objects.create(
            name="money column pickup",
            auction=self.auction,
            pickup_time=timezone.now() + datetime.timedelta(days=8),
        )
        self.tos = AuctionTOS.objects.create(user=self.user, auction=self.auction, pickup_location=self.location)
        self.lot = Lot.objects.create(
            lot_name="Money column lot",
            auction=self.auction,
            auctiontos_seller=self.tos,
            reserve_price=Decimal("2.00"),
            winning_price=Decimal("8.00"),
            quantity=1,
        )

    def _column_types(self):
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                [self.TABLE],
            )
            types = {column: data_type.lower() for column, data_type in cursor.fetchall()}
        return {column: types[column] for column in self.DRIFTED}

    def _set_column_types(self, definitions):
        from django.db import connection

        for column, definition in definitions.items():
            with connection.cursor() as cursor:
                cursor.execute(f"ALTER TABLE {self.TABLE} MODIFY COLUMN {column} {definition}")

    def _repair(self):
        import importlib

        from django.apps import apps
        from django.db import connection

        module = importlib.import_module("auctions.migrations.0437_fix_integer_money_columns")
        with connection.schema_editor() as schema_editor:
            module.fix_integer_money_columns(apps, schema_editor)

    def _toggle_whole_dollar_bids(self):
        data = {
            "title": self.auction.title,
            "summernote_description": self.auction.summernote_description or "",
            "lot_entry_fee": "0",
            "unsold_lot_fee": "0",
            "winning_bid_percent_to_club": "0",
            "winning_bid_percent_to_club_for_club_members": "0",
            "lot_entry_fee_for_club_members": "0",
            "pre_register_lot_discount_percent": "0",
            "alternate_split_mode": self.auction.alternate_split_mode,
            "alternative_split_label": self.auction.alternative_split_label or "",
            "reserve_price": self.auction.reserve_price,
            "buy_now": self.auction.buy_now,
            "tax": "0",
            "online_bidding": self.auction.online_bidding,
            "custom_field_1": self.auction.custom_field_1,
            "date_start": self.auction.date_start.strftime("%Y-%m-%d %H:%M:%S"),
            "date_end": self.auction.date_end.strftime("%Y-%m-%d %H:%M:%S"),
            "invoice_rounding": str(self.auction.invoice_rounding),
            "only_whole_dollar_bids": True,
            "minimum_bid": "2",
        }
        form = AuctionEditForm(data=data, instance=self.auction, user=self.user, cloned_from=None, user_timezone="UTC")
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

    def test_an_integer_money_column_is_converted_and_holds_cents_again(self):
        self._set_column_types(self.DRIFTED)
        try:
            drifted_lot = Lot.objects.get(pk=self.lot.pk)
            self.assertIsInstance(drifted_lot.winning_price, int)
            self.assertIsInstance(drifted_lot.reserve_price, int)
            # The integer column silently rounds 8.50 to 9.
            drifted_lot.winning_price = Decimal("8.50")
            drifted_lot.save()
            self.assertEqual(Lot.objects.get(pk=self.lot.pk).winning_price, 9)
            # This 500'd before the repair.
            self._toggle_whole_dollar_bids()
            self._repair()
            self.assertEqual(self._column_types(), dict.fromkeys(self.DRIFTED, "decimal"))
        finally:
            if self._column_types() != dict.fromkeys(self.DRIFTED, "decimal"):
                self._set_column_types(self.REPAIRED)
        lot = Lot.objects.get(pk=self.lot.pk)
        self.assertEqual(lot.reserve_price, Decimal("2.00"))
        lot.winning_price = Decimal("8.50")
        lot.save()
        self.assertEqual(Lot.objects.get(pk=self.lot.pk).winning_price, Decimal("8.50"))

    def test_the_repaired_column_keeps_its_nullability(self):
        """The conversion preserves each column's nullability."""
        from django.db import connection

        self._set_column_types(self.DRIFTED)
        try:
            self._repair()
        finally:
            if self._column_types() != dict.fromkeys(self.DRIFTED, "decimal"):
                self._set_column_types(self.REPAIRED)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COLUMN_NAME, IS_NULLABLE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                [self.TABLE],
            )
            nullable = dict(cursor.fetchall())
        self.assertEqual(nullable["reserve_price"], "NO")
        self.assertEqual(nullable["winning_price"], "YES")

    def test_a_decimal_money_column_is_left_alone(self):
        """On a database built from these migrations there is nothing to find."""
        self._repair()
        self.assertEqual(self._column_types(), dict.fromkeys(self.DRIFTED, "decimal"))
        self.assertEqual(Lot.objects.get(pk=self.lot.pk).winning_price, Decimal("8.00"))


class CreateLotFormWholeDollarValidationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="whole_dollar_lot_user", password="testpassword")
        self.auction = Auction.objects.create(
            created_by=self.user,
            title="Whole dollar lot form auction",
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=7),
            lot_submission_end_date=timezone.now() + datetime.timedelta(days=7),
            only_whole_dollar_bids=True,
        )
        self.location = PickupLocation.objects.create(
            name="whole dollar pickup",
            auction=self.auction,
            pickup_time=timezone.now() + datetime.timedelta(days=8),
        )
        AuctionTOS.objects.create(user=self.user, auction=self.auction, pickup_location=self.location)

    def test_single_lot_form_uses_whole_dollar_step_for_auction(self):
        form = CreateLotForm(user=self.user, cloned_from=None, auction=self.auction)
        self.assertEqual(form.fields["reserve_price"].widget.attrs.get("step"), "1")
        self.assertEqual(form.fields["reserve_price"].widget.attrs.get("min"), "1")
        self.assertEqual(form.fields["buy_now_price"].widget.attrs.get("step"), "1")
        self.assertEqual(form.fields["buy_now_price"].widget.attrs.get("min"), "1")

    def test_single_lot_form_rejects_fractional_prices_for_whole_dollar_auction(self):
        data = {
            "lot_name": "Whole dollar form lot",
            "auction": self.auction.pk,
            "quantity": 1,
            "reserve_price": "2.50",
            "buy_now_price": "3.75",
            "part_of_auction": "True",
            "run_duration": "10",
            "cloned_from": "",
            "image_url": "",
        }
        form = CreateLotForm(data=data, user=self.user, cloned_from=None, auction=self.auction)
        self.assertFalse(form.is_valid())
        self.assertIn("reserve_price", form.errors)
        self.assertIn("buy_now_price", form.errors)


class LotRefundDialogTests(TestCase):
    def setUp(self):
        time = timezone.now() - datetime.timedelta(days=2)
        timeStart = timezone.now() - datetime.timedelta(days=3)
        theFuture = timezone.now() + datetime.timedelta(days=3)
        self.user = User.objects.create_user(username="testuser", password="testpassword")
        self.user2 = User.objects.create_user(username="testuser2", password="password")
        self.auction = Auction.objects.create(
            created_by=self.user,
            title="A test auction",
            date_end=time,
            date_start=timeStart,
            winning_bid_percent_to_club=25,
            lot_entry_fee=2,
            unsold_lot_fee=10,
            tax=25,
        )
        self.location = PickupLocation.objects.create(name="location", auction=self.auction, pickup_time=theFuture)
        self.seller = AuctionTOS.objects.create(
            user=self.user,
            auction=self.auction,
            pickup_location=self.location,
            bidder_number="145",
        )
        self.bidder = AuctionTOS.objects.create(
            user=self.user2,
            auction=self.auction,
            pickup_location=self.location,
            bidder_number="225",
        )
        self.lot = Lot.objects.create(
            custom_lot_number="123",
            lot_name="A test lot",
            auction=self.auction,
            auctiontos_seller=self.seller,
            quantity=1,
        )
        self.lot2 = Lot.objects.create(
            custom_lot_number="124",
            lot_name="Another test lot",
            auction=self.auction,
            auctiontos_seller=self.seller,
            quantity=1,
        )
        self.client = Client()
        self.client.login(username="testuser", password="testpassword")
        self.lot_not_in_auction = Lot.objects.create(
            lot_name="not in auction",
            quantity=1,
            reserve_price=10,
            user=self.user,
            active=True,
        )
        self.lot_url = reverse("lot_refund", kwargs={"pk": self.lot.pk})

    def test_lot_not_in_auction(self):
        response = self.client.get(reverse("lot_refund", kwargs={"pk": self.lot_not_in_auction.pk}))
        assert response.status_code == 404

    def test_get_lot_refund_dialog(self):
        response = self.client.get(self.lot_url)
        assert response.status_code == 200
        self.assertTemplateUsed(response, "auctions/generic_admin_form.html")

    def test_post_lot_refund_dialog(self):
        data = {"partial_refund_percent": 50, "banned": False}
        response = self.client.post(self.lot_url, data)
        assert response.status_code == 200
        body = response.content.decode("utf-8")
        self.assertIn("closeModal", body)
        self.assertIn("reload-page", body)

        # Check if the lot was updated
        updated_lot = Lot.objects.get(pk=self.lot.pk)
        assert updated_lot.partial_refund_percent == 50
        assert updated_lot.banned is False


class BidDialogTests(StandardTestCase):
    """The dialog when the Bid button can't bid. Not being signed in isn't presented as a failure."""

    def setUp(self):
        super().setUp()
        self.live_auction = Auction.objects.create(
            created_by=self.user,
            title="A live online auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=3),
            promote_this_auction=True,
        )
        self.live_location = PickupLocation.objects.create(
            name="live location",
            auction=self.live_auction,
            pickup_time=timezone.now() + datetime.timedelta(days=5),
        )
        self.live_seller = AuctionTOS.objects.create(
            user=self.user,
            auction=self.live_auction,
            pickup_location=self.live_location,
            bidder_number="601",
        )
        self.live_lot = Lot.objects.create(
            lot_name="A live lot",
            auction=self.live_auction,
            auctiontos_seller=self.live_seller,
            quantity=1,
            reserve_price=5,
            active=True,
        )

    def _lot_page(self):
        return self.client.get(self.live_lot.lot_link).content.decode()

    def test_a_visitor_who_never_bid_is_not_told_their_bid_failed(self):
        page = self._lot_page()
        self.assertIn("Sign in to bid", page)
        self.assertNotIn("Bid failed", page)
        self.assertNotIn("bi-exclamation-circle-fill", page)

    def test_the_sign_in_dialog_offers_one_button_and_one_sentence(self):
        """The sign-in dialog has one sentence with a link and no extra button."""
        page = self._lot_page()
        self.assertIn("You have to <a href='/login/?next=", page)
        self.assertNotIn("Create an account</a>", page)
        footer = page.split('<div class="modal fade" id="bidError"')[1]
        self.assertEqual(footer.count('class="btn btn-primary"'), 1)

    def test_a_real_refusal_still_reads_as_one(self):
        """A signed-in user who hasn't joined sees a refusal."""
        self.client.login(username="no_joins", password="testpassword")
        page = self._lot_page()
        self.assertIn("Bid failed", page)
        self.assertNotIn("Sign in to bid", page)
        self.assertIn("read the auction's rules and join the auction", page)


class WholeDollarBidBoxTests(StandardTestCase):
    """Whole-dollar auctions show whole numbers in the bid box, next to its ".00"."""

    def setUp(self):
        super().setUp()
        self.live_auction = Auction.objects.create(
            created_by=self.user,
            title="A whole dollar auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=3),
            promote_this_auction=True,
        )
        self.live_location = PickupLocation.objects.create(
            name="whole dollar location",
            auction=self.live_auction,
            pickup_time=timezone.now() + datetime.timedelta(days=5),
        )
        self.dollar_seller = AuctionTOS.objects.create(
            user=self.user,
            auction=self.live_auction,
            pickup_location=self.live_location,
            bidder_number="701",
        )
        self.dollar_bidder = AuctionTOS.objects.create(
            user=self.user_with_no_lots,
            auction=self.live_auction,
            pickup_location=self.live_location,
            bidder_number="702",
        )
        self.dollar_lot = Lot.objects.create(
            lot_name="A whole dollar lot",
            auction=self.live_auction,
            auctiontos_seller=self.dollar_seller,
            quantity=1,
            reserve_price=5,
            active=True,
        )
        # Past the 20-minute new-lot hold; date_posted is auto_now_add.
        Lot.objects.filter(pk=self.dollar_lot.pk).update(date_posted=timezone.now() - datetime.timedelta(hours=1))

    def _bid_box(self):
        """The bid input tag itself."""
        self.client.login(username="no_lots", password="testpassword")
        page = self.client.get(self.dollar_lot.lot_link).content.decode()
        self.assertIn("id='bid_amount'", page)
        return page.split("id='bid_amount'")[1].split(">")[0]

    def test_the_box_holds_a_whole_number_when_the_auction_takes_whole_dollars(self):
        box = self._bid_box()
        self.assertIn('value="5"', box)
        self.assertNotIn('value="5.00"', box)

    def test_the_dot_zero_zero_suffix_is_the_only_place_cents_appear(self):
        """The suffix is the whole reason the value must not carry its own cents."""
        box = self._bid_box()
        self.assertIn('step="1"', box)

    def test_an_auction_that_takes_cents_still_offers_them(self):
        """The fix is about whole-dollar auctions; a cents auction keeps its cents."""
        self.live_auction.only_whole_dollar_bids = False
        self.live_auction.save()
        box = self._bid_box()
        self.assertIn('value="5.00"', box)
        self.assertIn('step="0.01"', box)
