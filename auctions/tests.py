from django.test import TestCase
import datetime
from django.test import TestCase
from django.utils import timezone
from django.db import IntegrityError
from django.contrib.auth.models import User
from django.test.client import Client
from .models import Lot, Bid

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

    def test_lot_with_two_bids_one_after_end(self):
        time = timezone.now() + datetime.timedelta(days=30)
        timeNow = timezone.now()
        afterEndTime = timezone.now() + datetime.timedelta(days=31)
        lot = Lot.objects.create(lot_name="A test lot", date_end=time, reserve_price=5)
        userA = User.objects.create(username="Test user")
        userB = User.objects.create(username="Test user B")
        bidA = Bid.objects.create(user=userA, lot_number=lot, amount=10)
        bidA.bid_time = afterEndTime
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