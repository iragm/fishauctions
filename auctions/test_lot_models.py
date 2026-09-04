"""Lot and auction model behaviour, and the chat subscriptions hanging off a lot."""

import datetime

from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionTOS,
    Bid,
    Category,
    ChatSubscription,
    Lot,
    LotHistory,
    PickupLocation,
)


class ViewLotTest(TestCase):
    def setUp(self):
        time = timezone.now() - datetime.timedelta(days=2)
        timeStart = timezone.now() - datetime.timedelta(days=3)
        the_future = timezone.now() + datetime.timedelta(days=3)
        self.auction = Auction.objects.create(title="A test auction", date_end=time, date_start=timeStart)
        self.location = PickupLocation.objects.create(name="location", auction=self.auction, pickup_time=the_future)
        self.user = User.objects.create_user(username="my_lot", password="testpassword")
        self.userB = User.objects.create_user(username="no_tos", password="testpassword")
        self.tos = AuctionTOS.objects.create(user=self.user, auction=self.auction, pickup_location=self.location)
        self.lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=the_future,
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

    def test_custom_dropdown_displays_on_lot_views(self):
        self.auction.use_custom_dropdown_field = "allow"
        self.auction.custom_dropdown_name = "Habitat"
        self.auction.save()
        self.lot.custom_dropdown = "River"
        self.lot.custom_lot_number = "101"
        self.lot.save()

        response = self.client.get(self.url)
        self.assertContains(response, "Habitat:")
        self.assertContains(response, "River")


class AuctionModelTests(TestCase):
    """Test for the auction model, duh"""

    def test_lots_in_auction_end_with_auction(self):
        time = timezone.now() - datetime.timedelta(days=2)
        timeStart = timezone.now() - datetime.timedelta(days=3)
        the_future = timezone.now() + datetime.timedelta(days=3)
        auction = Auction.objects.create(title="A test auction", date_end=time, date_start=timeStart)
        user = User.objects.create(username="Test user")
        lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=the_future,
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

    def test_lot_multiple_bids_per_user_only_latest_counts(self):
        """When a user has multiple bid records for a lot, only their latest (highest) bid should count"""
        time = timezone.now() + datetime.timedelta(days=30)
        lotuser = User.objects.create(username="lotowner_multi")
        lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=time,
            reserve_price=5,
            user=lotuser,
            quantity=1,
        )
        userA = User.objects.create(username="User A multi")
        userB = User.objects.create(username="User B multi")
        # userA places an initial proxy bid
        Bid.objects.create(user=userA, lot_number=lot, amount=10)
        assert lot.high_bidder.pk == userA.pk
        assert lot.high_bid == 5  # only one bid, returns reserve_price
        # userB bids the same amount as userA's proxy
        Bid.objects.create(user=userB, lot_number=lot, amount=10)
        assert lot.high_bidder.pk == userA.pk  # tied, userA bid first
        assert lot.high_bid == 10  # tied
        # userA raises proxy bid - new record created, old record kept
        Bid.objects.create(user=userA, lot_number=lot, amount=15)
        assert lot.high_bidder.pk == userA.pk
        assert lot.high_bid == 11  # $10 + 1 (one more than userB's $10)
        # Verify the old bid record still exists (kept, not deleted)
        assert Bid.objects.filter(user=userA, lot_number=lot, is_deleted=False).count() == 2

    def test_bid_on_lot_creates_new_record_not_update(self):
        """bid_on_lot should create a new bid record when a user raises their proxy bid, not update the old one"""
        from auctions.bidding import bid_on_lot

        time = timezone.now() + datetime.timedelta(days=30)
        pastTime = timezone.now() - datetime.timedelta(hours=1)
        lotuser = User.objects.create_user(username="lotowner_bidtest", password="x")
        category = Category.objects.create(name="Test Category bidtest")
        lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=time,
            reserve_price=5,
            user=lotuser,
            quantity=1,
            species_category=category,
        )
        # Backdate date_posted so the lot is old enough to accept bids (>20 minutes)
        lot.date_posted = pastTime
        lot.save()
        userA = User.objects.create_user(username="User_A_bidtest", password="x")
        userB = User.objects.create_user(username="User_B_bidtest", password="x")
        # userA places initial proxy bid
        bid_on_lot(lot, userA, 10)
        assert Bid.objects.filter(user=userA, lot_number=lot, is_deleted=False).count() == 1
        # userB bids the same amount as userA's proxy bid
        bid_on_lot(lot, userB, 10)
        assert Bid.objects.filter(user=userB, lot_number=lot, is_deleted=False).count() == 1
        assert lot.high_bidder.pk == userA.pk
        # userA raises their proxy bid - should create a NEW record, not update the old one
        bid_on_lot(lot, userA, 15)
        userA_bids = Bid.objects.filter(user=userA, lot_number=lot, is_deleted=False)
        assert userA_bids.count() == 2, "userA should have 2 bid records (old + new), not 1 updated record"
        assert userA_bids.order_by("-bid_time").first().amount == 15
        assert lot.high_bidder.pk == userA.pk
        assert lot.high_bid == 11  # $10 + 1 (one more than userB's $10)

    def test_sealed_bid_creates_exactly_one_record_per_bid(self):
        """For sealed bids, each call to bid_on_lot should create exactly one bid record (no duplicates)"""
        from auctions.bidding import bid_on_lot

        time = timezone.now() + datetime.timedelta(days=30)
        timeStart = timezone.now() - datetime.timedelta(days=1)
        pastTime = timezone.now() - datetime.timedelta(hours=1)
        lotuser = User.objects.create_user(username="lotowner_sealed", password="x")
        category = Category.objects.create(name="Test Category sealed")
        auction = Auction.objects.create(title="Sealed auction", date_end=time, date_start=timeStart, sealed_bid=True)
        location = PickupLocation.objects.create(name="location", auction=auction, pickup_time=time)
        lot = Lot.objects.create(
            lot_name="A sealed test lot",
            date_end=time,
            reserve_price=5,
            user=lotuser,
            quantity=1,
            species_category=category,
            auction=auction,
        )
        lot.date_posted = pastTime
        lot.save()
        userA = User.objects.create_user(username="User_A_sealed", password="x")
        AuctionTOS.objects.create(user=lotuser, auction=auction, pickup_location=location)
        AuctionTOS.objects.create(user=userA, auction=auction, pickup_location=location)
        # First bid by userA — should create exactly 1 record
        bid_on_lot(lot, userA, 10)
        assert Bid.objects.filter(user=userA, lot_number=lot, is_deleted=False).count() == 1
        # Second bid by userA (raising proxy) — should add 1 more record, total 2
        bid_on_lot(lot, userA, 15)
        assert Bid.objects.filter(user=userA, lot_number=lot, is_deleted=False).count() == 2

    def test_user_cannot_bid_against_themselves(self):
        """A user who is already the high bidder should raise their proxy bid silently (INFO),
        not generate a NEW_HIGH_BIDDER event — i.e., they cannot bid against themselves."""
        from auctions.bidding import bid_on_lot

        time = timezone.now() + datetime.timedelta(days=30)
        pastTime = timezone.now() - datetime.timedelta(hours=1)
        lotuser = User.objects.create_user(username="lotowner_selfbid", password="x")
        category = Category.objects.create(name="Test Category selfbid")
        lot = Lot.objects.create(
            lot_name="A test lot selfbid",
            date_end=time,
            reserve_price=5,
            user=lotuser,
            quantity=1,
            species_category=category,
        )
        lot.date_posted = pastTime
        lot.save()
        userA = User.objects.create_user(username="User_A_selfbid", password="x")
        userB = User.objects.create_user(username="User_B_selfbid", password="x")
        # userA places the first bid and becomes high bidder
        result = bid_on_lot(lot, userA, 10)
        assert result["type"] == "NEW_HIGH_BIDDER"
        assert lot.high_bidder.pk == userA.pk
        # userB places a competing bid, raising the price
        bid_on_lot(lot, userB, 10)
        assert lot.high_bidder.pk == userA.pk  # userA still wins (first bid)
        # userA raises their proxy bid — they are already the high bidder
        # This should be an INFO message, NOT a NEW_HIGH_BIDDER event
        result = bid_on_lot(lot, userA, 20)
        assert result["type"] == "INFO", "Raising proxy while already high bidder should be INFO, not NEW_HIGH_BIDDER"
        assert lot.high_bidder.pk == userA.pk
        # Confirm two bid records exist for userA (original + raised proxy)
        assert Bid.objects.filter(user=userA, lot_number=lot, is_deleted=False).count() == 2


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
