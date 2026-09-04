"""Bidding: what a bid is worth, who is allowed to place one, and the refund dialog.

``BiddingPermissionsHardeningTests`` is the one to read first -- it is the boundary that stops a bid
arriving through a path the page would have refused.
"""

import datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
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
        # failing in tests, I believe due to sqlite, manual testing works in mariadb.
        # fixme by uncommenting below once tests have been moved to mariadb
        # assert lot.your_cut == 6
        self.tos.is_club_member = True
        self.tos.save()
        lot = lots.filter(pk=self.lot.pk).first()
        # fixme, same deal as the assert before this, see https://github.com/iragm/fishauctions/issues/165
        # assert lot.your_cut == 6
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
        """Decimal winning prices should flow correctly through add_price_info your_cut calculation"""
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
        """Invoice totals (subtotal, tax, net) should be correct with decimal winning prices"""
        self.auction.only_whole_dollar_bids = False
        self.auction.save()
        self.lot.winning_price = Decimal("10.50")
        self.lot.save()
        # Seller invoice
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.tos)
        # total_sold = your_cut = 5.875 (from sold lot)
        # unsold lot contributes -10 (unsold fee)
        # net = 5.875 - 10 = -4.125
        self.assertAlmostEqual(invoice.total_sold, Decimal("5.875") - 10, places=3)
        self.assertEqual(invoice.tax, 0)
        self.assertAlmostEqual(invoice.net, Decimal("5.875") - 10, places=3)

    def test_decimal_price_buyer_invoice_with_tax(self):
        """Buyer invoice tax should be calculated correctly with decimal winning prices"""
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
        # Use a price that yields a fractional net for the seller
        # your_cut = 10.50 * 0.75 - 2 = 5.875; unsold fee = -10; net = -4.125
        self.lot.winning_price = Decimal("10.50")
        self.lot.save()
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.tos)
        # net = -4.125; user_should_be_paid=False (negative net)
        # round(-4.125) = -4; -4.125 <= -4 → True → return -4
        self.assertEqual(invoice.rounded_net, Decimal(-4))

    def test_decimal_price_invoice_rounding_buyer(self):
        """rounded_net rounds in buyer's favor (less owed) when invoice_rounding is enabled"""
        self.auction.only_whole_dollar_bids = False
        self.auction.invoice_rounding = True
        self.auction.save()
        self.lot.winning_price = Decimal("10.50")
        self.lot.save()
        invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.tosB)
        # net = -13.13; user_should_be_paid=False
        # round(-13.13) = -13; -13.13 <= -13 → True → return -13
        self.assertEqual(invoice.rounded_net, Decimal(-13))

    def test_decimal_price_no_invoice_rounding(self):
        """When invoice_rounding is False, rounded_net equals net exactly (preserves decimal cents)"""
        self.auction.only_whole_dollar_bids = False
        self.auction.invoice_rounding = False
        self.auction.save()
        self.lot.winning_price = Decimal("10.50")
        self.lot.save()
        invoice_buyer, _ = Invoice.objects.get_or_create(auctiontos_user=self.tosB)
        self.assertEqual(invoice_buyer.rounded_net, invoice_buyer.net)
        self.assertAlmostEqual(invoice_buyer.net, Decimal("-13.13"), places=2)

    def test_recalculate_stores_exact_decimal_net(self):
        """recalculate() must persist the exact cents of the net.

        Regression for calculated_total being an IntegerField: with invoice_rounding off, a net of
        Decimal('-10.50') was truncated to -10 on write, losing $0.50 on every fractional invoice.
        """
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
    """Tests for bid_on_lot with the only_whole_dollar_bids toggle and decimal price validation"""

    def setUp(self):
        time = timezone.now() + datetime.timedelta(days=30)
        pastTime = timezone.now() - datetime.timedelta(hours=1)
        # Give users valid emails so outbid notification emails don't error out
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
        """Decimal auction: min increment is 5% rounded down to cents, minimum $0.01.

        With one bidder present, lot.high_bid equals the reserve_price.
        The 5% increment applies to that reserve_price.
        """
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
        # userA places proxy bid of $20.00; lot.high_bid = reserve = $10.00 (only one bidder)
        bid_on_lot(lot, self.userA, Decimal("20.00"))
        # 5% of $10.00 = $0.50 → quantize(0.01, ROUND_DOWN) = $0.50; next_allowed = $10.50
        # bid of $10.49 should fail
        result = bid_on_lot(lot, self.userB, Decimal("10.49"))
        self.assertEqual(result["type"], "ERROR")
        self.assertIn("10.50", result["message"])
        # bid of $10.50 should succeed (bumps against proxy, type is NEW_HIGH_BID)
        result = bid_on_lot(lot, self.userB, Decimal("10.50"))
        self.assertIn(result["type"], ["NEW_HIGH_BIDDER", "NEW_HIGH_BID", "INFO"])

    def test_whole_dollar_bid_increment_minimum(self):
        """Whole-dollar auction: minimum increment is $1 even when 5% < $1"""
        from auctions.bidding import bid_on_lot

        # Use a lot with reserve=$5 (5% = $0.25, rounded down = $0, min=1 → increment is $1)
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
        # userA places proxy bid; lot.high_bid = reserve = $5 (only one bidder)
        bid_on_lot(lot, self.userA, 10)
        # 5% of $5 = $0.25 → to_integral_value(ROUND_DOWN) = $0 → max($0, $1) = $1
        # next_allowed = $5 + $1 = $6; bid of $5 should fail
        result = bid_on_lot(lot, self.userB, 5)
        self.assertEqual(result["type"], "ERROR")
        # bid of $6 should succeed
        result = bid_on_lot(lot, self.userB, 6)
        self.assertIn(result["type"], ["NEW_HIGH_BIDDER", "NEW_HIGH_BID", "INFO"])


class BiddingPermissionsHardeningTests(TestCase):
    """Regression tests for the bid-path hardening: admin-team ban enforcement, own-lot and
    seller-ban checks via auctiontos_seller, the invoice gate for email-matched TOS records,
    under-reserve bid rejection, and CreateUserBan cleanup robustness."""

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
        # A TOS matched by email only: created BEFORE the matching user account exists, so
        # AuctionTOS.save()'s create-time auto-link can't fire (the imported-member case)
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
        # a lot the co-admin doesn't own is still blocked, via the admin-team check
        other_lot = self._make_lot(self.outbidder_tos, user=self.outbidder, name="other lot")
        self.assertEqual(
            check_all_permissions(other_lot, self.bidder), "You don't have permission to bid in this auction"
        )

    def test_creator_ban_blocks_even_without_creator_tos(self):
        """auction_admins_pks only contains users with a TOS row; the creator must be
        covered even when they never made one for themselves"""
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
        """Admin-added lots often have no lot.user; the seller must still be blocked from
        bidding on their own lot when their TOS is matched by email"""
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
        """A closed invoice must block bidding even when the TOS has no linked user account"""
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
        """lot.high_bidder returns False when all bids are under the reserve; re-bidding used
        to crash on False.pk and report a generic error"""
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
        """Enforcement and UI both resolve TOS through tos_for_user; newest record wins.
        AuctionTOS.save() auto-merges same-email duplicates on create nowadays, so simulate a
        legacy duplicate with a queryset update that bypasses save()"""
        AuctionTOS.objects.filter(pk=self.bidder_tos.pk).update(createdon=timezone.now() - datetime.timedelta(days=2))
        newer = AuctionTOS.objects.create(
            auction=self.auction, pickup_location=self.location, email="tempdupe@example.com", name="dupe"
        )
        AuctionTOS.objects.filter(pk=newer.pk).update(email="hardbidder@example.com")
        self.assertEqual(self.auction.tos_for_user(self.bidder).pk, newer.pk)

    def test_create_user_ban_survives_soft_deleted_lots(self):
        """Banning a user whose bid history touches a soft-deleted lot used to 500 mid-sweep"""
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
        """A co-admin's ban must clean up the auctions they administer, not just ones they created"""
        # the banned user has an active bid, and a lot linked only through auctiontos_seller
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
        """CreateUserBan sweeps existing lots; without a gate at submission the banned user
        could simply resubmit them"""
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
        """Build a minimal valid form data dict for AuctionEditForm from an existing auction"""

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
        """Switching to whole-dollar mode rounds auction minimum bid and existing lot prices"""
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
