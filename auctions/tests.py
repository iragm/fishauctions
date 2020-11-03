import time
from django.test import TestCase
import datetime
from django.test import TestCase
from django.utils import timezone
from django.db import IntegrityError
from django.contrib.auth.models import User
from django.test.client import Client
from io import StringIO
from django.core.management import call_command
from .models import Lot, Bid, Auction, Invoice

class AuctionModelTests(TestCase):
    """Test for the auction model, duh"""
    def test_lots_in_auction_end_with_auction(self):
        time = timezone.now() - datetime.timedelta(days=2)
        timeStart = timezone.now() - datetime.timedelta(days=3)
        theFuture = timezone.now() + datetime.timedelta(days=3)
        auction = Auction.objects.create(title="A test auction", date_end=time, date_start=timeStart, pickup_location="None", pickup_location_map="None", pickup_time=theFuture)
        lot = Lot.objects.create(lot_name="A test lot", date_end=theFuture, reserve_price=5, auction=auction)
        self.assertIs(lot.ended, True)
    
    def test_auction_start_and_end(self):
        timeStart = timezone.now() - datetime.timedelta(days=2)
        timeEnd = timezone.now() + datetime.timedelta(minutes=60)
        theFuture = timezone.now() + datetime.timedelta(days=3)
        auction = Auction.objects.create(title="A test auction", date_end=timeEnd, date_start=timeStart, pickup_location="None", pickup_location_map="None", pickup_time=theFuture)
        self.assertIs(auction.closed, False)
        self.assertIs(auction.ending_soon, True)
        self.assertIs(auction.started, True)

class LotModelTests(TestCase):
    def test_calculated_end_bidding_closed(self):
        """
        Lot.ended should return true if the bidding has closed
        """
        time = timezone.now() + datetime.timedelta(days=30)
        testLot = Lot(lot_name="A test lot", date_end=time)
        self.assertIs(testLot.ended, False)
    
    def test_calculated_end_bidding_open(self):
        """
        Lot.ended should return false if the bidding is still open
        """
        time = timezone.now() - datetime.timedelta(days=1)
        testLot = Lot(lot_name="A test lot", date_end=time)
        self.assertIs(testLot.ended, True)

    # def test_lot_should_be_unique(self):
    #     """
    #     Lot.unique_together.  This test doesn't work with SQLlite
    #     """
    #     user = User(username="Test user")
    #     lotNumberA = Lot(lot_name="A test lot", user=user, active=True, description = "Unique")
        
    #     #self.assertRaises(lotNumberA, lotNumberB)
        
    #     with self.assertRaises(IntegrityError):
    #         lotNumberB = Lot(lot_name="A test lot", user=user, active=True, description = "Unique")

    def test_lot_with_no_bids(self):
        time = timezone.now() + datetime.timedelta(days=30)
        lot = Lot(lot_name="A lot with no bids", date_end=time, reserve_price=5)
        self.assertIs(lot.high_bid, 5)
    
    def test_lot_with_one_bids(self):
        time = timezone.now() + datetime.timedelta(days=30)
        timeNow = timezone.now()
        lot = Lot.objects.create(lot_name="A test lot", date_end=time, reserve_price=5)
        user = User.objects.create(username="Test user")
        bidA = Bid.objects.create(user=user, lot_number=lot, amount=10)
        self.assertIs(lot.high_bidder.pk, user.pk)
        self.assertIs(lot.high_bid, 5)
    
    def test_lot_with_two_bids(self):
        time = timezone.now() + datetime.timedelta(days=30)
        timeNow = timezone.now()
        lot = Lot.objects.create(lot_name="A test lot", date_end=time, reserve_price=5)
        userA = User.objects.create(username="Test user")
        userB = User.objects.create(username="Test user B")
        bidA = Bid.objects.create(user=userA, lot_number=lot, amount=10)
        bidB = Bid.objects.create(user=userB, lot_number=lot, amount=6)
        self.assertIs(lot.high_bidder.pk, userA.pk)
        self.assertIs(lot.high_bid, 7)

    def test_lot_with_tie_bids(self):
        time = timezone.now() + datetime.timedelta(days=30)
        tenDaysAgo = timezone.now() - datetime.timedelta(days=10)
        fiveDaysAgo = timezone.now() - datetime.timedelta(days=5)
        timeNow = timezone.now()
        lot = Lot.objects.create(lot_name="A test lot", date_end=time, reserve_price=5)
        userA = User.objects.create(username="Late user")
        userB = User.objects.create(username="Early bird")
        bidA = Bid.objects.create(user=userA, lot_number=lot, amount=6)
        bidB = Bid.objects.create(user=userB, lot_number=lot, amount=6)
        bidA.last_bid_time=fiveDaysAgo
        bidA.save()
        bidB.last_bid_time=tenDaysAgo
        bidB.save()
        self.assertIs(lot.high_bidder.pk, userB.pk)
        self.assertIs(lot.high_bid, 6)
        self.assertIs(lot.max_bid, 6)

    def test_lot_with_three_and_two_tie_bids(self):
        time = timezone.now() + datetime.timedelta(days=30)
        tenDaysAgo = timezone.now() - datetime.timedelta(days=10)
        fiveDaysAgo = timezone.now() - datetime.timedelta(days=5)
        oneDaysAgo = timezone.now() - datetime.timedelta(days=1)
        timeNow = timezone.now()
        lot = Lot.objects.create(lot_name="A test lot", date_end=time, reserve_price=5)
        userA = User.objects.create(username="Early bidder")
        userB = User.objects.create(username="First tie")
        userC = User.objects.create(username="Late tie")
        bidA = Bid.objects.create(user=userA, lot_number=lot, amount=5)
        bidB = Bid.objects.create(user=userB, lot_number=lot, amount=7)
        bidC = Bid.objects.create(user=userC, lot_number=lot, amount=7)
        bidA.last_bid_time=tenDaysAgo
        bidA.save()
        bidB.last_bid_time=fiveDaysAgo
        bidB.save()
        bidC.last_bid_time=oneDaysAgo
        bidC.save()
        self.assertIs(lot.high_bidder.pk, userB.pk)
        self.assertIs(lot.high_bid, 7)
        self.assertIs(lot.max_bid, 7)

    def test_lot_with_two_bids_one_after_end(self):
        time = timezone.now() + datetime.timedelta(days=30)
        timeNow = timezone.now()
        afterEndTime = timezone.now() + datetime.timedelta(days=31)
        lot = Lot.objects.create(lot_name="A test lot", date_end=time, reserve_price=5)
        userA = User.objects.create(username="Test user")
        userB = User.objects.create(username="Test user B")
        bidA = Bid.objects.create(user=userA, lot_number=lot, amount=10)
        bidA.last_bid_time = afterEndTime
        bidA.save()
        bidB = Bid.objects.create(user=userB, lot_number=lot, amount=6)
        self.assertIs(lot.high_bidder.pk, userB.pk)
        self.assertIs(lot.high_bid, 5)

    def test_lot_with_one_bids_below_reserve(self):
        time = timezone.now() + datetime.timedelta(days=30)
        timeNow = timezone.now()
        lot = Lot.objects.create(lot_name="A test lot", date_end=time, reserve_price=5)
        user = User.objects.create(username="Test user")
        bidA = Bid.objects.create(user=user, lot_number=lot, amount=2)
        self.assertIs(lot.high_bidder, False)
        self.assertIs(lot.high_bid, 5)

class InvoiceModelTests(TestCase):
    """Make sure auctions/lots end and invoices get created correctly"""
    def test_invoices(self):
        # setting up
        timeStart = timezone.now() - datetime.timedelta(days=2)
        bidTime = timezone.now() - datetime.timedelta(days=1)
        timeEnd = timezone.now() + datetime.timedelta(minutes=60)
        theFuture = timezone.now() + datetime.timedelta(days=3)
        auction = Auction.objects.create(title="A test auction", date_end=timeEnd, date_start=timeStart, pickup_location="None", pickup_location_map="None", pickup_time=theFuture, winning_bid_percent_to_club=25, lot_entry_fee=2, unsold_lot_fee=1)
        seller = User.objects.create(username="Seller")
        lot = Lot.objects.create(lot_name="A test lot", date_end=timeStart, reserve_price=5, auction=auction, user=seller)
        unsoldLot = Lot.objects.create(lot_name="Unsold lot", date_end=timeStart, reserve_price=10, auction=auction, user=seller)
        userA = User.objects.create(username="Winner of the lot")
        bid = Bid.objects.create(user=userA, lot_number=lot, amount=10)
        bid.last_bid_time = bidTime
        bid.save()
        # other tests check all these as well
        self.assertIs(auction.ending_soon, True)
        self.assertIs(auction.closed, False)
        self.assertIs(lot.winner, None)
        self.assertIs(lot.ended, False)
        self.assertIs(lot.high_bidder.pk, userA.pk)
        # change the time
        timeEnd = timezone.now() - datetime.timedelta(minutes=60)
        auction.date_end = timeEnd
        auction.save()
        self.assertIs(auction.closed, True)
        self.assertIs(lot.ended, True)
        self.assertIs(lot.high_bidder.pk, userA.pk)
        out = StringIO()
        call_command('endauctions', stdout=out)
        self.assertIn(f'has been won by {userA}', out.getvalue())
        lot.refresh_from_db()
        self.assertIs(lot.winner.pk, userA.pk)
        self.assertIs(lot.active, False)
        self.assertIs(lot.winning_price, 5)
        call_command('invoice', stdout=out)
        auction.refresh_from_db()
        self.assertIs(auction.invoiced, True)
        
        # check seller invoice
        invoice = Invoice.objects.get(user=seller)
        self.assertIs(invoice.user_should_be_paid, True)
        self.assertIs(invoice.total_bought, 0)
        self.assertAlmostEqual(lot.club_cut, 3.25)
        self.assertAlmostEqual(lot.your_cut, 1.75)
        self.assertAlmostEqual(invoice.total_sold, 0.75)
        self.assertAlmostEqual(invoice.absolute_amount, 1)
        self.assertAlmostEqual(invoice.net, 0.75)
       
        # check buyer invoice
        invoice = Invoice.objects.get(user=userA)
        self.assertIs(invoice.user_should_be_paid, False)
        self.assertIs(invoice.total_bought, 5)
        self.assertAlmostEqual(invoice.total_sold, 0)
        self.assertAlmostEqual(invoice.absolute_amount, 5)
        self.assertAlmostEqual(invoice.net, -5)