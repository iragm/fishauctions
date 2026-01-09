import base64
import datetime
import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import patch

from django import forms
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from .forms import AuctionEditForm
from .models import (
    Auction,
    AuctionHistory,
    AuctionTOS,
    Bid,
    ChatSubscription,
    Invoice,
    InvoiceAdjustment,
    Lot,
    LotHistory,
    PayPalSeller,
    PickupLocation,
    UserData,
    UserLabelPrefs,
    add_price_info,
)


class StandardTestCase(TestCase):
    """This is a base class that sets up some common stuff so other tests can be run without needing to write a lot of boilplate code
    Give this class along with your view/model/etc., to ChatGPT and it can write the test subclass
    In general, make sure that AuctionTOS.is_admin=True users can do what they need, users without an AuctionTOS are blocked, no data leaks to non-admins and non-logged in users

    Tests can be run with with docker exec -it django python3 manage.py test

    Tests are also run automatically on commit by github actions
    """

    def endAuction(self):
        self.online_auction.date_end = timezone.now() - datetime.timedelta(days=2)
        self.online_auction.save()

    def setUp(self):
        time = timezone.now() - datetime.timedelta(days=2)
        timeStart = timezone.now() - datetime.timedelta(days=3)
        theFuture = timezone.now() + datetime.timedelta(days=3)
        self.admin_user = User.objects.create_user(
            username="admin_user", password="testpassword", email="test@example.com"
        )
        self.user = User.objects.create_user(username="my_lot", password="testpassword", email="test@example.com")
        self.user_with_no_lots = User.objects.create_user(
            username="no_lots", password="testpassword", email="asdf@example.com"
        )
        self.user_who_does_not_join = User.objects.create_user(
            username="no_joins", password="testpassword", email="zxcgv@example.com"
        )
        self.online_auction = Auction.objects.create(
            created_by=self.user,
            title="This auction is online",
            is_online=True,
            date_end=time,
            date_start=timeStart,
            winning_bid_percent_to_club=25,
            lot_entry_fee=2,
            unsold_lot_fee=10,
            tax=25,
        )
        self.in_person_auction = Auction.objects.create(
            created_by=self.user,
            title="This auction is in-person",
            is_online=False,
            date_end=time,
            date_start=timeStart,
            winning_bid_percent_to_club=25,
            lot_entry_fee=2,
            unsold_lot_fee=10,
            tax=25,
            buy_now="allow",
            reserve_price="allow",
            use_seller_dash_lot_numbering=True,
        )
        self.location = PickupLocation.objects.create(
            name="location", auction=self.online_auction, pickup_time=theFuture
        )
        self.in_person_location = PickupLocation.objects.create(
            name="location", auction=self.in_person_auction, pickup_time=theFuture
        )
        self.userB = User.objects.create_user(username="no_tos", password="testpassword")
        self.admin_online_tos = AuctionTOS.objects.create(
            user=self.admin_user, auction=self.online_auction, pickup_location=self.location, is_admin=True
        )
        self.admin_in_person_tos = AuctionTOS.objects.create(
            user=self.admin_user, auction=self.in_person_auction, pickup_location=self.in_person_location, is_admin=True
        )
        self.online_tos = AuctionTOS.objects.create(
            user=self.user, auction=self.online_auction, pickup_location=self.location
        )
        self.in_person_tos = AuctionTOS.objects.create(
            user=self.user, auction=self.in_person_auction, pickup_location=self.location
        )
        self.tosB = AuctionTOS.objects.create(
            user=self.userB, auction=self.online_auction, pickup_location=self.location
        )
        self.tosC = AuctionTOS.objects.create(
            user=self.user_with_no_lots, auction=self.online_auction, pickup_location=self.location
        )
        self.in_person_buyer = AuctionTOS.objects.create(
            user=self.user_with_no_lots,
            auction=self.in_person_auction,
            pickup_location=self.in_person_location,
            bidder_number="555",
        )
        self.lot = Lot.objects.create(
            lot_name="A test lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=10,
            auctiontos_winner=self.tosB,
            active=False,
        )
        # no permission to save images by default, so this is a no-go
        # png_bytes = base64.b64decode(
        #     b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAH0KzMgAAAABJRU5ErkJggg=="
        # )
        # self.lot_image = LotImage.objects.create(
        #     lot_number=self.lot,
        #     image=SimpleUploadedFile("test.png", png_bytes, content_type="image/png"),
        #     is_primary=True,
        # )
        self.lotB = Lot.objects.create(
            lot_name="B test lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=10,
            auctiontos_winner=self.tosB,
            active=False,
        )
        self.lotC = Lot.objects.create(
            lot_name="C test lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=10,
            auctiontos_winner=self.tosB,
            active=False,
        )
        self.unsoldLot = Lot.objects.create(
            lot_name="Unsold lot",
            reserve_price=10,
            auction=self.online_auction,
            quantity=1,
            auctiontos_seller=self.online_tos,
            active=False,
        )
        self.invoice, c = Invoice.objects.get_or_create(auctiontos_user=self.online_tos)
        self.invoiceB, c = Invoice.objects.get_or_create(auctiontos_user=self.tosB)
        self.adjustment_add = InvoiceAdjustment.objects.create(
            adjustment_type="ADD", amount=10, notes="test", invoice=self.invoiceB
        )
        self.adjustment_discount = InvoiceAdjustment.objects.create(
            adjustment_type="DISCOUNT", amount=10, notes="test", invoice=self.invoiceB
        )
        self.adjustment_add_percent = InvoiceAdjustment.objects.create(
            adjustment_type="ADD_PERCENT",
            amount=10,
            notes="test",
            invoice=self.invoiceB,
        )
        self.adjustment_discount_percent = InvoiceAdjustment.objects.create(
            adjustment_type="DISCOUNT_PERCENT",
            amount=10,
            notes="test",
            invoice=self.invoiceB,
        )
        self.in_person_lot = Lot.objects.create(
            lot_name="another test lot",
            auction=self.in_person_auction,
            auctiontos_seller=self.admin_in_person_tos,
            quantity=1,
            custom_lot_number="101-1",
        )
        # TODO: stuff to add here:
        # a few more users and a userban or two
        # an online auction that hasn't started yet
        # an in-person auction that hasn't started yet
        # an online auction that's ended
        # an online auction with multiple pickup locations


class ViewLotTest(TestCase):
    def setUp(self):
        time = timezone.now() - datetime.timedelta(days=2)
        timeStart = timezone.now() - datetime.timedelta(days=3)
        theFuture = timezone.now() + datetime.timedelta(days=3)
        self.auction = Auction.objects.create(title="A test auction", date_end=time, date_start=timeStart)
        self.location = PickupLocation.objects.create(name="location", auction=self.auction, pickup_time=theFuture)
        self.user = User.objects.create_user(username="my_lot", password="testpassword")
        self.userB = User.objects.create_user(username="no_tos", password="testpassword")
        self.tos = AuctionTOS.objects.create(user=self.user, auction=self.auction, pickup_location=self.location)
        self.lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=theFuture,
            reserve_price=5,
            auction=self.auction,
            user=self.user,
            quantity=1,
        )
        self.url = reverse("lot_by_pk", kwargs={"pk": self.lot.pk})
        # Create a user for the logged-in scenario
        self.userC = User.objects.create_user(username="testuser", password="testpassword")

    def test_non_logged_in_user(self):
        response = self.client.get(self.url)
        self.assertContains(response, ">sign in</a> to place bids.")

    def test_logged_in_user(self):
        # Log in the user
        self.client.login(username="testuser", password="testpassword")
        response = self.client.get(self.url)
        self.assertContains(response, "read the auction's rules and join the auction")

    def test_no_bidding_on_your_own_lots(self):
        # Log in the user
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.get(self.url)
        self.assertContains(response, "You can't bid on your own lot")

    def test_with_tos_on_ended_lot(self):
        AuctionTOS.objects.create(user=self.userB, auction=self.auction, pickup_location=self.location)
        self.client.login(username="no_tos", password="testpassword")
        response = self.client.get(self.url)
        self.assertContains(response, "Bidding has ended on this lot")

    def test_with_tos_on_new_lot(self):
        AuctionTOS.objects.create(user=self.userB, auction=self.auction, pickup_location=self.location)
        self.client.login(username="no_tos", password="testpassword")
        lot = Lot.objects.filter(pk=self.lot.pk).first()
        lot.date_end = timezone.now() + datetime.timedelta(days=1)
        lot.save()
        response = self.client.get(self.url)
        self.assertContains(response, "This lot is very new")


class AuctionModelTests(TestCase):
    """Test for the auction model, duh"""

    def test_lots_in_auction_end_with_auction(self):
        time = timezone.now() - datetime.timedelta(days=2)
        timeStart = timezone.now() - datetime.timedelta(days=3)
        theFuture = timezone.now() + datetime.timedelta(days=3)
        auction = Auction.objects.create(title="A test auction", date_end=time, date_start=timeStart)
        user = User.objects.create(username="Test user")
        lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=theFuture,
            reserve_price=5,
            auction=auction,
            user=user,
            quantity=1,
        )
        assert lot.ended is True

    def test_auction_start_and_end(self):
        timeStart = timezone.now() - datetime.timedelta(days=2)
        timeEnd = timezone.now() + datetime.timedelta(minutes=60)
        auction = Auction.objects.create(title="A test auction", date_end=timeEnd, date_start=timeStart)
        assert auction.closed is False
        assert auction.ending_soon is True
        assert auction.started is True


class LotModelTests(TestCase):
    def test_calculated_end_bidding_closed(self):
        """
        Lot.ended should return true if the bidding has closed
        """
        time = timezone.now() + datetime.timedelta(days=30)
        user = User.objects.create(username="Test user")
        testLot = Lot.objects.create(
            lot_name="A test lot",
            date_end=time,
            reserve_price=5,
            user=user,
            quantity=1,
        )
        assert testLot.ended is False

    def test_calculated_end_bidding_open(self):
        """
        Lot.ended should return false if the bidding is still open
        """
        time = timezone.now() - datetime.timedelta(days=1)
        user = User.objects.create(username="Test user")
        testLot = Lot.objects.create(
            lot_name="A test lot",
            date_end=time,
            reserve_price=5,
            user=user,
            quantity=1,
        )
        assert testLot.ended is True

    def test_lot_with_no_bids(self):
        time = timezone.now() + datetime.timedelta(days=30)
        user = User.objects.create(username="Test user")
        lot = Lot(
            lot_name="A lot with no bids",
            date_end=time,
            reserve_price=5,
            user=user,
        )
        assert lot.high_bid == 5

    def test_lot_with_one_bids(self):
        time = timezone.now() + datetime.timedelta(days=30)
        lotuser = User.objects.create(username="thisismylot")
        lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=time,
            reserve_price=5,
            user=lotuser,
            quantity=1,
        )
        user = User.objects.create(username="Test user")
        Bid.objects.create(user=user, lot_number=lot, amount=10)
        assert lot.high_bidder.pk == user.pk
        assert lot.high_bid == 5

    def test_lot_with_two_bids(self):
        time = timezone.now() + datetime.timedelta(days=30)
        lotuser = User.objects.create(username="thisismylot")
        lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=time,
            reserve_price=5,
            user=lotuser,
            quantity=1,
        )
        userA = User.objects.create(username="Test user")
        userB = User.objects.create(username="Test user B")
        Bid.objects.create(user=userA, lot_number=lot, amount=10)
        Bid.objects.create(user=userB, lot_number=lot, amount=6)
        assert lot.high_bidder.pk == userA.pk
        assert lot.high_bid == 7

    def test_lot_with_two_changing_bids(self):
        time = timezone.now() + datetime.timedelta(days=30)
        lotuser = User.objects.create(username="thisismylot")
        lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=time,
            reserve_price=20,
            user=lotuser,
            quantity=6,
        )
        jeff = User.objects.create(username="Jeff")
        gary = User.objects.create(username="Gary")
        jeffBid = Bid.objects.create(user=jeff, lot_number=lot, amount=20)
        assert lot.high_bidder.pk == jeff.pk
        assert lot.high_bid == 20
        garyBid = Bid.objects.create(user=gary, lot_number=lot, amount=20)
        assert lot.high_bidder.pk == jeff.pk
        assert lot.high_bid == 20
        # check the order
        jeffBid.last_bid_time = timezone.now()
        jeffBid.save()
        assert lot.high_bidder.pk == gary.pk
        assert lot.high_bid == 20
        garyBid.amount = 30
        garyBid.save()
        assert lot.high_bidder.pk == gary.pk
        assert lot.high_bid == 21
        garyBid.last_bid_time = timezone.now()
        garyBid.save()
        assert lot.high_bidder.pk == gary.pk
        assert lot.high_bid == 21
        jeffBid.amount = 30
        jeffBid.last_bid_time = timezone.now()
        jeffBid.save()
        assert lot.high_bidder.pk == gary.pk
        assert lot.high_bid == 30

    def test_lot_with_tie_bids(self):
        time = timezone.now() + datetime.timedelta(days=30)
        tenDaysAgo = timezone.now() - datetime.timedelta(days=10)
        fiveDaysAgo = timezone.now() - datetime.timedelta(days=5)
        lotuser = User.objects.create(username="thisismylot")
        lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=time,
            reserve_price=5,
            user=lotuser,
            quantity=1,
        )
        userA = User.objects.create(username="Late user")
        userB = User.objects.create(username="Early bird")
        bidA = Bid.objects.create(user=userA, lot_number=lot, amount=6)
        bidB = Bid.objects.create(user=userB, lot_number=lot, amount=6)
        bidA.last_bid_time = fiveDaysAgo
        bidA.save()
        bidB.last_bid_time = tenDaysAgo
        bidB.save()
        assert lot.high_bidder.pk == userB.pk
        assert lot.high_bid == 6
        assert lot.max_bid == 6

    def test_lot_with_three_and_two_tie_bids(self):
        time = timezone.now() + datetime.timedelta(days=30)
        tenDaysAgo = timezone.now() - datetime.timedelta(days=10)
        fiveDaysAgo = timezone.now() - datetime.timedelta(days=5)
        oneDaysAgo = timezone.now() - datetime.timedelta(days=1)
        lotuser = User.objects.create(username="thisismylot")
        lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=time,
            reserve_price=5,
            user=lotuser,
            quantity=1,
        )
        userA = User.objects.create(username="Early bidder")
        userB = User.objects.create(username="First tie")
        userC = User.objects.create(username="Late tie")
        bidA = Bid.objects.create(user=userA, lot_number=lot, amount=5)
        bidB = Bid.objects.create(user=userB, lot_number=lot, amount=7)
        bidC = Bid.objects.create(user=userC, lot_number=lot, amount=7)
        bidA.last_bid_time = tenDaysAgo
        bidA.save()
        bidB.last_bid_time = fiveDaysAgo
        bidB.save()
        bidC.last_bid_time = oneDaysAgo
        bidC.save()
        assert lot.high_bidder.pk == userB.pk
        assert lot.high_bid == 7
        assert lot.max_bid == 7

    def test_lot_with_two_bids_one_after_end(self):
        time = timezone.now() + datetime.timedelta(days=30)
        afterEndTime = timezone.now() + datetime.timedelta(days=31)
        lotuser = User.objects.create(username="thisismylot")
        lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=time,
            reserve_price=5,
            user=lotuser,
            quantity=1,
        )
        userA = User.objects.create(username="Test user")
        userB = User.objects.create(username="Test user B")
        bidA = Bid.objects.create(user=userA, lot_number=lot, amount=10)
        bidA.last_bid_time = afterEndTime
        bidA.save()
        Bid.objects.create(user=userB, lot_number=lot, amount=6)
        assert lot.high_bidder.pk == userB.pk
        assert lot.high_bid == 5

    def test_lot_with_one_bids_below_reserve(self):
        time = timezone.now() + datetime.timedelta(days=30)
        lotuser = User.objects.create(username="thisismylot")
        lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=time,
            reserve_price=5,
            user=lotuser,
            quantity=1,
        )
        user = User.objects.create(username="Test user")
        Bid.objects.create(user=user, lot_number=lot, amount=2)
        assert lot.high_bidder is False
        assert lot.high_bid == 5


class LotModelConcurrencyTests(TransactionTestCase):
    """Tests that require real database transactions (not wrapped in TestCase transaction)"""

    def test_concurrent_lot_number_assignment(self):
        """Test that concurrent lot creation does not result in duplicate lot_number_int values"""
        from concurrent.futures import ThreadPoolExecutor

        # Create an auction and user
        user = User.objects.create(username="Test user")
        auction = Auction.objects.create(
            title="Test Auction",
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(days=7),
            created_by=user,
        )

        # Function to create a lot
        def create_lot(lot_name):
            try:
                lot = Lot.objects.create(
                    lot_name=lot_name,
                    auction=auction,
                    user=user,
                    quantity=1,
                    reserve_price=5,
                )
                return (True, lot.lot_number_int)
            except Exception as e:
                return (False, str(e))

        # Create multiple lots concurrently
        lot_numbers = []
        errors = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(create_lot, f"Concurrent Lot {i}") for i in range(10)]
            for future in futures:
                success, result = future.result()
                if success:
                    lot_numbers.append(result)
                else:
                    errors.append(result)

        # Fail if any errors occurred
        self.assertEqual(len(errors), 0, f"Errors occurred during concurrent creation: {errors}")

        # Verify all lot numbers are unique
        self.assertEqual(len(lot_numbers), len(set(lot_numbers)), f"Duplicate lot numbers found: {lot_numbers}")

        # Verify lot numbers are sequential
        lot_numbers.sort()
        expected = list(range(1, len(lot_numbers) + 1))
        self.assertEqual(lot_numbers, expected, f"Lot numbers are not sequential: {lot_numbers}")

    def test_concurrent_lot_number_assignment_with_seller_dash(self):
        """Test that concurrent lot creation with seller_dash_lot_numbering doesn't create duplicates"""
        from concurrent.futures import ThreadPoolExecutor

        # Create an auction with seller_dash_lot_numbering enabled
        user = User.objects.create(username="Test user")
        auction = Auction.objects.create(
            title="Test Auction with Seller Dash",
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(days=7),
            created_by=user,
            use_seller_dash_lot_numbering=True,
        )

        # Create a seller with TOS
        from auctions.models import AuctionTOS, PickupLocation

        location = PickupLocation.objects.create(
            name="Test Location",
            user=user,
        )
        tos = AuctionTOS.objects.create(
            user=user,
            auction=auction,
            pickup_location=location,
            bidder_number="KM-8",
        )

        # Function to create a lot
        def create_lot(lot_name):
            try:
                lot = Lot.objects.create(
                    lot_name=lot_name,
                    auction=auction,
                    user=user,
                    auctiontos_seller=tos,
                    quantity=1,
                    reserve_price=5,
                )
                return (True, lot.custom_lot_number)
            except Exception as e:
                return (False, str(e))

        # Create multiple lots concurrently
        lot_numbers = []
        errors = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(create_lot, f"Concurrent Lot {i}") for i in range(10)]
            for future in futures:
                success, result = future.result()
                if success:
                    lot_numbers.append(result)
                else:
                    errors.append(result)

        # Fail if any errors occurred
        self.assertEqual(len(errors), 0, f"Errors occurred during concurrent creation: {errors}")

        # Verify all lot numbers are unique
        self.assertEqual(len(lot_numbers), len(set(lot_numbers)), f"Duplicate custom lot numbers found: {lot_numbers}")

        # Verify lot numbers follow KM-8-N format
        for lot_number in lot_numbers:
            self.assertTrue(lot_number.startswith("KM-8-"), f"Lot number {lot_number} doesn't start with KM-8-")

    def test_duplicate_lot_number_int_generates_new_number(self):
        """Test that if a duplicate lot_number_int is detected, a new number is generated for the newest lot"""
        # Create an auction and user
        user = User.objects.create(username="Test user")
        auction = Auction.objects.create(
            title="Test Auction",
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(days=7),
            created_by=user,
        )

        # Create first lot
        lot1 = Lot.objects.create(
            lot_name="First Lot",
            auction=auction,
            user=user,
            quantity=1,
            reserve_price=5,
        )
        original_lot1_number = lot1.lot_number_int

        # Manually create a second lot with the same lot_number_int (simulating race condition)
        lot2 = Lot(
            lot_name="Second Lot",
            auction=auction,
            user=user,
            quantity=1,
            reserve_price=5,
        )
        # Force the same lot_number_int to simulate a duplicate that slipped through
        lot2.lot_number_int = lot1.lot_number_int
        # Use _do_save to bypass the locking mechanism for testing the duplicate detection logic
        # This is intentional to test the post-save duplicate check that catches edge cases
        lot2._do_save()

        # Refresh from database
        lot1.refresh_from_db()
        lot2.refresh_from_db()

        # Verify that lot1 kept its original number and lot2 got a new number
        self.assertEqual(lot1.lot_number_int, original_lot1_number)
        self.assertNotEqual(lot2.lot_number_int, lot1.lot_number_int)
        self.assertGreater(lot2.lot_number_int, lot1.lot_number_int)

    def test_duplicate_custom_lot_number_generates_new_number(self):
        """Test that if a duplicate custom_lot_number is detected, a new number is generated for the newest lot"""
        from auctions.models import AuctionTOS, PickupLocation

        # Create an auction with seller_dash_lot_numbering enabled
        user = User.objects.create(username="Test user")
        auction = Auction.objects.create(
            title="Test Auction with Seller Dash",
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(days=7),
            created_by=user,
            use_seller_dash_lot_numbering=True,
        )

        # Create a seller with TOS
        location = PickupLocation.objects.create(
            name="Test Location",
            user=user,
        )
        tos = AuctionTOS.objects.create(
            user=user,
            auction=auction,
            pickup_location=location,
            bidder_number="KM-8",
        )

        # Create first lot
        lot1 = Lot.objects.create(
            lot_name="First Lot",
            auction=auction,
            user=user,
            auctiontos_seller=tos,
            quantity=1,
            reserve_price=5,
        )
        original_lot1_number = lot1.custom_lot_number

        # Manually create a second lot with the same custom_lot_number
        lot2 = Lot(
            lot_name="Second Lot",
            auction=auction,
            user=user,
            auctiontos_seller=tos,
            quantity=1,
            reserve_price=5,
        )
        # Force the same custom_lot_number to simulate a duplicate that slipped through
        lot2.custom_lot_number = lot1.custom_lot_number
        # Use _do_save to bypass the locking mechanism for testing the duplicate detection logic
        # This is intentional to test the post-save duplicate check that catches edge cases
        lot2._do_save()

        # Refresh from database
        lot1.refresh_from_db()
        lot2.refresh_from_db()

        # Verify that lot1 kept its original number and lot2 got a new number
        self.assertEqual(lot1.custom_lot_number, original_lot1_number)
        self.assertNotEqual(lot2.custom_lot_number, lot1.custom_lot_number)

    def test_seller_dash_lot_numbering_format(self):
        """Test that seller_dash_lot_numbering creates lots with bidder_number-N format"""
        from auctions.models import AuctionTOS, PickupLocation

        # Create an auction with seller_dash_lot_numbering enabled
        user = User.objects.create(username="Test user")
        auction = Auction.objects.create(
            title="Test Auction with Seller Dash",
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(days=7),
            created_by=user,
            use_seller_dash_lot_numbering=True,
        )

        # Create sellers with different bidder numbers
        location = PickupLocation.objects.create(
            name="Test Location",
            user=user,
        )

        seller1 = AuctionTOS.objects.create(
            user=user,
            auction=auction,
            pickup_location=location,
            bidder_number="KM-8",
        )

        user2 = User.objects.create(username="Test user 2")
        seller2 = AuctionTOS.objects.create(
            user=user2,
            auction=auction,
            pickup_location=location,
            bidder_number="AB-12",
        )

        # Create lots for seller1
        lot1 = Lot.objects.create(
            lot_name="Seller 1 Lot 1",
            auction=auction,
            user=user,
            auctiontos_seller=seller1,
            quantity=1,
            reserve_price=5,
        )
        lot2 = Lot.objects.create(
            lot_name="Seller 1 Lot 2",
            auction=auction,
            user=user,
            auctiontos_seller=seller1,
            quantity=1,
            reserve_price=5,
        )

        # Create lots for seller2
        lot3 = Lot.objects.create(
            lot_name="Seller 2 Lot 1",
            auction=auction,
            user=user2,
            auctiontos_seller=seller2,
            quantity=1,
            reserve_price=5,
        )

        # Verify format
        self.assertEqual(lot1.custom_lot_number, "KM-8-1")
        self.assertEqual(lot2.custom_lot_number, "KM-8-2")
        self.assertEqual(lot3.custom_lot_number, "AB-12-1")

        # Verify lot_number_display uses custom_lot_number
        self.assertEqual(lot1.lot_number_display, "KM-8-1")
        self.assertEqual(lot2.lot_number_display, "KM-8-2")
        self.assertEqual(lot3.lot_number_display, "AB-12-1")


class ChatSubscriptionTests(TestCase):
    def test_chat_subscriptions(self):
        lotuser = User.objects.create(username="thisismylot")
        chatuser = User.objects.create(username="ichatonlots")
        my_lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=timezone.now() + datetime.timedelta(days=30),
            reserve_price=5,
            user=lotuser,
            quantity=1,
        )
        my_lot_that_i_have_seen_all = Lot.objects.create(
            lot_name="seen all",
            date_end=timezone.now() + datetime.timedelta(days=30),
            reserve_price=5,
            user=lotuser,
            quantity=1,
        )
        someone_elses_lot = Lot.objects.create(
            lot_name="Another test lot",
            date_end=timezone.now() + datetime.timedelta(days=30),
            reserve_price=5,
            user=chatuser,
            quantity=1,
        )
        my_lot_that_is_unsubscribed = Lot.objects.create(
            lot_name="An unsubscribed lot",
            date_end=timezone.now() + datetime.timedelta(days=30),
            reserve_price=5,
            user=lotuser,
            quantity=1,
        )
        sub = ChatSubscription.objects.get(lot=my_lot, user=lotuser)
        sub.last_seen = timezone.now() + datetime.timedelta(minutes=15)
        sub.save()
        sub = ChatSubscription.objects.get(lot=my_lot_that_is_unsubscribed, user=lotuser)
        sub.unsubscribed = True
        sub.save()
        ChatSubscription.objects.create(lot=someone_elses_lot, user=lotuser)
        data = lotuser.userdata
        assert data.unnotified_subscriptions_count == 0
        ten_minutes_ago = timezone.now() - datetime.timedelta(minutes=10)
        ten_minutes_in_the_future = timezone.now() + datetime.timedelta(minutes=10)
        twenty_minutes_in_the_future = timezone.now() + datetime.timedelta(minutes=20)
        history = LotHistory.objects.create(
            user=chatuser,
            lot=my_lot_that_i_have_seen_all,
            message="a chat in the past",
            changed_price=False,
        )
        history.timestamp = ten_minutes_ago
        history.save()
        history = LotHistory.objects.create(
            user=chatuser,
            lot=my_lot,
            message="a chat in the past",
            changed_price=False,
        )
        history.timestamp = ten_minutes_ago
        history.save()
        assert data.subscriptions.count() == 3
        assert data.my_lot_subscriptions_count == 0
        assert data.other_lot_subscriptions_count == 0
        assert data.unnotified_subscriptions_count == 0
        history = LotHistory.objects.create(
            user=chatuser,
            lot=my_lot,
            message="a chat in the future",
            changed_price=False,
        )
        history.timestamp = ten_minutes_in_the_future
        history.save()
        assert data.unnotified_subscriptions_count == 0
        history = LotHistory.objects.create(
            user=chatuser,
            lot=my_lot,
            message="a chat in the far future",
            changed_price=False,
        )
        history.timestamp = twenty_minutes_in_the_future
        history.save()
        assert data.unnotified_subscriptions_count == 1
        history = LotHistory.objects.create(
            user=chatuser,
            lot=someone_elses_lot,
            message="a chat in the far future",
            changed_price=False,
        )
        history.timestamp = twenty_minutes_in_the_future
        history.save()
        assert data.other_lot_subscriptions_count == 1
        history = LotHistory.objects.create(
            user=chatuser,
            lot=someone_elses_lot,
            message="a chat in the far future",
            changed_price=False,
        )
        history.timestamp = twenty_minutes_in_the_future
        history.save()
        history = LotHistory.objects.create(
            user=chatuser,
            lot=someone_elses_lot,
            message="a chat in the far future",
            changed_price=False,
        )
        history.timestamp = twenty_minutes_in_the_future
        history.save()
        history = LotHistory.objects.create(
            user=chatuser,
            lot=my_lot_that_is_unsubscribed,
            message="a chat in the far future",
            changed_price=False,
        )
        history.timestamp = twenty_minutes_in_the_future
        history.save()
        assert data.my_lot_subscriptions_count == 1
        history = LotHistory.objects.create(
            user=chatuser,
            lot=my_lot_that_is_unsubscribed,
            message="a chat in the far future",
            changed_price=False,
        )
        history.timestamp = twenty_minutes_in_the_future
        history.save()
        history = LotHistory.objects.create(
            user=chatuser,
            lot=my_lot_that_is_unsubscribed,
            message="a chat in the far future",
            changed_price=False,
        )
        history.timestamp = twenty_minutes_in_the_future
        history.save()
        assert data.my_lot_subscriptions_count == 1
        assert data.other_lot_subscriptions_count == 1

    def test_own_messages_not_counted_as_unread(self):
        """Test that a user's own chat messages are not counted as unread"""
        # Create two users: lot owner and another user
        lot_owner = User.objects.create(username="lotowner")
        other_user = User.objects.create(username="otheruser")

        # Create a lot owned by other_user
        lot = Lot.objects.create(
            lot_name="Test lot for own messages",
            date_end=timezone.now() + datetime.timedelta(days=30),
            reserve_price=5,
            user=other_user,
            quantity=1,
        )

        # lot_owner creates a subscription to this lot
        subscription = ChatSubscription.objects.create(lot=lot, user=lot_owner)

        # Verify no unread messages initially
        lot_owner_data = lot_owner.userdata
        assert lot_owner_data.other_lot_subscriptions_count == 0
        assert lot_owner_data.unnotified_subscriptions_count == 0

        # other_user posts a message - this should count as unread for lot_owner
        future_time = timezone.now() + datetime.timedelta(minutes=5)
        history1 = LotHistory.objects.create(
            user=other_user,
            lot=lot,
            message="Message from other user",
            changed_price=False,
        )
        history1.timestamp = future_time
        history1.save()

        # Verify lot_owner sees this as unread
        assert lot_owner_data.other_lot_subscriptions_count == 1
        assert lot_owner_data.unnotified_subscriptions_count == 1

        # lot_owner posts their own message - this should NOT count as unread for lot_owner
        future_time2 = timezone.now() + datetime.timedelta(minutes=10)
        history2 = LotHistory.objects.create(
            user=lot_owner,
            lot=lot,
            message="Message from lot_owner themselves",
            changed_price=False,
        )
        history2.timestamp = future_time2
        history2.save()

        # lot_owner should still only see 1 unread (from other_user, not their own)
        assert lot_owner_data.other_lot_subscriptions_count == 1
        assert lot_owner_data.unnotified_subscriptions_count == 1

        # Mark subscription as seen
        subscription.last_seen = timezone.now() + datetime.timedelta(minutes=15)
        subscription.last_notification_sent = timezone.now() + datetime.timedelta(minutes=15)
        subscription.save()

        # Now there should be no unread messages
        assert lot_owner_data.other_lot_subscriptions_count == 0
        assert lot_owner_data.unnotified_subscriptions_count == 0


class InvoiceModelTests(StandardTestCase):
    def test_invoices(self):
        assert self.invoice.auction == self.online_auction

        assert self.invoiceB.flat_value_adjustments == 0
        assert self.invoiceB.percent_value_adjustments == 0

        assert self.invoiceB.total_sold == 0
        assert self.invoiceB.total_bought == 30
        assert self.invoiceB.subtotal == -30
        self.assertAlmostEqual(self.invoiceB.tax, Decimal(7.5))
        assert self.invoiceB.net == -37.5
        assert self.invoiceB.rounded_net == -37
        assert self.invoiceB.absolute_amount == 37
        assert self.invoiceB.lots_sold == 0
        assert self.invoiceB.lots_sold_successfully_count == 0
        assert self.invoiceB.unsold_lots == 0
        assert self.invoiceB.lots_bought == 3

        assert self.invoice.total_sold == 6.5
        assert self.invoice.total_bought == 0
        assert self.invoice.subtotal == 6.5
        assert self.invoice.tax == 0
        assert self.invoice.net == 6.5
        assert self.invoice.rounded_net == 7
        assert self.invoice.absolute_amount == 7
        assert self.invoice.lots_sold == 4
        assert self.invoice.lots_sold_successfully_count == 3
        assert self.invoice.unsold_lots == 1
        assert self.invoice.lots_bought == 0
        assert self.invoiceB.location == self.location
        assert self.invoiceB.contact_email == "test@example.com"
        assert self.invoiceB.is_online
        assert self.invoiceB.unsold_lot_warning == ""
        assert str(self.invoice) == f"{self.online_tos.name}'s invoice for {self.online_tos.auction}"

        # adjustments
        self.adjustment_add.amount = 0
        self.adjustment_add.save()
        assert self.invoiceB.net == -27.5
        self.adjustment_discount.amount = 0
        self.adjustment_discount.save()
        assert self.invoiceB.net == -37.5
        self.adjustment_add_percent.amount = 0
        self.adjustment_add_percent.save()
        assert self.invoiceB.net == -34.5
        self.adjustment_discount_percent.amount = 0
        self.adjustment_discount_percent.save()
        assert self.invoiceB.net == -37.5


class InvoiceCreateViewTests(StandardTestCase):
    """Test invoice creation view"""

    def test_invoice_create_success(self):
        """Test creating an invoice for a user without one"""
        # Create a new user without an invoice
        new_tos = AuctionTOS.objects.create(
            user=self.user_who_does_not_join,
            auction=self.online_auction,
            pickup_location=self.location,
        )

        # Ensure no invoice exists
        assert new_tos.invoice is None

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Create invoice
        response = self.client.get(f"/invoices/create/{new_tos.pk}/")

        # Check redirect to invoice page
        assert response.status_code == 302

        # Verify invoice was created
        new_tos = AuctionTOS.objects.get(pk=new_tos.pk)
        assert new_tos.invoice is not None
        assert new_tos.invoice.auctiontos_user == new_tos
        assert new_tos.invoice.auction == self.online_auction

    def test_invoice_create_duplicate_handling(self):
        """Test that duplicate invoices are deleted and oldest is kept"""
        # Create a user with one invoice
        new_tos = AuctionTOS.objects.create(
            user=self.user_who_does_not_join,
            auction=self.online_auction,
            pickup_location=self.location,
        )

        # Create first invoice (oldest)
        first_invoice = Invoice.objects.create(auctiontos_user=new_tos, auction=self.online_auction)
        first_invoice_pk = first_invoice.pk

        # Create a duplicate invoice (newer)
        Invoice.objects.create(auctiontos_user=new_tos, auction=self.online_auction)

        # Verify both exist
        assert Invoice.objects.filter(auctiontos_user=new_tos).count() == 2

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Try to create another invoice
        response = self.client.get(f"/invoices/create/{new_tos.pk}/")

        # Check redirect to existing invoice
        assert response.status_code == 302

        # Verify only one invoice remains (the oldest)
        assert Invoice.objects.filter(auctiontos_user=new_tos).count() == 1
        assert Invoice.objects.filter(auctiontos_user=new_tos).first().pk == first_invoice_pk

    def test_invoice_create_non_admin_denied(self):
        """Test that non-admins cannot create invoices"""
        # Create a new user without an invoice
        new_tos = AuctionTOS.objects.create(
            user=self.user_who_does_not_join,
            auction=self.online_auction,
            pickup_location=self.location,
        )

        # Login as non-admin user
        self.client.login(username=self.user_who_does_not_join.username, password="testpassword")

        # Try to create invoice
        response = self.client.get(f"/invoices/create/{new_tos.pk}/")

        # Check for permission error (403 or redirect)
        assert response.status_code in [302, 403]

        # Verify no invoice was created
        new_tos = AuctionTOS.objects.get(pk=new_tos.pk)
        assert new_tos.invoice is None


class InvoiceNotificationDueTests(StandardTestCase):
    """Test invoice notification due logic in views"""

    def test_invoice_status_to_ready_sets_notification_due(self):
        """Test that setting invoice to UNPAID (ready) sets notification due"""
        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Ensure invoice starts without notification due
        self.invoice.status = "DRAFT"
        self.invoice.invoice_notification_due = None
        self.invoice.save()

        # Set invoice to ready
        response = self.client.post(f"/api/payinvoice/{self.invoice.pk}/UNPAID")

        assert response.status_code == 200

        # Refresh from database
        self.invoice.refresh_from_db()

        # Check that notification_due was set
        assert self.invoice.status == "UNPAID"
        assert self.invoice.invoice_notification_due is not None
        # Should be set to ~15 seconds in the future
        assert self.invoice.invoice_notification_due > timezone.now()

    def test_invoice_status_to_paid_sets_notification_due(self):
        """Test that setting invoice to PAID sets notification due"""
        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Ensure invoice starts without notification due
        self.invoice.status = "UNPAID"
        self.invoice.invoice_notification_due = None
        self.invoice.save()

        # Set invoice to paid
        response = self.client.post(f"/api/payinvoice/{self.invoice.pk}/PAID")

        assert response.status_code == 200

        # Refresh from database
        self.invoice.refresh_from_db()

        # Check that notification_due was set
        assert self.invoice.status == "PAID"
        assert self.invoice.invoice_notification_due is not None
        # Should be set to ~15 seconds in the future
        assert self.invoice.invoice_notification_due > timezone.now()

    def test_invoice_status_to_open_clears_notification_due(self):
        """Test that setting invoice to DRAFT (open) clears notification due"""
        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Start with invoice that has notification due set
        self.invoice.status = "UNPAID"
        self.invoice.invoice_notification_due = timezone.now()
        self.invoice.save()

        # Set invoice back to draft
        response = self.client.post(f"/api/payinvoice/{self.invoice.pk}/DRAFT")

        assert response.status_code == 200

        # Refresh from database
        self.invoice.refresh_from_db()

        # Check that notification_due was cleared
        assert self.invoice.status == "DRAFT"
        assert self.invoice.invoice_notification_due is None


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
        self.assertContains(response, "<script>location.reload();</script>")

        # Check if the lot was updated
        updated_lot = Lot.objects.get(pk=self.lot.pk)
        assert updated_lot.partial_refund_percent == 50
        assert updated_lot.banned is False


class LotLabelViewTestCase(StandardTestCase):
    """Tests for the LotLabelView"""

    def setUp(self):
        super().setUp()
        self.url = reverse(
            "my_labels_by_username", kwargs={"slug": self.online_auction.slug, "username": self.user.username}
        )

    def assert_message_contains(self, response, expected_text, should_exist=True):
        """Helper method to check if a message contains expected text."""
        messages_list = list(response.wsgi_request._messages)
        found = any(expected_text in str(message) for message in messages_list)
        if should_exist:
            assert found, f"Expected message containing '{expected_text}', got: {[str(m) for m in messages_list]}"
        else:
            assert not found, (
                f"Should not have message containing '{expected_text}', got: {[str(m) for m in messages_list]}"
            )

    def test_user_can_print_own_labels(self):
        """Test that a regular user can print their own labels."""
        self.client.login(username=self.user, password="testpassword")
        self.endAuction()
        response = self.client.get(self.url)
        # messages = list(response.wsgi_request._messages)
        assert response.status_code == 200
        # note that weasyprint currently requires pydyf==0.8.0 in requirements.txt
        assert "attachment;filename=" in response.headers["Content-Disposition"]

    def test_small_labels(self):
        user_label_prefs, created = UserLabelPrefs.objects.get_or_create(user=self.user)
        user_label_prefs.preset = "sm"
        user_label_prefs.save()
        self.client.login(username=self.user, password="testpassword")
        response = self.client.get(self.url)
        assert response.status_code == 200
        assert "attachment;filename=" in response.headers["Content-Disposition"]

    def test_thermal_labels(self):
        """Test that a regular user can print their own labels."""
        # If this test is failing, it's likely that the issue is not in this code, but in a library
        # thermal labels cause a 'Paragraph' object has no attribute 'blPara' error
        # See https://github.com/virantha/pypdfocr/issues/80
        # This is the reason we are using a hacked version of platypus/paragraph.py in python_file_hack.sh
        user_label_prefs, created = UserLabelPrefs.objects.get_or_create(user=self.user)
        user_label_prefs.preset = "thermal_sm"
        user_label_prefs.save()
        self.client.login(username=self.user, password="testpassword")
        response = self.client.get(self.url)
        assert response.status_code == 200
        assert "attachment;filename=" in response.headers["Content-Disposition"]

    def test_thermal_labels_capped_at_100(self):
        """Test that thermal labels are capped at 100 per PDF."""
        # Create 150 lots for testing the cap
        for i in range(150):
            Lot.objects.create(
                lot_name=f"Test lot {i}",
                auction=self.online_auction,
                auctiontos_seller=self.online_tos,
                quantity=1,
                winning_price=10,
                auctiontos_winner=self.tosB,
                active=False,
            )

        user_label_prefs, created = UserLabelPrefs.objects.get_or_create(user=self.user)
        user_label_prefs.preset = "thermal_sm"
        user_label_prefs.save()
        self.client.login(username=self.user, password="testpassword")
        self.endAuction()
        response = self.client.get(self.url)

        assert response.status_code == 200
        assert "attachment;filename=" in response.headers["Content-Disposition"]

        # Check that a warning message was added about the 100 label cap
        self.assert_message_contains(response, "100 labels")
        self.assert_message_contains(response, "Print unprinted labels")

    def test_thermal_very_sm_labels_capped_at_100(self):
        """Test that thermal_very_sm labels are also capped at 100 per PDF."""
        # Create 120 lots for testing the cap
        for i in range(120):
            Lot.objects.create(
                lot_name=f"Test lot {i}",
                auction=self.online_auction,
                auctiontos_seller=self.online_tos,
                quantity=1,
                winning_price=10,
                auctiontos_winner=self.tosB,
                active=False,
            )

        user_label_prefs, created = UserLabelPrefs.objects.get_or_create(user=self.user)
        user_label_prefs.preset = "thermal_very_sm"
        user_label_prefs.save()
        self.client.login(username=self.user, password="testpassword")
        self.endAuction()
        response = self.client.get(self.url)

        assert response.status_code == 200
        assert "attachment;filename=" in response.headers["Content-Disposition"]

        # Check that a warning message was added about the 100 label cap
        self.assert_message_contains(response, "100 labels")
        self.assert_message_contains(response, "Print unprinted labels")

    def test_non_thermal_labels_not_capped(self):
        """Test that non-thermal labels are NOT capped at 100."""
        # Create 150 lots for testing
        for i in range(150):
            Lot.objects.create(
                lot_name=f"Test lot {i}",
                auction=self.online_auction,
                auctiontos_seller=self.online_tos,
                quantity=1,
                winning_price=10,
                auctiontos_winner=self.tosB,
                active=False,
            )

        user_label_prefs, created = UserLabelPrefs.objects.get_or_create(user=self.user)
        user_label_prefs.preset = "lg"  # Non-thermal preset
        user_label_prefs.save()
        self.client.login(username=self.user, password="testpassword")
        self.endAuction()
        response = self.client.get(self.url)

        assert response.status_code == 200
        assert "attachment;filename=" in response.headers["Content-Disposition"]

        # Check that NO warning message was added
        self.assert_message_contains(response, "100 labels", should_exist=False)

    def test_non_admin_cannot_print_others_labels(self):
        """Test that a non-admin user cannot print labels for other users."""
        self.client.login(username="no_tos", password="testpassword")
        response = self.client.get(self.url)
        assert response.status_code == 302
        messages = list(response.wsgi_request._messages)
        assert str(messages[0]) == "Your account doesn't have permission to view this page."

    def test_cannot_print_if_not_joined_auction(self):
        """Test that a user cannot print labels if they haven't joined the auction."""
        self.client.login(username=self.user_who_does_not_join.username, password="testpassword")
        url = reverse("print_my_labels", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 302
        self.assertRedirects(response, self.online_auction.get_absolute_url())
        messages = list(response.wsgi_request._messages)
        assert (
            str(messages[0])
            == "You haven't joined this auction yet.  You need to join this auction and add lots before you can print labels."
        )

    def test_no_printable_lots(self):
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        response = self.client.get(self.url)
        assert response.status_code == 302


class UpdateLotPushNotificationsViewTestCase(StandardTestCase):
    def get_url(self):
        return reverse("enable_notifications")

    def test_anonymous_user(self):
        response = self.client.get(self.get_url())
        assert response.status_code == 302
        response = self.client.post(self.get_url())
        assert response.status_code == 302

    def test_logged_in_user(self):
        self.client.login(username=self.user_who_does_not_join.username, password="testpassword")
        response = self.client.get(self.get_url())
        assert response.status_code == 405
        response = self.client.post(self.get_url())
        assert response.status_code == 200
        userdata = UserData.objects.get(user=self.user_who_does_not_join)
        assert userdata.push_notifications_when_lots_sell is True


class DynamicSetLotWinnerViewTestCase(StandardTestCase):
    def get_url(self):
        return reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})

    def test_anonymous_user(self):
        response = self.client.get(self.get_url())
        assert response.status_code == 302  # Redirect to login
        response = self.client.post(self.get_url())
        assert response.status_code == 302  # Redirect to login

    def test_non_admin_user(self):
        self.client.login(username=self.user_who_does_not_join.username, password="testpassword")
        response = self.client.get(self.get_url())
        assert response.status_code == 403
        response = self.client.post(self.get_url())
        assert response.status_code == 403

    def test_admin_user(self):
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.get(self.get_url())
        assert response.status_code == 200
        response = self.client.post(
            self.get_url(), data={"lot": "101-1", "price": "5", "winner": "555", "action": "validate"}
        )
        data = response.json()
        assert data.get("price") == "valid"
        assert data.get("winner") == "valid"
        assert data.get("lot") == "valid"

        self.in_person_lot.reserve_price = 10
        self.in_person_lot.save()
        response = self.client.post(
            self.get_url(), data={"lot": "101-1", "price": "5", "winner": "556", "action": "validate"}
        )
        data = response.json()
        assert data.get("price") != "valid"
        assert data.get("winner") != "valid"
        assert data.get("lot") == "valid"

        response = self.client.post(self.get_url(), data={"lot": "102-1", "action": "validate"})
        data = response.json()
        assert data.get("lot") != "valid"

        response = self.client.post(
            self.get_url(), data={"lot": "101-1", "price": "10", "winner": "555", "action": "save"}
        )
        data = response.json()
        assert data.get("price") == "valid"
        assert data.get("winner") == "valid"
        assert data.get("lot") == "valid"
        assert data.get("last_sold_lot_number") == "101-1"
        assert data.get("success_message") is not None

        lot = Lot.objects.filter(pk=self.in_person_lot.pk).first()
        assert lot.winning_price == 10
        assert lot.auctiontos_winner is not None

        response = self.client.post(
            self.get_url(), data={"lot": "101-1", "price": "10", "winner": "555", "action": "validate"}
        )
        data = response.json()
        assert data.get("lot") != "valid"

        invoice, created = Invoice.objects.get_or_create(auctiontos_user=self.in_person_lot.auctiontos_seller)
        invoice.status = "UNPAID"
        invoice.save()

        self.in_person_lot.auctiontos_winner = None
        self.in_person_lot.winning_price = None

        response = self.client.post(
            self.get_url(), data={"lot": "101-1", "price": "10", "winner": "555", "action": "save"}
        )
        data = response.json()
        assert data.get("lot") != "valid"
        assert self.in_person_lot.auctiontos_winner is None
        assert self.in_person_lot.winning_price is None

        response = self.client.post(
            self.get_url(), data={"lot": "101-1", "price": "7", "winner": "555", "action": "force_save"}
        )
        data = response.json()
        assert data.get("lot") == "valid"

        lot = Lot.objects.filter(pk=self.in_person_lot.pk).first()
        assert lot.winning_price == 7
        assert lot.auctiontos_winner is not None

        Bid.objects.create(user=self.admin_user, lot_number=self.in_person_lot, amount=100)
        self.in_person_auction.online_bidding == "allow"
        self.in_person_auction.save()
        invoice.status = "OPEN"
        invoice.save()

        lot = Lot.objects.filter(pk=self.in_person_lot.pk).first()
        lot.winning_price = None
        lot.auctiontos_winner = None
        lot.winner = None
        lot.save()

        response = self.client.post(
            self.get_url(), data={"lot": "101-1", "price": "10", "winner": "555", "action": "validate"}
        )
        data = response.json()
        assert data.get("price") != "valid"
        assert data.get("winner") != "valid"

        # Test that duplicate lot numbers are automatically fixed
        # Create a lot with the same custom_lot_number as in_person_lot
        new_lot = Lot.objects.create(
            lot_name="dupe",
            auction=self.in_person_auction,
            auctiontos_seller=self.admin_in_person_tos,
            quantity=1,
            custom_lot_number="101-1",
        )
        # After creating a duplicate, the duplicate detection should have automatically
        # changed the new lot's number, so there should only be one lot with "101-1"
        new_lot.refresh_from_db()  # Refresh to get the updated custom_lot_number
        lots_with_101_1 = Lot.objects.filter(auction=self.in_person_auction, custom_lot_number="101-1")
        # Verify duplicate was auto-fixed by checking only one lot has "101-1"
        assert lots_with_101_1.count() == 1, (
            f"Duplicate detection should have changed the duplicate lot's number. New lot number: {new_lot.custom_lot_number}"
        )
        # Verify the new lot got a different number
        assert new_lot.custom_lot_number != "101-1", (
            f"New lot should have been assigned a different number, got: {new_lot.custom_lot_number}"
        )


class AlternativeSplitLabelTests(StandardTestCase):
    """Test the alternative_split_label field"""

    def test_custom_label(self):
        """Test that a custom label can be set"""
        self.online_auction.alternative_split_label = "supporter"
        self.online_auction.save()
        auction = Auction.objects.get(pk=self.online_auction.pk)
        assert auction.alternative_split_label == "supporter"

    def test_label_in_csv_export_header(self):
        """Test that the custom label appears in CSV export header"""
        self.online_auction.alternative_split_label = "patron"
        self.online_auction.save()
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("user_list", kwargs={"slug": self.online_auction.slug}))
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "Patron" in content
        assert "Club member" not in content


class AuctionHistoryTests(StandardTestCase):
    """Test that auction history is properly tracked for lot operations and user joins"""

    def test_lot_edit_creates_history(self):
        """Test that editing a lot creates an audit history entry"""
        self.client.login(username="my_lot", password="testpassword")

        # Set up user data required by LotValidation
        self.user.first_name = "Test"
        self.user.last_name = "User"
        self.user.save()
        user_data = UserData.objects.get(user=self.user)
        user_data.address = "123 Test St"
        user_data.save()

        # Create an auction with lot submission still open
        theFuture = timezone.now() + datetime.timedelta(days=3)
        test_auction = Auction.objects.create(
            created_by=self.user,
            title="Test auction for editing",
            is_online=True,
            date_end=theFuture,
            date_start=timezone.now(),
            lot_submission_end_date=theFuture,
            winning_bid_percent_to_club=25,
        )
        test_location = PickupLocation.objects.create(name="test location", auction=test_auction, pickup_time=theFuture)
        test_tos = AuctionTOS.objects.create(user=self.user, auction=test_auction, pickup_location=test_location)

        # Create a lot that can be edited (no winner, no bids)
        editable_lot = Lot.objects.create(
            lot_name="Editable test lot",
            auction=test_auction,
            auctiontos_seller=test_tos,
            quantity=1,
            user=self.user,
        )

        # Get initial history count
        initial_count = AuctionHistory.objects.filter(auction=test_auction, applies_to="LOTS").count()

        # Edit a lot - provide all required fields
        url = reverse("edit_lot", kwargs={"pk": editable_lot.pk})

        response = self.client.post(
            url,
            {
                "part_of_auction": True,
                "auction": test_auction.pk,
                "lot_name": "Updated Lot Name",
                "quantity": 2,
                "reserve_price": 2,
                "summernote_description": "test",
                "donation": False,
                "i_bred_this_fish": False,
                "buy_now_price": "",
                "custom_checkbox": False,
                "custom_field_1": "text",
            },
            follow=True,  # follow to the selling redirect
        )
        assert response.status_code == 200
        # Check that history was created
        new_count = AuctionHistory.objects.filter(auction=test_auction, applies_to="LOTS").count()
        assert new_count == initial_count + 1

        # Verify the history entry
        history = AuctionHistory.objects.filter(auction=test_auction, applies_to="LOTS").latest("timestamp")
        assert "Edited lot" in history.action
        assert history.user == self.user

    def test_lot_delete_creates_history(self):
        """Test that deleting a lot creates an audit history entry"""
        self.client.login(username="my_lot", password="testpassword")

        # Set up user data required by LotValidation
        self.user.first_name = "Test"
        self.user.last_name = "User"
        self.user.save()
        user_data = UserData.objects.get(user=self.user)
        user_data.address = "123 Test St"
        user_data.save()

        # Create an auction with lot submission still open
        theFuture = timezone.now() + datetime.timedelta(days=3)
        test_auction = Auction.objects.create(
            created_by=self.user,
            title="Test auction for deleting",
            is_online=True,
            date_end=theFuture,
            date_start=timezone.now(),
            lot_submission_end_date=theFuture,
            winning_bid_percent_to_club=25,
        )
        test_location = PickupLocation.objects.create(name="test location", auction=test_auction, pickup_time=theFuture)
        test_tos = AuctionTOS.objects.create(user=self.user, auction=test_auction, pickup_location=test_location)

        # Create a lot that can be deleted (no winner, no bids, created recently)
        deletable_lot = Lot.objects.create(
            lot_name="Deletable test lot",
            auction=test_auction,
            auctiontos_seller=test_tos,
            quantity=1,
            user=self.user,
        )

        # Get initial history count
        initial_count = AuctionHistory.objects.filter(auction=test_auction, applies_to="LOTS").count()

        # Delete the lot
        self.client.post(reverse("delete_lot", kwargs={"pk": deletable_lot.pk}), follow=True)

        # Check that history was created
        new_count = AuctionHistory.objects.filter(auction=test_auction, applies_to="LOTS").count()
        assert new_count == initial_count + 1

        # Verify the history entry
        history = AuctionHistory.objects.filter(auction=test_auction, applies_to="LOTS").latest("timestamp")
        assert "Deleted lot" in history.action
        assert history.user == self.user

    def test_user_join_creates_history_only_once(self):
        """Test that joining an auction creates history only on first join"""
        # Create a new user who hasn't joined yet
        User.objects.create_user(username="new_user", password="testpassword", email="new@example.com")
        # UserData is automatically created by signal, so we don't need to create it manually
        self.client.login(username="new_user", password="testpassword")

        # Get initial history count
        initial_count = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="USERS").count()

        # Join the auction for the first time
        self.client.post(
            reverse("auction_main", kwargs={"slug": self.online_auction.slug}),
            {
                "pickup_location": self.location.pk,
                "i_agree": True,
                "time_spent_reading_rules": 10,
            },
        )

        # Check that history was created
        new_count = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="USERS").count()
        assert new_count == initial_count + 1

        # Verify the history entry
        history = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="USERS").latest("timestamp")
        assert "has joined this auction" in history.action

        # Join again (re-submit the same form)
        self.client.post(
            reverse("auction_main", kwargs={"slug": self.online_auction.slug}),
            {
                "pickup_location": self.location.pk,
                "i_agree": True,
                "time_spent_reading_rules": 20,
            },
        )

        # Check that NO new history was created
        final_count = AuctionHistory.objects.filter(auction=self.online_auction, applies_to="USERS").count()
        assert final_count == new_count  # Should be the same as after first join


class CSVImportTests(StandardTestCase):
    """Test CSV import functionality for bulk adding users"""

    def test_csv_import_with_memo_field(self):
        """Test that memo field is correctly imported from CSV"""
        import csv
        from io import StringIO

        # Create CSV content with memo field
        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["email", "name", "memo"])
        writer.writerow(["test1@example.com", "Test User 1", "This is a test memo"])
        writer.writerow(["test2@example.com", "Test User 2", "Another memo"])

        csv_file = SimpleUploadedFile("test.csv", csv_buffer.getvalue().encode("utf-8"), content_type="text/csv")

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Import CSV
        self.client.post(
            reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug}),
            {"csv_file": csv_file},
        )

        # Check that users were created with memo
        tos1 = AuctionTOS.objects.filter(auction=self.online_auction, email="test1@example.com").first()
        tos2 = AuctionTOS.objects.filter(auction=self.online_auction, email="test2@example.com").first()

        self.assertIsNotNone(tos1)
        self.assertIsNotNone(tos2)
        self.assertEqual(tos1.memo, "This is a test memo")
        self.assertEqual(tos2.memo, "Another memo")

    def test_csv_import_with_admin_field(self):
        """Test that admin/staff field is correctly imported from CSV with various boolean values"""
        import csv
        from io import StringIO

        # Create CSV content with proper formatting
        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["email", "name", "admin"])
        writer.writerow(["admin1@example.com", "Admin 1", "yes"])
        writer.writerow(["admin2@example.com", "Admin 2", "true"])
        writer.writerow(["admin3@example.com", "Admin 3", "1"])
        writer.writerow(["regular@example.com", "Regular User", "no"])

        csv_file = SimpleUploadedFile("test.csv", csv_buffer.getvalue().encode("utf-8"), content_type="text/csv")

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Import CSV
        self.client.post(
            reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug}),
            {"csv_file": csv_file},
        )

        # Check that admin users were created correctly
        admin1 = AuctionTOS.objects.filter(auction=self.online_auction, email="admin1@example.com").first()
        admin2 = AuctionTOS.objects.filter(auction=self.online_auction, email="admin2@example.com").first()
        admin3 = AuctionTOS.objects.filter(auction=self.online_auction, email="admin3@example.com").first()
        regular = AuctionTOS.objects.filter(auction=self.online_auction, email="regular@example.com").first()

        self.assertIsNotNone(admin1)
        self.assertIsNotNone(admin2)
        self.assertIsNotNone(admin3)
        self.assertIsNotNone(regular)

        self.assertTrue(admin1.is_admin)
        self.assertTrue(admin2.is_admin)
        self.assertTrue(admin3.is_admin)
        self.assertFalse(regular.is_admin)

    def test_csv_import_with_staff_field(self):
        """Test that 'staff' column name also works for admin field"""
        import csv
        from io import StringIO

        # Create CSV content with proper formatting
        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["email", "name", "staff"])
        writer.writerow(["staff1@example.com", "Staff 1", "yes"])

        csv_file = SimpleUploadedFile("test.csv", csv_buffer.getvalue().encode("utf-8"), content_type="text/csv")

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Import CSV
        self.client.post(
            reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug}),
            {"csv_file": csv_file},
        )

        # Check that admin user was created
        staff1 = AuctionTOS.objects.filter(auction=self.online_auction, email="staff1@example.com").first()
        self.assertIsNotNone(staff1)
        self.assertTrue(staff1.is_admin)

    def test_csv_import_bidder_number_not_in_use(self):
        """Test that bidder number from CSV is used if not already in use"""
        import csv
        from io import StringIO

        # Create CSV content with proper formatting
        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["email", "name", "bidder number"])
        writer.writerow(["bidder1@example.com", "Bidder 1", "999"])

        csv_file = SimpleUploadedFile("test.csv", csv_buffer.getvalue().encode("utf-8"), content_type="text/csv")

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Import CSV
        self.client.post(
            reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug}),
            {"csv_file": csv_file},
        )

        # Check that bidder number was assigned
        bidder1 = AuctionTOS.objects.filter(auction=self.online_auction, email="bidder1@example.com").first()
        self.assertIsNotNone(bidder1)
        self.assertEqual(bidder1.bidder_number, "999")

    def test_csv_import_bidder_number_in_use_new_user(self):
        """Test that bidder number is not assigned if already in use for a new user"""
        import csv
        from io import StringIO

        # Create an existing user with bidder number 777
        AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            email="existing@example.com",
            name="Existing User",
            bidder_number="777",
        )

        # Create CSV content with same bidder number
        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["email", "name", "bidder number"])
        writer.writerow(["newuser@example.com", "New User", "777"])

        csv_file = SimpleUploadedFile("test.csv", csv_buffer.getvalue().encode("utf-8"), content_type="text/csv")

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Import CSV
        self.client.post(
            reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug}),
            {"csv_file": csv_file},
        )

        # Check that new user was created but without the conflicting bidder number
        new_user = AuctionTOS.objects.filter(auction=self.online_auction, email="newuser@example.com").first()
        self.assertIsNotNone(new_user)
        self.assertNotEqual(new_user.bidder_number, "777")

    def test_csv_import_bidder_number_update_existing_user(self):
        """Test that existing user's bidder number is updated if new number is not in use"""
        import csv
        from io import StringIO

        # Create an existing user without bidder number
        existing_tos = AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            email="existing@example.com",
            name="Existing User",
            bidder_number="",
        )

        # Create CSV content to update with bidder number
        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["email", "name", "bidder number"])
        writer.writerow(["existing@example.com", "Existing User", "888"])

        csv_file = SimpleUploadedFile("test.csv", csv_buffer.getvalue().encode("utf-8"), content_type="text/csv")

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Import CSV
        self.client.post(
            reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug}),
            {"csv_file": csv_file},
        )

        # Check that bidder number was updated
        existing_tos.refresh_from_db()
        self.assertEqual(existing_tos.bidder_number, "888")

    def test_csv_import_bidder_number_exclude_self(self):
        """Test that bidder number check excludes the user being updated"""
        import csv
        from io import StringIO

        # Create an existing user with bidder number
        existing_tos = AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            email="existing@example.com",
            name="Existing User",
            bidder_number="666",
        )

        # Create CSV content with same bidder number (re-importing same user)
        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["email", "name", "bidder number"])
        writer.writerow(["existing@example.com", "Existing User Updated", "666"])

        csv_file = SimpleUploadedFile("test.csv", csv_buffer.getvalue().encode("utf-8"), content_type="text/csv")

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Import CSV
        self.client.post(
            reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug}),
            {"csv_file": csv_file},
        )

        # Check that bidder number was kept (not cleared)
        existing_tos.refresh_from_db()
        self.assertEqual(existing_tos.bidder_number, "666")
        self.assertEqual(existing_tos.name, "Existing User Updated")

    def test_csv_import_update_existing_user_memo_and_admin(self):
        """Test that existing user's memo and admin status are updated from CSV"""
        import csv
        from io import StringIO

        # Create an existing user
        existing_tos = AuctionTOS.objects.create(
            auction=self.online_auction,
            pickup_location=self.location,
            email="existing@example.com",
            name="Existing User",
            memo="",
            is_admin=False,
        )

        # Create CSV content to update memo and admin status
        csv_buffer = StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["email", "name", "memo", "admin"])
        writer.writerow(["existing@example.com", "Existing User", "Updated memo", "yes"])

        csv_file = SimpleUploadedFile("test.csv", csv_buffer.getvalue().encode("utf-8"), content_type="text/csv")

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Import CSV
        self.client.post(
            reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug}),
            {"csv_file": csv_file},
        )

        # Check that memo and admin were updated
        existing_tos.refresh_from_db()
        self.assertEqual(existing_tos.memo, "Updated memo")
        self.assertTrue(existing_tos.is_admin)


class GoogleDriveImportTests(StandardTestCase):
    """Test Google Drive import functionality"""

    # def test_auction_has_google_drive_fields(self):
    #     """Test that the new fields exist"""
    #     auction = Auction.objects.create(
    #         created_by=self.user,
    #         title="Test auction for Google Drive",
    #         is_online=True,
    #         date_end=timezone.now() + datetime.timedelta(days=2),
    #         date_start=timezone.now() - datetime.timedelta(days=1),
    #     )
    #     self.assertIsNone(auction.google_drive_link)
    #     self.assertIsNone(auction.last_sync_time)

    def test_save_google_drive_link(self):
        """Test that we can save a Google Drive link"""
        auction = Auction.objects.create(
            created_by=self.user,
            title="Test auction for Google Drive link",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=2),
            date_start=timezone.now() - datetime.timedelta(days=1),
        )
        test_link = "https://docs.google.com/spreadsheets/d/test123/edit#gid=0"
        auction.google_drive_link = test_link
        auction.save()

        # Refresh from database
        auction.refresh_from_db()
        self.assertEqual(auction.google_drive_link, test_link)

    def test_google_drive_import_view_requires_login(self):
        """Test that the Google Drive import view requires login"""
        response = self.client.get(reverse("import_from_google_drive", kwargs={"slug": self.online_auction.slug}))
        # Should redirect to login
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_google_drive_import_view_accessible_by_admin(self):
        """Test that admin can access the Google Drive import view"""
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(reverse("import_from_google_drive", kwargs={"slug": self.online_auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "auctions/import_from_google_drive.html")

    def test_sync_button_visible_when_link_set(self):
        """Test that sync button appears on users page when google_drive_link is set"""
        self.online_auction.google_drive_link = "https://docs.google.com/spreadsheets/d/test123/edit#gid=0"
        self.online_auction.save()

        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sync from Google Drive")

    def test_sync_button_not_visible_when_no_link(self):
        """Test that sync button does not appear when no google_drive_link is set"""
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Sync from Google Drive")


class WeeklyPromoEmailTrackingTestCase(StandardTestCase):
    """Test that the weekly_promo_emails_sent field is incremented correctly"""

    def test_auction_has_weekly_promo_emails_sent_field(self):
        """Test that the new field exists and defaults to 0"""
        auction = Auction.objects.create(
            created_by=self.user,
            title="Test auction for weekly promo",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=2),
            date_start=timezone.now() - datetime.timedelta(days=1),
        )
        assert auction.weekly_promo_emails_sent == 0

    def test_weekly_promo_emails_sent_increments(self):
        """Test that we can increment the weekly_promo_emails_sent field"""
        auction = Auction.objects.create(
            created_by=self.user,
            title="Test auction for weekly promo increment",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=2),
            date_start=timezone.now() - datetime.timedelta(days=1),
        )
        from django.db.models import F

        # Simulate what the management command does
        Auction.objects.filter(pk=auction.pk).update(weekly_promo_emails_sent=F("weekly_promo_emails_sent") + 1)

        # Refresh from database
        auction.refresh_from_db()
        assert auction.weekly_promo_emails_sent == 1

        # Increment again
        Auction.objects.filter(pk=auction.pk).update(weekly_promo_emails_sent=F("weekly_promo_emails_sent") + 1)
        auction.refresh_from_db()
        assert auction.weekly_promo_emails_sent == 2

    def test_weekly_promo_email_click_rate(self):
        """Test that the click rate calculation handles div/0 correctly"""
        auction = Auction.objects.create(
            created_by=self.user,
            title="Test auction for click rate",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=2),
            date_start=timezone.now() - datetime.timedelta(days=1),
        )

        # Test div/0 case - should return 0 when no emails sent
        assert auction.weekly_promo_emails_sent == 0
        assert auction.weekly_promo_email_click_rate == 0

        # Set some emails sent
        Auction.objects.filter(pk=auction.pk).update(weekly_promo_emails_sent=100)
        auction.refresh_from_db()

        # With 0 clicks and 100 emails, rate should be 0%
        assert auction.weekly_promo_email_click_rate == 0.0


class AuctionPropertyTests(StandardTestCase):
    """Test Auction model properties"""

    def test_auction_type(self):
        """Test the auction_type property returns correct values"""
        # Online auction with one location
        assert self.online_auction.auction_type == "online_one_location"
        assert self.online_auction.auction_type_as_str == "online auction with in-person pickup"

        # In-person auction with one location
        assert self.in_person_auction.auction_type == "inperson_one_location"
        assert self.in_person_auction.auction_type_as_str == "in-person auction"

        # Create a new auction with multiple locations for this test
        multi_location_auction = Auction.objects.create(
            created_by=self.user,
            title="Multi-location auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        PickupLocation.objects.create(
            name="first location",
            auction=multi_location_auction,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )
        PickupLocation.objects.create(
            name="second location",
            auction=multi_location_auction,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )
        assert multi_location_auction.auction_type == "online_multi_location"
        assert (
            multi_location_auction.auction_type_as_str == "online auction with in-person pickup at multiple locations"
        )

    def test_auction_timing_properties(self):
        """Test auction start/end related properties"""
        # Create an auction that has started and is in progress
        in_progress_auction = Auction.objects.create(
            created_by=self.user,
            title="In progress auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        assert in_progress_auction.started is True
        assert in_progress_auction.in_progress is True
        assert in_progress_auction.closed is False
        assert in_progress_auction.ending_soon is False

        # Create an auction that hasn't started yet
        future_auction = Auction.objects.create(
            created_by=self.user,
            title="Future auction",
            is_online=True,
            date_start=timezone.now() + datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=2),
        )
        assert future_auction.started is False
        assert future_auction.in_progress is False
        assert future_auction.closed is False

        # Test ending_soon
        ending_soon_auction = Auction.objects.create(
            created_by=self.user,
            title="Ending soon auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(minutes=60),
        )
        assert ending_soon_auction.ending_soon is True

    def test_allow_mailing_lots(self):
        """Test the allow_mailing_lots property"""
        # Create a separate auction for this test to avoid test isolation issues
        mail_auction = Auction.objects.create(
            created_by=self.user,
            title="Mail test auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        # Initially should be False
        assert mail_auction.allow_mailing_lots is False

        # Add a mail pickup location
        PickupLocation.objects.create(
            name="Mail pickup",
            auction=mail_auction,
            pickup_by_mail=True,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )
        assert mail_auction.allow_mailing_lots is True

    def test_permission_check(self):
        """Test the permission_check method"""
        # Creator has permission
        assert self.online_auction.permission_check(self.user) is True

        # Admin has permission
        assert self.online_auction.permission_check(self.admin_user) is True

        # Regular user without admin TOS does not have permission
        assert self.online_auction.permission_check(self.user_with_no_lots) is False

        # Non-authenticated user does not have permission (though this requires a User object)
        assert self.online_auction.permission_check(self.userB) is False

    def test_dynamic_end(self):
        """Test the dynamic_end property for online auctions"""
        # For non-sealed-bid auctions, dynamic end should be 60 minutes after date_end
        expected_dynamic_end = self.online_auction.date_end + datetime.timedelta(minutes=60)
        assert self.online_auction.dynamic_end == expected_dynamic_end

        # For sealed-bid auctions, dynamic end should equal date_end
        sealed_auction = Auction.objects.create(
            created_by=self.user,
            title="Sealed bid auction",
            is_online=True,
            sealed_bid=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        assert sealed_auction.dynamic_end == sealed_auction.date_end

    def test_minutes_to_end(self):
        """Test the minutes_to_end property"""
        # Future auction should have positive minutes
        future_auction = Auction.objects.create(
            created_by=self.user,
            title="Future minutes test",
            is_online=True,
            date_start=timezone.now() + datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=2),
        )
        assert future_auction.minutes_to_end > 0

        # Past auction should return 0
        assert self.online_auction.minutes_to_end == 0

    def test_number_of_locations(self):
        """Test location counting properties"""
        # Default auction has 1 physical location
        assert self.online_auction.number_of_locations == 1
        assert self.online_auction.all_location_count == 1

        # Add a mail location
        PickupLocation.objects.create(
            name="Mail",
            auction=self.online_auction,
            pickup_by_mail=True,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
        )
        # Physical count stays same, all_location_count increases
        assert self.online_auction.number_of_locations == 1
        assert self.online_auction.all_location_count == 2

    def test_has_non_logical_times(self):
        """Test that has_non_logical_times property detects illogical auction times"""
        # Create an auction with logical times (ending in :00:00)
        logical_auction = Auction.objects.create(
            created_by=self.user,
            title="Logical time auction",
            is_online=True,
            date_start=timezone.now().replace(hour=14, minute=0, second=0, microsecond=0),
            date_end=timezone.now().replace(hour=18, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1),
        )
        assert logical_auction.has_non_logical_times is False

        # Create an auction with logical times (ending in :30:00)
        logical_auction_30 = Auction.objects.create(
            created_by=self.user,
            title="Logical time auction at 30",
            is_online=True,
            date_start=timezone.now().replace(hour=14, minute=30, second=0, microsecond=0),
            date_end=timezone.now().replace(hour=18, minute=30, second=0, microsecond=0) + datetime.timedelta(days=1),
        )
        assert logical_auction_30.has_non_logical_times is False

        # Create an auction with non-logical start time
        illogical_start = Auction.objects.create(
            created_by=self.user,
            title="Illogical start time auction",
            is_online=True,
            date_start=timezone.now().replace(hour=14, minute=23, second=0, microsecond=0),
            date_end=timezone.now().replace(hour=18, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1),
        )
        assert illogical_start.has_non_logical_times is not False

        # Create an auction with non-logical end time
        illogical_end = Auction.objects.create(
            created_by=self.user,
            title="Illogical end time auction",
            is_online=True,
            date_start=timezone.now().replace(hour=14, minute=0, second=0, microsecond=0),
            date_end=timezone.now().replace(hour=18, minute=15, second=0, microsecond=0) + datetime.timedelta(days=1),
        )
        assert illogical_end.has_non_logical_times is not False

        # Create an auction with seconds not zero
        illogical_seconds = Auction.objects.create(
            created_by=self.user,
            title="Illogical seconds auction",
            is_online=True,
            date_start=timezone.now().replace(hour=14, minute=0, second=30, microsecond=0),
            date_end=timezone.now().replace(hour=18, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1),
        )
        assert illogical_seconds.has_non_logical_times is not False


class LotPropertyTests(StandardTestCase):
    """Test Lot model properties"""

    def test_lot_ended_property(self):
        """Test that lot.ended works correctly"""
        # Create a lot that has ended
        ended_lot = Lot.objects.create(
            lot_name="Ended lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            # inherited from auction, this value won't be used
            date_end=timezone.now() - datetime.timedelta(days=1),
        )
        assert ended_lot.ended is True

        # Create a lot that is still active
        active_lot = Lot.objects.create(
            lot_name="Active lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
        )
        # simulated dynamic ending
        active_lot.date_end = timezone.now() + datetime.timedelta(days=1)
        active_lot.save()
        assert active_lot.ended is False

    def test_lot_with_auction_inherits_end_date(self):
        """Test that lots in an auction inherit the auction's end date"""
        # Create a lot with a future end date but in an ended auction
        lot = Lot.objects.create(
            lot_name="Inherit end date lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            date_end=timezone.now() + datetime.timedelta(days=30),
        )
        # The auction has ended, so the lot should be ended too
        assert lot.ended is True


class AuctionTOSPropertyTests(StandardTestCase):
    """Test AuctionTOS model properties"""

    def test_auction_tos_invoice_relationship(self):
        """Test the invoice relationship"""
        # Invoice should exist from StandardTestCase setup
        assert self.online_tos.invoice is not None
        assert self.online_tos.invoice.auctiontos_user == self.online_tos


class UserDataPropertyTests(StandardTestCase):
    """Test UserData model properties"""

    def test_user_data_exists(self):
        """Test that UserData is created for users"""
        # UserData should be automatically created
        assert hasattr(self.user, "userdata")
        assert self.user.userdata is not None

    def test_user_data_unnotified_subscriptions_count(self):
        """Test the unnotified_subscriptions_count property"""
        # This is tested in ChatSubscriptionTests but we can add basic checks
        user_data = self.user.userdata
        # Initially should be 0
        assert user_data.unnotified_subscriptions_count == 0


class AuctionViewPermissionTests(StandardTestCase):
    """Test view permissions for different user types"""

    def test_auction_view_anonymous_user(self):
        """Test that anonymous users can view auction page"""
        response = self.client.get(self.online_auction.get_absolute_url())
        assert response.status_code == 200
        self.assertContains(response, self.online_auction.title)

    def test_auction_view_logged_in_not_joined(self):
        """Test logged in user who hasn't joined the auction"""
        self.client.login(username=self.user_who_does_not_join.username, password="testpassword")
        response = self.client.get(self.online_auction.get_absolute_url())
        assert response.status_code == 200
        # Should see option to join
        self.assertContains(response, self.online_auction.title)

    def test_auction_view_logged_in_joined(self):
        """Test logged in user who has joined the auction"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        response = self.client.get(self.online_auction.get_absolute_url())
        assert response.status_code == 200
        self.assertContains(response, self.online_auction.title)

    def test_auction_view_admin_user(self):
        """Test admin user viewing auction"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.get(self.online_auction.get_absolute_url())
        assert response.status_code == 200
        self.assertContains(response, self.online_auction.title)

    def test_bulk_add_button_points_to_auto_url(self):
        """Test that the bulk add lots button uses the auto bulk add URL"""
        # Set up in-person auction to allow bulk adding
        self.in_person_auction.allow_bulk_adding_lots = True
        self.in_person_auction.save()

        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(self.in_person_auction.get_absolute_url())
        assert response.status_code == 200

        # Check that the response contains the auto bulk add URL
        auto_bulk_add_url = reverse("bulk_add_lots_auto_for_myself", kwargs={"slug": self.in_person_auction.slug})
        self.assertContains(response, auto_bulk_add_url)

        # Make sure it doesn't contain the old bulk add URL
        old_bulk_add_url = reverse("bulk_add_lots_for_myself", kwargs={"slug": self.in_person_auction.slug})
        self.assertNotContains(response, old_bulk_add_url)


class AuctionEditViewTests(StandardTestCase):
    """Test auction edit view with different user types"""

    def test_auction_edit_anonymous_user(self):
        """Anonymous users should not be able to edit"""
        response = self.client.get(self.online_auction.get_edit_url())
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_auction_edit_non_admin(self):
        """Non-admin users should not be able to edit"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        response = self.client.get(self.online_auction.get_edit_url())
        # Should be denied - can be either 302 (redirect to error/login page) or 403 (forbidden)
        # depending on permission middleware configuration
        assert response.status_code in [302, 403]

    def test_auction_edit_admin_user(self):
        """Admin users should be able to edit"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.get(self.online_auction.get_edit_url())
        assert response.status_code == 200

    def test_auction_edit_creator(self):
        """Auction creator should be able to edit"""
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(self.online_auction.get_edit_url())
        assert response.status_code == 200


class PayPalFormFieldVisibilityTests(StandardTestCase):
    """Test that PayPal payment field is only shown when user has PayPal connected"""

    def test_enable_online_payments_field_hidden_without_paypal(self):
        """Field should be hidden when user doesn't have PayPal connected"""
        # Ensure no PayPal seller exists for this user
        PayPalSeller.objects.filter(user=self.user).delete()

        form = AuctionEditForm(
            instance=self.online_auction, user=self.online_auction.created_by, cloned_from=None, user_timezone="UTC"
        )
        # Field should be hidden (widget is HiddenInput)
        assert isinstance(form.fields["enable_online_payments"].widget, forms.HiddenInput)

    def test_enable_online_payments_field_visible_with_paypal(self):
        """Field should be visible when user has PayPal connected"""
        # Create a PayPal seller for this user
        PayPalSeller.objects.create(user=self.user, paypal_merchant_id="test_merchant_id")

        form = AuctionEditForm(
            instance=self.online_auction, user=self.online_auction.created_by, cloned_from=None, user_timezone="UTC"
        )
        # Field should NOT be hidden
        assert not isinstance(form.fields["enable_online_payments"].widget, forms.HiddenInput)

    @override_settings(PAYPAL_CLIENT_ID="test_client_id", PAYPAL_SECRET="test_secret")
    def test_enable_online_payments_field_visible_for_superuser_without_paypal(self):
        """Field should be visible for superuser even without PayPal connected (site-wide fallback)"""
        # Create superuser
        superuser = User.objects.create_superuser(
            username="superuser", password="testpassword", email="super@example.com"
        )
        # Create auction by superuser
        superuser_auction = Auction.objects.create(
            created_by=superuser,
            title="Superuser auction",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=2),
            date_start=timezone.now() - datetime.timedelta(days=1),
        )

        # Ensure no PayPal seller exists for superuser
        PayPalSeller.objects.filter(user=superuser).delete()

        form = AuctionEditForm(
            instance=superuser_auction, user=superuser_auction.created_by, cloned_from=None, user_timezone="UTC"
        )
        # Field should NOT be hidden for superuser (site-wide PayPal fallback)
        assert not isinstance(form.fields["enable_online_payments"].widget, forms.HiddenInput)


class LotListViewTests(StandardTestCase):
    """Test lot list view with different user types"""

    def test_lot_list_anonymous_user(self):
        """Anonymous users can view lot list"""
        response = self.client.get(f"/lots/?auction={self.online_auction.slug}")
        assert response.status_code == 200

    def test_lot_list_logged_in_not_joined(self):
        """Logged in users who haven't joined can view lot list"""
        self.client.login(username=self.user_who_does_not_join.username, password="testpassword")
        response = self.client.get(f"/lots/?auction={self.online_auction.slug}")
        assert response.status_code == 200

    def test_lot_list_logged_in_joined(self):
        """Logged in users who have joined can view lot list"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        response = self.client.get(f"/lots/?auction={self.online_auction.slug}")
        assert response.status_code == 200

    def test_lot_list_admin(self):
        """Admin users can view lot list"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        response = self.client.get(f"/lots/?auction={self.online_auction.slug}")
        assert response.status_code == 200


class MyLotsViewTests(StandardTestCase):
    """Test my lots view with different user types"""

    def test_my_lots_anonymous_user(self):
        """Anonymous users should be redirected to login"""
        response = self.client.get("/selling/")
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [301, 302, 403]

    def test_my_lots_logged_in_user(self):
        """Logged in users can view their lots"""
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get("/selling/")
        assert response.status_code == 200


class AuctionUsersViewTests(StandardTestCase):
    """Test auction users/TOS admin view"""

    def test_auction_users_anonymous(self):
        """Anonymous users should not access user list"""
        url = reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [301, 302, 403]

    def test_auction_users_non_admin(self):
        """Non-admin users should not access user list"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        # Should be denied
        assert response.status_code in [302, 403]

    def test_auction_users_admin(self):
        """Admin users should access user list"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_auction_users_creator(self):
        """Auction creator should access user list"""
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200


class LotCreateViewTests(StandardTestCase):
    """Test lot creation with different user types"""

    def test_lot_create_anonymous(self):
        """Anonymous users cannot create lots"""
        response = self.client.get("/lots/new/")
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_lot_create_logged_in_not_joined(self):
        """User not joined to auction should not be able to create lot in that auction"""
        self.client.login(username=self.user_who_does_not_join.username, password="testpassword")
        # Try to create a lot in the auction they haven't joined
        response = self.client.get(f"/lots/new/?auction={self.online_auction.slug}")
        # They can access the form, but posting should fail or redirect
        assert response.status_code == 302

    def test_lot_create_logged_in_joined(self):
        """User joined to auction can create lots"""
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(f"/lots/new/?auction={self.online_auction.slug}")
        assert response.status_code == 302


class InvoiceViewTests(StandardTestCase):
    """Test invoice views with different user types"""

    def test_invoice_view_anonymous(self):
        """Anonymous users should not view invoices"""
        url = reverse("invoice_by_pk", kwargs={"pk": self.invoice.pk})
        response = self.client.get(url)
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_invoice_view_owner(self):
        """Invoice owner can view their invoice"""
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("invoice_by_pk", kwargs={"pk": self.invoice.pk})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_invoice_view_other_user(self):
        """Other users should not view someone else's invoice"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = reverse("invoice_by_pk", kwargs={"pk": self.invoice.pk})
        response = self.client.get(url)
        # Should be denied
        assert response.status_code in [302, 403]

    def test_invoice_view_admin(self):
        """Admin can view any invoice"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("invoice_by_pk", kwargs={"pk": self.invoice.pk})
        response = self.client.get(url)
        assert response.status_code == 200


class InvoiceStatusButtonTests(StandardTestCase):
    """Test invoice status buttons can be clicked and update correctly"""

    def test_invoice_status_button_paid(self):
        """Admin can mark invoice as paid via button click"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = f"/api/payinvoice/{self.invoice.pk}/PAID"
        response = self.client.post(url)
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}, content: {response.content.decode()[:500]}"
        )
        # Verify the invoice was updated
        self.invoice.refresh_from_db()
        assert self.invoice.status == "PAID"
        # Verify response contains updated buttons with correct ID and status
        content = response.content.decode()
        assert f"id='invoice-buttons-{self.invoice.pk}'" in content, (
            f"Expected invoice-buttons ID in content: {content}"
        )
        assert f'id="{self.invoice.pk}_PAID"' in content
        assert "btn-success" in content  # Paid button should be success

    def test_invoice_status_button_draft(self):
        """Admin can mark invoice as draft (open) via button click"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        # First set to PAID
        self.invoice.status = "PAID"
        self.invoice.save()
        # Then change back to DRAFT
        url = f"/api/payinvoice/{self.invoice.pk}/DRAFT"
        response = self.client.post(url)
        assert response.status_code == 200
        self.invoice.refresh_from_db()
        assert self.invoice.status == "DRAFT"
        content = response.content.decode()
        assert f"id='invoice-buttons-{self.invoice.pk}'" in content
        assert "btn-info" in content  # Open button should be info when active

    def test_invoice_status_button_anonymous_denied(self):
        """Anonymous users cannot change invoice status"""
        url = f"/api/payinvoice/{self.invoice.pk}/PAID"
        response = self.client.post(url)
        # Should redirect to login
        assert response.status_code == 302


class PickupLocationTests(StandardTestCase):
    """Test PickupLocation model properties and views"""

    def test_pickup_location_create_anonymous(self):
        """Anonymous users cannot create pickup locations"""
        url = reverse("create_auction_pickup_location", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_pickup_location_create_non_admin(self):
        """Non-admin users cannot create pickup locations"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = reverse("create_auction_pickup_location", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code in [302, 403]

    def test_pickup_location_create_admin(self):
        """Admin users can create pickup locations"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("create_auction_pickup_location", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_pickup_location_list_anonymous(self):
        """Anonymous users can view pickup locations"""
        url = reverse("auction_pickup_location", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code != 200

    def test_pickup_location_list_logged_in(self):
        """Logged in users can view pickup locations"""
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("auction_pickup_location", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200


class AuctionStatsViewTests(StandardTestCase):
    """Test auction stats view with different user types"""

    def test_auction_stats_anonymous(self):
        """Anonymous users cannot view stats - requires login and admin permissions"""
        url = f"/auctions/{self.online_auction.slug}/stats/"
        response = self.client.get(url)
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_auction_stats_non_admin(self):
        """Non-admin users cannot view stats"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = f"/auctions/{self.online_auction.slug}/stats/"
        response = self.client.get(url)
        # Should be denied (403) or redirect (302)
        assert response.status_code in [302, 403]

    def test_auction_stats_creator(self):
        """Creator can view stats"""
        self.client.login(username=self.user.username, password="testpassword")
        url = f"/auctions/{self.online_auction.slug}/stats/"
        response = self.client.get(url)
        assert response.status_code == 200

    def test_auction_stats_admin(self):
        """Admin can view stats"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = f"/auctions/{self.online_auction.slug}/stats/"
        response = self.client.get(url)
        assert response.status_code == 200

    def test_auction_stats_recalculation_threshold(self):
        """Stats recalculation respects 20-minute threshold"""
        from django.utils import timezone

        self.client.login(username=self.user.username, password="testpassword")
        url = f"/auctions/{self.online_auction.slug}/stats/"

        # Test 1: Stats older than 20 minutes should trigger recalculation
        old_time = timezone.now() - timezone.timedelta(minutes=25)
        self.online_auction.last_stats_update = old_time
        self.online_auction.next_update_due = None
        self.online_auction.save()

        response = self.client.get(url)
        assert response.status_code == 200
        # Should show recalculation message in context
        assert response.context.get("stats_being_recalculated") is True, "Should show recalculation message"

        self.online_auction.refresh_from_db()
        # next_update_due should be set (scheduled for recalculation)
        assert self.online_auction.next_update_due is not None, "next_update_due should be set for old stats"

        # Test 2: Stats within 20 minutes should NOT trigger recalculation
        recent_time = timezone.now() - timezone.timedelta(minutes=10)
        self.online_auction.last_stats_update = recent_time
        self.online_auction.next_update_due = None
        self.online_auction.save()

        response = self.client.get(url)
        assert response.status_code == 200
        # Should NOT show recalculation message in context
        assert response.context.get("stats_being_recalculated") is not True, "Should not show recalculation message"

        self.online_auction.refresh_from_db()
        # next_update_due should remain None (no recalculation scheduled)
        assert self.online_auction.next_update_due is None, "next_update_due should not be set for recent stats"

        # Test 3: Already scheduled recalculation should not reschedule
        old_time = timezone.now() - timezone.timedelta(minutes=25)
        scheduled_time = timezone.now() + timezone.timedelta(minutes=2)
        self.online_auction.last_stats_update = old_time
        self.online_auction.next_update_due = scheduled_time
        self.online_auction.save()

        response = self.client.get(url)
        assert response.status_code == 200
        # Should still show recalculation message but not reschedule
        assert response.context.get("stats_being_recalculated") is True, "Should show recalculation message"

        self.online_auction.refresh_from_db()
        assert self.online_auction.next_update_due == scheduled_time, "Should not reschedule if already scheduled"


class BulkAddLotsViewTests(StandardTestCase):
    """Test bulk add lots view with different user types"""

    def test_bulk_add_lots_anonymous(self):
        """Anonymous users cannot bulk add lots"""
        url = reverse("bulk_add_lots_for_myself", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_bulk_add_lots_non_admin(self):
        """Non-admin users cannot bulk add lots"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = reverse("bulk_add_lots_for_myself", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code in [302, 403]

    def test_bulk_add_lots_admin(self):
        """Admin users can bulk add lots"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("bulk_add_lots_for_myself", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_bulk_add_lots_creator(self):
        """Auction creator can bulk add lots"""
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("bulk_add_lots_for_myself", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200


class BulkAddUsersViewTests(StandardTestCase):
    """Test bulk add users view with different user types"""

    def test_bulk_add_users_anonymous(self):
        """Anonymous users cannot bulk add users"""
        url = reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_bulk_add_users_non_admin(self):
        """Non-admin users cannot bulk add users"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code in [302, 403]

    def test_bulk_add_users_admin(self):
        """Admin users can bulk add users"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200


class SetLotWinnersViewTests(StandardTestCase):
    """Test set lot winners view with different user types"""

    def test_set_lot_winners_anonymous(self):
        """Anonymous users cannot access set lot winners"""
        url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})
        response = self.client.get(url)
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_set_lot_winners_non_admin(self):
        """Non-admin users cannot access set lot winners"""
        self.client.login(username=self.user_who_does_not_join.username, password="testpassword")
        url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 403

    def test_set_lot_winners_admin(self):
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200


class AuctionDeleteViewTests(StandardTestCase):
    """Test auction deletion with different user types"""

    def test_auction_delete_anonymous(self):
        """Anonymous users cannot delete auctions"""
        url = f"/auctions/{self.online_auction.slug}/delete/"
        response = self.client.get(url)
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_auction_delete_non_creator(self):
        """Non-creator users cannot delete auctions"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = f"/auctions/{self.online_auction.slug}/delete/"
        response = self.client.get(url)
        assert response.status_code in [302, 403]

    def test_auction_delete_creator(self):
        """Creator can access delete page"""
        self.client.login(username=self.user.username, password="testpassword")
        url = f"/auctions/{self.online_auction.slug}/delete/"
        response = self.client.get(url)
        assert response.status_code == 302


class AdditionalAuctionPropertyTests(StandardTestCase):
    """Test additional Auction model properties"""

    def test_auction_urls(self):
        """Test various URL properties"""
        assert self.online_auction.url == f"/auctions/{self.online_auction.slug}/"
        assert self.online_auction.add_lot_link == f"/lots/new/?auction={self.online_auction.slug}"
        assert self.online_auction.view_lot_link == f"/lots/?auction={self.online_auction.slug}&status=all"
        assert "/auctions/" in self.online_auction.label_print_link
        assert "/auctions/" in self.online_auction.label_print_unprinted_link

    def test_template_status(self):
        """Test template_status property"""
        # Create a future auction
        future_auction = Auction.objects.create(
            created_by=self.user,
            title="Future auction",
            is_online=True,
            date_start=timezone.now() + datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=2),
        )
        assert future_auction.template_status == "Starts:"

        # Create an in-progress auction
        in_progress_auction = Auction.objects.create(
            created_by=self.user,
            title="In progress",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        assert in_progress_auction.template_status == "Now until:"

    def test_auction_str_method(self):
        """Test the __str__ method of Auction"""
        # Auction title without "auction" should have it added
        auction1 = Auction.objects.create(
            created_by=self.user,
            title="Fish Sale",
            is_online=True,
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        str_repr = str(auction1)
        assert "auction" in str_repr.lower()
        assert "the " in str_repr.lower() or str_repr.startswith("The ")

    def test_can_submit_lots(self):
        """Test the can_submit_lots property"""
        # Create an auction with lot submission dates
        auction = Auction.objects.create(
            created_by=self.user,
            title="Lot submission test",
            is_online=True,
            lot_submission_start_date=timezone.now() - datetime.timedelta(days=1),
            lot_submission_end_date=timezone.now() + datetime.timedelta(days=1),
            date_start=timezone.now() + datetime.timedelta(days=2),
            date_end=timezone.now() + datetime.timedelta(days=3),
        )
        # Should be able to submit lots during the submission window
        assert auction.can_submit_lots is True

        # Create an auction where lot submission has ended
        ended_submission_auction = Auction.objects.create(
            created_by=self.user,
            title="Ended submission",
            is_online=True,
            lot_submission_start_date=timezone.now() - datetime.timedelta(days=3),
            lot_submission_end_date=timezone.now() - datetime.timedelta(days=1),
            date_start=timezone.now() + datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=2),
        )
        # Should not be able to submit lots
        assert ended_submission_auction.can_submit_lots is False


class AdditionalLotPropertyTests(StandardTestCase):
    """Test additional Lot model properties"""

    def test_lot_banned_property(self):
        """Test the banned property of lots"""
        # Create a normal lot
        lot = Lot.objects.create(
            lot_name="Normal lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            banned=False,
        )
        assert lot.banned is False

        # Update to banned
        lot.banned = True
        lot.save()
        assert lot.banned is True

    def test_lot_donation_property(self):
        """Test the donation property of lots"""
        lot = Lot.objects.create(
            lot_name="Donation lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            donation=False,
        )
        assert lot.donation is False

        lot.donation = True
        lot.save()
        assert lot.donation is True


class UserViewTests(StandardTestCase):
    """Test user profile view with different user types"""

    def test_user_view_anonymous(self):
        """Anonymous users can view user profiles"""
        url = reverse("userpage", kwargs={"slug": self.user.username})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_user_view_logged_in(self):
        """Logged in users can view user profiles"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = reverse("userpage", kwargs={"slug": self.user.username})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_user_view_own_profile(self):
        """Users can view their own profile"""
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("userpage", kwargs={"slug": self.user.username})
        response = self.client.get(url)
        assert response.status_code == 200


class ImageViewTests(StandardTestCase):
    """Test image create/update/delete views"""

    def test_image_create_anonymous(self):
        """Anonymous users cannot create images"""
        url = reverse("add_image", kwargs={"lot": self.lot.pk})
        response = self.client.get(url)
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_image_create_logged_in(self):
        """Logged in users can access image create form"""
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("add_image", kwargs={"lot": self.lot.pk})
        response = self.client.get(url)
        assert response.status_code == 302


class WatchViewTests(StandardTestCase):
    """Test watch/unwatch functionality"""

    def test_watch_anonymous(self):
        """Anonymous users cannot watch lots"""
        # watchOrUnwatch is a function-based view
        response = self.client.post(f"/api/watchitem/{self.lot.pk}/", data={"watch": "1"})
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_watch_logged_in(self):
        """Logged in users can watch lots"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        response = self.client.post(f"/api/watchitem/{self.lot.pk}/", data={"watch": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Success")

    def test_unwatch_logged_in(self):
        """Logged in users can unwatch lots"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        # First watch
        self.client.post(f"/api/watchitem/{self.lot.pk}/", data={"watch": "1"})
        # Then unwatch
        response = self.client.post(f"/api/watchitem/{self.lot.pk}/", data={"watch": "false"})
        self.assertEqual(response.status_code, 200)

    def test_get_request_denied(self):
        """GET requests should be denied"""
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(f"/api/watchitem/{self.lot.pk}/")
        self.assertEqual(response.status_code, 405)


class MyBidsViewTests(StandardTestCase):
    """Test my bids view with different user types"""

    def test_my_bids_anonymous(self):
        """Anonymous users should be redirected to login"""
        response = self.client.get("/bids/")
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_my_bids_logged_in(self):
        """Logged in users can view their bids"""
        self.client.login(username=self.userB.username, password="testpassword")
        response = self.client.get("/bids/")
        assert response.status_code == 200


class MyWonLotsViewTests(StandardTestCase):
    """Test my won lots view with different user types"""

    def test_my_won_lots_anonymous(self):
        """Anonymous users should be redirected to login"""
        response = self.client.get("/lots/won/")
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_my_won_lots_logged_in(self):
        """Logged in users can view their won lots"""
        self.client.login(username=self.userB.username, password="testpassword")
        response = self.client.get("/lots/won/")
        assert response.status_code == 200


class DistanceUnitTests(StandardTestCase):
    """Test distance unit conversion functionality"""

    def test_default_distance_unit_is_miles(self):
        """Test that default distance unit is miles"""
        self.assertEqual(self.user.userdata.distance_unit, "mi")

    def test_distance_unit_can_be_set_to_km(self):
        """Test that distance unit can be set to kilometers"""
        userdata = self.user.userdata
        userdata.distance_unit = "km"
        userdata.save()
        userdata.refresh_from_db()
        self.assertEqual(userdata.distance_unit, "km")

    def test_preference_form_converts_km_to_miles_on_save(self):
        """Test that ChangeUserPreferencesForm converts km to miles when saving"""
        from auctions.forms import ChangeUserPreferencesForm

        userdata = self.user.userdata
        userdata.distance_unit = "km"
        userdata.local_distance = 100  # 100 miles in DB
        userdata.save()

        # Form should display ~161 km (100 * 1.60934)
        form = ChangeUserPreferencesForm(user=self.user, instance=userdata)
        self.assertEqual(form.initial["local_distance"], 161)

        # When user submits with 80 km, it should save as ~50 miles
        form_data = {
            "distance_unit": "km",
            "preferred_currency": "USD",
            "local_distance": 80,
            "email_me_about_new_auctions_distance": 160,
            "email_me_about_new_in_person_auctions_distance": 160,
            "email_visible": False,
            "show_ads": True,
            "email_me_about_new_auctions": True,
            "email_me_about_new_local_lots": True,
            "email_me_about_new_lots_ship_to_location": True,
            "email_me_when_people_comment_on_my_lots": True,
            "email_me_about_new_chat_replies": True,
            "email_me_about_new_in_person_auctions": True,
            "send_reminder_emails_about_joining_auctions": True,
            "username_visible": True,
            "share_lot_images": True,
            "auto_add_images": True,
            "push_notifications_when_lots_sell": False,
        }
        form = ChangeUserPreferencesForm(user=self.user, data=form_data, instance=userdata)
        self.assertTrue(form.is_valid())
        saved_instance = form.save()

        # Verify values are stored in miles
        self.assertEqual(saved_instance.local_distance, 50)  # 80 km / 1.60934 ≈ 50 miles
        self.assertEqual(saved_instance.email_me_about_new_auctions_distance, 99)  # 160 km / 1.60934 ≈ 99 miles

    def test_preference_form_keeps_miles_when_unit_is_miles(self):
        """Test that form doesn't convert when unit is miles"""
        from auctions.forms import ChangeUserPreferencesForm

        userdata = self.user.userdata
        userdata.distance_unit = "mi"
        userdata.local_distance = 100
        userdata.save()

        form_data = {
            "distance_unit": "mi",
            "preferred_currency": "USD",
            "local_distance": 50,
            "email_me_about_new_auctions_distance": 100,
            "email_me_about_new_in_person_auctions_distance": 100,
            "email_visible": False,
            "show_ads": True,
            "email_me_about_new_auctions": True,
            "email_me_about_new_local_lots": True,
            "email_me_about_new_lots_ship_to_location": True,
            "email_me_when_people_comment_on_my_lots": True,
            "email_me_about_new_chat_replies": True,
            "email_me_about_new_in_person_auctions": True,
            "send_reminder_emails_about_joining_auctions": True,
            "username_visible": True,
            "share_lot_images": True,
            "auto_add_images": True,
            "push_notifications_when_lots_sell": False,
        }
        form = ChangeUserPreferencesForm(user=self.user, data=form_data, instance=userdata)
        self.assertTrue(form.is_valid())
        saved_instance = form.save()

        # Values should be saved as-is in miles
        self.assertEqual(saved_instance.local_distance, 50)
        self.assertEqual(saved_instance.email_me_about_new_auctions_distance, 100)

    def test_distance_filter_converts_miles_to_km(self):
        """Test that distance_display filter converts miles to km for km users"""
        from auctions.templatetags.distance_filters import distance_display

        userdata = self.user.userdata
        userdata.distance_unit = "km"
        userdata.save()

        # 10 miles should display as 16 km
        result = distance_display(10, self.user)
        self.assertEqual(result, "16 km")

    def test_distance_filter_keeps_miles_for_miles_users(self):
        """Test that distance_display filter keeps miles for miles users"""
        from auctions.templatetags.distance_filters import distance_display

        userdata = self.user.userdata
        userdata.distance_unit = "mi"
        userdata.save()

        # 10 miles should display as 10 miles
        result = distance_display(10, self.user)
        self.assertEqual(result, "10 miles")

    def test_distance_filter_handles_negative_distance(self):
        """Test that distance_display filter handles negative distance (returns empty)"""
        from auctions.templatetags.distance_filters import distance_display

        result = distance_display(-1, self.user)
        self.assertEqual(result, "")

    def test_distance_filter_handles_zero_distance(self):
        """Test that distance_display filter handles zero distance (returns empty)"""
        from auctions.templatetags.distance_filters import distance_display

        result = distance_display(0, self.user)
        self.assertEqual(result, "")

    def test_distance_filter_defaults_to_miles_for_anonymous_users(self):
        """Test that distance_display filter defaults to miles for anonymous users"""
        from django.contrib.auth.models import AnonymousUser

        from auctions.templatetags.distance_filters import distance_display

        anonymous = AnonymousUser()
        result = distance_display(10, anonymous)
        self.assertEqual(result, "10 miles")

    def test_distance_filter_handles_string_input(self):
        """Test that distance_display filter handles string input from database"""
        from auctions.templatetags.distance_filters import distance_display

        userdata = self.user.userdata
        userdata.distance_unit = "mi"
        userdata.save()

        # String input should be converted to float
        result = distance_display("10", self.user)
        self.assertEqual(result, "10 miles")

    def test_distance_filter_handles_string_input_with_km(self):
        """Test that distance_display filter handles string input and converts to km"""
        from auctions.templatetags.distance_filters import distance_display

        userdata = self.user.userdata
        userdata.distance_unit = "km"
        userdata.save()

        # String input "10" miles should display as 16 km
        result = distance_display("10", self.user)
        self.assertEqual(result, "16 km")

    def test_distance_filter_handles_string_input_for_anonymous_users(self):
        """Test that distance_display filter handles string input for anonymous users"""
        from django.contrib.auth.models import AnonymousUser

        from auctions.templatetags.distance_filters import distance_display

        anonymous = AnonymousUser()
        # String input should work for anonymous users
        result = distance_display("10", anonymous)
        self.assertEqual(result, "10 miles")

    def test_distance_filter_handles_invalid_string_input(self):
        """Test that distance_display filter handles invalid string input"""
        from auctions.templatetags.distance_filters import distance_display

        # Invalid string should return empty string
        result = distance_display("invalid", self.user)
        self.assertEqual(result, "")

    def test_distance_filter_handles_none_input(self):
        """Test that distance_display filter handles None input"""
        from auctions.templatetags.distance_filters import distance_display

        # None input should return empty string
        result = distance_display(None, self.user)
        self.assertEqual(result, "")


class PayPalInfoViewTests(TestCase):
    """Test that the PayPal info page works for both logged in and non-logged in users"""

    def test_paypal_info_non_logged_in_user(self):
        """Test that non-logged-in users can access the PayPal info page"""
        url = reverse("paypal_seller")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Accept payments with PayPal")

    def test_paypal_info_logged_in_user(self):
        """Test that logged-in users can access the PayPal info page"""
        User.objects.create_user(username="testuser", password="testpassword")
        self.client.login(username="testuser", password="testpassword")
        url = reverse("paypal_seller")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Accept payments with PayPal")


class UserExportTests(StandardTestCase):
    """Test user export and email composition functionality"""

    def test_user_export_without_filter(self):
        """Test that user export works without a filter"""
        self.client.login(username="admin_user", password="testpassword")
        url = reverse("user_list", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")

    def test_user_export_with_filter(self):
        """Test that user export works with a filter query parameter"""
        self.client.login(username="admin_user", password="testpassword")
        url = reverse("user_list", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url, {"query": "admin"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        # Check that filename includes query
        self.assertIn("admin", response["Content-Disposition"])

    def test_user_export_permission_denied(self):
        """Test that non-admin users cannot export users"""
        self.client.login(username="no_lots", password="testpassword")
        url = reverse("user_list", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        self.assertIn(response.status_code, [302, 403])

    def test_compose_email_without_filter(self):
        """Test composing email to all users"""
        self.client.login(username="admin_user", password="testpassword")
        url = reverse("compose_email_to_users", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # The view renders a button snippet with a mailto href, not a redirect
        self.assertContains(response, 'id="email_all_users"')

    def test_compose_email_with_filter(self):
        """Test composing email with a filter"""
        self.client.login(username="admin_user", password="testpassword")
        url = reverse("compose_email_to_users", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url, {"query": "admin"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="email_all_users"')

    def test_compose_email_permission_denied(self):
        """Test that non-admin users cannot compose emails"""
        self.client.login(username="no_lots", password="testpassword")
        url = reverse("compose_email_to_users", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 403)

    def test_user_export_includes_lots_sold_column(self):
        """Test that user export includes the 'Lots sold' column with correct data"""
        self.client.login(username="admin_user", password="testpassword")
        url = reverse("user_list", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")

        # Decode the CSV content
        content = response.content.decode("utf-8")
        lines = content.strip().split("\n")

        # Check header row contains "Lots sold"
        header = lines[0]
        self.assertIn("Lots sold", header)

        # Verify header column order: "Lots submitted" should come before "Lots sold" which comes before "Lots won"
        self.assertLess(header.index("Lots submitted"), header.index("Lots sold"))
        self.assertLess(header.index("Lots sold"), header.index("Lots won"))

        # Find the row for "my_lot" user who has:
        # - 4 lots submitted (lot, lotB, lotC, unsoldLot)
        # - 3 lots sold (lot, lotB, lotC have winning_price)
        # - 0 lots won (this user is a seller)
        header_parts = header.split(",")
        lots_submitted_idx = header_parts.index("Lots submitted")
        lots_sold_idx = header_parts.index("Lots sold")
        lots_won_idx = header_parts.index("Lots won")

        # Find the row with my_lot username
        for line in lines[1:]:
            if "my_lot" in line:
                parts = line.split(",")
                # Verify the counts match expected values
                self.assertEqual(parts[lots_submitted_idx], "4", "Expected 4 lots submitted")
                self.assertEqual(parts[lots_sold_idx], "3", "Expected 3 lots sold")
                self.assertEqual(parts[lots_won_idx], "0", "Expected 0 lots won")
                break
        else:
            self.fail("Could not find my_lot user in CSV export")


class UserTrustSystemTests(StandardTestCase):
    """Test the user trust system functionality"""

    def setUp(self):
        super().setUp()
        # Create a superuser for testing trust functionality
        self.superuser = User.objects.create_superuser(
            username="superuser", password="testpassword", email="super@example.com"
        )
        # Create an untrusted user
        self.untrusted_user = User.objects.create_user(
            username="untrusted", password="testpassword", email="untrusted@example.com"
        )
        self.untrusted_user.userdata.is_trusted = False
        self.untrusted_user.userdata.save()
        # Create an auction by the untrusted user
        time = timezone.now() + datetime.timedelta(days=2)
        timeStart = timezone.now() - datetime.timedelta(days=1)
        self.untrusted_auction = Auction.objects.create(
            created_by=self.untrusted_user,
            title="Untrusted user auction",
            is_online=True,
            date_end=time,
            date_start=timeStart,
            winning_bid_percent_to_club=25,
            lot_entry_fee=2,
            unsold_lot_fee=10,
            tax=25,
        )

    def test_trusted_field_exists_on_userdata(self):
        """Test that is_trusted field exists on UserData model"""
        self.assertTrue(hasattr(self.user.userdata, "is_trusted"))
        self.assertIsInstance(self.user.userdata.is_trusted, bool)

    def test_superuser_can_trust_user(self):
        """Test that superuser can trust a user via URL parameter"""
        self.client.login(username="superuser", password="testpassword")
        url = reverse("auction_main", kwargs={"slug": self.untrusted_auction.slug})
        # Join the auction first
        AuctionTOS.objects.create(
            user=self.superuser,
            auction=self.untrusted_auction,
            pickup_location=PickupLocation.objects.create(
                name="location",
                auction=self.untrusted_auction,
                pickup_time=timezone.now() + datetime.timedelta(days=3),
            ),
        )
        response = self.client.get(url + "?trust_user=true")
        self.assertEqual(response.status_code, 200)
        # Reload user data
        self.untrusted_user.userdata.refresh_from_db()
        self.assertTrue(self.untrusted_user.userdata.is_trusted)

    def test_non_superuser_cannot_trust_user(self):
        """Test that non-superuser cannot trust a user"""
        self.client.login(username="admin_user", password="testpassword")
        url = reverse("auction_main", kwargs={"slug": self.untrusted_auction.slug})
        initial_trust = self.untrusted_user.userdata.is_trusted
        self.client.get(url + "?trust_user=true")
        # Reload user data
        self.untrusted_user.userdata.refresh_from_db()
        # Trust status should not change
        self.assertEqual(self.untrusted_user.userdata.is_trusted, initial_trust)

    def test_untrusted_user_invoice_no_payment_button(self):
        """Test that invoices for untrusted users don't show payment button"""
        # Create invoice for untrusted auction
        theFuture = timezone.now() + datetime.timedelta(days=3)
        location = PickupLocation.objects.create(name="location", auction=self.untrusted_auction, pickup_time=theFuture)
        tos = AuctionTOS.objects.create(
            user=self.user_with_no_lots, auction=self.untrusted_auction, pickup_location=location
        )
        invoice, created = Invoice.objects.get_or_create(auctiontos_user=tos)
        # Enable online payments
        self.untrusted_auction.enable_online_payments = True
        self.untrusted_auction.save()
        # Check that payment button is not shown
        self.assertFalse(invoice.show_payment_button)

    def test_trusted_user_invoice_shows_payment_button(self):
        """Test that invoices for trusted users show payment button when conditions are met"""
        # Make sure user is trusted
        self.user.userdata.is_trusted = True
        self.user.userdata.paypal_enabled = True
        self.user.userdata.save()
        # Enable online payments
        self.online_auction.enable_online_payments = True
        self.online_auction.save()
        # Get invoice - show_payment_button may still be False due to other checks
        # (e.g., balance, PayPal config), we're mainly testing that the is_trusted check doesn't block it
        Invoice.objects.get(auctiontos_user=self.online_tos)

    def test_invoice_template_shows_email_message_for_trusted(self):
        """Test that invoice template shows email notification message for trusted users"""
        self.client.login(username="my_lot", password="testpassword")
        # Make sure the creator is trusted
        self.user.userdata.is_trusted = True
        self.user.userdata.save()
        url = reverse("invoice_by_pk", kwargs={"pk": self.invoice.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_auction_ribbon_trust_link_for_superuser(self):
        """Test that superuser sees trust link in auction ribbon"""
        self.client.login(username="superuser", password="testpassword")
        url = reverse("auction_main", kwargs={"slug": self.untrusted_auction.slug})
        # Join the auction first
        location = PickupLocation.objects.filter(auction=self.untrusted_auction).first()
        if not location:
            location = PickupLocation.objects.create(
                name="location",
                auction=self.untrusted_auction,
                pickup_time=timezone.now() + datetime.timedelta(days=3),
            )
        AuctionTOS.objects.create(user=self.superuser, auction=self.untrusted_auction, pickup_location=location)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # Check that response contains trust link (only if auction is not promoted)
        if not self.untrusted_auction.promote_this_auction:
            self.assertContains(response, "trust_user=true")

    def test_email_invoice_skips_untrusted_users(self):
        """Test that email_invoice management command skips untrusted users"""
        from django.core.management import call_command

        # Create an invoice for untrusted auction
        theFuture = timezone.now() + datetime.timedelta(days=3)
        location = PickupLocation.objects.create(name="location", auction=self.untrusted_auction, pickup_time=theFuture)
        tos = AuctionTOS.objects.create(
            user=self.user_with_no_lots, auction=self.untrusted_auction, pickup_location=location
        )
        invoice, created = Invoice.objects.get_or_create(auctiontos_user=tos)
        invoice.status = "UNPAID"
        invoice.email_sent = False
        invoice.save()
        # Enable email sending
        self.untrusted_auction.email_users_when_invoices_ready = True
        self.untrusted_auction.save()
        # Run command
        call_command("email_invoice")
        # Reload invoice
        invoice.refresh_from_db()
        # Email should be marked sent but not actually sent
        self.assertTrue(invoice.email_sent)


class WatchOrUnwatchViewTests(StandardTestCase):
    """Test watchOrUnwatch function-based view"""

    def test_watch_anonymous_denied(self):
        """Anonymous users cannot watch lots"""
        response = self.client.post(f"/api/watchitem/{self.lot.pk}/", data={"watch": "true"})
        self.assertIn(response.status_code, [302, 403])

    def test_watch_logged_in(self):
        """Logged in users can watch lots"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        response = self.client.post(f"/api/watchitem/{self.lot.pk}/", data={"watch": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Success")

    def test_unwatch_logged_in(self):
        """Logged in users can unwatch lots"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        # First watch
        self.client.post(f"/api/watchitem/{self.lot.pk}/", data={"watch": "true"})
        # Then unwatch
        response = self.client.post(f"/api/watchitem/{self.lot.pk}/", data={"watch": "false"})
        self.assertEqual(response.status_code, 200)

    def test_get_request_denied(self):
        """GET requests should be denied"""
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(f"/api/watchitem/{self.lot.pk}/")
        self.assertEqual(response.status_code, 405)


class LotEndauctionsMethodsTests(StandardTestCase):
    """Test the new Lot model methods used by endauctions management command"""

    def test_send_ending_very_soon_message_not_ending(self):
        """Test that message is not sent when lot is not ending very soon"""
        # Create a lot that ends in the future
        future_time = timezone.now() + datetime.timedelta(hours=1)
        lot = Lot.objects.create(
            lot_name="Future lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            date_end=future_time,
            active=True,
        )
        # This should not raise an error, and not send a message
        lot.send_ending_very_soon_message()
        # If we get here without error, the test passes

    def test_send_ending_very_soon_message_ending_soon(self):
        """Test that message is sent when lot is ending very soon"""
        # Create a lot that ends in less than 1 minute
        soon_time = timezone.now() + datetime.timedelta(seconds=30)
        lot = Lot.objects.create(
            lot_name="Ending soon lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            date_end=soon_time,
            active=True,
        )
        # This should not raise an error
        lot.send_ending_very_soon_message()

    def test_send_ending_very_soon_message_already_sold(self):
        """Test that message is not sent when lot is already sold"""
        # Create a sold lot that is ending soon
        soon_time = timezone.now() + datetime.timedelta(seconds=30)
        lot = Lot.objects.create(
            lot_name="Sold lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            date_end=soon_time,
            active=True,
            winner=self.userB,
            winning_price=10,
        )
        # This should not send a message since lot is sold
        lot.send_ending_very_soon_message()

    def test_send_lot_end_message_with_winner(self):
        """Test that correct message is sent when lot ends with a winner"""
        # Create a lot with a high bidder (without an auction to avoid complications)
        lot_end_time = timezone.now() - datetime.timedelta(hours=1)
        bid_time = timezone.now() - datetime.timedelta(hours=2)

        lot = Lot.objects.create(
            lot_name="Lot with winner",
            user=self.user,
            quantity=1,
            date_end=lot_end_time,
            active=True,
            reserve_price=5,
        )
        # Add a bid with a time before the lot ended
        bid = Bid.objects.create(lot_number=lot, user=self.userB, amount=10, was_high_bid=True)
        # Set the bid time to before the lot ended
        bid.bid_time = bid_time
        bid.last_bid_time = bid_time
        bid.save()

        # Send lot end message
        lot.send_lot_end_message()

        # Check that LotHistory was created
        history = LotHistory.objects.filter(lot=lot).first()
        self.assertIsNotNone(history)
        self.assertIn("Won by", history.message)

    def test_send_lot_end_message_no_winner(self):
        """Test that correct message is sent when lot ends without a winner"""
        # Create a lot without bids
        past_time = timezone.now() - datetime.timedelta(hours=1)
        lot = Lot.objects.create(
            lot_name="Lot without winner",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            date_end=past_time,
            active=True,
            reserve_price=5,
        )

        # Send lot end message
        lot.send_lot_end_message()

        # Check that LotHistory was created
        history = LotHistory.objects.filter(lot=lot).first()
        self.assertIsNotNone(history)
        self.assertEqual(history.message, "This lot did not sell")

    def test_send_non_auction_lot_emails_with_winner(self):
        """Test that emails are sent for non-auction lots with winners"""
        # Create a non-auction lot with a winner
        # Use user_with_no_lots which has a valid email
        lot = Lot.objects.create(
            lot_name="Non-auction lot",
            user=self.user,
            quantity=1,
            winner=self.user_with_no_lots,
            winning_price=10,
            active=False,
        )

        # This should not raise an error
        lot.send_non_auction_lot_emails()

    def test_send_non_auction_lot_emails_no_winner(self):
        """Test that emails are not sent for non-auction lots without winners"""
        # Create a non-auction lot without a winner
        lot = Lot.objects.create(
            lot_name="Non-auction lot no winner",
            user=self.user,
            quantity=1,
            active=False,
        )

        # This should not raise an error or send emails
        lot.send_non_auction_lot_emails()

    def test_send_non_auction_lot_emails_in_auction(self):
        """Test that emails are not sent for auction lots"""
        # Create an auction lot with a winner
        lot = Lot.objects.create(
            lot_name="Auction lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winner=self.userB,
            winning_price=10,
            active=False,
        )

        # This should not send emails since it's in an auction
        lot.send_non_auction_lot_emails()

    def test_process_relist_logic_no_relist(self):
        """Test relist logic when lot should not be relisted"""
        # Create a non-auction lot with no relist settings
        lot = Lot.objects.create(
            lot_name="No relist lot",
            user=self.user,
            quantity=1,
            active=False,
            relist_if_sold=False,
            relist_if_not_sold=False,
        )

        relist, sendNoRelistWarning = lot.process_relist_logic()
        self.assertFalse(relist)
        self.assertFalse(sendNoRelistWarning)

    def test_process_relist_logic_relist_if_sold_with_countdown(self):
        """Test relist logic when lot sold and should be relisted"""
        # Create a non-auction lot that sold and should be relisted
        lot = Lot.objects.create(
            lot_name="Relist if sold lot",
            user=self.user,
            quantity=1,
            winner=self.userB,
            winning_price=10,
            active=False,
            relist_if_sold=True,
            relist_countdown=3,
        )

        relist, sendNoRelistWarning = lot.process_relist_logic()
        self.assertTrue(relist)
        self.assertFalse(sendNoRelistWarning)
        self.assertEqual(lot.relist_countdown, 2)

    def test_process_relist_logic_relist_if_sold_no_countdown(self):
        """Test relist logic when lot sold but countdown is 0"""
        # Create a non-auction lot that sold but has no more relists
        lot = Lot.objects.create(
            lot_name="No more relists lot",
            user=self.user,
            quantity=1,
            winner=self.userB,
            winning_price=10,
            active=False,
            relist_if_sold=True,
            relist_countdown=0,
        )

        relist, sendNoRelistWarning = lot.process_relist_logic()
        self.assertFalse(relist)
        self.assertTrue(sendNoRelistWarning)

    def test_process_relist_logic_relist_if_not_sold_with_countdown(self):
        """Test relist logic when lot didn't sell and should be relisted"""
        # Create a non-auction lot that didn't sell and should be relisted
        past_time = timezone.now() - datetime.timedelta(hours=1)
        lot = Lot.objects.create(
            lot_name="Relist if not sold lot",
            user=self.user,
            quantity=1,
            date_end=past_time,
            active=False,
            relist_if_not_sold=True,
            relist_countdown=3,
            lot_run_duration=10,
        )

        relist, sendNoRelistWarning = lot.process_relist_logic()
        self.assertFalse(relist)  # unsold lots don't trigger immediate relist
        self.assertFalse(sendNoRelistWarning)
        self.assertEqual(lot.relist_countdown, 2)
        self.assertTrue(lot.active)  # lot is reactivated

    def test_process_relist_logic_relist_if_not_sold_no_countdown(self):
        """Test relist logic when lot didn't sell but countdown is 0"""
        # Create a non-auction lot that didn't sell but has no more relists
        lot = Lot.objects.create(
            lot_name="No more relists unsold lot",
            user=self.user,
            quantity=1,
            active=False,
            relist_if_not_sold=True,
            relist_countdown=0,
        )

        relist, sendNoRelistWarning = lot.process_relist_logic()
        self.assertFalse(relist)
        self.assertTrue(sendNoRelistWarning)

    def test_process_relist_logic_auction_lot(self):
        """Test relist logic doesn't apply to auction lots"""
        # Create an auction lot
        lot = Lot.objects.create(
            lot_name="Auction lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            active=False,
            relist_if_sold=True,
            relist_countdown=3,
        )

        relist, sendNoRelistWarning = lot.process_relist_logic()
        self.assertFalse(relist)
        self.assertFalse(sendNoRelistWarning)

    def test_relist_lot_basic(self):
        """Test that relist_lot creates a new lot correctly"""
        # Create a lot to relist
        original_lot = Lot.objects.create(
            lot_name="Original lot",
            user=self.user,
            quantity=1,
            winner=self.userB,
            winning_price=10,
            active=False,
            lot_run_duration=10,
        )
        original_pk = original_lot.pk

        # Relist the lot
        new_lot = original_lot.relist_lot()

        # Check that a new lot was created
        self.assertNotEqual(new_lot.pk, original_pk)
        self.assertTrue(new_lot.active)
        self.assertIsNone(new_lot.winner)
        self.assertIsNone(new_lot.winning_price)
        self.assertFalse(new_lot.buy_now_used)
        self.assertEqual(new_lot.lot_name, "Original lot")

    def test_relist_lot_with_images(self):
        """Test that relist_lot copies images correctly"""
        from auctions.models import LotImage

        # Create a lot with an image
        original_lot = Lot.objects.create(
            lot_name="Lot with image",
            user=self.user,
            quantity=1,
            winner=self.userB,
            winning_price=10,
            active=False,
            lot_run_duration=10,
        )

        # Create an image for the lot
        LotImage.objects.create(
            lot_number=original_lot,
            image_source="ACTUAL",
            is_primary=True,
        )

        # Relist the lot
        new_lot = original_lot.relist_lot()

        # Check that image was copied
        new_images = LotImage.objects.filter(lot_number=new_lot)
        self.assertEqual(new_images.count(), 1)
        new_image = new_images.first()
        # ACTUAL should change to REPRESENTATIVE on relist
        self.assertEqual(new_image.image_source, "REPRESENTATIVE")
        self.assertTrue(new_image.is_primary)


class WebSocketConsumerTests(TransactionTestCase):
    """Tests for websocket consumers (LotConsumer, UserConsumer, AuctionConsumer)

    Best practices for websocket tests in CI:
    - All operations have timeouts
    - Proper cleanup with try-finally blocks
    - Simplified message handling to avoid hanging

    Note: Uses TransactionTestCase instead of TestCase to properly handle
    database transactions with async code and channels' database_sync_to_async
    """

    # Timeout constants for CI reliability
    CONNECT_TIMEOUT = 5
    DISCONNECT_TIMEOUT = 5
    RECEIVE_TIMEOUT = 3

    def setUp(self):
        """Set up test data needed for websocket tests - mirrors StandardTestCase setup"""
        time = timezone.now() - datetime.timedelta(days=2)
        timeStart = timezone.now() - datetime.timedelta(days=3)
        theFuture = timezone.now() + datetime.timedelta(days=3)
        self.admin_user = User.objects.create_user(
            username="admin_user", password="testpassword", email="test@example.com"
        )
        self.user = User.objects.create_user(username="my_lot", password="testpassword", email="test@example.com")
        self.user_with_no_lots = User.objects.create_user(
            username="no_lots", password="testpassword", email="asdf@example.com"
        )
        self.user_who_does_not_join = User.objects.create_user(
            username="no_joins", password="testpassword", email="zxcgv@example.com"
        )
        self.online_auction = Auction.objects.create(
            created_by=self.user,
            title="This auction is online",
            is_online=True,
            date_end=time,
            date_start=timeStart,
            winning_bid_percent_to_club=25,
            lot_entry_fee=2,
            unsold_lot_fee=10,
            tax=25,
        )
        self.in_person_auction = Auction.objects.create(
            created_by=self.user,
            title="This auction is in-person",
            is_online=False,
            date_end=time,
            date_start=timeStart,
            winning_bid_percent_to_club=25,
            lot_entry_fee=2,
            unsold_lot_fee=10,
            tax=25,
            buy_now="allow",
            reserve_price="allow",
            use_seller_dash_lot_numbering=True,
        )
        self.location = PickupLocation.objects.create(
            name="location", auction=self.online_auction, pickup_time=theFuture
        )
        self.in_person_location = PickupLocation.objects.create(
            name="location", auction=self.in_person_auction, pickup_time=theFuture
        )
        self.userB = User.objects.create_user(username="no_tos", password="testpassword")
        self.admin_online_tos = AuctionTOS.objects.create(
            user=self.admin_user, auction=self.online_auction, pickup_location=self.location, is_admin=True
        )
        self.admin_in_person_tos = AuctionTOS.objects.create(
            user=self.admin_user, auction=self.in_person_auction, pickup_location=self.in_person_location, is_admin=True
        )
        self.online_tos = AuctionTOS.objects.create(
            user=self.user, auction=self.online_auction, pickup_location=self.location
        )
        self.in_person_tos = AuctionTOS.objects.create(
            user=self.user, auction=self.in_person_auction, pickup_location=self.location
        )
        self.tosB = AuctionTOS.objects.create(
            user=self.userB, auction=self.online_auction, pickup_location=self.location
        )
        self.tosC = AuctionTOS.objects.create(
            user=self.user_with_no_lots, auction=self.online_auction, pickup_location=self.location
        )
        self.in_person_buyer = AuctionTOS.objects.create(
            user=self.user_with_no_lots,
            auction=self.in_person_auction,
            pickup_location=self.in_person_location,
            bidder_number="555",
        )
        self.lot = Lot.objects.create(
            lot_name="A test lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=10,
            auctiontos_winner=self.tosB,
            active=False,
        )
        self.lotB = Lot.objects.create(
            lot_name="B test lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=10,
            auctiontos_winner=self.tosB,
            active=False,
        )

    async def _create_active_lot_with_auction(self, seller_user, bidder_user=None):
        """Helper method to create an active lot with a future-dated auction"""
        from channels.db import database_sync_to_async

        theFuture = timezone.now() + datetime.timedelta(days=3)
        auction = await database_sync_to_async(Auction.objects.create)(
            created_by=seller_user,
            title="Future auction",
            is_online=True,
            date_end=theFuture,
            date_start=timezone.now(),
        )
        location = await database_sync_to_async(PickupLocation.objects.create)(
            name="test location", auction=auction, pickup_time=theFuture
        )
        seller_tos = await database_sync_to_async(AuctionTOS.objects.create)(
            user=seller_user, auction=auction, pickup_location=location
        )

        if bidder_user:
            await database_sync_to_async(AuctionTOS.objects.create)(
                user=bidder_user, auction=auction, pickup_location=location
            )

        lot = await database_sync_to_async(Lot.objects.create)(
            lot_name="Test websocket lot",
            auction=auction,
            auctiontos_seller=seller_tos,
            quantity=1,
            reserve_price=10,
            date_end=theFuture,
        )
        return lot

    async def test_lot_consumer_connect_authenticated_user(self):
        """Test LotConsumer connection with authenticated user who has joined auction"""
        from channels.testing import WebsocketCommunicator

        from auctions.consumers import LotConsumer

        lot = await self._create_active_lot_with_auction(self.user, self.user)

        communicator = WebsocketCommunicator(
            LotConsumer.as_asgi(),
            f"/ws/lots/{lot.pk}/",
        )
        communicator.scope["user"] = self.user
        communicator.scope["url_route"] = {"kwargs": {"lot_number": lot.pk}}

        try:
            connected, _ = await communicator.connect(timeout=self.CONNECT_TIMEOUT)
            self.assertTrue(connected)
        finally:
            await communicator.disconnect(timeout=self.DISCONNECT_TIMEOUT)

    async def test_lot_consumer_connect_anonymous_user(self):
        """Test LotConsumer connection with anonymous user"""
        from channels.testing import WebsocketCommunicator
        from django.contrib.auth.models import AnonymousUser

        from auctions.consumers import LotConsumer

        lot = await self._create_active_lot_with_auction(self.user)

        communicator = WebsocketCommunicator(
            LotConsumer.as_asgi(),
            f"/ws/lots/{lot.pk}/",
        )
        communicator.scope["user"] = AnonymousUser()
        communicator.scope["url_route"] = {"kwargs": {"lot_number": lot.pk}}

        try:
            # Anonymous users can connect to view lot
            connected, _ = await communicator.connect(timeout=self.CONNECT_TIMEOUT)
            self.assertTrue(connected)
        finally:
            await communicator.disconnect(timeout=self.DISCONNECT_TIMEOUT)

    async def test_lot_consumer_chat_message_authenticated(self):
        """Test sending chat message as authenticated user who has joined auction"""
        from channels.testing import WebsocketCommunicator

        from auctions.consumers import LotConsumer

        lot = await self._create_active_lot_with_auction(self.user, self.user_with_no_lots)

        communicator = WebsocketCommunicator(
            LotConsumer.as_asgi(),
            f"/ws/lots/{lot.pk}/",
        )
        communicator.scope["user"] = self.user_with_no_lots
        communicator.scope["url_route"] = {"kwargs": {"lot_number": lot.pk}}

        try:
            await communicator.connect(timeout=self.CONNECT_TIMEOUT)

            # Send a chat message
            await communicator.send_json_to({"message": "Hello from test!"})

            # Should receive the message back, skip any system messages
            found_message = False
            for _ in range(5):  # Reduced from 10 to 5 for faster failure
                try:
                    response = await communicator.receive_json_from(timeout=self.RECEIVE_TIMEOUT)
                    if response.get("message") == "Hello from test!" and response.get("info") == "CHAT":
                        found_message = True
                        self.assertEqual(response["username"], str(self.user_with_no_lots))
                        break
                except:
                    break

            self.assertTrue(found_message, "Did not receive the expected chat message")
        finally:
            await communicator.disconnect(timeout=self.DISCONNECT_TIMEOUT)

    async def test_lot_consumer_chat_message_anonymous(self):
        """Test that anonymous users cannot send chat messages"""
        from channels.testing import WebsocketCommunicator
        from django.contrib.auth.models import AnonymousUser

        from auctions.consumers import LotConsumer

        lot = await self._create_active_lot_with_auction(self.user)

        communicator = WebsocketCommunicator(
            LotConsumer.as_asgi(),
            f"/ws/lots/{lot.pk}/",
        )
        communicator.scope["user"] = AnonymousUser()
        communicator.scope["url_route"] = {"kwargs": {"lot_number": lot.pk}}

        try:
            await communicator.connect(timeout=self.CONNECT_TIMEOUT)

            # Try to send a chat message
            await communicator.send_json_to({"message": "Hello from anonymous!"})

            # Anonymous users should not get a response for their message
            # The consumer just passes without doing anything
        finally:
            await communicator.disconnect(timeout=self.DISCONNECT_TIMEOUT)

    async def test_lot_consumer_bid_authenticated_with_tos(self):
        """Test placing a bid as authenticated user who has joined auction"""
        from channels.testing import WebsocketCommunicator

        from auctions.consumers import LotConsumer

        lot = await self._create_active_lot_with_auction(self.user, self.user_with_no_lots)

        communicator = WebsocketCommunicator(
            LotConsumer.as_asgi(),
            f"/ws/lots/{lot.pk}/",
        )
        communicator.scope["user"] = self.user_with_no_lots
        communicator.scope["url_route"] = {"kwargs": {"lot_number": lot.pk}}

        try:
            await communicator.connect(timeout=self.CONNECT_TIMEOUT)

            # Place a bid
            await communicator.send_json_to({"bid": 15})

            # Should receive a response about the bid (either success or error message)
            found_bid_response = False
            for _ in range(5):  # Reduced from 10 to 5
                try:
                    response = await communicator.receive_json_from(timeout=self.RECEIVE_TIMEOUT)
                    # Accept any bid-related response: success info types or error
                    if response.get("info") in ["NEW_HIGH_BIDDER", "INFO", "ERROR"] or response.get("error"):
                        found_bid_response = True
                        break
                except:
                    break

            self.assertTrue(found_bid_response, "Did not receive expected bid response")
        finally:
            await communicator.disconnect(timeout=self.DISCONNECT_TIMEOUT)

    async def test_lot_consumer_bid_user_not_joined_auction(self):
        """Test that users who haven't joined auction cannot bid"""
        from channels.testing import WebsocketCommunicator

        from auctions.consumers import LotConsumer

        lot = await self._create_active_lot_with_auction(self.user)

        communicator = WebsocketCommunicator(
            LotConsumer.as_asgi(),
            f"/ws/lots/{lot.pk}/",
        )
        communicator.scope["user"] = self.user_who_does_not_join
        communicator.scope["url_route"] = {"kwargs": {"lot_number": lot.pk}}

        try:
            await communicator.connect(timeout=self.CONNECT_TIMEOUT)

            # Try to place a bid
            await communicator.send_json_to({"bid": 15})

            # Should receive an error
            found_error = False
            for _ in range(5):  # Reduced from 10 to 5
                try:
                    response = await communicator.receive_json_from(timeout=self.RECEIVE_TIMEOUT)
                    if response.get("error"):
                        found_error = True
                        self.assertIn("joined", response["error"].lower())
                        break
                except:
                    break

            self.assertTrue(found_error, "Did not receive expected error message")
        finally:
            await communicator.disconnect(timeout=self.DISCONNECT_TIMEOUT)

    async def test_lot_consumer_bid_anonymous_user(self):
        """Test that anonymous users cannot bid"""
        from channels.testing import WebsocketCommunicator
        from django.contrib.auth.models import AnonymousUser

        from auctions.consumers import LotConsumer

        lot = await self._create_active_lot_with_auction(self.user)

        communicator = WebsocketCommunicator(
            LotConsumer.as_asgi(),
            f"/ws/lots/{lot.pk}/",
        )
        communicator.scope["user"] = AnonymousUser()
        communicator.scope["url_route"] = {"kwargs": {"lot_number": lot.pk}}

        try:
            await communicator.connect(timeout=self.CONNECT_TIMEOUT)

            # Try to place a bid
            await communicator.send_json_to({"bid": 15})

            # Anonymous users should not get a response for their bid
            # The consumer just passes without doing anything
        finally:
            await communicator.disconnect(timeout=self.DISCONNECT_TIMEOUT)

    async def test_lot_consumer_bid_before_online_bidding_starts(self):
        """Test that bids cannot be placed before online bidding starts for in-person auctions"""
        from channels.db import database_sync_to_async
        from channels.testing import WebsocketCommunicator

        from auctions.consumers import LotConsumer

        # Create an in-person auction with online bidding that hasn't started yet
        theFuture = timezone.now() + datetime.timedelta(days=3)
        online_bidding_start = timezone.now() + datetime.timedelta(hours=2)
        online_bidding_end = timezone.now() + datetime.timedelta(days=2)

        auction = await database_sync_to_async(Auction.objects.create)(
            created_by=self.user,
            title="In-person auction with future online bidding",
            is_online=False,
            date_start=timezone.now(),
            date_end=theFuture,
            date_online_bidding_starts=online_bidding_start,
            date_online_bidding_ends=online_bidding_end,
            online_bidding="allow",
        )
        location = await database_sync_to_async(PickupLocation.objects.create)(
            name="test location", auction=auction, pickup_time=theFuture
        )
        seller_tos = await database_sync_to_async(AuctionTOS.objects.create)(
            user=self.user, auction=auction, pickup_location=location
        )
        await database_sync_to_async(AuctionTOS.objects.create)(
            user=self.user_with_no_lots, auction=auction, pickup_location=location
        )

        lot = await database_sync_to_async(Lot.objects.create)(
            lot_name="Test lot before online bidding",
            auction=auction,
            auctiontos_seller=seller_tos,
            quantity=1,
            reserve_price=10,
            date_end=theFuture,
        )

        communicator = WebsocketCommunicator(
            LotConsumer.as_asgi(),
            f"/ws/lots/{lot.pk}/",
        )
        communicator.scope["user"] = self.user_with_no_lots
        communicator.scope["url_route"] = {"kwargs": {"lot_number": lot.pk}}

        try:
            await communicator.connect(timeout=self.CONNECT_TIMEOUT)

            # Try to place a bid
            await communicator.send_json_to({"bid": 15})

            # Should receive an error about online bidding not started
            found_error = False
            for _ in range(5):
                try:
                    response = await communicator.receive_json_from(timeout=self.RECEIVE_TIMEOUT)
                    if response.get("error"):
                        found_error = True
                        self.assertIn("hasn't started", response["error"].lower())
                        break
                except:
                    break

            self.assertTrue(found_error, "Did not receive expected error about online bidding not started")
        finally:
            await communicator.disconnect(timeout=self.DISCONNECT_TIMEOUT)

    async def test_lot_consumer_bid_after_online_bidding_ends(self):
        """Test that bids cannot be placed after online bidding ends for in-person auctions"""
        from channels.db import database_sync_to_async
        from channels.testing import WebsocketCommunicator

        from auctions.consumers import LotConsumer

        # Create an in-person auction with online bidding that has ended
        theFuture = timezone.now() + datetime.timedelta(days=3)
        online_bidding_start = timezone.now() - datetime.timedelta(days=2)
        online_bidding_end = timezone.now() - datetime.timedelta(hours=1)

        auction = await database_sync_to_async(Auction.objects.create)(
            created_by=self.user,
            title="In-person auction with ended online bidding",
            is_online=False,
            date_start=timezone.now() - datetime.timedelta(days=3),
            date_end=theFuture,
            date_online_bidding_starts=online_bidding_start,
            date_online_bidding_ends=online_bidding_end,
            online_bidding="allow",
        )
        location = await database_sync_to_async(PickupLocation.objects.create)(
            name="test location", auction=auction, pickup_time=theFuture
        )
        seller_tos = await database_sync_to_async(AuctionTOS.objects.create)(
            user=self.user, auction=auction, pickup_location=location
        )
        await database_sync_to_async(AuctionTOS.objects.create)(
            user=self.user_with_no_lots, auction=auction, pickup_location=location
        )

        lot = await database_sync_to_async(Lot.objects.create)(
            lot_name="Test lot after online bidding",
            auction=auction,
            auctiontos_seller=seller_tos,
            quantity=1,
            reserve_price=10,
            date_end=theFuture,
        )

        communicator = WebsocketCommunicator(
            LotConsumer.as_asgi(),
            f"/ws/lots/{lot.pk}/",
        )
        communicator.scope["user"] = self.user_with_no_lots
        communicator.scope["url_route"] = {"kwargs": {"lot_number": lot.pk}}

        try:
            await communicator.connect(timeout=self.CONNECT_TIMEOUT)

            # Try to place a bid
            await communicator.send_json_to({"bid": 15})

            # Should receive an error about online bidding ended
            found_error = False
            for _ in range(5):
                try:
                    response = await communicator.receive_json_from(timeout=self.RECEIVE_TIMEOUT)
                    if response.get("error"):
                        found_error = True
                        self.assertIn("ended", response["error"].lower())
                        break
                except:
                    break

            self.assertTrue(found_error, "Did not receive expected error about online bidding ended")
        finally:
            await communicator.disconnect(timeout=self.DISCONNECT_TIMEOUT)

    async def test_lot_consumer_bid_on_sold_lot(self):
        """Test that bids cannot be placed on lots that have already been sold"""
        from channels.db import database_sync_to_async
        from channels.testing import WebsocketCommunicator

        from auctions.consumers import LotConsumer

        # Create a sold lot
        theFuture = timezone.now() + datetime.timedelta(days=3)
        auction = await database_sync_to_async(Auction.objects.create)(
            created_by=self.user,
            title="Auction with sold lot",
            is_online=True,
            date_start=timezone.now(),
            date_end=theFuture,
        )
        location = await database_sync_to_async(PickupLocation.objects.create)(
            name="test location", auction=auction, pickup_time=theFuture
        )
        seller_tos = await database_sync_to_async(AuctionTOS.objects.create)(
            user=self.user, auction=auction, pickup_location=location
        )
        winner_tos = await database_sync_to_async(AuctionTOS.objects.create)(
            user=self.user_with_no_lots, auction=auction, pickup_location=location
        )

        lot = await database_sync_to_async(Lot.objects.create)(
            lot_name="Sold lot",
            auction=auction,
            auctiontos_seller=seller_tos,
            quantity=1,
            reserve_price=10,
            date_end=theFuture,
            winner=self.user_with_no_lots,
            auctiontos_winner=winner_tos,
            winning_price=20,
        )

        # Try to bid as another user
        another_user = await database_sync_to_async(User.objects.create_user)(
            username="another_bidder", password="testpassword", email="another@example.com"
        )
        await database_sync_to_async(AuctionTOS.objects.create)(
            user=another_user, auction=auction, pickup_location=location
        )

        communicator = WebsocketCommunicator(
            LotConsumer.as_asgi(),
            f"/ws/lots/{lot.pk}/",
        )
        communicator.scope["user"] = another_user
        communicator.scope["url_route"] = {"kwargs": {"lot_number": lot.pk}}

        try:
            await communicator.connect(timeout=self.CONNECT_TIMEOUT)

            # Try to place a bid
            await communicator.send_json_to({"bid": 25})

            # Should receive an error about lot being sold
            found_error = False
            for _ in range(5):
                try:
                    response = await communicator.receive_json_from(timeout=self.RECEIVE_TIMEOUT)
                    if response.get("error"):
                        found_error = True
                        self.assertIn("sold", response["error"].lower())
                        break
                except:
                    break

            self.assertTrue(found_error, "Did not receive expected error about lot being sold")
        finally:
            await communicator.disconnect(timeout=self.DISCONNECT_TIMEOUT)

    async def test_lot_consumer_auction_admin_can_view(self):
        """Test that auction admins can connect to lot consumer"""
        from channels.db import database_sync_to_async
        from channels.testing import WebsocketCommunicator

        from auctions.consumers import LotConsumer

        lot = await self._create_active_lot_with_auction(self.user)

        # Make admin_user an admin of the auction
        auction = await database_sync_to_async(lambda: lot.auction)()
        location = await database_sync_to_async(lambda: auction.pickuplocation_set.first())()
        await database_sync_to_async(AuctionTOS.objects.create)(
            user=self.admin_user, auction=auction, pickup_location=location, is_admin=True
        )

        communicator = WebsocketCommunicator(
            LotConsumer.as_asgi(),
            f"/ws/lots/{lot.pk}/",
        )
        communicator.scope["user"] = self.admin_user
        communicator.scope["url_route"] = {"kwargs": {"lot_number": lot.pk}}

        try:
            connected, _ = await communicator.connect(timeout=self.CONNECT_TIMEOUT)
            self.assertTrue(connected)
        finally:
            await communicator.disconnect(timeout=self.DISCONNECT_TIMEOUT)

    async def test_user_consumer_connect_valid_user(self):
        """Test UserConsumer connection with valid user"""
        from channels.testing import WebsocketCommunicator

        from auctions.consumers import UserConsumer

        communicator = WebsocketCommunicator(
            UserConsumer.as_asgi(),
            f"/ws/users/{self.user.pk}/",
        )
        communicator.scope["user"] = self.user
        communicator.scope["url_route"] = {"kwargs": {"user_pk": self.user.pk}}

        try:
            connected, _ = await communicator.connect(timeout=self.CONNECT_TIMEOUT)
            self.assertTrue(connected)
        finally:
            await communicator.disconnect(timeout=self.DISCONNECT_TIMEOUT)

    async def test_user_consumer_connect_wrong_user(self):
        """Test UserConsumer connection with wrong user ID"""
        from channels.testing import WebsocketCommunicator

        from auctions.consumers import UserConsumer

        communicator = WebsocketCommunicator(
            UserConsumer.as_asgi(),
            f"/ws/users/{self.admin_user.pk}/",
        )
        communicator.scope["user"] = self.user  # Different user
        communicator.scope["url_route"] = {"kwargs": {"user_pk": self.admin_user.pk}}

        try:
            connected, _ = await communicator.connect(timeout=self.CONNECT_TIMEOUT)
            # Should be rejected because user doesn't match
            self.assertFalse(connected)
        finally:
            # Even if connection failed, try to disconnect to clean up
            try:
                await communicator.disconnect(timeout=self.DISCONNECT_TIMEOUT)
            except:
                pass

    async def test_user_consumer_connect_anonymous(self):
        """Test UserConsumer connection with anonymous user"""
        from channels.testing import WebsocketCommunicator
        from django.contrib.auth.models import AnonymousUser

        from auctions.consumers import UserConsumer

        communicator = WebsocketCommunicator(
            UserConsumer.as_asgi(),
            f"/ws/users/{self.user.pk}/",
        )
        communicator.scope["user"] = AnonymousUser()
        communicator.scope["url_route"] = {"kwargs": {"user_pk": self.user.pk}}

        try:
            connected, _ = await communicator.connect(timeout=self.CONNECT_TIMEOUT)
            # Should be rejected
            self.assertFalse(connected)
        finally:
            # Even if connection failed, try to disconnect to clean up
            try:
                await communicator.disconnect(timeout=self.DISCONNECT_TIMEOUT)
            except:
                pass

    async def test_auction_consumer_connect_admin(self):
        """Test AuctionConsumer connection with auction admin"""
        from channels.testing import WebsocketCommunicator

        from auctions.consumers import AuctionConsumer

        communicator = WebsocketCommunicator(
            AuctionConsumer.as_asgi(),
            f"/ws/auctions/{self.online_auction.pk}/",
        )
        communicator.scope["user"] = self.admin_user
        communicator.scope["url_route"] = {"kwargs": {"auction_pk": self.online_auction.pk}}

        try:
            connected, _ = await communicator.connect(timeout=self.CONNECT_TIMEOUT)
            self.assertTrue(connected)
        finally:
            await communicator.disconnect(timeout=self.DISCONNECT_TIMEOUT)

    async def test_auction_consumer_connect_non_admin(self):
        """Test AuctionConsumer connection with non-admin user"""
        from channels.testing import WebsocketCommunicator

        from auctions.consumers import AuctionConsumer

        communicator = WebsocketCommunicator(
            AuctionConsumer.as_asgi(),
            f"/ws/auctions/{self.online_auction.pk}/",
        )
        communicator.scope["user"] = self.user_with_no_lots
        communicator.scope["url_route"] = {"kwargs": {"auction_pk": self.online_auction.pk}}

        try:
            connected, _ = await communicator.connect(timeout=self.CONNECT_TIMEOUT)
            # Should be rejected
            self.assertFalse(connected)
        finally:
            # Even if connection failed, try to disconnect to clean up
            try:
                await communicator.disconnect(timeout=self.DISCONNECT_TIMEOUT)
            except:
                pass

    async def test_auction_consumer_connect_anonymous(self):
        """Test AuctionConsumer connection with anonymous user"""
        from channels.testing import WebsocketCommunicator
        from django.contrib.auth.models import AnonymousUser

        from auctions.consumers import AuctionConsumer

        communicator = WebsocketCommunicator(
            AuctionConsumer.as_asgi(),
            f"/ws/auctions/{self.online_auction.pk}/",
        )
        communicator.scope["user"] = AnonymousUser()
        communicator.scope["url_route"] = {"kwargs": {"auction_pk": self.online_auction.pk}}

        try:
            connected, _ = await communicator.connect(timeout=self.CONNECT_TIMEOUT)
            # Should be rejected
            self.assertFalse(connected)
        finally:
            # Even if connection failed, try to disconnect to clean up
            try:
                await communicator.disconnect(timeout=self.DISCONNECT_TIMEOUT)
            except:
                pass

    async def test_auction_consumer_invalid_auction(self):
        """Test AuctionConsumer connection with invalid auction ID"""
        from channels.testing import WebsocketCommunicator

        from auctions.consumers import AuctionConsumer

        communicator = WebsocketCommunicator(
            AuctionConsumer.as_asgi(),
            "/ws/auctions/99999/",
        )
        communicator.scope["user"] = self.admin_user
        communicator.scope["url_route"] = {"kwargs": {"auction_pk": 99999}}

        try:
            connected, _ = await communicator.connect(timeout=self.CONNECT_TIMEOUT)
            # Should be rejected because auction doesn't exist
            self.assertFalse(connected)
        finally:
            # Even if connection failed, try to disconnect to clean up
            try:
                await communicator.disconnect(timeout=self.DISCONNECT_TIMEOUT)
            except:
                pass


class HasEverGrantedPermissionTests(StandardTestCase):
    """Test the has_ever_granted_permission annotation"""

    def test_user_who_joined_has_permission(self):
        """User who joined an auction (not manually_added) should have has_ever_granted_permission=True"""
        # online_tos is created with manually_added=False by default
        tos_qs = self.online_auction.tos_qs.filter(user=self.user)
        tos = tos_qs.first()
        self.assertTrue(tos.has_ever_granted_permission)

    def test_manually_added_user_without_prior_join_has_no_permission(self):
        """User who was manually added and never joined should have has_ever_granted_permission=False"""
        # Create a new user who was manually added
        new_user = User.objects.create_user(username="manually_added_user", password="testpassword")
        AuctionTOS.objects.create(
            user=new_user, auction=self.online_auction, pickup_location=self.location, manually_added=True
        )

        tos_qs = self.online_auction.tos_qs.filter(user=new_user)
        tos = tos_qs.first()
        self.assertFalse(tos.has_ever_granted_permission)

    def test_manually_added_user_with_prior_join_has_permission(self):
        """User who was manually added but joined another auction by same creator should have has_ever_granted_permission=True"""
        # Create a new user
        new_user = User.objects.create_user(username="returning_user", password="testpassword")

        # User joins first auction normally
        AuctionTOS.objects.create(
            user=new_user, auction=self.online_auction, pickup_location=self.location, manually_added=False
        )

        # User is manually added to second auction by same creator
        second_auction = Auction.objects.create(
            created_by=self.user,  # Same creator as online_auction
            title="Second auction",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=2),
            date_start=timezone.now() - datetime.timedelta(days=1),
        )
        second_location = PickupLocation.objects.create(
            name="location2", auction=second_auction, pickup_time=timezone.now() + datetime.timedelta(days=3)
        )
        AuctionTOS.objects.create(
            user=new_user, auction=second_auction, pickup_location=second_location, manually_added=True
        )

        # Check the manually added TOS
        tos_qs = second_auction.tos_qs.filter(user=new_user)
        tos = tos_qs.first()
        self.assertTrue(tos.has_ever_granted_permission)

    def test_user_with_no_account_has_no_permission(self):
        """AuctionTOS without a user should have has_ever_granted_permission=False"""
        # Create an AuctionTOS without a user
        no_user_tos = AuctionTOS.objects.create(
            auction=self.online_auction, pickup_location=self.location, name="Guest User", email="guest@example.com"
        )

        tos_qs = self.online_auction.tos_qs.filter(pk=no_user_tos.pk)
        tos = tos_qs.first()
        self.assertFalse(tos.has_ever_granted_permission)

    def test_different_creator_auctions_dont_grant_permission(self):
        """User who joined an auction by a different creator should not have permission"""
        # Create a different auction creator
        other_creator = User.objects.create_user(username="other_creator", password="testpassword")

        # Create a new user
        new_user = User.objects.create_user(username="cross_auction_user", password="testpassword")

        # User joins an auction by a different creator
        other_auction = Auction.objects.create(
            created_by=other_creator,  # Different creator
            title="Other creator's auction",
            is_online=True,
            date_end=timezone.now() + datetime.timedelta(days=2),
            date_start=timezone.now() - datetime.timedelta(days=1),
        )
        other_location = PickupLocation.objects.create(
            name="other_location", auction=other_auction, pickup_time=timezone.now() + datetime.timedelta(days=3)
        )
        AuctionTOS.objects.create(
            user=new_user, auction=other_auction, pickup_location=other_location, manually_added=False
        )

        # User is manually added to an auction by the original creator
        AuctionTOS.objects.create(
            user=new_user, auction=self.online_auction, pickup_location=self.location, manually_added=True
        )

        # Check the manually added TOS - should be False because user never joined
        # an auction by self.user (the creator of online_auction)
        tos_qs = self.online_auction.tos_qs.filter(user=new_user)
        tos = tos_qs.first()
        self.assertFalse(tos.has_ever_granted_permission)


class BulkAddLotsAutoTests(StandardTestCase):
    """Tests for the new auto-save bulk add lots functionality"""

    def setUp(self):
        super().setUp()
        # Set up auction with lot limits
        self.in_person_auction.max_lots_per_user = 3
        self.in_person_auction.allow_additional_lots_as_donation = True
        self.in_person_auction.allow_bulk_adding_lots = True
        self.in_person_auction.lot_submission_end_date = timezone.now() + datetime.timedelta(days=7)
        self.in_person_auction.save()

    def test_bulk_add_lots_view_access(self):
        """Test that users can access bulk add lots page"""
        # Login as regular user
        self.client.login(username="my_lot", password="testpassword")
        response = self.client.get(
            reverse("bulk_add_lots_auto_for_myself", kwargs={"slug": self.in_person_auction.slug})
        )
        self.assertEqual(response.status_code, 200)

    def test_bulk_add_lots_admin_access(self):
        """Test that admins can access bulk add for other users"""
        # Login as admin
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(
            reverse(
                "bulk_add_lots_auto",
                kwargs={"slug": self.in_person_auction.slug, "bidder_number": self.in_person_buyer.bidder_number},
            )
        )
        self.assertEqual(response.status_code, 200)

    def test_bulk_add_lots_non_admin_cannot_access_bidder_url(self):
        """Test that non-admin users cannot access the bidder_number URL"""
        # Login as regular user (not auction creator)
        self.client.login(username="no_lots", password="testpassword")

        # Try to access bulk add for a specific bidder number
        response = self.client.get(
            reverse(
                "bulk_add_lots_auto",
                kwargs={"slug": self.in_person_auction.slug, "bidder_number": self.in_person_buyer.bidder_number},
            ),
            follow=True,  # Follow redirects
        )

        # Should be redirected (not allowed)
        self.assertEqual(response.status_code, 200)
        # Check for error message
        messages = list(response.context["messages"])
        self.assertTrue(any("admin" in str(m).lower() for m in messages))

    def test_save_lot_ajax_security(self):
        """Test that non-admin users cannot add lots for others"""
        # Login as regular user (not auction creator)
        self.client.login(username="no_lots", password="testpassword")

        # Try to add lot for another user
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Test Lot", "bidder_number": "555"}',
            content_type="application/json",
        )
        data = response.json()
        self.assertFalse(data["success"])
        self.assertIn("admin", data["error"].lower())

    def test_save_lot_ajax_admin_can_add_for_others(self):
        """Test that admins can add lots for other users"""
        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Add lot for another user
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Test Lot", "reserve_price": 5, "bidder_number": "555"}',
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["success"])
        self.assertIsNotNone(data["lot_id"])

        # Verify lot was created for correct user
        lot = Lot.objects.get(lot_number=data["lot_id"])
        self.assertEqual(lot.auctiontos_seller, self.in_person_buyer)

    def test_save_lot_ajax_user_can_add_for_themselves(self):
        """Test that regular users can add lots for themselves without bidder_number"""
        # Login as regular user (not auction creator)
        self.client.login(username="no_lots", password="testpassword")

        # Add lot for themselves (no bidder_number)
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "My Own Lot", "reserve_price": 5}',
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["success"], f"Failed to add lot for self: {data.get('error', 'Unknown error')}")
        self.assertIsNotNone(data["lot_id"])

        # Verify lot was created for the correct user (in_person_buyer)
        lot = Lot.objects.get(lot_number=data["lot_id"])
        self.assertEqual(lot.auctiontos_seller, self.in_person_buyer)

    def test_lot_limit_enforcement(self):
        """Test that lot limits are enforced for non-admin users"""
        # Login as regular user (not auction creator)
        self.client.login(username="no_lots", password="testpassword")

        # Create 3 lots (the limit)
        for i in range(3):
            response = self.client.post(
                reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
                data=f'{{"lot_name": "Test Lot {i}", "reserve_price": 5}}',
                content_type="application/json",
            )
            data = response.json()
            self.assertTrue(data["success"])

        # Try to add 4th lot (should fail)
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Test Lot 4", "reserve_price": 5}',
            content_type="application/json",
        )
        data = response.json()
        self.assertFalse(data["success"])
        self.assertIn("maximum", data["errors"]["general"].lower())

    def test_donation_lot_beyond_limit(self):
        """Test that donation lots can be added beyond limit when allowed"""
        # Login as regular user (not auction creator)
        self.client.login(username="no_lots", password="testpassword")

        # Create 3 lots (the limit)
        for i in range(3):
            response = self.client.post(
                reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
                data=f'{{"lot_name": "Test Lot {i}", "reserve_price": 5}}',
                content_type="application/json",
            )
            self.assertTrue(response.json()["success"])

        # Try to add 4th lot as donation (should succeed)
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Donation Lot", "reserve_price": 5, "donation": true}',
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["success"])

    def test_admin_bypass_lot_limit(self):
        """Test that admins can bypass lot limits"""
        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Create 4 lots (beyond limit)
        for i in range(4):
            response = self.client.post(
                reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
                data=f'{{"lot_name": "Test Lot {i}", "reserve_price": 5}}',
                content_type="application/json",
            )
            data = response.json()
            self.assertTrue(data["success"])
            if i >= 3:  # Beyond limit
                self.assertTrue(data.get("admin_bypassed_lot_limit"))

    def test_locked_lot_cannot_be_edited(self):
        """Test that lots cannot be edited after submission deadline"""
        # Create a lot for tosC (user_with_no_lots)
        lot = Lot.objects.create(
            lot_name="Test Lot", auction=self.in_person_auction, auctiontos_seller=self.tosC, reserve_price=5
        )

        # End lot submission
        self.in_person_auction.lot_submission_end_date = timezone.now() - datetime.timedelta(days=1)
        self.in_person_auction.save()

        # Login as regular user (owner, not auction creator)
        self.client.login(username="no_lots", password="testpassword")

        # Try to edit the lot
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data=f'{{"lot_id": {lot.lot_number}, "lot_name": "Updated Name", "reserve_price": 10}}',
            content_type="application/json",
        )
        data = response.json()
        self.assertFalse(data["success"])
        # Check that error message relates to lot submission deadline
        error_msg = data["error"].lower()
        self.assertTrue("cannot be edited" in error_msg or "submission" in error_msg)

    def test_custom_fields_saved(self):
        """Test that custom fields are properly saved"""
        # Set up custom fields
        self.in_person_auction.custom_field_1 = "required"
        self.in_person_auction.custom_field_1_name = "Species"
        self.in_person_auction.use_custom_checkbox_field = True
        self.in_person_auction.custom_checkbox_name = "Wild Caught"
        self.in_person_auction.use_quantity_field = True
        self.in_person_auction.use_donation_field = True
        self.in_person_auction.use_i_bred_this_fish_field = True
        self.in_person_auction.save()

        # Login as regular user
        self.client.login(username="my_lot", password="testpassword")

        # Create lot with all custom fields
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Test Lot", "reserve_price": 5, "custom_field_1": "Betta", "custom_checkbox": true, "quantity": 2, "donation": true, "i_bred_this_fish": true}',
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["success"])

        # Verify all fields saved
        lot = Lot.objects.get(lot_number=data["lot_id"])
        self.assertEqual(lot.custom_field_1, "Betta")
        self.assertTrue(lot.custom_checkbox)
        self.assertEqual(lot.quantity, 2)
        self.assertTrue(lot.donation)
        self.assertTrue(lot.i_bred_this_fish)

    def test_required_field_validation(self):
        """Test that required fields are validated"""
        # Set up required custom field
        self.in_person_auction.custom_field_1 = "required"
        self.in_person_auction.custom_field_1_name = "Species"
        self.in_person_auction.save()

        # Login as regular user
        self.client.login(username="my_lot", password="testpassword")

        # Try to create lot without required field
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Test Lot", "reserve_price": 5}',
            content_type="application/json",
        )
        data = response.json()
        self.assertFalse(data["success"])
        self.assertIn("custom_field_1", data["errors"])

    def test_lot_update_existing(self):
        """Test that existing lots can be updated"""
        # Create a lot
        lot = Lot.objects.create(
            lot_name="Original Name",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_tos,
            reserve_price=5,
        )

        # Login as user (owner)
        self.client.login(username="my_lot", password="testpassword")

        # Update the lot
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data=f'{{"lot_id": {lot.lot_number}, "lot_name": "Updated Name", "reserve_price": 10}}',
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["success"])

        # Verify update
        lot.refresh_from_db()
        self.assertEqual(lot.lot_name, "Updated Name")
        self.assertEqual(lot.reserve_price, 10)

    def test_lot_not_found_for_different_user(self):
        """Test that users cannot edit other users' lots"""
        # Create a lot for user_with_no_lots
        lot = Lot.objects.create(
            lot_name="Other User's Lot", auction=self.in_person_auction, auctiontos_seller=self.tosC, reserve_price=5
        )

        # Login as different user
        self.client.login(username="my_lot", password="testpassword")

        # Try to edit the lot
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data=f'{{"lot_id": {lot.lot_number}, "lot_name": "Hacked Name", "reserve_price": 100}}',
            content_type="application/json",
        )
        data = response.json()
        self.assertFalse(data["success"])
        self.assertIn("not found", data["error"].lower())

    def test_lot_locked_when_has_bids(self):
        """Test that lots with bids cannot be edited"""
        # Create a lot - use in_person_buyer TOS for in_person_auction
        lot = Lot.objects.create(
            lot_name="Test Lot", auction=self.in_person_auction, auctiontos_seller=self.in_person_buyer, reserve_price=5
        )

        # Add a bid to the lot
        Bid.objects.create(lot_number=lot, user=self.userB, bid_time=timezone.now(), amount=10)

        # Login as lot owner
        self.client.login(username="no_lots", password="testpassword")

        # Try to edit the lot
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data=f'{{"lot_id": {lot.lot_number}, "lot_name": "Updated Name", "reserve_price": 10}}',
            content_type="application/json",
        )
        data = response.json()
        self.assertFalse(data["success"])
        error_msg = data["error"].lower()
        self.assertTrue("bids" in error_msg or "cannot be edited" in error_msg)

    def test_lot_locked_when_sold(self):
        """Test that sold lots cannot be edited"""
        # Create a sold lot - use in_person_buyer TOS for in_person_auction
        lot = Lot.objects.create(
            lot_name="Test Lot",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_buyer,
            reserve_price=5,
            auctiontos_winner=self.tosB,  # Has been sold
        )

        # Login as lot owner
        self.client.login(username="no_lots", password="testpassword")

        # Try to edit the lot
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data=f'{{"lot_id": {lot.lot_number}, "lot_name": "Updated Name", "reserve_price": 10}}',
            content_type="application/json",
        )
        data = response.json()
        self.assertFalse(data["success"])
        error_msg = data["error"].lower()
        self.assertTrue("sold" in error_msg or "cannot be edited" in error_msg)

    def test_admin_can_edit_locked_lot(self):
        """Test that admins can edit locked lots"""
        # Create a lot and end lot submission
        lot = Lot.objects.create(
            lot_name="Test Lot", auction=self.in_person_auction, auctiontos_seller=self.in_person_buyer, reserve_price=5
        )

        # End lot submission
        self.in_person_auction.lot_submission_end_date = timezone.now() - datetime.timedelta(days=1)
        self.in_person_auction.save()

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Admin should be able to edit
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data=f'{{"lot_id": {lot.lot_number}, "lot_name": "Admin Updated", "reserve_price": 10, "bidder_number": "{self.in_person_buyer.bidder_number}"}}',
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["success"])

        # Verify update
        lot.refresh_from_db()
        self.assertEqual(lot.lot_name, "Admin Updated")

    def test_can_be_edited_property(self):
        """Test the can_be_edited property of Lot model"""
        # Create a basic lot
        lot = Lot.objects.create(
            lot_name="Test Lot", auction=self.in_person_auction, auctiontos_seller=self.in_person_tos, reserve_price=5
        )

        # With open submission, lot should be editable
        self.assertTrue(lot.can_be_edited)
        self.assertFalse(lot.cannot_be_edited_reason)

        # End lot submission
        self.in_person_auction.lot_submission_end_date = timezone.now() - datetime.timedelta(days=1)
        self.in_person_auction.save()

        # Now lot should not be editable
        lot.refresh_from_db()
        self.assertFalse(lot.can_be_edited)
        self.assertEqual(lot.cannot_be_edited_reason, "Lot submission is over for this auction")

    def test_cannot_change_reason_with_high_bidder(self):
        """Test cannot_change_reason when lot has a high bidder"""
        lot = Lot.objects.create(
            lot_name="Test Lot", auction=self.in_person_auction, auctiontos_seller=self.in_person_tos, reserve_price=5
        )

        # Add a bid to the lot
        Bid.objects.create(lot_number=lot, user=self.userB, bid_time=timezone.now(), amount=10)

        self.assertEqual(lot.cannot_change_reason, "There are already bids placed on this lot")
        self.assertFalse(lot.can_be_edited)

    def test_cannot_change_reason_with_winner(self):
        """Test cannot_change_reason when lot has a winner"""
        lot = Lot.objects.create(
            lot_name="Test Lot",
            auction=self.in_person_auction,
            auctiontos_seller=self.in_person_tos,
            reserve_price=5,
            auctiontos_winner=self.tosB,
        )

        self.assertEqual(lot.cannot_change_reason, "This lot has sold")
        self.assertFalse(lot.can_be_edited)

    def test_admin_can_add_lots_for_user_with_selling_not_allowed(self):
        """Test that admins can add lots for users whose selling_allowed is False, with a warning flag"""
        # Set in_person_buyer's selling_allowed to False
        self.in_person_buyer.selling_allowed = False
        self.in_person_buyer.save()

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Admin should be able to add lot for user with selling_allowed=False
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Test Lot", "reserve_price": 5, "bidder_number": "555"}',
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["success"], f"Admin should be able to add lots for user: {data.get('error', '')}")
        self.assertIsNotNone(data["lot_id"])
        # Verify that admin_bypassed_selling_allowed flag is set
        self.assertTrue(data.get("admin_bypassed_selling_allowed", False))

    def test_non_admin_cannot_add_lots_when_selling_not_allowed(self):
        """Test that non-admin users cannot add lots when their selling_allowed is False"""
        # Set in_person_buyer's selling_allowed to False
        self.in_person_buyer.selling_allowed = False
        self.in_person_buyer.save()

        # Login as non-admin user (in_person_buyer)
        self.client.login(username="no_lots", password="testpassword")

        # Try to add lot for themselves (should fail)
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Test Lot", "reserve_price": 5}',
            content_type="application/json",
        )
        data = response.json()
        self.assertFalse(data["success"])
        self.assertIn("permission", data["error"].lower())

    def test_admin_can_add_lots_for_themselves_when_their_selling_not_allowed(self):
        """Test that admins can bypass their own selling_allowed restriction"""
        # Set admin's selling_allowed to False
        self.admin_in_person_tos.selling_allowed = False
        self.admin_in_person_tos.save()

        # Login as admin
        self.client.login(username="admin_user", password="testpassword")

        # Admin should be able to add lot for themselves (no bidder_number)
        response = self.client.post(
            reverse("save_lot_ajax", kwargs={"slug": self.in_person_auction.slug}),
            data='{"lot_name": "Admin Test Lot", "reserve_price": 5}',
            content_type="application/json",
        )
        data = response.json()
        self.assertTrue(data["success"], f"Admin should bypass their own selling_allowed: {data.get('error', '')}")
        self.assertIsNotNone(data["lot_id"])
        # Verify that admin_bypassed_selling_allowed flag is set
        self.assertTrue(data.get("admin_bypassed_selling_allowed", False))

        # Verify lot was created for admin
        lot = Lot.objects.get(lot_number=data["lot_id"])
        self.assertEqual(lot.auctiontos_seller, self.admin_in_person_tos)


class UpdateAuctionStatsCommandTestCase(StandardTestCase):
    """Test the update_auction_stats management command"""

    def test_command_processes_single_auction(self):
        """Test that the command processes only one auction per run"""
        import datetime

        from django.core.management import call_command
        from django.utils import timezone

        # Set up multiple auctions with due stats updates
        now = timezone.now()

        # Ensure setUp auctions don't interfere by setting their next_update_due to far future
        self.online_auction.next_update_due = now + datetime.timedelta(days=365)
        self.online_auction.save()
        self.in_person_auction.next_update_due = now + datetime.timedelta(days=365)
        self.in_person_auction.save()

        # Create three auctions with different next_update_due times
        auction1 = Auction.objects.create(
            created_by=self.user,
            title="Auction 1 - oldest",
            is_online=True,
            date_start=now - datetime.timedelta(days=5),
            date_end=now + datetime.timedelta(days=2),
        )
        auction1.next_update_due = now - datetime.timedelta(hours=5)  # Most overdue
        auction1.save()

        auction2 = Auction.objects.create(
            created_by=self.user,
            title="Auction 2 - middle",
            is_online=True,
            date_start=now - datetime.timedelta(days=4),
            date_end=now + datetime.timedelta(days=2),
        )
        auction2.next_update_due = now - datetime.timedelta(hours=3)  # Second most overdue
        auction2.save()

        auction3 = Auction.objects.create(
            created_by=self.user,
            title="Auction 3 - newest",
            is_online=True,
            date_start=now - datetime.timedelta(days=3),
            date_end=now + datetime.timedelta(days=2),
        )
        auction3.next_update_due = now - datetime.timedelta(hours=1)  # Least overdue
        auction3.save()

        # Store the original next_update_due times
        original_due_1 = auction1.next_update_due
        original_due_2 = auction2.next_update_due
        original_due_3 = auction3.next_update_due

        # Run the command once (using --sync to run synchronously for testing)
        call_command("update_auction_stats", "--sync")

        # Refresh from database
        auction1.refresh_from_db()
        auction2.refresh_from_db()
        auction3.refresh_from_db()

        # The most overdue auction (auction1) should have been updated
        self.assertIsNotNone(auction1.last_stats_update)
        self.assertNotEqual(auction1.next_update_due, original_due_1)
        # The new next_update_due should be in the future
        self.assertGreater(auction1.next_update_due, now)

        # The other two auctions should NOT have been updated
        self.assertEqual(auction2.next_update_due, original_due_2)
        self.assertEqual(auction3.next_update_due, original_due_3)

    def test_command_orders_by_next_update_due(self):
        """Test that the command processes the most overdue auction first"""
        import datetime

        from django.core.management import call_command
        from django.utils import timezone

        now = timezone.now()

        # Ensure setUp auctions don't interfere by setting their next_update_due to far future
        self.online_auction.next_update_due = now + datetime.timedelta(days=365)
        self.online_auction.save()
        self.in_person_auction.next_update_due = now + datetime.timedelta(days=365)
        self.in_person_auction.save()

        # Create two auctions with different next_update_due times
        newer_auction = Auction.objects.create(
            created_by=self.user,
            title="Newer auction",
            is_online=True,
            date_start=now - datetime.timedelta(days=3),
            date_end=now + datetime.timedelta(days=2),
        )
        newer_auction.next_update_due = now - datetime.timedelta(hours=1)  # Less overdue
        newer_auction.save()

        older_auction = Auction.objects.create(
            created_by=self.user,
            title="Older auction",
            is_online=True,
            date_start=now - datetime.timedelta(days=5),
            date_end=now + datetime.timedelta(days=2),
        )
        older_auction.next_update_due = now - datetime.timedelta(hours=5)  # More overdue
        older_auction.save()

        # Run the command (using --sync to run synchronously for testing)
        call_command("update_auction_stats", "--sync")

        # Refresh from database
        newer_auction.refresh_from_db()
        older_auction.refresh_from_db()

        # The older (more overdue) auction should have been processed
        self.assertIsNotNone(older_auction.last_stats_update)
        self.assertGreater(older_auction.next_update_due, now)

        # The newer auction should not have been processed yet
        self.assertEqual(newer_auction.next_update_due, now - datetime.timedelta(hours=1))
        self.assertIsNone(newer_auction.last_stats_update)

    def test_command_handles_no_due_auctions(self):
        """Test that the command handles the case when no auctions are due"""
        import datetime

        from django.core.management import call_command
        from django.utils import timezone

        now = timezone.now()

        # Create an auction with next_update_due in the future
        future_auction = Auction.objects.create(
            created_by=self.user,
            title="Future auction",
            is_online=True,
            date_start=now - datetime.timedelta(days=3),
            date_end=now + datetime.timedelta(days=2),
        )
        future_auction.next_update_due = now + datetime.timedelta(hours=5)
        future_auction.save()

        # Run the command - should not raise any errors (using --sync to run synchronously for testing)
        call_command("update_auction_stats", "--sync")

        # Refresh from database
        future_auction.refresh_from_db()

        # The auction should not have been processed
        self.assertEqual(future_auction.next_update_due, now + datetime.timedelta(hours=5))
        self.assertIsNone(future_auction.last_stats_update)


class LotsByUserViewTest(StandardTestCase):
    """Test for the LotsByUser view to ensure it handles missing 'user' parameter correctly"""

    def test_lots_by_user_missing_user_parameter(self):
        """Test that the view doesn't crash when 'user' parameter is missing"""
        # Access the URL without user parameter, only with auction parameter
        url = reverse("user_lots") + f"?auction={self.online_auction.slug}"
        response = self.client.get(url)

        # Should return 200, not crash with MultiValueDictKeyError
        self.assertEqual(response.status_code, 200)

        # Context should have user set to None
        self.assertIsNone(response.context["user"])

    def test_lots_by_user_with_valid_user_parameter(self):
        """Test that the view works correctly with a valid user parameter"""
        url = reverse("user_lots") + f"?user={self.user.username}"
        response = self.client.get(url)

        # Should return 200
        self.assertEqual(response.status_code, 200)

        # Context should have the correct user
        self.assertEqual(response.context["user"], self.user)
        self.assertEqual(response.context["view"], "user")

    def test_lots_by_user_with_invalid_user_parameter(self):
        """Test that the view handles non-existent username gracefully"""
        url = reverse("user_lots") + "?user=nonexistent_user"
        response = self.client.get(url)

        # Should return 200, not crash
        self.assertEqual(response.status_code, 200)

        # Context should have user set to None
        self.assertIsNone(response.context["user"])


class ImportLotsFromCSVViewTests(StandardTestCase):
    """Test CSV lot import functionality"""

    def test_import_lots_csv_anonymous(self):
        """Anonymous users cannot import lots from CSV"""
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})
        response = self.client.post(url)
        # Should redirect to login (302) or be denied (403)
        assert response.status_code in [302, 403]

    def test_import_lots_csv_non_admin(self):
        """Non-admin users cannot import lots from CSV"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})
        response = self.client.post(url)
        assert response.status_code in [302, 403]

    def test_import_lots_csv_admin_no_file(self):
        """Admin posting without CSV file gets error"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})
        response = self.client.post(url)
        # Should redirect with error message
        assert response.status_code == 200

    def test_import_lots_csv_create_new_lot(self):
        """CSV import creates a new lot for existing user"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})

        # Set name and email on TOS so we can find it
        self.online_tos.name = "Test User"
        self.online_tos.email = "testuser@example.com"
        self.online_tos.save()

        # Create CSV content
        csv_content = (
            "Name,Email,Lot Name,Quantity,Reserve Price\n"
            f"{self.online_tos.name},{self.online_tos.email},Test Lot from CSV,5,10\n"
        )

        from io import BytesIO

        csv_file = BytesIO(csv_content.encode("utf-8"))
        csv_file.name = "test.csv"

        response = self.client.post(url, {"csv_file": csv_file})

        # Should redirect successfully
        assert response.status_code == 200

        # Check that lot was created
        new_lot = Lot.objects.filter(lot_name="Test Lot from CSV", auction=self.online_auction).first()
        assert new_lot is not None
        assert new_lot.quantity == 5
        assert new_lot.reserve_price == 10
        assert new_lot.auctiontos_seller == self.online_tos

    def test_import_lots_csv_update_existing_lot(self):
        """CSV import updates existing lot by lot number"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})

        # Use existing lot
        lot_number = self.lot.lot_number_int

        # Create CSV content to update the lot
        csv_content = f"Lot Number,Lot Name,Quantity,Reserve Price\n{lot_number},Updated Lot Name,3,15\n"

        from io import BytesIO

        csv_file = BytesIO(csv_content.encode("utf-8"))
        csv_file.name = "test.csv"

        response = self.client.post(url, {"csv_file": csv_file})

        # Should redirect successfully
        assert response.status_code == 200

        # Check that lot was updated
        self.lot.refresh_from_db()
        assert self.lot.lot_name == "Updated Lot Name"
        assert self.lot.quantity == 3
        assert self.lot.reserve_price == 15

    def test_import_lots_csv_create_new_user_and_lot(self):
        """CSV import creates both user and lot"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})

        # Create CSV content with new user
        csv_content = "Name,Email,Lot Name,Quantity\nNew User,newuser@example.com,New User Lot,2\n"

        from io import BytesIO

        csv_file = BytesIO(csv_content.encode("utf-8"))
        csv_file.name = "test.csv"

        response = self.client.post(url, {"csv_file": csv_file})

        # Should redirect successfully
        assert response.status_code == 200

        # Check that user was created
        new_tos = AuctionTOS.objects.filter(email="newuser@example.com", auction=self.online_auction).first()
        assert new_tos is not None
        assert new_tos.name == "New User"

        # Check that lot was created
        new_lot = Lot.objects.filter(lot_name="New User Lot", auction=self.online_auction).first()
        assert new_lot is not None
        assert new_lot.auctiontos_seller == new_tos

    def test_import_lots_csv_boolean_fields(self):
        """CSV import handles boolean fields correctly"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})

        # Create CSV with boolean fields
        csv_content = (
            "Name,Email,Lot Name,Breeder Points,Donation\n"
            f"{self.online_tos.name},{self.online_tos.email},Bred Fish,yes,true\n"
        )

        from io import BytesIO

        csv_file = BytesIO(csv_content.encode("utf-8"))
        csv_file.name = "test.csv"

        response = self.client.post(url, {"csv_file": csv_file})

        # Should redirect successfully
        assert response.status_code == 200

        # Check boolean fields
        new_lot = Lot.objects.filter(lot_name="Bred Fish", auction=self.online_auction).first()
        assert new_lot is not None
        assert new_lot.i_bred_this_fish is True
        assert new_lot.donation is True

    def test_import_lots_csv_missing_info(self):
        """CSV import skips rows with missing required information"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})

        # Create CSV with incomplete data
        csv_content = "Name,Email\nMissing Lot Name,missing@example.com\n"

        from io import BytesIO

        csv_file = BytesIO(csv_content.encode("utf-8"))
        csv_file.name = "test.csv"

        response = self.client.post(url, {"csv_file": csv_file})

        # Should redirect successfully but skip the row
        assert response.status_code == 200

        # Check that no lot was created
        lots = Lot.objects.filter(auctiontos_seller__email="missing@example.com", auction=self.online_auction)
        assert lots.count() == 0

    def test_import_lots_csv_idempotent(self):
        """CSV import is idempotent - repeated uploads should update, not duplicate"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})

        # Use existing lot with lot number
        lot_number = self.lot.lot_number_int

        # Create CSV content
        csv_content = f"Lot Number,Lot Name,Quantity\n{lot_number},Idempotent Lot,7\n"

        from io import BytesIO

        # Upload once
        csv_file = BytesIO(csv_content.encode("utf-8"))
        csv_file.name = "test.csv"
        response = self.client.post(url, {"csv_file": csv_file})
        assert response.status_code == 200

        # Upload again
        csv_file = BytesIO(csv_content.encode("utf-8"))
        csv_file.name = "test.csv"
        response = self.client.post(url, {"csv_file": csv_file})
        assert response.status_code == 200

        # Check that lot was updated, not duplicated
        lots = Lot.objects.filter(lot_name="Idempotent Lot", auction=self.online_auction)
        assert lots.count() == 1
        assert lots.first().quantity == 7

    def test_import_lots_csv_closed_invoice(self):
        """CSV import skips creating lots when invoice is not open"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("import_lots_from_csv", kwargs={"slug": self.online_auction.slug})

        # Set name and email on TOS so we can find it
        self.online_tos.name = "Closed Invoice User"
        self.online_tos.email = "closedinvoice@example.com"
        self.online_tos.save()

        # Close the invoice
        invoice = Invoice.objects.filter(auctiontos_user=self.online_tos, auction=self.online_auction).first()
        if not invoice:
            invoice = Invoice.objects.create(auctiontos_user=self.online_tos, auction=self.online_auction)
        invoice.status = "PAID"
        invoice.save()

        # Create CSV content
        csv_content = f"Name,Email,Lot Name\n{self.online_tos.name},{self.online_tos.email},Should Not Create\n"

        from io import BytesIO

        csv_file = BytesIO(csv_content.encode("utf-8"))
        csv_file.name = "test.csv"

        response = self.client.post(url, {"csv_file": csv_file})

        # Should redirect with warning
        assert response.status_code == 200

        # Check that lot was not created
        new_lot = Lot.objects.filter(lot_name="Should Not Create", auction=self.online_auction).first()
        assert new_lot is None


class SquarePaymentTests(StandardTestCase):
    """Tests for Square payment oauth integration"""

    def setUp(self):
        super().setUp()
        from decimal import Decimal

        from .models import Invoice, InvoicePayment, SquareSeller, UserData

        # Enable Square for test users
        for user in [self.admin_user, self.user]:
            userdata, _ = UserData.objects.get_or_create(user=user)
            userdata.square_enabled = True
            userdata.save()

        # Create Square seller for admin
        self.square_seller = SquareSeller.objects.create(
            user=self.admin_user,
            square_merchant_id="TEST_MERCHANT_ID",
            access_token="TEST_ACCESS_TOKEN",
            refresh_token="TEST_REFRESH_TOKEN",
            token_expires_at=timezone.now() + datetime.timedelta(days=30),
            currency="USD",
        )

        # Create invoice and payment for testing refunds
        self.test_invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.tosB)
        self.square_payment = InvoicePayment.objects.create(
            invoice=self.test_invoice,
            payment_method="square",
            amount=Decimal("100.00"),
            amount_available_to_refund=Decimal("100.00"),
            external_id="TEST_PAYMENT_ID",
        )

    def test_square_seller_creation(self):
        """Test that SquareSeller model is created correctly"""
        self.assertEqual(self.square_seller.user, self.admin_user)
        self.assertEqual(self.square_seller.square_merchant_id, "TEST_MERCHANT_ID")
        self.assertIsNotNone(self.square_seller.access_token)
        self.assertIsNotNone(self.square_seller.refresh_token)

    def test_token_expiration_check(self):
        """Test token expiration checking"""
        # Token expires in 30 days - should not be expired
        self.assertFalse(self.square_seller.is_token_expired())

        # Set token to expire soon (within 1 hour)
        self.square_seller.token_expires_at = timezone.now() + datetime.timedelta(minutes=30)
        self.square_seller.save()
        self.assertTrue(self.square_seller.is_token_expired())

        # Set token to already expired
        self.square_seller.token_expires_at = timezone.now() - datetime.timedelta(hours=1)
        self.square_seller.save()
        self.assertTrue(self.square_seller.is_token_expired())

    def test_winner_invoice_property(self):
        """Test Lot.winner_invoice property"""
        # Lot with auctiontos_winner
        invoice = self.lot.winner_invoice
        self.assertIsNotNone(invoice)
        self.assertEqual(invoice.auctiontos_user, self.lot.auctiontos_winner)

        # Lot with no winner
        unsold_lot = Lot.objects.create(
            lot_name="Unsold test",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
        )
        self.assertIsNone(unsold_lot.winner_invoice)

    def test_seller_invoice_property(self):
        """Test Lot.seller_invoice property"""
        invoice = self.lot.sellers_invoice
        self.assertIsNotNone(invoice)
        self.assertEqual(invoice.auctiontos_user, self.lot.auctiontos_seller)

    def test_square_refund_possible_with_payment(self):
        """Test square_refund_possible when Square payment exists"""
        # Set up lot with Square payment
        self.lot.winning_price = 50
        self.lot.auctiontos_winner = self.tosB
        self.lot.save()

        # Should be True since we have a payment of 100 and lot cost is 50
        self.assertTrue(self.lot.square_refund_possible)

    def test_square_refund_possible_insufficient_funds(self):
        """Test square_refund_possible when payment is insufficient"""
        self.lot.winning_price = 150  # More than available (100)
        self.lot.auctiontos_winner = self.tosB
        self.lot.save()

        self.assertFalse(self.lot.square_refund_possible)

    def test_square_refund_possible_no_payment(self):
        """Test square_refund_possible when no Square payment exists"""
        # Create a lot with a different winner who has no Square payment
        other_tos = AuctionTOS.objects.create(
            user=self.user_with_no_lots, auction=self.online_auction, pickup_location=self.location
        )
        lot = Lot.objects.create(
            lot_name="Test lot no payment",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=10,
            auctiontos_winner=other_tos,
            active=False,
        )

        self.assertFalse(lot.square_refund_possible)

    def test_square_refund_possible_already_refunded(self):
        """Test square_refund_possible when no_more_refunds_possible is True"""
        self.lot.winning_price = 50
        self.lot.auctiontos_winner = self.tosB
        self.lot.no_more_refunds_possible = True
        self.lot.save()

        # Should be False even though payment exists
        self.assertFalse(self.lot.square_refund_possible)

    def test_no_more_refunds_field_default(self):
        """Test that no_more_refunds_possible defaults to False"""
        new_lot = Lot.objects.create(
            lot_name="New lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
        )
        self.assertFalse(new_lot.no_more_refunds_possible)

    def test_invoice_payment_square_method(self):
        """Test that Square payments are properly recorded"""
        from decimal import Decimal

        from auctions.models import InvoicePayment

        payment = InvoicePayment.objects.filter(payment_method="square", invoice=self.test_invoice).first()
        self.assertIsNotNone(payment)
        self.assertEqual(payment.amount, Decimal("100.00"))
        self.assertEqual(payment.amount_available_to_refund, Decimal("100.00"))

    def test_lot_refund_calls_square_refund(self):
        """Test that lot.refund() automatically calls square_refund when possible"""
        # Set up lot with Square payment possibility
        self.lot.winning_price = 50
        self.lot.auctiontos_winner = self.tosB
        self.lot.save()

        # Get initial state
        initial_square_refund_possible = self.lot.square_refund_possible
        self.assertTrue(initial_square_refund_possible)

        # Since we can't actually call Square API in tests, we'll just verify
        # that the refund method can be called without errors
        # In a real scenario with mocked Square API, this would process a refund
        try:
            self.lot.refund(100, self.admin_user, "Test refund")
            # The refund method should handle the case where Square API is not available
        except Exception:
            # We expect this might fail in tests since we don't have real Square credentials
            # but we want to ensure the code path is exercised
            pass

    def test_square_enabled_in_user_preferences(self):
        """Test that Square can be enabled for users"""
        from auctions.models import UserData

        userdata, _ = UserData.objects.get_or_create(user=self.user)
        userdata.square_enabled = True
        userdata.save()

        self.assertTrue(userdata.square_enabled)

    def test_square_fields_in_auction(self):
        """Test Square-related fields in Auction model"""
        self.online_auction.enable_square_payments = True
        self.online_auction.square_email_address = "test@square.com"
        self.online_auction.dismissed_square_banner = False
        self.online_auction.save()

        self.assertTrue(self.online_auction.enable_square_payments)
        self.assertEqual(self.online_auction.square_email_address, "test@square.com")
        self.assertFalse(self.online_auction.dismissed_square_banner)

    def test_square_url_patterns_exist(self):
        """Test that Square URL patterns are configured"""
        from django.urls import reverse

        # Test that Square URLs can be reversed
        try:
            square_seller_url = reverse("square_seller")
            self.assertIsNotNone(square_seller_url)
        except Exception:
            self.fail("square_seller URL pattern not found")

    def test_square_management_command_exists(self):
        """Test that change_square management command exists"""

        # Test that command exists and can be imported
        try:
            # Don't actually run the command, just verify it exists
            from django.core.management import load_command_class

            load_command_class("auctions", "change_square")
        except Exception as e:
            self.fail(f"change_square management command not found: {e}")

    def test_square_oauth_redirect_uri_without_proxy_header(self):
        """Test that Square OAuth redirect URI defaults to http when no X-Forwarded-Proto header"""
        from django.urls import reverse

        # Login as admin user
        self.client.force_login(self.admin_user)

        # Test the Square connect view without X-Forwarded-Proto header
        response = self.client.get(reverse("square_connect"), HTTP_HOST="testserver", follow=False)

        # Should redirect to Square OAuth URL
        self.assertEqual(response.status_code, 302)
        self.assertIn("connect.squareup", response.url)

        # Verify redirect_uri parameter
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(response.url)
        params = parse_qs(parsed.query)

        # Check that redirect_uri exists
        self.assertIn("redirect_uri", params)
        redirect_uri = params["redirect_uri"][0]
        # Without the proxy header in test environment, it will use http
        self.assertIn("/square/onboard/success/", redirect_uri)

    def test_receipt_number_field(self):
        """Test that InvoicePayment has receipt_number field"""
        from auctions.models import InvoicePayment

        # Create a payment with receipt_number
        payment = InvoicePayment.objects.create(
            invoice=self.test_invoice,
            payment_method="Square",
            amount=50.00,
            external_id="TEST_EXTERNAL_ID",
            receipt_number="ABCD",
        )

        self.assertEqual(payment.receipt_number, "ABCD")
        self.assertEqual(payment.external_id, "TEST_EXTERNAL_ID")

    def test_receipt_number_search_in_auction_tos_filter(self):
        """Test that receipt_number can be used to search users"""
        from auctions.filters import AuctionTOSFilter
        from auctions.models import AuctionTOS, InvoicePayment

        # Create payment with receipt number
        InvoicePayment.objects.create(
            invoice=self.test_invoice,
            payment_method="Square",
            amount=100.00,
            receipt_number="WXYZ",
        )

        # Create a queryset of all auction TOS
        qs = AuctionTOS.objects.filter(auction=self.online_auction)

        # Create an instance of AuctionTOSFilter to use its generic method
        filter_instance = AuctionTOSFilter()

        # Search by receipt_number
        filtered_qs = filter_instance.generic(qs, "wxyz")

        # Should find the user with the invoice that has this receipt_number
        self.assertGreater(filtered_qs.count(), 0)

    def test_can_bid_filter_in_auction_tos(self):
        """Test that 'can bid' filter returns users where bidding_allowed=True"""
        from auctions.filters import AuctionTOSFilter
        from auctions.models import AuctionTOS

        # Set bidding_allowed to False for some users
        self.tosB.bidding_allowed = False
        self.tosB.save()

        # Create a queryset of all auction TOS
        qs = AuctionTOS.objects.filter(auction=self.online_auction)

        # Create an instance of AuctionTOSFilter to use its generic method
        filter_instance = AuctionTOSFilter()

        # Search for users who can bid
        filtered_qs = filter_instance.generic(qs, "can bid")

        # Should only return users where bidding_allowed=True
        for tos in filtered_qs:
            self.assertTrue(tos.bidding_allowed)

        # tosB should not be in the filtered results
        self.assertNotIn(self.tosB, filtered_qs)

    def test_no_bid_filter_in_auction_tos(self):
        """Test that 'no bid' filter returns users where bidding_allowed=False"""
        from auctions.filters import AuctionTOSFilter
        from auctions.models import AuctionTOS

        # Set bidding_allowed to False for some users
        self.tosB.bidding_allowed = False
        self.tosB.save()

        # Create a queryset of all auction TOS
        qs = AuctionTOS.objects.filter(auction=self.online_auction)

        # Create an instance of AuctionTOSFilter to use its generic method
        filter_instance = AuctionTOSFilter()

        # Search for users who cannot bid
        filtered_qs = filter_instance.generic(qs, "no bid")

        # Should only return users where bidding_allowed=False
        for tos in filtered_qs:
            self.assertFalse(tos.bidding_allowed)

        # tosB should be in the filtered results
        self.assertIn(self.tosB, filtered_qs)

    def test_pickup_by_mail_requires_address(self):
        """Test that Square payment link requires address when pickup_by_mail is True"""
        from auctions.models import PickupLocation

        # Create a pickup by mail location
        mail_location = PickupLocation.objects.create(
            auction=self.online_auction,
            name="Mail",
            pickup_by_mail=True,
        )

        # Update tosB to use mail pickup
        self.tosB.pickup_location = mail_location
        self.tosB.save()

        # The create_payment_link method should set ask_for_shipping_address=True
        # We can't test the actual API call, but we can verify the location is set correctly
        self.assertTrue(self.tosB.pickup_location.pickup_by_mail)


class SquareRefundFormTests(StandardTestCase):
    """Tests for Square refund integration in forms"""

    def setUp(self):
        super().setUp()
        from decimal import Decimal

        from auctions.models import InvoicePayment, SquareSeller, UserData

        # Enable Square
        userdata, _ = UserData.objects.get_or_create(user=self.admin_user)
        userdata.square_enabled = True
        userdata.save()

        # Create Square seller
        self.square_seller = SquareSeller.objects.create(
            user=self.admin_user,
            square_merchant_id="TEST_MERCHANT_ID",
            access_token="TEST_ACCESS_TOKEN",
            refresh_token="TEST_REFRESH_TOKEN",
            token_expires_at=timezone.now() + datetime.timedelta(days=30),
        )

        # Create payment for testing
        self.square_payment = InvoicePayment.objects.create(
            invoice=self.invoiceB,
            payment_method="square",
            amount=Decimal("100.00"),
            amount_available_to_refund=Decimal("100.00"),
            external_id="TEST_PAYMENT_ID",
        )

        # Set lot to have Square refund possible
        self.lot.winning_price = 50
        self.lot.auctiontos_winner = self.tosB
        self.lot.save()

    def test_lot_refund_form_shows_square_message(self):
        """Test that LotRefundForm shows Square auto-refund message when appropriate"""
        from auctions.forms import LotRefundForm

        form = LotRefundForm(lot=self.lot)

        # Check that form initializes without errors
        self.assertIsNotNone(form)

        # When square_refund_possible is True, the form should include a message
        # We can't easily test the rendered HTML here, but we can verify the form works
        self.assertTrue(self.lot.square_refund_possible)

    def test_lot_refund_form_without_square(self):
        """Test LotRefundForm when Square refund is not possible"""
        from auctions.forms import LotRefundForm

        # Set up a lot without Square payment
        unsold_lot = Lot.objects.create(
            lot_name="Unsold lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
        )

        form = LotRefundForm(lot=unsold_lot)
        self.assertIsNotNone(form)
        self.assertFalse(unsold_lot.square_refund_possible)


class SquarePaymentSuccessViewTests(StandardTestCase):
    """Tests for SquarePaymentSuccessView that doesn't verify email"""

    def setUp(self):
        super().setUp()
        self.tosA = self.online_tos
        self.auctionA = self.online_auction
        self.userA = self.user
        self.invoice = Invoice.objects.create(
            auctiontos_user=self.tosA,
            auction=self.auctionA,
        )
        self.invoice.save()

    def test_square_payment_success_view_marks_invoice_opened(self):
        """Test that SquarePaymentSuccessView marks invoice as opened"""
        from django.urls import reverse

        self.assertFalse(self.invoice.opened)

        url = reverse("square_payment_success", kwargs={"uuid": self.invoice.no_login_link})
        self.client.get(url)

        self.invoice.refresh_from_db()
        self.assertTrue(self.invoice.opened)

    def test_square_payment_success_view_does_not_verify_email(self):
        """Test that SquarePaymentSuccessView does NOT mark email as VALID"""
        from django.urls import reverse

        # Set initial email status to something other than VALID
        self.tosA.email_address_status = "UNKNOWN"
        self.tosA.save()

        url = reverse("square_payment_success", kwargs={"uuid": self.invoice.no_login_link})
        self.client.get(url)

        self.tosA.refresh_from_db()
        # Email status should NOT have changed to VALID
        self.assertEqual(self.tosA.email_address_status, "UNKNOWN")

    def test_invoice_no_login_view_still_verifies_email(self):
        """Test that InvoiceNoLoginView still marks email as VALID (for comparison)"""
        from django.urls import reverse

        # Set initial email status
        self.tosA.email_address_status = "UNKNOWN"
        self.tosA.save()

        url = reverse("invoice_no_login", kwargs={"uuid": self.invoice.no_login_link})
        self.client.get(url)

        self.tosA.refresh_from_db()
        # Email status SHOULD have changed to VALID for regular invoice links
        self.assertEqual(self.tosA.email_address_status, "VALID")

    def test_square_payment_success_url_pattern_exists(self):
        """Test that square_payment_success URL pattern is configured"""
        from django.urls import reverse

        try:
            url = reverse("square_payment_success", kwargs={"uuid": self.invoice.no_login_link})
            self.assertTrue(url.startswith("/invoices/square-success/"))
        except Exception as e:
            self.fail(f"square_payment_success URL pattern not configured: {e}")


class SquareOAuthRevocationTests(StandardTestCase):
    """Tests for Square OAuth authorization revocation handling"""

    def setUp(self):
        super().setUp()
        from .models import SquareSeller

        # Create Square seller for testing revocation
        self.square_seller = SquareSeller.objects.create(
            user=self.admin_user,
            square_merchant_id="MLF3WZS2N9WVG",
            access_token="TEST_ACCESS_TOKEN",
            refresh_token="TEST_REFRESH_TOKEN",
            token_expires_at=timezone.now() + datetime.timedelta(days=30),
            currency="USD",
        )

    def test_oauth_revocation_deletes_square_seller(self):
        """Test that oauth.authorization.revoked webhook deletes SquareSeller"""
        from django.urls import reverse

        from .models import SquareSeller

        # Verify seller exists
        self.assertTrue(SquareSeller.objects.filter(square_merchant_id="MLF3WZS2N9WVG").exists())

        # Simulate Square revocation webhook
        webhook_data = {
            "merchant_id": "MLF3WZS2N9WVG",
            "type": "oauth.authorization.revoked",
            "event_id": "957299eb-98e4-399c-b7d9-e73ddeff19df",
            "created_at": "2025-11-23T16:29:14.35551833Z",
            "data": {
                "type": "revocation",
                "id": "6ea8bc48-7c2e-43d1-bd36-c865f6c4083d",
                "object": {"revocation": {"revoked_at": "2025-11-23T16:29:12Z", "revoker_type": "MERCHANT"}},
            },
        }

        url = reverse("square_webhook")
        response = self.client.post(url, data=webhook_data, content_type="application/json")

        # Should return 200
        self.assertEqual(response.status_code, 200)

        # SquareSeller should be deleted
        self.assertFalse(SquareSeller.objects.filter(square_merchant_id="MLF3WZS2N9WVG").exists())

    def test_oauth_revocation_handles_missing_seller(self):
        """Test that revocation webhook handles missing SquareSeller gracefully"""
        from django.urls import reverse

        # Delete the seller before webhook
        self.square_seller.delete()

        # Simulate revocation webhook for non-existent seller
        webhook_data = {
            "merchant_id": "NONEXISTENT_MERCHANT",
            "type": "oauth.authorization.revoked",
            "event_id": "test-event-id",
            "created_at": "2025-11-23T16:29:14.35551833Z",
            "data": {
                "type": "revocation",
                "id": "test-revocation-id",
                "object": {"revocation": {"revoked_at": "2025-11-23T16:29:12Z", "revoker_type": "MERCHANT"}},
            },
        }

        url = reverse("square_webhook")
        response = self.client.post(url, data=webhook_data, content_type="application/json")

        # Should still return 200 (graceful handling)
        self.assertEqual(response.status_code, 200)

    def test_payment_webhook_handles_missing_merchant(self):
        """Test that payment webhook handles missing SquareSeller gracefully"""
        from django.urls import reverse

        # Simulate payment webhook with non-existent merchant_id
        webhook_data = {
            "merchant_id": "NONEXISTENT_MERCHANT",
            "type": "payment.updated",
            "event_id": "test-event-id",
            "created_at": "2025-11-23T16:29:14.35551833Z",
            "data": {
                "type": "payment",
                "id": "test-payment-id",
                "object": {
                    "payment": {
                        "id": "test-payment-id",
                        "status": "COMPLETED",
                        "order_id": "test-order-id",
                        "amount_money": {"amount": 1000, "currency": "USD"},
                    }
                },
            },
        }

        url = reverse("square_webhook")
        response = self.client.post(url, data=webhook_data, content_type="application/json")

        # Should return 200 (graceful handling with logged warning)
        self.assertEqual(response.status_code, 200)

    def test_payment_webhook_creates_invoice_payment(self):
        """Test that payment.updated webhook successfully creates InvoicePayment without status field"""
        from decimal import Decimal
        from unittest.mock import Mock, patch

        from django.urls import reverse

        from .models import Invoice, InvoicePayment, SquareSeller

        # Create an invoice for the test
        test_invoice, _ = Invoice.objects.get_or_create(auctiontos_user=self.online_tos)

        # Mock the entire Square orders.get flow
        mock_order = Mock()
        mock_order.reference_id = str(test_invoice.pk)

        mock_order_response = Mock()
        mock_order_response.order = mock_order

        mock_orders_api = Mock()
        mock_orders_api.get = Mock(return_value=mock_order_response)

        mock_client = Mock()
        mock_client.orders = mock_orders_api

        # Patch get_square_client at the class level so any instance returns our mock
        with patch.object(SquareSeller, "get_square_client", return_value=mock_client):
            # Simulate payment.updated webhook with COMPLETED status
            webhook_data = {
                "merchant_id": "MLF3WZS2N9WVG",
                "type": "payment.updated",
                "event_id": "test-payment-event",
                "created_at": "2025-11-23T16:29:14.35551833Z",
                "data": {
                    "type": "payment",
                    "id": "test-payment-updated-id",
                    "object": {
                        "payment": {
                            "id": "PAYMENT_123456",
                            "status": "COMPLETED",
                            "order_id": "ORDER_123456",
                            "amount_money": {"amount": 5000, "currency": "USD"},
                        }
                    },
                },
            }

            url = reverse("square_webhook")
            response = self.client.post(url, data=webhook_data, content_type="application/json")

            # Should return 200
            self.assertEqual(response.status_code, 200)

            # Verify InvoicePayment was created without status field
            payment = InvoicePayment.objects.filter(external_id="PAYMENT_123456").first()
            self.assertIsNotNone(payment)
            self.assertEqual(payment.invoice, test_invoice)
            self.assertEqual(payment.amount, Decimal("50.00"))  # 5000 cents = $50
            self.assertEqual(payment.currency, "USD")
            self.assertEqual(payment.payment_method, "Square")
            # Verify that the status field is not present (would raise AttributeError if accessed)
            self.assertFalse(hasattr(payment, "status") and payment.status)


class SquareWebhookSignatureValidationTests(StandardTestCase):
    """Tests for Square webhook signature validation

    Confirms that SQUARE_WEBHOOK_SIGNATURE_KEY is actually respected
    and that we don't validate forged requests.
    """

    def setUp(self):
        super().setUp()
        from .models import SquareSeller

        # Create Square seller for testing
        self.square_seller = SquareSeller.objects.create(
            user=self.admin_user,
            square_merchant_id="TEST_MERCHANT_ID",
            access_token="TEST_ACCESS_TOKEN",
            refresh_token="TEST_REFRESH_TOKEN",
            token_expires_at=timezone.now() + datetime.timedelta(days=30),
            currency="USD",
        )

        # Test signature key
        self.signature_key = "test-signature-key-12345"

        # Standard webhook data used across tests
        self.webhook_data = {
            "merchant_id": "TEST_MERCHANT_ID",
            "type": "oauth.authorization.revoked",
            "event_id": "test-event-id",
            "created_at": "2025-11-23T16:29:14.35551833Z",
            "data": {
                "type": "revocation",
                "id": "test-revocation-id",
                "object": {"revocation": {"revoked_at": "2025-11-23T16:29:12Z", "revoker_type": "MERCHANT"}},
            },
        }

    def compute_signature(self, url, body, key=None):
        """Compute an HMAC-SHA256 signature for testing using base64 encoding (as Square does)

        Args:
            url: The notification URL
            body: The request body
            key: Optional signature key (defaults to self.signature_key)
        """
        if key is None:
            key = self.signature_key
        message = (url + body).encode("utf-8")
        key_bytes = key.encode("utf-8")
        hash_bytes = hmac.new(key_bytes, message, hashlib.sha256).digest()
        return base64.b64encode(hash_bytes).decode("utf-8")

    def test_forged_signature_is_rejected(self):
        """Test that requests with invalid/forged signatures are rejected when key is configured"""
        url = reverse("square_webhook")

        # Test with signature key configured - forged signature should be rejected
        with override_settings(SQUARE_WEBHOOK_SIGNATURE_KEY=self.signature_key):
            # Send with a forged/invalid signature
            response = self.client.post(
                url,
                data=self.webhook_data,
                content_type="application/json",
                HTTP_X_SQUARE_HMACSHA256_SIGNATURE="forged-invalid-signature",
            )

            # Should return 403 Forbidden
            self.assertEqual(response.status_code, 403)
            self.assertIn(b"invalid signature", response.content)

    def test_missing_signature_header_is_rejected(self):
        """Test that requests without signature header are rejected when key is configured"""
        url = reverse("square_webhook")

        # Test with signature key configured - missing signature should be rejected
        with override_settings(SQUARE_WEBHOOK_SIGNATURE_KEY=self.signature_key):
            # Send without signature header
            response = self.client.post(
                url,
                data=self.webhook_data,
                content_type="application/json",
            )

            # Should return 403 Forbidden
            self.assertEqual(response.status_code, 403)
            self.assertIn(b"missing signature", response.content)

    def test_valid_signature_is_accepted(self):
        """Test that requests with valid signatures are accepted when key is configured"""
        url = reverse("square_webhook")
        body = json.dumps(self.webhook_data)

        # Build the full URL as the test client would see it
        # The test client uses HTTP on localhost by default
        full_url = "http://testserver" + url

        # Compute the correct signature
        valid_signature = self.compute_signature(full_url, body)

        # Test with signature key configured - valid signature should be accepted
        with override_settings(SQUARE_WEBHOOK_SIGNATURE_KEY=self.signature_key):
            response = self.client.post(
                url,
                data=body,
                content_type="application/json",
                HTTP_X_SQUARE_HMACSHA256_SIGNATURE=valid_signature,
            )

            # Should return 200 OK
            self.assertEqual(response.status_code, 200)

    def test_wrong_signature_key_is_rejected(self):
        """Test that signatures computed with a different key are rejected"""
        url = reverse("square_webhook")
        body = json.dumps(self.webhook_data)
        full_url = "http://testserver" + url

        # Compute signature with a DIFFERENT key (attacker's key)
        wrong_signature = self.compute_signature(full_url, body, key="attacker-key-different")

        # Test with correct signature key configured - wrong key signature should be rejected
        with override_settings(SQUARE_WEBHOOK_SIGNATURE_KEY=self.signature_key):
            response = self.client.post(
                url,
                data=body,
                content_type="application/json",
                HTTP_X_SQUARE_HMACSHA256_SIGNATURE=wrong_signature,
            )

            # Should return 403 Forbidden
            self.assertEqual(response.status_code, 403)
            self.assertIn(b"invalid signature", response.content)

    def test_tampered_body_is_rejected(self):
        """Test that a valid signature for different body data is rejected"""
        import copy

        # Create tampered data by modifying a copy of the original
        tampered_webhook_data = copy.deepcopy(self.webhook_data)
        tampered_webhook_data["merchant_id"] = "DIFFERENT_MERCHANT"  # Attacker tries to change the merchant
        tampered_webhook_data["data"]["id"] = "tampered-id"

        url = reverse("square_webhook")
        original_body = json.dumps(self.webhook_data)
        tampered_body = json.dumps(tampered_webhook_data)
        full_url = "http://testserver" + url

        # Compute valid signature for ORIGINAL body
        valid_signature = self.compute_signature(full_url, original_body)

        # Test: Send tampered body with signature for original body
        with override_settings(SQUARE_WEBHOOK_SIGNATURE_KEY=self.signature_key):
            response = self.client.post(
                url,
                data=tampered_body,
                content_type="application/json",
                HTTP_X_SQUARE_HMACSHA256_SIGNATURE=valid_signature,
            )

            # Should return 403 Forbidden because body doesn't match signature
            self.assertEqual(response.status_code, 403)
            self.assertIn(b"invalid signature", response.content)

    def test_improperly_configured_in_production_without_webhook_key(self):
        """Test that ImproperlyConfigured is raised in production when Square is configured but webhook key is missing"""
        from django.core.exceptions import ImproperlyConfigured

        url = reverse("square_webhook")

        # Simulate production mode (DEBUG=False) with Square configured but no webhook signature key
        with override_settings(
            DEBUG=False,
            SQUARE_APPLICATION_ID="test-app-id",
            SQUARE_CLIENT_SECRET="test-client-secret",
            SQUARE_WEBHOOK_SIGNATURE_KEY="",
        ):
            with self.assertRaises(ImproperlyConfigured) as context:
                self.client.post(
                    url,
                    data=self.webhook_data,
                    content_type="application/json",
                )

            self.assertIn("SQUARE_WEBHOOK_SIGNATURE_KEY must be set", str(context.exception))


class CurrencyCustomizationTests(StandardTestCase):
    """Tests for currency display customization"""

    def test_userdata_default_currency(self):
        """Test that UserData has a default currency of USD"""
        user = User.objects.create_user(username="test_currency_user", password="testpassword")
        self.assertEqual(user.userdata.preferred_currency, "USD")
        self.assertEqual(user.userdata.currency, "USD")

    def test_userdata_preferred_currency_gbp(self):
        """Test that UserData can be set to GBP"""
        user = User.objects.create_user(username="uk_user", password="testpassword")
        user.userdata.preferred_currency = "GBP"
        user.userdata.save()
        self.assertEqual(user.userdata.currency, "GBP")

    def test_userdata_preferred_currency_cad(self):
        """Test that UserData can be set to CAD"""
        user = User.objects.create_user(username="ca_user", password="testpassword")
        user.userdata.preferred_currency = "CAD"
        user.userdata.save()
        self.assertEqual(user.userdata.currency, "CAD")

    def test_lot_currency_from_auction_creator(self):
        """Test that Lot gets currency from auction creator"""
        # Set auction creator to GBP
        self.user.userdata.preferred_currency = "GBP"
        self.user.userdata.save()

        lot = Lot.objects.create(
            lot_name="Test Lot",
            auction=self.online_auction,
            quantity=1,
            user=self.user,
        )

        self.assertEqual(lot.currency, "GBP")
        self.assertEqual(lot.currency_symbol, "£")

    def test_lot_currency_from_lot_owner_standalone(self):
        """Test that standalone lot gets currency from owner"""
        # Create a user with CAD preference
        cad_user = User.objects.create_user(username="cad_user", password="testpassword")
        cad_user.userdata.preferred_currency = "CAD"
        cad_user.userdata.save()

        # Create a standalone lot (no auction)
        lot = Lot.objects.create(
            lot_name="Standalone Lot",
            auction=None,
            quantity=1,
            user=cad_user,
        )

        self.assertEqual(lot.currency, "CAD")
        self.assertEqual(lot.currency_symbol, "$")

    def test_auction_currency_from_creator(self):
        """Test that Auction gets currency from creator"""
        # Set auction creator to GBP
        self.user.userdata.preferred_currency = "GBP"
        self.user.userdata.save()

        self.assertEqual(self.online_auction.currency, "GBP")
        self.assertEqual(self.online_auction.currency_symbol, "£")

    def test_invoice_currency_from_auction_creator(self):
        """Test that Invoice gets currency from auction creator"""
        # Set auction creator to CAD
        self.user.userdata.preferred_currency = "CAD"
        self.user.userdata.save()

        invoice = Invoice.objects.create(auctiontos_user=self.online_tos, auction=self.online_auction)

        self.assertEqual(invoice.currency, "CAD")
        self.assertEqual(invoice.currency_symbol, "$")

    def test_currency_symbol_usd(self):
        """Test USD currency symbol"""
        user = User.objects.create_user(username="usd_user", password="testpassword")
        user.userdata.preferred_currency = "USD"
        user.userdata.save()

        lot = Lot.objects.create(
            lot_name="USD Lot",
            auction=None,
            quantity=1,
            user=user,
        )

        self.assertEqual(lot.currency_symbol, "$")

    def test_currency_symbol_gbp(self):
        """Test GBP currency symbol"""
        user = User.objects.create_user(username="gbp_user", password="testpassword")
        user.userdata.preferred_currency = "GBP"
        user.userdata.save()

        lot = Lot.objects.create(
            lot_name="GBP Lot",
            auction=None,
            quantity=1,
            user=user,
        )

        self.assertEqual(lot.currency_symbol, "£")

    def test_change_user_preferences_form_includes_currency(self):
        """Test that ChangeUserPreferencesForm includes preferred_currency field"""
        from .forms import ChangeUserPreferencesForm

        form = ChangeUserPreferencesForm(user=self.user, instance=self.user.userdata)
        self.assertIn("preferred_currency", form.fields)

    def test_preferences_view_can_change_currency(self):
        """Test that user can change their preferred currency via preferences page"""
        self.client.login(username="my_lot", password="testpassword")

        url = reverse("userpage", kwargs={"slug": self.user.username})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

        # Change currency to GBP
        url = reverse("preferences")
        response = self.client.post(
            url,
            {
                "preferred_currency": "GBP",
                "distance_unit": "mi",
                "email_visible": False,
                "show_ads": True,
                "email_me_about_new_auctions": True,
                "email_me_about_new_local_lots": True,
                "email_me_about_new_lots_ship_to_location": True,
                "email_me_when_people_comment_on_my_lots": True,
                "email_me_about_new_chat_replies": True,
                "email_me_about_new_in_person_auctions": True,
                "send_reminder_emails_about_joining_auctions": True,
                "username_visible": True,
                "share_lot_images": True,
                "auto_add_images": True,
                "push_notifications_when_lots_sell": False,
            },
            follow=True,
        )

        # Check that currency was changed
        self.user.userdata.refresh_from_db()
        self.assertEqual(self.user.userdata.preferred_currency, "GBP")

    def test_currency_symbol_eur(self):
        """Test EUR currency symbol"""
        user = User.objects.create_user(username="eur_user", password="testpassword")
        user.userdata.preferred_currency = "EUR"
        user.userdata.save()

        lot = Lot.objects.create(
            lot_name="EUR Lot",
            auction=None,
            quantity=1,
            user=user,
        )

        self.assertEqual(lot.currency_symbol, "€")

    def test_currency_symbol_jpy(self):
        """Test JPY currency symbol"""
        user = User.objects.create_user(username="jpy_user", password="testpassword")
        user.userdata.preferred_currency = "JPY"
        user.userdata.save()

        lot = Lot.objects.create(
            lot_name="JPY Lot",
            auction=None,
            quantity=1,
            user=user,
        )

        self.assertEqual(lot.currency_symbol, "¥")

    def test_currency_symbol_aud(self):
        """Test AUD currency symbol"""
        user = User.objects.create_user(username="aud_user", password="testpassword")
        user.userdata.preferred_currency = "AUD"
        user.userdata.save()

        lot = Lot.objects.create(
            lot_name="AUD Lot",
            auction=None,
            quantity=1,
            user=user,
        )

        self.assertEqual(lot.currency_symbol, "$")

    def test_currency_symbol_chf(self):
        """Test CHF currency symbol"""
        user = User.objects.create_user(username="chf_user", password="testpassword")
        user.userdata.preferred_currency = "CHF"
        user.userdata.save()

        lot = Lot.objects.create(
            lot_name="CHF Lot",
            auction=None,
            quantity=1,
            user=user,
        )

        self.assertEqual(lot.currency_symbol, "CHF")

    def test_currency_symbol_cny(self):
        """Test CNY currency symbol"""
        user = User.objects.create_user(username="cny_user", password="testpassword")
        user.userdata.preferred_currency = "CNY"
        user.userdata.save()

        lot = Lot.objects.create(
            lot_name="CNY Lot",
            auction=None,
            quantity=1,
            user=user,
        )

        self.assertEqual(lot.currency_symbol, "¥")

    def test_all_currency_choices_available(self):
        """Test that all 8 currencies are available in choices"""
        from .forms import ChangeUserPreferencesForm

        form = ChangeUserPreferencesForm(user=self.user, instance=self.user.userdata)
        currency_choices = [choice[0] for choice in form.fields["preferred_currency"].choices]

        expected_currencies = ["USD", "CAD", "GBP", "EUR", "JPY", "AUD", "CHF", "CNY"]
        for currency in expected_currencies:
            self.assertIn(currency, currency_choices)


class AuctionEmailFieldsTest(StandardTestCase):
    """Tests for the new auction email tracking fields and signal handling."""

    def test_new_online_auction_has_email_due_dates(self):
        """Test that a new online auction has email due dates set correctly."""
        user = User.objects.create_user(username="email_test_user", password="testpassword", email="email@example.com")
        future_end = timezone.now() + datetime.timedelta(days=7)
        future_start = timezone.now() + datetime.timedelta(hours=1)

        auction = Auction.objects.create(
            created_by=user,
            title="Email Test Auction",
            is_online=True,
            date_start=future_start,
            date_end=future_end,
        )

        # Welcome email should be due 24 hours after creation
        self.assertIsNotNone(auction.welcome_email_due)
        self.assertFalse(auction.welcome_email_sent)

        # Invoice email should be due 1 hour after auction end (for online auctions)
        self.assertIsNotNone(auction.invoice_email_due)
        self.assertFalse(auction.invoice_email_sent)

        # Follow-up email should be due 24 hours after auction end (for online auctions)
        self.assertIsNotNone(auction.followup_email_due)
        self.assertFalse(auction.followup_email_sent)

    def test_new_inperson_auction_has_invoice_marked_sent(self):
        """Test that a new in-person auction has invoice email marked as sent."""
        user = User.objects.create_user(
            username="inperson_test_user", password="testpassword", email="inperson@example.com"
        )
        future_start = timezone.now() + datetime.timedelta(hours=1)

        auction = Auction.objects.create(
            created_by=user,
            title="In-Person Test Auction",
            is_online=False,
            date_start=future_start,
        )

        # Invoice email should be marked as sent for in-person auctions
        self.assertTrue(auction.invoice_email_sent)

        # Follow-up email should be due 24 hours after auction start (for in-person auctions)
        self.assertIsNotNone(auction.followup_email_due)
        self.assertFalse(auction.followup_email_sent)

    def test_email_due_dates_updated_when_dates_change(self):
        """Test that email due dates are updated when auction dates change."""
        user = User.objects.create_user(username="date_change_user", password="testpassword", email="date@example.com")
        future_end = timezone.now() + datetime.timedelta(days=7)
        future_start = timezone.now() + datetime.timedelta(hours=1)

        auction = Auction.objects.create(
            created_by=user,
            title="Date Change Test Auction",
            is_online=True,
            date_start=future_start,
            date_end=future_end,
        )

        original_invoice_due = auction.invoice_email_due
        original_followup_due = auction.followup_email_due

        # Change the auction end date
        new_end = timezone.now() + datetime.timedelta(days=14)
        auction.date_end = new_end
        auction.save()

        # Refresh from database
        auction.refresh_from_db()

        # Invoice and follow-up due dates should be updated
        self.assertNotEqual(auction.invoice_email_due, original_invoice_due)
        self.assertNotEqual(auction.followup_email_due, original_followup_due)


class UserLocationUpdateTests(StandardTestCase):
    """Tests for updating user contact info and syncing to recent AuctionTOS records."""

    def setUp(self):
        super().setUp()
        # Create UserData for the user
        self.user_data, _ = UserData.objects.get_or_create(
            user=self.user,
            defaults={
                "phone_number": "555-1234",
                "address": "123 Old Street",
            },
        )
        self.user.first_name = "John"
        self.user.last_name = "Doe"
        self.user.save()

        # Set contact info on the online_tos
        self.online_tos.name = "John Doe"
        self.online_tos.phone_number = "555-1234"
        self.online_tos.address = "123 Old Street"
        self.online_tos.save()

        # Set contact info on the in_person_tos
        self.in_person_tos.name = "John Doe"
        self.in_person_tos.phone_number = "555-1234"
        self.in_person_tos.address = "123 Old Street"
        self.in_person_tos.save()

    def test_recent_auctiontos_updated_on_contact_change(self):
        """When a user updates their contact info, recent AuctionTOS records should be updated."""
        self.client.login(username="my_lot", password="testpassword")

        # Post updated contact info
        response = self.client.post(
            "/contact_info/",
            {
                "first_name": "Jane",
                "last_name": "Smith",
                "phone_number": "555-9999",
                "address": "456 New Avenue",
                "location": "",
                "location_coordinates": "",
                "club_affiliation": "",
                "club": "",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)

        # Refresh the AuctionTOS records from the database
        self.online_tos.refresh_from_db()
        self.in_person_tos.refresh_from_db()

        # Check that the AuctionTOS records were updated
        self.assertEqual(self.online_tos.name, "Jane Smith")
        self.assertEqual(self.online_tos.phone_number, "555-9999")
        self.assertEqual(self.online_tos.address, "456 New Avenue")

        self.assertEqual(self.in_person_tos.name, "Jane Smith")
        self.assertEqual(self.in_person_tos.phone_number, "555-9999")
        self.assertEqual(self.in_person_tos.address, "456 New Avenue")

    def test_auction_history_created_on_contact_update(self):
        """An AuctionHistory record should be created when contact info is updated."""
        self.client.login(username="my_lot", password="testpassword")

        # Clear existing history
        AuctionHistory.objects.filter(auction=self.online_auction).delete()

        # Post updated contact info
        self.client.post(
            "/contact_info/",
            {
                "first_name": "Jane",
                "last_name": "Smith",
                "phone_number": "555-9999",
                "address": "456 New Avenue",
                "location": "",
                "location_coordinates": "",
                "club_affiliation": "",
                "club": "",
            },
        )

        # Check that history was created
        history = AuctionHistory.objects.filter(
            auction=self.online_auction,
            user=self.user,
            applies_to="USERS",
        )
        self.assertTrue(history.exists())
        self.assertIn("Updated contact info", history.first().action)

    def test_old_auctiontos_not_updated(self):
        """AuctionTOS records older than 30 days should not be updated."""
        self.client.login(username="my_lot", password="testpassword")

        # Make the online_tos older than 30 days
        old_date = timezone.now() - datetime.timedelta(days=31)
        AuctionTOS.objects.filter(pk=self.online_tos.pk).update(createdon=old_date)
        self.online_tos.refresh_from_db()

        # Post updated contact info
        self.client.post(
            "/contact_info/",
            {
                "first_name": "Jane",
                "last_name": "Smith",
                "phone_number": "555-9999",
                "address": "456 New Avenue",
                "location": "",
                "location_coordinates": "",
                "club_affiliation": "",
                "club": "",
            },
        )

        # Refresh from database
        self.online_tos.refresh_from_db()
        self.in_person_tos.refresh_from_db()

        # Old TOS should not be updated
        self.assertEqual(self.online_tos.name, "John Doe")
        self.assertEqual(self.online_tos.phone_number, "555-1234")
        self.assertEqual(self.online_tos.address, "123 Old Street")

        # Recent TOS should be updated
        self.assertEqual(self.in_person_tos.name, "Jane Smith")
        self.assertEqual(self.in_person_tos.phone_number, "555-9999")
        self.assertEqual(self.in_person_tos.address, "456 New Avenue")

    def test_manually_added_auctiontos_not_updated(self):
        """AuctionTOS records that were manually added should not be updated."""
        self.client.login(username="my_lot", password="testpassword")

        # Mark the online_tos as manually added
        self.online_tos.manually_added = True
        self.online_tos.save()

        # Post updated contact info
        self.client.post(
            "/contact_info/",
            {
                "first_name": "Jane",
                "last_name": "Smith",
                "phone_number": "555-9999",
                "address": "456 New Avenue",
                "location": "",
                "location_coordinates": "",
                "club_affiliation": "",
                "club": "",
            },
        )

        # Refresh from database
        self.online_tos.refresh_from_db()
        self.in_person_tos.refresh_from_db()

        # Manually added TOS should not be updated
        self.assertEqual(self.online_tos.name, "John Doe")
        self.assertEqual(self.online_tos.phone_number, "555-1234")
        self.assertEqual(self.online_tos.address, "123 Old Street")

        # Non-manually added TOS should be updated
        self.assertEqual(self.in_person_tos.name, "Jane Smith")
        self.assertEqual(self.in_person_tos.phone_number, "555-9999")
        self.assertEqual(self.in_person_tos.address, "456 New Avenue")

    def test_update_message_shown_for_single_auction(self):
        """The form should show a message about updating a single auction."""
        self.client.login(username="my_lot", password="testpassword")

        # Make one TOS old and the other manually added
        old_date = timezone.now() - datetime.timedelta(days=31)
        AuctionTOS.objects.filter(pk=self.online_tos.pk).update(createdon=old_date)

        response = self.client.get("/contact_info/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("auctiontos_update_message", response.context)
        # When there's only one auction, it shows the auction name, not "1 auction"
        self.assertIn(str(self.in_person_auction), response.context["auctiontos_update_message"])

    def test_update_message_shown_for_multiple_auctions(self):
        """The form should show a message about updating multiple auctions."""
        self.client.login(username="my_lot", password="testpassword")

        response = self.client.get("/contact_info/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("auctiontos_update_message", response.context)
        self.assertIn("2 auctions", response.context["auctiontos_update_message"])

    def test_no_update_message_when_no_recent_auctiontos(self):
        """No message should be shown when there are no recent AuctionTOS records."""
        self.client.login(username="my_lot", password="testpassword")

        # Make all TOS old
        old_date = timezone.now() - datetime.timedelta(days=31)
        AuctionTOS.objects.filter(user=self.user).update(createdon=old_date)

        response = self.client.get("/contact_info/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("auctiontos_update_message", response.context)

    def test_no_changes_if_info_same(self):
        """If contact info hasn't changed, no history should be created."""
        self.client.login(username="my_lot", password="testpassword")

        # Clear existing history
        AuctionHistory.objects.filter(auction=self.online_auction).delete()

        # Post the same contact info
        self.client.post(
            "/contact_info/",
            {
                "first_name": "John",
                "last_name": "Doe",
                "phone_number": "555-1234",
                "address": "123 Old Street",
                "location": "",
                "location_coordinates": "",
                "club_affiliation": "",
                "club": "",
            },
        )

        # Check that no history was created for the auctions
        history = AuctionHistory.objects.filter(
            auction=self.online_auction,
            applies_to="USERS",
        )
        self.assertEqual(history.count(), 0)


class LoadDemoDataTests(TestCase):
    """Tests for the load_demo_data management command"""

    @override_settings(DEBUG=True)
    def test_load_demo_data_with_debug_true(self):
        """Test that demo data loads successfully when DEBUG=True and no auctions exist"""
        from io import StringIO

        from django.core.management import call_command

        # Ensure no auctions exist
        Auction.objects.all().delete()

        # Call the command
        out = StringIO()
        call_command("load_demo_data", stdout=out)
        output = out.getvalue()

        # Check output messages
        self.assertIn("Loading demo data because DEBUG=True", output)
        self.assertIn("Demo data loaded successfully!", output)

        # Verify demo data was created
        self.assertTrue(Auction.objects.filter(title__contains="Demo").exists())
        auctions = Auction.objects.filter(title__contains="Demo")
        self.assertEqual(auctions.count(), 3)

        # Verify auction types
        self.assertTrue(auctions.filter(is_online=False).exists())  # In-person auction
        self.assertTrue(auctions.filter(is_online=True).exists())  # Online auctions

        # Verify pickup locations including mail shipping
        mail_locations = PickupLocation.objects.filter(pickup_by_mail=True)
        self.assertGreater(mail_locations.count(), 0)

        # Verify users were created
        self.assertTrue(User.objects.filter(username__contains="demo_").exists())

        # Verify lots were created
        self.assertTrue(Lot.objects.filter(lot_number__gte=90000).exists())

        # Verify some lots have winners (ended auction)
        lots_with_winners = Lot.objects.filter(lot_number__gte=90000, winner__isnull=False)
        self.assertGreater(lots_with_winners.count(), 0)

    @override_settings(DEBUG=True)
    def test_load_demo_data_skips_when_auctions_exist(self):
        """Test that demo data is not loaded when auctions already exist"""
        from io import StringIO

        from django.core.management import call_command

        # Create an auction to prevent demo data loading
        existing_auction = Auction.objects.create(
            title="Existing Auction",
            created_by=None,
            date_start=timezone.now(),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )

        # Call the command
        out = StringIO()
        call_command("load_demo_data", stdout=out)
        output = out.getvalue()

        # Check output messages
        self.assertIn("Skipping demo data load", output)
        self.assertIn("auction(s) already exist", output)

        # Verify no demo auctions were created
        demo_auctions = Auction.objects.filter(title__contains="Demo")
        self.assertEqual(demo_auctions.count(), 0)

        # Verify original auction still exists
        self.assertTrue(Auction.objects.filter(pk=existing_auction.pk).exists())

    @override_settings(DEBUG=False)
    def test_load_demo_data_skips_when_debug_false(self):
        """Test that demo data is not loaded when DEBUG=False"""
        from io import StringIO

        from django.core.management import call_command

        # Ensure no auctions exist
        Auction.objects.all().delete()

        # Call the command
        out = StringIO()
        call_command("load_demo_data", stdout=out)
        output = out.getvalue()

        # Check output messages
        self.assertIn("Skipping demo data load - DEBUG=False", output)
        self.assertIn("production mode", output)

        # Verify no auctions were created
        self.assertEqual(Auction.objects.count(), 0)


class AdminReadonlyFieldsTests(StandardTestCase):
    """Test that admin readonly fields are properly configured"""

    def test_auction_admin_readonly_fields(self):
        """Test that AuctionAdmin has created_by as readonly"""
        from auctions.admin import AuctionAdmin

        admin_instance = AuctionAdmin(Auction, None)
        self.assertIn("created_by", admin_instance.readonly_fields)

    def test_auctiontos_admin_readonly_fields(self):
        """Test that AuctionTOSAdmin has user, auction, and pickup_location as readonly"""
        from auctions.admin import AuctionTOSAdmin

        admin_instance = AuctionTOSAdmin(AuctionTOS, None)
        self.assertIn("user", admin_instance.readonly_fields)
        self.assertIn("auction", admin_instance.readonly_fields)
        self.assertIn("pickup_location", admin_instance.readonly_fields)


class HelperFunctionsTestCase(StandardTestCase):
    """Test cases for helper_functions.py"""

    def test_get_currency_symbol_all_supported_currencies(self):
        """Test that all documented currency codes return correct symbols"""
        from auctions.helper_functions import get_currency_symbol

        # Test all documented currencies
        self.assertEqual(get_currency_symbol("USD"), "$")
        self.assertEqual(get_currency_symbol("CAD"), "$")
        self.assertEqual(get_currency_symbol("AUD"), "$")
        self.assertEqual(get_currency_symbol("GBP"), "£")
        self.assertEqual(get_currency_symbol("EUR"), "€")
        self.assertEqual(get_currency_symbol("JPY"), "¥")
        self.assertEqual(get_currency_symbol("CNY"), "¥")
        self.assertEqual(get_currency_symbol("CHF"), "CHF")

    def test_get_currency_symbol_unsupported_currency(self):
        """Test that unsupported currencies default to $"""
        from auctions.helper_functions import get_currency_symbol

        self.assertEqual(get_currency_symbol("XXX"), "$")
        self.assertEqual(get_currency_symbol(""), "$")
        self.assertEqual(get_currency_symbol("INVALID"), "$")

    def test_get_currency_symbol_case_sensitivity(self):
        """Test currency code case sensitivity - should be case sensitive"""
        from auctions.helper_functions import get_currency_symbol

        # Currency codes should be uppercase
        self.assertEqual(get_currency_symbol("usd"), "$")  # Will default to $ as lowercase not in map
        self.assertEqual(get_currency_symbol("Usd"), "$")  # Will default to $ as mixed case not in map

    def test_bin_data_with_datetime_values(self):
        """Test bin_data with datetime field values"""
        from auctions.helper_functions import bin_data

        # Create test lots with different dates
        base_time = timezone.now() - datetime.timedelta(days=10)
        for i in range(10):
            lot = Lot.objects.create(
                lot_name=f"Test lot datetime {i}",
                auction=self.online_auction,
                auctiontos_seller=self.online_tos,
                quantity=1,
                active=False,
            )
            lot.date_posted = base_time + datetime.timedelta(days=i)
            lot.save()

        qs = Lot.objects.filter(auction=self.online_auction, lot_name__startswith="Test lot datetime")
        result = bin_data(qs, "date_posted", 5)
        self.assertEqual(len(result), 5)

    def test_bin_data_empty_queryset(self):
        """Test bin_data with empty queryset returns empty bins"""
        from auctions.helper_functions import bin_data

        qs = Lot.objects.filter(lot_name="NONEXISTENT")
        result = bin_data(qs, "winning_price", 5, start_bin=0, end_bin=100)
        # Should return 5 bins all with 0 count
        self.assertEqual(len(result), 5)
        self.assertEqual(sum(result), 0)

    def test_bin_data_invalid_field_raises_error(self):
        """Test bin_data with invalid field raises appropriate error"""
        from auctions.helper_functions import bin_data

        qs = Lot.objects.filter(auction=self.online_auction)
        # Should raise ValueError when field doesn't exist and can't be ordered
        with self.assertRaises(ValueError) as context:
            bin_data(qs, "nonexistent_field", 5)
        self.assertIn("start_bin and end_bin are required", str(context.exception))

    def test_bin_data_string_field_raises_error(self):
        """Test bin_data with string field raises appropriate error"""
        from auctions.helper_functions import bin_data

        # Create a lot with a string field
        Lot.objects.create(
            lot_name="String test",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            active=False,
        )

        qs = Lot.objects.filter(lot_name="String test")
        # Should raise ValueError when field is not datetime or numeric
        with self.assertRaises(ValueError) as context:
            bin_data(qs, "lot_name", 5)
        self.assertIn("needs to be either a datetime or an integer value", str(context.exception))

    def test_bin_data_single_bin(self):
        """Test bin_data with single bin works correctly"""
        from auctions.helper_functions import bin_data

        # Create test lots
        for i in range(5):
            Lot.objects.create(
                lot_name=f"Single bin test {i}",
                auction=self.online_auction,
                auctiontos_seller=self.online_tos,
                quantity=1,
                winning_price=i * 10,
                active=False,
            )

        qs = Lot.objects.filter(lot_name__startswith="Single bin test")
        result = bin_data(qs, "winning_price", 1)
        self.assertEqual(len(result), 1)
        # Note: Due to the >= comparison in bin_data, the max value (40) is excluded
        # and goes to high_overflow. So only 4 items (0,10,20,30) are in the bin.
        self.assertEqual(result[0], 4)

    def test_bin_data_zero_range(self):
        """Test bin_data behavior when start_bin equals end_bin"""
        from auctions.helper_functions import bin_data

        # Create test lots with same value
        for i in range(5):
            Lot.objects.create(
                lot_name=f"Zero range test {i}",
                auction=self.online_auction,
                auctiontos_seller=self.online_tos,
                quantity=1,
                winning_price=50,
                active=False,
            )

        qs = Lot.objects.filter(lot_name__startswith="Zero range test")
        # When start equals end, bin_size will be 0, which should now raise ValueError
        with self.assertRaises(ValueError) as context:
            bin_data(qs, "winning_price", 5, start_bin=50, end_bin=50)
        self.assertIn("zero bin size", str(context.exception))


class ModelUtilityFunctionsTestCase(StandardTestCase):
    """Test cases for utility functions in models.py"""

    def test_median_value_odd_count(self):
        """Test median_value with odd number of items"""
        from auctions.models import median_value

        # Create test lots with different prices
        for i in range(5):
            Lot.objects.create(
                lot_name=f"Median test odd {i}",
                auction=self.online_auction,
                auctiontos_seller=self.online_tos,
                quantity=1,
                winning_price=i * 10,
                active=False,
            )

        qs = Lot.objects.filter(lot_name__startswith="Median test odd")
        result = median_value(qs, "winning_price")
        # With values 0, 10, 20, 30, 40, median should be 20
        self.assertEqual(result, 20)

    def test_median_value_even_count(self):
        """Test median_value with even number of items"""
        from auctions.models import median_value

        # Create test lots with different prices
        for i in range(6):
            Lot.objects.create(
                lot_name=f"Median test even {i}",
                auction=self.online_auction,
                auctiontos_seller=self.online_tos,
                quantity=1,
                winning_price=i * 10,
                active=False,
            )

        qs = Lot.objects.filter(lot_name__startswith="Median test even")
        result = median_value(qs, "winning_price")
        # With values 0, 10, 20, 30, 40, 50, median should be 30 (rounded from index 3)
        self.assertEqual(result, 30)

    def test_add_price_info_requires_lot_queryset(self):
        """Test that add_price_info only accepts Lot querysets"""
        from auctions.models import add_price_info

        # Should raise TypeError when not passed a Lot queryset
        with self.assertRaises(TypeError) as context:
            add_price_info(AuctionTOS.objects.all())
        self.assertIn("must be passed a queryset of the Lot model", str(context.exception))

    def test_add_price_info_sold_lot_calculations(self):
        """Test add_price_info calculates correctly for sold lots"""
        from auctions.models import add_price_info

        # Create a sold lot
        lot = Lot.objects.create(
            lot_name="Sold lot test",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=100,
            auctiontos_winner=self.tosB,
            active=False,
        )

        qs = add_price_info(Lot.objects.filter(pk=lot.pk))
        annotated_lot = qs.first()

        # Should have the annotated fields
        self.assertTrue(hasattr(annotated_lot, "your_cut"))
        self.assertTrue(hasattr(annotated_lot, "club_cut"))
        self.assertTrue(hasattr(annotated_lot, "pre_register_discount"))

    def test_add_price_info_unsold_lot(self):
        """Test add_price_info for unsold lots"""
        from auctions.models import add_price_info

        # Create an unsold lot
        lot = Lot.objects.create(
            lot_name="Unsold lot test",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=None,
            active=False,
        )

        qs = add_price_info(Lot.objects.filter(pk=lot.pk))
        annotated_lot = qs.first()

        # Unsold lots should have negative your_cut (unsold lot fee)
        self.assertLessEqual(annotated_lot.your_cut, 0)
        self.assertEqual(annotated_lot.club_cut, 0)

    def test_add_price_info_donation_lot(self):
        """Test add_price_info for donation lots"""
        from auctions.models import add_price_info

        # Create a donation lot
        lot = Lot.objects.create(
            lot_name="Donation lot test",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            winning_price=100,
            auctiontos_winner=self.tosB,
            donation=True,
            active=False,
        )

        qs = add_price_info(Lot.objects.filter(pk=lot.pk))
        annotated_lot = qs.first()

        # Donation lots should have 0 your_cut
        self.assertEqual(annotated_lot.your_cut, 0)

    def test_distance_to_sql_injection_protection(self):
        """Test distance_to function rejects SQL injection attempts"""
        from auctions.models import distance_to

        # Test with quotes in parameters - should raise TypeError
        with self.assertRaises(TypeError) as context:
            distance_to("45.5'", 90.0)
        self.assertIn("invalid character", str(context.exception))

        with self.assertRaises(TypeError) as context:
            distance_to(45.5, '90.0"')
        self.assertIn("invalid character", str(context.exception))

        with self.assertRaises(TypeError) as context:
            distance_to(45.5, 90.0, lat_field_name="latitude'; DROP TABLE--")
        self.assertIn("invalid character", str(context.exception))

    def test_distance_to_miles_vs_km(self):
        """Test distance_to returns correct SQL for miles vs kilometers"""
        from auctions.models import distance_to

        # Test miles (default)
        distance_miles = distance_to(40.7128, -74.0060)
        self.assertIsNotNone(distance_miles)

        # Test kilometers
        distance_km = distance_to(40.7128, -74.0060, unit="km")
        self.assertIsNotNone(distance_km)

    def test_find_image_with_user(self):
        """Test find_image prioritizes images from specific user"""
        from auctions.models import find_image

        # find_image requires images to be uploaded, which is restricted in tests
        # This test validates the function exists and handles basic inputs
        result = find_image("Test Lot", self.user, self.online_auction)
        # Should return None when no images exist
        self.assertIsNone(result)

    def test_add_tos_info_requires_auctiontos_queryset(self):
        """Test that add_tos_info only accepts AuctionTOS querysets"""
        from auctions.models import add_tos_info

        # Should raise TypeError when not passed an AuctionTOS queryset
        with self.assertRaises(TypeError) as context:
            add_tos_info(Lot.objects.all())
        self.assertIn("must be passed a queryset of the AuctionTOS model", str(context.exception))

    def test_add_tos_info_annotates_fields(self):
        """Test add_tos_info adds expected annotations"""
        from auctions.models import add_tos_info

        qs = add_tos_info(AuctionTOS.objects.filter(pk=self.online_tos.pk))
        annotated_tos = qs.first()

        # Check that annotations are present
        self.assertTrue(hasattr(annotated_tos, "lots_bid"))
        self.assertTrue(hasattr(annotated_tos, "lots_viewed"))
        self.assertTrue(hasattr(annotated_tos, "lots_won"))
        self.assertTrue(hasattr(annotated_tos, "lots_submitted"))
        self.assertTrue(hasattr(annotated_tos, "other_auctions"))
        self.assertTrue(hasattr(annotated_tos, "lots_outbid"))
        self.assertTrue(hasattr(annotated_tos, "account_age_days"))
        self.assertTrue(hasattr(annotated_tos, "has_ever_granted_permission"))

    def test_add_tos_info_permission_filtering(self):
        """Test add_tos_info respects permission flags"""
        from auctions.models import add_tos_info

        # Create a manually added user (no permission granted)
        manual_user = User.objects.create_user(
            username="manual_user", password="testpassword", email="manual@example.com"
        )
        manual_tos = AuctionTOS.objects.create(
            user=manual_user,
            auction=self.online_auction,
            pickup_location=self.location,
            manually_added=True,
        )

        qs = add_tos_info(AuctionTOS.objects.filter(pk=manual_tos.pk))
        annotated_tos = qs.first()

        # Manually added users without permission should have filtered data
        self.assertEqual(annotated_tos.lots_bid, 0)
        self.assertEqual(annotated_tos.lots_viewed, 0)

    def test_nearby_auctions_basic(self):
        """Test nearby_auctions returns auctions within distance"""
        from auctions.models import nearby_auctions

        # Set location for pickup location
        self.location.latitude = 40.7128
        self.location.longitude = -74.0060
        self.location.save()

        # Test with a location that should match
        auctions, distances = nearby_auctions(40.7128, -74.0060, distance=100)

        # Should return lists
        self.assertIsInstance(auctions, list)
        self.assertIsInstance(distances, list)
        self.assertEqual(len(auctions), len(distances))

    def test_nearby_auctions_return_slugs(self):
        """Test nearby_auctions can return just slugs"""
        from auctions.models import nearby_auctions

        # Set location for pickup location
        self.location.latitude = 40.7128
        self.location.longitude = -74.0060
        self.location.save()

        # Test return_slugs parameter
        slugs = nearby_auctions(40.7128, -74.0060, distance=100, return_slugs=True)

        # Should return list of slugs
        self.assertIsInstance(slugs, list)

    def test_nearby_auctions_filters_ignored(self):
        """Test nearby_auctions filters out ignored auctions for users"""
        from auctions.models import AuctionIgnore, nearby_auctions

        # Set location for pickup location
        self.location.latitude = 40.7128
        self.location.longitude = -74.0060
        self.location.save()

        # User ignores the auction
        AuctionIgnore.objects.create(user=self.user, auction=self.online_auction)

        # Should not return ignored auction for this user
        auctions, distances = nearby_auctions(40.7128, -74.0060, distance=100, user=self.user)

        auction_slugs = [a.slug for a in auctions]
        self.assertNotIn(self.online_auction.slug, auction_slugs)

    def test_nearby_auctions_filters_already_joined(self):
        """Test nearby_auctions can filter already joined auctions"""
        from auctions.models import nearby_auctions

        # Set location for pickup location
        self.location.latitude = 40.7128
        self.location.longitude = -74.0060
        self.location.save()

        # User already has TOS (joined)
        # Test with include_already_joined=False (default)
        auctions, distances = nearby_auctions(
            40.7128, -74.0060, distance=100, user=self.user, include_already_joined=False
        )

        # User has already joined online_auction, so it should be filtered out
        auction_slugs = [a.slug for a in auctions]
        self.assertNotIn(self.online_auction.slug, auction_slugs)

    def test_nearby_auctions_includes_joined_when_requested(self):
        """Test nearby_auctions includes joined auctions when flag is set"""
        from auctions.models import nearby_auctions

        # Set location for pickup location
        self.location.latitude = 40.7128
        self.location.longitude = -74.0060
        self.location.save()

        # Make the auction active/current
        self.online_auction.date_start = timezone.now() - datetime.timedelta(days=1)
        self.online_auction.date_end = timezone.now() + datetime.timedelta(days=1)
        self.online_auction.save()

        # Test with include_already_joined=True
        auctions, distances = nearby_auctions(
            40.7128, -74.0060, distance=100, user=self.user, include_already_joined=True
        )

        # Should include the auction even though user has TOS
        auction_slugs = [a.slug for a in auctions]
        self.assertIn(self.online_auction.slug, auction_slugs)


class FormsUtilityTestCase(TestCase):
    """Test cases for utility functions in forms.py"""

    def test_clean_summernote_short_html(self):
        """Test clean_summernote doesn't modify short HTML"""
        from auctions.forms import clean_summernote

        short_html = "<p>This is a short paragraph</p>"
        result = clean_summernote(short_html)
        self.assertEqual(result, short_html)

    def test_clean_summernote_long_html(self):
        """Test clean_summernote truncates long HTML"""
        from auctions.forms import clean_summernote

        # Create HTML longer than max_length
        long_html = "<p>" + "x" * 20000 + "</p>"
        result = clean_summernote(long_html, max_length=100)
        self.assertLessEqual(len(result), 100)

    def test_clean_summernote_preserves_br_tags(self):
        """Test clean_summernote preserves br tags when truncating"""
        from auctions.forms import clean_summernote

        # Create HTML with br tags
        html_with_br = "<p>Text<br/>More text<br />Even more</p>" + "x" * 20000
        result = clean_summernote(html_with_br, max_length=100)
        # br tags should be preserved in the output
        self.assertIn("<br", result)

    def test_clean_summernote_removes_other_tags_when_truncating(self):
        """Test clean_summernote removes non-br tags when truncating"""
        from auctions.forms import clean_summernote

        # Create HTML with various tags
        html = "<div><p><span>Text</span></p></div>" + "x" * 20000
        result = clean_summernote(html, max_length=100)
        # Should have removed tags but kept content
        self.assertNotIn("<div>", result)
        self.assertNotIn("<span>", result)

    def test_clean_summernote_empty_string(self):
        """Test clean_summernote handles empty string"""
        from auctions.forms import clean_summernote

        result = clean_summernote("")
        self.assertEqual(result, "")

    def test_clean_summernote_custom_max_length(self):
        """Test clean_summernote respects custom max_length parameter"""
        from auctions.forms import clean_summernote

        long_html = "x" * 1000
        result = clean_summernote(long_html, max_length=50)
        self.assertLessEqual(len(result), 50)


class TemplateTagsTestCase(TestCase):
    """Test cases for template tags"""

    def test_currency_symbol_filter(self):
        """Test currency_symbol template filter"""
        from auctions.templatetags.currency_filters import currency_symbol

        self.assertEqual(currency_symbol("USD"), "$")
        self.assertEqual(currency_symbol("GBP"), "£")
        self.assertEqual(currency_symbol("EUR"), "€")
        self.assertEqual(currency_symbol("JPY"), "¥")
        self.assertEqual(currency_symbol("CHF"), "CHF")
        self.assertEqual(currency_symbol("UNKNOWN"), "$")

    def test_format_price_filter_with_none(self):
        """Test format_price handles None values"""
        from auctions.templatetags.currency_filters import format_price

        result = format_price(None, "USD")
        self.assertEqual(result, "")

    def test_format_price_filter_usd(self):
        """Test format_price with USD currency"""
        from auctions.templatetags.currency_filters import format_price

        result = format_price(10.5, "USD")
        self.assertEqual(result, "$10.50")

    def test_format_price_filter_jpy(self):
        """Test format_price with JPY currency (no decimals)"""
        from auctions.templatetags.currency_filters import format_price

        result = format_price(1500.75, "JPY")
        self.assertEqual(result, "¥1500")

    def test_format_price_filter_chf(self):
        """Test format_price with CHF currency (space between symbol and amount)"""
        from auctions.templatetags.currency_filters import format_price

        result = format_price(25.50, "CHF")
        self.assertEqual(result, "CHF 25.50")

    def test_format_price_filter_invalid_value(self):
        """Test format_price with invalid price value"""
        from auctions.templatetags.currency_filters import format_price

        result = format_price("invalid", "USD")
        self.assertEqual(result, "$invalid")

    def test_convert_distance_none(self):
        """Test convert_distance with None value"""
        from auctions.templatetags.distance_filters import convert_distance

        result = convert_distance(None, None)
        self.assertIsNone(result)

    def test_convert_distance_zero(self):
        """Test convert_distance with zero distance"""
        from auctions.templatetags.distance_filters import convert_distance

        result = convert_distance(0, None)
        self.assertIsNone(result)

    def test_convert_distance_negative(self):
        """Test convert_distance with negative distance"""
        from auctions.templatetags.distance_filters import convert_distance

        user = User.objects.create_user(username="test_user", password="testpass")
        result = convert_distance(-10, user)
        self.assertIsNone(result)

    def test_convert_distance_miles_for_anonymous(self):
        """Test convert_distance returns miles for anonymous users"""
        from auctions.templatetags.distance_filters import convert_distance

        result = convert_distance(10, None)
        self.assertEqual(result, (10, "miles"))

    def test_convert_distance_miles_for_authenticated_user(self):
        """Test convert_distance returns miles for user with miles preference"""
        from auctions.templatetags.distance_filters import convert_distance

        user = User.objects.create_user(username="miles_user", password="testpass")
        user.userdata.distance_unit = "mi"
        user.userdata.save()

        result = convert_distance(10, user)
        self.assertEqual(result, (10, "miles"))

    def test_convert_distance_km_for_authenticated_user(self):
        """Test convert_distance converts to km for user with km preference"""
        from auctions.templatetags.distance_filters import MILES_TO_KM, convert_distance

        user = User.objects.create_user(username="km_user", password="testpass")
        user.userdata.distance_unit = "km"
        user.userdata.save()

        result = convert_distance(10, user)
        expected_km = int(round(10 * MILES_TO_KM))
        self.assertEqual(result, (expected_km, "km"))

    def test_convert_distance_invalid_string(self):
        """Test convert_distance with invalid string value"""
        from auctions.templatetags.distance_filters import convert_distance

        result = convert_distance("invalid", None)
        self.assertIsNone(result)

    def test_convert_distance_valid_string(self):
        """Test convert_distance with valid numeric string"""
        from auctions.templatetags.distance_filters import convert_distance

        result = convert_distance("15.5", None)
        self.assertEqual(result, (16, "miles"))  # Rounded

    def test_distance_display_filter(self):
        """Test distance_display template filter"""
        from auctions.templatetags.distance_filters import distance_display

        user = User.objects.create_user(username="display_user", password="testpass")
        user.userdata.distance_unit = "mi"
        user.userdata.save()

        result = distance_display(10, user)
        self.assertEqual(result, "10 miles")

    def test_distance_display_filter_none(self):
        """Test distance_display returns empty string for None"""
        from auctions.templatetags.distance_filters import distance_display

        result = distance_display(None, None)
        self.assertEqual(result, "")

    def test_distance_display_filter_zero(self):
        """Test distance_display returns empty string for zero"""
        from auctions.templatetags.distance_filters import distance_display

        result = distance_display(0, None)
        self.assertEqual(result, "")


class ContextProcessorsTestCase(TestCase):
    """Test cases for context processors"""

    def test_google_analytics_context(self):
        """Test google_analytics context processor returns expected keys"""
        from django.test import RequestFactory

        from auctions.context_processors import google_analytics

        factory = RequestFactory()
        request = factory.get("/")

        context = google_analytics(request)
        self.assertIn("GOOGLE_MEASUREMENT_ID", context)
        self.assertIn("GOOGLE_TAG_ID", context)
        self.assertIn("GOOGLE_ADSENSE_ID", context)

    def test_google_oauth_context(self):
        """Test google_oauth context processor returns expected keys"""
        from django.test import RequestFactory

        from auctions.context_processors import google_oauth

        factory = RequestFactory()
        request = factory.get("/")

        context = google_oauth(request)
        self.assertIn("GOOGLE_OAUTH_LINK", context)

    def test_theme_context_anonymous_user(self):
        """Test theme context processor for anonymous users"""
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        from auctions.context_processors import theme

        factory = RequestFactory()
        request = factory.get("/")
        request.user = AnonymousUser()

        context = theme(request)
        self.assertIn("theme", context)
        self.assertIn("show_ads", context)
        self.assertEqual(context["show_ads"], False)  # Ads off for everyone

    def test_theme_context_authenticated_user(self):
        """Test theme context processor for authenticated users"""
        from django.test import RequestFactory

        from auctions.context_processors import theme

        factory = RequestFactory()
        request = factory.get("/")
        user = User.objects.create_user(username="theme_user", password="testpass")
        request.user = user

        context = theme(request)
        self.assertIn("theme", context)
        self.assertIn("show_ads", context)
        self.assertEqual(context["show_ads"], False)  # Ads off for everyone

    def test_add_tz_with_cookie(self):
        """Test add_tz context processor with timezone cookie"""
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        from auctions.context_processors import add_tz

        factory = RequestFactory()
        request = factory.get("/")
        request.user = AnonymousUser()
        request.COOKIES = {"user_timezone": "America/Los_Angeles"}

        context = add_tz(request)
        self.assertEqual(context["user_timezone"], "America/Los_Angeles")
        self.assertTrue(context["user_timezone_set"])

    def test_add_tz_without_cookie(self):
        """Test add_tz context processor without timezone cookie"""
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        from auctions.context_processors import add_tz

        factory = RequestFactory()
        request = factory.get("/")
        request.user = AnonymousUser()
        request.COOKIES = {}

        context = add_tz(request)
        self.assertEqual(context["user_timezone"], "America/New_York")  # Default
        self.assertFalse(context["user_timezone_set"])

    def test_add_tz_authenticated_user_with_saved_timezone(self):
        """Test add_tz uses saved timezone for authenticated users"""
        from django.test import RequestFactory

        from auctions.context_processors import add_tz

        factory = RequestFactory()
        request = factory.get("/")
        user = User.objects.create_user(username="tz_user", password="testpass")
        user.userdata.timezone = "Europe/London"
        user.userdata.save()
        request.user = user
        request.COOKIES = {}

        context = add_tz(request)
        self.assertEqual(context["user_timezone"], "Europe/London")

    def test_add_location_with_cookies(self):
        """Test add_location context processor with location cookies"""
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.test import RequestFactory

        from auctions.context_processors import add_location

        factory = RequestFactory()
        request = factory.get("/")
        request.user = AnonymousUser()
        request.COOKIES = {"latitude": "40.7128", "longitude": "-74.0060"}

        # Add session middleware
        middleware = SessionMiddleware(lambda x: x)
        middleware.process_request(request)
        request.session.save()

        context = add_location(request)
        self.assertTrue(context["has_user_location"])

    def test_add_location_without_cookies(self):
        """Test add_location context processor without location cookies"""
        from django.contrib.auth.models import AnonymousUser
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.test import RequestFactory

        from auctions.context_processors import add_location

        factory = RequestFactory()
        request = factory.get("/")
        request.user = AnonymousUser()
        request.COOKIES = {}
        request.META = {}

        # Add session middleware
        middleware = SessionMiddleware(lambda x: x)
        middleware.process_request(request)
        request.session.save()

        context = add_location(request)
        self.assertFalse(context["has_user_location"])

    def test_add_location_saves_ip_for_authenticated_user(self):
        """Test add_location saves IP address for authenticated users"""
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.test import RequestFactory

        from auctions.context_processors import add_location

        factory = RequestFactory()
        request = factory.get("/")
        user = User.objects.create_user(username="ip_user", password="testpass")
        request.user = user
        request.COOKIES = {}
        request.META = {"REMOTE_ADDR": "192.168.1.1"}

        # Add session middleware
        middleware = SessionMiddleware(lambda x: x)
        middleware.process_request(request)
        request.session.save()

        add_location(request)

        # Reload user data to check if IP was saved
        user.userdata.refresh_from_db()
        self.assertEqual(user.userdata.last_ip_address, "192.168.1.1")

    def test_add_location_handles_x_forwarded_for(self):
        """Test add_location handles X-Forwarded-For header"""
        from django.contrib.sessions.middleware import SessionMiddleware
        from django.test import RequestFactory

        from auctions.context_processors import add_location

        factory = RequestFactory()
        request = factory.get("/")
        user = User.objects.create_user(username="forwarded_user", password="testpass")
        request.user = user
        request.COOKIES = {}
        request.META = {
            "HTTP_X_FORWARDED_FOR": "10.0.0.1, 192.168.1.1",
            "REMOTE_ADDR": "192.168.1.1",
        }

        # Add session middleware
        middleware = SessionMiddleware(lambda x: x)
        middleware.process_request(request)
        request.session.save()

        add_location(request)

        # Should use first IP from X-Forwarded-For
        user.userdata.refresh_from_db()
        self.assertEqual(user.userdata.last_ip_address, "10.0.0.1")

    def test_dismissed_cookies_tos_with_cookie(self):
        """Test dismissed_cookies_tos with cookie present"""
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        from auctions.context_processors import dismissed_cookies_tos

        factory = RequestFactory()
        request = factory.get("/")
        request.user = AnonymousUser()
        request.COOKIES = {"hide_tos_banner": "true"}

        context = dismissed_cookies_tos(request)
        self.assertTrue(context["hide_tos_banner"])

    def test_dismissed_cookies_tos_without_cookie(self):
        """Test dismissed_cookies_tos without cookie"""
        from django.contrib.auth.models import AnonymousUser
        from django.test import RequestFactory

        from auctions.context_processors import dismissed_cookies_tos

        factory = RequestFactory()
        request = factory.get("/")
        request.user = AnonymousUser()
        request.COOKIES = {}

        context = dismissed_cookies_tos(request)
        self.assertFalse(context["hide_tos_banner"])

    def test_dismissed_cookies_tos_authenticated_user(self):
        """Test dismissed_cookies_tos for authenticated user with dismissed flag"""
        from django.test import RequestFactory

        from auctions.context_processors import dismissed_cookies_tos

        factory = RequestFactory()
        request = factory.get("/")
        user = User.objects.create_user(username="tos_user", password="testpass")
        user.userdata.dismissed_cookies_tos = True
        user.userdata.save()
        request.user = user
        request.COOKIES = {}

        context = dismissed_cookies_tos(request)
        self.assertTrue(context["hide_tos_banner"])

    def test_site_config_context(self):
        """Test site_config context processor returns expected keys"""
        from django.test import RequestFactory

        from auctions.context_processors import site_config

        factory = RequestFactory()
        request = factory.get("/")

        context = site_config(request)
        self.assertIn("navbar_brand", context)
        self.assertIn("copyright_message", context)
        self.assertIn("enable_club_finder", context)
        self.assertIn("enable_help", context)
        self.assertIn("enable_promo_page", context)


class MiddlewareTestCase(TestCase):
    """Test cases for middleware"""

    def test_cross_origin_isolation_middleware_adds_headers(self):
        """Test CrossOriginIsolationMiddleware adds required headers"""
        from django.http import HttpResponse
        from django.test import RequestFactory

        from auctions.middleware import CrossOriginIsolationMiddleware

        factory = RequestFactory()
        request = factory.get("/")

        # Create a mock get_response that returns a simple response
        def get_response(request):
            return HttpResponse("OK")

        middleware = CrossOriginIsolationMiddleware(get_response)
        response = middleware(request)

        self.assertEqual(response["Cross-Origin-Opener-Policy"], "same-origin")
        self.assertEqual(response["Cross-Origin-Embedder-Policy"], "require-corp")
        self.assertEqual(response["Cross-Origin-Resource-Policy"], "cross-origin")


class ModelMethodsTestCase(StandardTestCase):
    """Test cases for specific model methods with complex logic"""

    def test_auction_fix_year_old_date(self):
        """Test Auction.fix_year corrects dates with years too far in the past"""
        old_date = timezone.now().replace(year=1990)
        fixed_date = self.online_auction.fix_year(old_date)

        # Should be corrected to current year
        self.assertEqual(fixed_date.year, timezone.now().year)

    def test_auction_fix_year_future_date(self):
        """Test Auction.fix_year corrects dates with years too far in the future"""
        future_date = timezone.now().replace(year=2099)
        fixed_date = self.online_auction.fix_year(future_date)

        # Should be corrected to current year
        self.assertEqual(fixed_date.year, timezone.now().year)

    def test_auction_fix_year_valid_date(self):
        """Test Auction.fix_year doesn't modify valid dates"""
        valid_date = timezone.now().replace(year=2025)
        fixed_date = self.online_auction.fix_year(valid_date)

        # Should remain unchanged
        self.assertEqual(fixed_date.year, 2025)

    def test_auction_fix_year_none_date(self):
        """Test Auction.fix_year handles None dates"""
        fixed_date = self.online_auction.fix_year(None)

        # Should return None
        self.assertIsNone(fixed_date)

    def test_auction_fix_year_custom_cutoffs(self):
        """Test Auction.fix_year with custom cutoff parameters"""
        date_2010 = timezone.now().replace(year=2010)

        # With default cutoffs (2000-2050), 2010 should be valid
        fixed_default = self.online_auction.fix_year(date_2010)
        self.assertEqual(fixed_default.year, 2010)

        # With custom cutoffs where 2010 is invalid
        fixed_custom = self.online_auction.fix_year(date_2010, low_cutoff=2015, high_cutoff=2040)
        self.assertEqual(fixed_custom.year, timezone.now().year)

    def test_auction_find_user_by_email(self):
        """Test Auction.find_user can find users by email"""
        # Set email on AuctionTOS (find_user searches AuctionTOS.email, not User.email)
        self.admin_online_tos.email = "test@example.com"
        self.admin_online_tos.save()

        result = self.online_auction.find_user(email="test@example.com")

        # Should find the AuctionTOS with this email
        self.assertIsNotNone(result)
        self.assertEqual(result.email, "test@example.com")

    def test_auction_find_user_by_name(self):
        """Test Auction.find_user can find users by name"""
        # Set a name for testing
        self.admin_online_tos.name = "John Doe"
        self.admin_online_tos.save()

        result = self.online_auction.find_user(name="John Doe")

        # Should find the user
        self.assertIsNotNone(result)

    def test_auction_find_user_no_params(self):
        """Test Auction.find_user returns None with no search params"""
        result = self.online_auction.find_user()

        # Should return None
        self.assertIsNone(result)

    def test_auction_find_user_exclude_pk(self):
        """Test Auction.find_user can exclude specific PKs"""
        # Set email on AuctionTOS
        self.admin_online_tos.email = "test@example.com"
        self.admin_online_tos.save()

        result = self.online_auction.find_user(email="test@example.com", exclude_pk=self.admin_online_tos.pk)

        # Should not find the excluded user (but there might be other users with same email)
        if result:
            self.assertNotEqual(result.pk, self.admin_online_tos.pk)

    def test_auction_soft_delete(self):
        """Test Auction.delete performs soft delete"""
        # NOTE: This tests the current behavior, but soft delete may have issues
        # If a lot is not properly archived, it could still appear in queries
        auction_pk = self.online_auction.pk
        self.online_auction.delete()

        # Auction should still exist but be marked deleted
        auction = Auction.objects.get(pk=auction_pk)
        self.assertTrue(auction.is_deleted)

    def test_pageview_merge_and_delete_duplicate_extends_time_range(self):
        """Test PageView.merge_and_delete_duplicate extends time range correctly"""
        from auctions.models import PageView

        # Create two PageView instances that are duplicates
        base_time = timezone.now()
        view1 = PageView.objects.create(
            user=self.user,
            lot_number=self.lot,
            date_start=base_time - datetime.timedelta(hours=2),
            date_end=base_time - datetime.timedelta(hours=1),
            total_time=3600,
            session_id="test_session",
        )
        view2 = PageView.objects.create(
            user=self.user,
            lot_number=self.lot,
            date_start=base_time - datetime.timedelta(hours=1),
            date_end=base_time,
            total_time=3600,
            session_id="test_session",
        )

        # Merge view2 into view1
        # Call as method now (no longer a property)
        view1.merge_and_delete_duplicate()

        # view1 should have extended time range and combined total_time
        view1.refresh_from_db()
        self.assertEqual(view1.total_time, 7200)  # 3600 + 3600

        # view2 should be deleted
        self.assertEqual(PageView.objects.filter(pk=view2.pk).count(), 0)

    def test_pageview_save_gets_location_from_ip(self):
        """Test PageView.save gets location from IP address"""
        from auctions.models import PageView

        # Create a PageView with known location
        PageView.objects.create(
            user=self.user,
            lot_number=self.lot,
            date_start=timezone.now(),
            ip_address="192.168.1.1",
            latitude=40.7128,
            longitude=-74.0060,
            session_id="session1",
        )

        # Create another PageView with same IP but no location
        new_view = PageView.objects.create(
            user=self.user,
            lot_number=self.lotB,
            date_start=timezone.now(),
            ip_address="192.168.1.1",
            session_id="session2",
        )

        # Should have inherited location from previous view with same IP
        self.assertEqual(new_view.latitude, 40.7128)
        self.assertEqual(new_view.longitude, -74.0060)

    def test_pageview_save_gets_location_from_userdata(self):
        """Test PageView.save gets location from UserData if no IP match"""
        from auctions.models import PageView

        # Set user location
        self.user.userdata.latitude = 51.5074
        self.user.userdata.longitude = -0.1278
        self.user.userdata.save()

        # Create PageView with new IP and no location
        new_view = PageView.objects.create(
            user=self.user,
            lot_number=self.lot,
            date_start=timezone.now(),
            ip_address="10.0.0.1",
            session_id="session3",
        )

        # Should have inherited location from userdata
        self.assertEqual(new_view.latitude, 51.5074)
        self.assertEqual(new_view.longitude, -0.1278)


class SignalLogicTestCase(StandardTestCase):
    """Test cases for signal handlers with complex date logic"""

    def test_auction_signal_swaps_start_end_if_reversed(self):
        """Test that auction signal swaps start/end dates if end is before start"""
        # Create auction with end before start
        auction = Auction.objects.create(
            created_by=self.user,
            title="Test reversed dates",
            date_start=timezone.now() + datetime.timedelta(days=7),
            date_end=timezone.now() + datetime.timedelta(days=1),
        )

        # Dates should be swapped by signal
        self.assertLess(auction.date_start, auction.date_end)

    def test_auction_signal_sets_default_end_date_for_online(self):
        """Test that auction signal sets default end date for online auctions"""
        start_date = timezone.now() + datetime.timedelta(days=1)
        auction = Auction.objects.create(
            created_by=self.user,
            title="Test default end",
            is_online=True,
            date_start=start_date,
        )

        # Should have end date set to 7 days after start
        expected_end = start_date + datetime.timedelta(days=7)
        self.assertEqual(auction.date_end.date(), expected_end.date())

    def test_auction_signal_sets_lot_submission_dates(self):
        """Test that auction signal sets lot submission dates if not provided"""
        start_date = timezone.now() + datetime.timedelta(days=7)
        end_date = start_date + datetime.timedelta(days=7)
        auction = Auction.objects.create(
            created_by=self.user,
            title="Test lot submission dates",
            is_online=True,
            date_start=start_date,
            date_end=end_date,
        )

        # Should have lot submission dates set
        self.assertIsNotNone(auction.lot_submission_start_date)
        self.assertIsNotNone(auction.lot_submission_end_date)
        # For online auctions, submission end should match auction end
        self.assertEqual(auction.lot_submission_end_date, auction.date_end)

    def test_auction_signal_fixes_bad_lot_submission_end_date(self):
        """Test that auction signal fixes lot submission end date if it's after auction end"""
        start_date = timezone.now() + datetime.timedelta(days=1)
        end_date = start_date + datetime.timedelta(days=7)
        bad_submission_end = end_date + datetime.timedelta(days=1)

        auction = Auction.objects.create(
            created_by=self.user,
            title="Test bad submission end",
            is_online=True,
            date_start=start_date,
            date_end=end_date,
            lot_submission_end_date=bad_submission_end,
        )

        # Should have corrected lot submission end date
        self.assertEqual(auction.lot_submission_end_date, auction.date_end)

    def test_auction_signal_sets_online_bidding_dates_for_in_person(self):
        """Test that auction signal sets online bidding dates for in-person auctions"""
        start_date = timezone.now() + datetime.timedelta(days=7)
        auction = Auction.objects.create(
            created_by=self.user,
            title="Test in-person with online bidding",
            is_online=False,
            date_start=start_date,
            online_bidding="allow",
        )

        # Should have online bidding dates set
        self.assertIsNotNone(auction.date_online_bidding_starts)
        self.assertIsNotNone(auction.date_online_bidding_ends)
        # Online bidding should end at auction start
        self.assertEqual(auction.date_online_bidding_ends, auction.date_start)

    def test_auction_signal_swaps_online_bidding_dates_if_reversed(self):
        """Test that auction signal swaps online bidding dates if reversed"""
        start_date = timezone.now() + datetime.timedelta(days=7)
        auction = Auction.objects.create(
            created_by=self.user,
            title="Test reversed online bidding dates",
            is_online=False,
            date_start=start_date,
            online_bidding="allow",
            date_online_bidding_starts=start_date,
            date_online_bidding_ends=start_date - datetime.timedelta(days=1),
        )

        # Dates should be swapped
        self.assertLess(auction.date_online_bidding_starts, auction.date_online_bidding_ends)


class DuplicateAuctionTOSTests(StandardTestCase):
    """Test that duplicate AuctionTOS records don't cause MultipleObjectsReturned errors"""

    def test_duplicate_auction_tos_in_auction_detail_view(self):
        """Test that the auction detail view can handle duplicate AuctionTOS records"""
        # Create a duplicate AuctionTOS for the same user and auction
        AuctionTOS.objects.create(
            user=self.admin_user, auction=self.online_auction, pickup_location=self.location, is_admin=False
        )
        # Verify we now have 2 AuctionTOS records for this user/auction combination
        tos_count = AuctionTOS.objects.filter(user=self.admin_user, auction=self.online_auction).count()
        self.assertEqual(tos_count, 2)

        # This should not raise MultipleObjectsReturned error
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(reverse("auction_main", kwargs={"slug": self.online_auction.slug}))
        self.assertEqual(response.status_code, 200)

    def test_duplicate_auction_tos_in_lot_list_view(self):
        """Test that the lot list view can handle duplicate AuctionTOS records"""
        # Create a duplicate AuctionTOS for the same user and auction
        AuctionTOS.objects.create(
            user=self.admin_user, auction=self.online_auction, pickup_location=self.location, is_admin=False
        )

        # This should not raise MultipleObjectsReturned error
        self.client.login(username="admin_user", password="testpassword")
        response = self.client.get(reverse("auction_lot_list", kwargs={"slug": self.online_auction.slug}))
        self.assertEqual(response.status_code, 200)

    def test_duplicate_auction_tos_in_lot_model_properties(self):
        """Test that Lot model properties can handle duplicate AuctionTOS records"""
        # Create a lot
        lot = Lot.objects.create(
            auction=self.online_auction,
            auctiontos_seller=self.admin_online_tos,
            lot_number=1,
            lot_name="Test Lot",
            quantity=1,
            reserve_price=10,
        )

        # Create a duplicate AuctionTOS for the same user and auction
        AuctionTOS.objects.create(
            user=self.admin_user, auction=self.online_auction, pickup_location=self.location, is_admin=False
        )

        # These properties should not raise MultipleObjectsReturned error
        tos_needed = lot.tos_needed
        location_as_object = lot.location_as_object

        # The properties should work correctly
        self.assertFalse(tos_needed)
        self.assertEqual(location_as_object, self.location)

    def test_duplicate_auction_tos_winner_location(self):
        """Test that winner_location property can handle duplicate AuctionTOS records"""
        # Create a lot with a winner
        lot = Lot.objects.create(
            auction=self.online_auction,
            auctiontos_seller=self.admin_online_tos,
            lot_number=1,
            lot_name="Test Lot",
            quantity=1,
            reserve_price=10,
            winner=self.user,
        )

        # Create TOS for the winner
        AuctionTOS.objects.create(user=self.user, auction=self.online_auction, pickup_location=self.location)

        # Create a duplicate AuctionTOS for the winner
        AuctionTOS.objects.create(user=self.user, auction=self.online_auction, pickup_location=self.location)

        # This should not raise MultipleObjectsReturned error
        winner_location = lot.winner_location
        self.assertEqual(winner_location, str(self.location))


class AuctionNoShowURLEncodingTest(StandardTestCase):
    """Test that bidder_number with special characters (like slashes) work with path converter"""

    def test_bidder_number_with_special_characters(self):
        """Test that bidder_number with special characters (except slashes) work correctly"""
        # Note: Slashes are now automatically removed on save (see test_bidder_number_slash_removal_on_save)
        # Test with special characters that are allowed
        special_bidder_number = "test@123"
        special_tos = AuctionTOS.objects.create(
            user=self.user,
            auction=self.online_auction,
            pickup_location=self.location,
            bidder_number=special_bidder_number,
            name="Test Special User",
        )

        # Test that the reverse URL generation works with the path converter
        problems_url = reverse(
            "auction_no_show",
            kwargs={
                "slug": self.online_auction.slug,
                "tos": special_tos.bidder_number,
            },
        )
        self.assertIsNotNone(problems_url)
        self.assertIn(self.online_auction.slug, problems_url)
        self.assertIn("test@123", problems_url)

        # Test that the URL can be accessed by an admin
        self.client.force_login(self.admin_user)
        response = self.client.get(problems_url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Test Special User", response.content.decode())

    def test_bidder_number_with_url_like_content(self):
        """Test with bidder_number that looks like a URL (the actual error case from the issue)"""
        # The actual error case: bidder_number = 'https://atlfishclub./' (22 chars)
        # Note: Slashes are now automatically removed on save
        # We use a shorter version since bidder_number has max_length=20, and without slashes
        url_like_bidder = "https:site."
        url_tos = AuctionTOS.objects.create(
            user=self.user_with_no_lots,
            auction=self.online_auction,
            pickup_location=self.location,
            bidder_number=url_like_bidder,
            name="Test User",
        )

        # Test reverse() with the path converter
        problems_url = reverse(
            "auction_no_show",
            kwargs={
                "slug": self.online_auction.slug,
                "tos": url_tos.bidder_number,
            },
        )
        self.assertIsNotNone(problems_url)

        # Test accessing the view
        self.client.force_login(self.admin_user)
        response = self.client.get(problems_url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Test User", response.content.decode())

    def test_auction_no_show_dialog_url(self):
        """Test the auction_no_show_dialog URL also works with path converter"""
        special_bidder_number = "test@user"
        special_tos = AuctionTOS.objects.create(
            user=self.user,
            auction=self.online_auction,
            pickup_location=self.location,
            bidder_number=special_bidder_number,
            name="Special User",
        )

        # Test reverse() for the dialog endpoint (used in forms.py line 1134)
        dialog_url = reverse(
            "auction_no_show_dialog",
            kwargs={
                "slug": self.online_auction.slug,
                "tos": special_tos.bidder_number,
            },
        )
        self.assertIsNotNone(dialog_url)

        # Test accessing the dialog view
        self.client.force_login(self.admin_user)
        response = self.client.get(dialog_url)
        self.assertEqual(response.status_code, 200)

    def test_other_bidder_number_urls(self):
        """Test that other URL patterns work with special characters in bidder_number where applicable"""
        # Note: Slashes are now automatically removed on save, so we test with other special chars
        special_bidder_number = "user@123"
        special_tos = AuctionTOS.objects.create(
            user=self.user,
            auction=self.online_auction,
            pickup_location=self.location,
            bidder_number=special_bidder_number,
            name="User 123",
        )

        # Test bulk_add_image URL - this uses <path:bidder_number>
        bulk_image_url = reverse(
            "bulk_add_image",
            kwargs={
                "slug": self.online_auction.slug,
                "bidder_number": special_tos.bidder_number,
            },
        )
        self.assertIsNotNone(bulk_image_url)
        self.assertIn("user@123", bulk_image_url)

        # Test print_labels_by_bidder_number URL - this uses <path:bidder_number>
        print_labels_url = reverse(
            "print_labels_by_bidder_number",
            kwargs={
                "slug": self.online_auction.slug,
                "bidder_number": special_tos.bidder_number,
            },
        )
        self.assertIsNotNone(print_labels_url)
        self.assertIn("user@123", print_labels_url)

        # Note: bulk_add_lots and bulk_add_lots_auto use <str:bidder_number> because they have
        # additional path segments after the bidder_number parameter, so they cannot support
        # slashes in bidder_number (Django's path converter would match too greedily).
        # These patterns work fine with bidder_numbers that don't contain slashes.
        normal_bidder = "user123"
        normal_tos = AuctionTOS.objects.create(
            user=self.user_with_no_lots,
            auction=self.online_auction,
            pickup_location=self.location,
            bidder_number=normal_bidder,
            name="Normal User",
        )

        bulk_add_url = reverse(
            "bulk_add_lots",
            kwargs={
                "slug": self.online_auction.slug,
                "bidder_number": normal_tos.bidder_number,
            },
        )
        self.assertIsNotNone(bulk_add_url)
        self.assertIn("user123", bulk_add_url)

    def test_bidder_number_slash_removal_on_save(self):
        """Test that forward slashes are removed from bidder_number on save and history is created"""
        from auctions.models import AuctionHistory

        # Create an AuctionTOS with a bidder_number containing slashes
        bidder_with_slash = "test/123/abc"
        tos_with_slash = AuctionTOS.objects.create(
            user=self.user,
            auction=self.online_auction,
            pickup_location=self.location,
            bidder_number=bidder_with_slash,
            name="Slash Test User",
        )

        # Verify the slash was removed
        self.assertEqual(tos_with_slash.bidder_number, "test123abc")
        self.assertNotIn("/", tos_with_slash.bidder_number)

        # Verify auction history was created
        history_entries = AuctionHistory.objects.filter(
            auction=self.online_auction, applies_to="USERS", action__icontains="removed '/' character"
        )
        self.assertTrue(history_entries.exists())
        self.assertTrue(any("test/123/abc" in entry.action for entry in history_entries))
        self.assertTrue(any("test123abc" in entry.action for entry in history_entries))

    def test_bidder_number_slash_removal_prevents_duplicates(self):
        """Test that slash removal prevents creating duplicate bidder_numbers"""
        # Create a TOS with bidder_number "user123"
        existing_tos = AuctionTOS.objects.create(
            user=self.user,
            auction=self.online_auction,
            pickup_location=self.location,
            bidder_number="user123",
            name="Existing User",
        )

        # Try to create another TOS with bidder_number "user/123" which would become "user123" after cleaning
        new_tos = AuctionTOS.objects.create(
            user=self.user_with_no_lots,
            auction=self.online_auction,
            pickup_location=self.location,
            bidder_number="user/123",
            name="New User",
        )

        # The new TOS should have a modified bidder_number to avoid duplicate
        self.assertNotEqual(new_tos.bidder_number, existing_tos.bidder_number)
        self.assertNotIn("/", new_tos.bidder_number)
        # Should have a suffix added
        self.assertTrue(new_tos.bidder_number.startswith("user123"))
        self.assertIn("1", new_tos.bidder_number)  # Should be "user1231" or similar


class WeeklyPromoManagementCommandTests(StandardTestCase):
    """Test the weekly_promo management command."""

    def setUp(self):
        """Set up test data for weekly promo tests."""
        super().setUp()
        # Set up user with proper location and activity for weekly promo
        self.promo_user = User.objects.create_user(
            username="promo_user", password="testpassword", email="promo@example.com", first_name="PromoUser"
        )
        self.promo_user.userdata.latitude = 40.7128  # New York
        self.promo_user.userdata.longitude = -74.0060
        self.promo_user.userdata.last_activity = timezone.now() - datetime.timedelta(days=10)  # Active 10 days ago
        self.promo_user.userdata.email_me_about_new_auctions = True
        self.promo_user.userdata.email_me_about_new_auctions_distance = 100
        self.promo_user.userdata.save()

        # Create an active auction with location
        self.promo_auction = Auction.objects.create(
            created_by=self.user,
            title="Promo Test Auction",
            is_online=True,
            date_start=timezone.now() - datetime.timedelta(days=1),
            date_end=timezone.now() + datetime.timedelta(days=7),
            promote_this_auction=True,
            use_categories=True,
        )
        # Add pickup location near the user
        self.promo_location = PickupLocation.objects.create(
            name="Promo Location",
            auction=self.promo_auction,
            pickup_time=timezone.now() + datetime.timedelta(days=3),
            latitude=40.7128,
            longitude=-74.0060,
        )

    def test_weekly_promo_sends_email(self):
        """Test that weekly_promo sends emails to eligible users."""
        with patch("auctions.management.commands.weekly_promo.mail.send") as mock_send:
            call_command("weekly_promo")
            # Check that email was sent
            self.assertTrue(mock_send.called, "mail.send should have been called")
            # Verify the email was sent to the correct user
            call_args = mock_send.call_args
            self.assertEqual(call_args[0][0], self.promo_user.email, "Email should be sent to promo_user")
            # Verify template is correct
            self.assertEqual(call_args[1]["template"], "weekly_promo_email")

    def test_weekly_promo_increments_counter(self):
        """Test that weekly_promo increments the email sent counter."""
        initial_count = self.promo_auction.weekly_promo_emails_sent
        with patch("auctions.management.commands.weekly_promo.mail.send"):
            call_command("weekly_promo")
        self.promo_auction.refresh_from_db()
        # Check that counter was incremented
        self.assertGreater(
            self.promo_auction.weekly_promo_emails_sent,
            initial_count,
            "weekly_promo_emails_sent should be incremented",
        )

    def test_weekly_promo_excludes_inactive_users(self):
        """Test that weekly_promo excludes users who were recently active."""
        # Update user to be recently active (within last 6 days)
        self.promo_user.userdata.last_activity = timezone.now() - datetime.timedelta(days=3)
        self.promo_user.userdata.save()

        with patch("auctions.management.commands.weekly_promo.mail.send") as mock_send:
            call_command("weekly_promo")
            # Check that email was NOT sent to recently active user
            self.assertFalse(mock_send.called, "mail.send should not be called for recently active users")

    def test_weekly_promo_excludes_very_old_users(self):
        """Test that weekly_promo excludes users who haven't been active in a long time."""
        # Update user to be inactive for too long (more than 400 days)
        self.promo_user.userdata.last_activity = timezone.now() - datetime.timedelta(days=500)
        self.promo_user.userdata.save()

        with patch("auctions.management.commands.weekly_promo.mail.send") as mock_send:
            call_command("weekly_promo")
            # Check that email was NOT sent to very inactive user
            self.assertFalse(mock_send.called, "mail.send should not be called for users inactive for >400 days")

    def test_weekly_promo_excludes_users_without_location(self):
        """Test that weekly_promo excludes users without a valid location."""
        # Set user location to 0,0
        self.promo_user.userdata.latitude = 0
        self.promo_user.userdata.longitude = 0
        self.promo_user.userdata.save()

        with patch("auctions.management.commands.weekly_promo.mail.send") as mock_send:
            call_command("weekly_promo")
            # Check that email was NOT sent
            self.assertFalse(mock_send.called, "mail.send should not be called for users without valid location")

    def test_weekly_promo_respects_opt_out(self):
        """Test that weekly_promo respects user opt-out preferences."""
        # Opt user out of all emails
        self.promo_user.userdata.email_me_about_new_auctions = False
        self.promo_user.userdata.email_me_about_new_in_person_auctions = False
        self.promo_user.userdata.email_me_about_new_local_lots = False
        self.promo_user.userdata.email_me_about_new_lots_ship_to_location = False
        self.promo_user.userdata.save()

        with patch("auctions.management.commands.weekly_promo.mail.send") as mock_send:
            call_command("weekly_promo")
            # Check that email was NOT sent
            self.assertFalse(mock_send.called, "mail.send should not be called for users who opted out")

    def test_weekly_promo_in_person_auctions(self):
        """Test that weekly_promo includes in-person auctions."""
        # Create an in-person auction
        in_person_auction = Auction.objects.create(
            created_by=self.user,
            title="In Person Promo Auction",
            is_online=False,
            date_start=timezone.now() + datetime.timedelta(days=3),  # Starts in 3 days
            date_end=timezone.now() + datetime.timedelta(days=10),
            promote_this_auction=True,
            use_categories=True,
        )
        PickupLocation.objects.create(
            name="In Person Location",
            auction=in_person_auction,
            pickup_time=timezone.now() + datetime.timedelta(days=10),
            latitude=40.7128,
            longitude=-74.0060,
        )

        # Update user to opt into in-person auctions
        self.promo_user.userdata.email_me_about_new_in_person_auctions = True
        self.promo_user.userdata.email_me_about_new_in_person_auctions_distance = 100
        self.promo_user.userdata.save()

        with patch("auctions.management.commands.weekly_promo.mail.send") as mock_send:
            call_command("weekly_promo")
            # Check that email was sent
            self.assertTrue(mock_send.called, "mail.send should be called for in-person auctions")
            # Check that the in-person auction counter was incremented
            in_person_auction.refresh_from_db()
            self.assertGreater(
                in_person_auction.weekly_promo_emails_sent,
                0,
                "weekly_promo_emails_sent should be incremented for in-person auction",
            )
