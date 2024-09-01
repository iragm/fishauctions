import datetime

from django.contrib.auth.models import User
from django.test import TestCase
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from .models import (
    Auction,
    AuctionTOS,
    Bid,
    ChatSubscription,
    Invoice,
    InvoiceAdjustment,
    Lot,
    LotHistory,
    PickupLocation,
    add_price_info,
)


class StandardTestCase(TestCase):
    """This is a base class that sets up some common stuff so other tests can be run without needing to write a lot of boilplate code
    Give this class along with your view/model/etc., to ChatGPT and it can write the test subclass
    In general, make sure that AuctionTOS.is_admin=True users can do what they need, users without an AuctionTOS are blocked, no data leaks to non-admins and non-logged in users

    Tests can be run with with docker exec -it django python3 manage.py test

    Tests are also run automatically on commit by github actions
    """

    def setUp(self):
        time = timezone.now() - datetime.timedelta(days=2)
        timeStart = timezone.now() - datetime.timedelta(days=3)
        theFuture = timezone.now() + datetime.timedelta(days=3)
        self.user = User.objects.create_user(
            username="my_lot", password="testpassword", email="test@example.com"
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
        )
        self.location = PickupLocation.objects.create(
            name="location", auction=self.online_auction, pickup_time=theFuture
        )
        self.userB = User.objects.create_user(
            username="no_tos", password="testpassword"
        )
        self.tos = AuctionTOS.objects.create(
            user=self.user, auction=self.online_auction, pickup_location=self.location
        )
        self.tosB = AuctionTOS.objects.create(
            user=self.userB, auction=self.online_auction, pickup_location=self.location
        )
        self.lot = Lot.objects.create(
            lot_name="A test lot",
            auction=self.online_auction,
            auctiontos_seller=self.tos,
            quantity=1,
            description="",
            winning_price=10,
            auctiontos_winner=self.tosB,
            active=False,
        )
        self.lotB = Lot.objects.create(
            lot_name="B test lot",
            auction=self.online_auction,
            auctiontos_seller=self.tos,
            quantity=1,
            description="",
            winning_price=10,
            auctiontos_winner=self.tosB,
            active=False,
        )
        self.lotC = Lot.objects.create(
            lot_name="C test lot",
            auction=self.online_auction,
            auctiontos_seller=self.tos,
            quantity=1,
            description="",
            winning_price=10,
            auctiontos_winner=self.tosB,
            active=False,
        )
        self.unsoldLot = Lot.objects.create(
            lot_name="Unsold lot",
            reserve_price=10,
            description="",
            auction=self.online_auction,
            quantity=1,
            auctiontos_seller=self.tos,
            active=False,
        )
        self.invoice = Invoice.objects.create(auctiontos_user=self.tos)
        self.invoiceB = Invoice.objects.create(auctiontos_user=self.tosB)
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
        # TODO: stuff to add here:
        # a normal user that has joined no auctions
        # a user that has joined self.online_auction
        # a user that is an admin for both auctions (tos.is_admin=True)
        # lots in the in-person auction
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
        self.auction = Auction.objects.create(
            title="A test auction", date_end=time, date_start=timeStart
        )
        self.location = PickupLocation.objects.create(
            name="location", auction=self.auction, pickup_time=theFuture
        )
        self.user = User.objects.create_user(username="my_lot", password="testpassword")
        self.userB = User.objects.create_user(
            username="no_tos", password="testpassword"
        )
        self.tos = AuctionTOS.objects.create(
            user=self.user, auction=self.auction, pickup_location=self.location
        )
        self.lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=theFuture,
            reserve_price=5,
            auction=self.auction,
            user=self.user,
            quantity=1,
            description="",
        )
        self.url = reverse("lot_by_pk", kwargs={"pk": self.lot.pk})
        # Create a user for the logged-in scenario
        self.userC = User.objects.create_user(
            username="testuser", password="testpassword"
        )

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

    def test_with_tos_on_new_lot(self):
        AuctionTOS.objects.create(
            user=self.userB, auction=self.auction, pickup_location=self.location
        )
        self.client.login(username="no_tos", password="testpassword")
        response = self.client.get(self.url)
        self.assertContains(response, "This lot is very new")


class AuctionModelTests(TestCase):
    """Test for the auction model, duh"""

    def test_lots_in_auction_end_with_auction(self):
        time = timezone.now() - datetime.timedelta(days=2)
        timeStart = timezone.now() - datetime.timedelta(days=3)
        theFuture = timezone.now() + datetime.timedelta(days=3)
        auction = Auction.objects.create(
            title="A test auction", date_end=time, date_start=timeStart
        )
        user = User.objects.create(username="Test user")
        lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=theFuture,
            reserve_price=5,
            auction=auction,
            user=user,
            quantity=1,
            description="",
        )
        self.assertIs(lot.ended, True)

    def test_auction_start_and_end(self):
        timeStart = timezone.now() - datetime.timedelta(days=2)
        timeEnd = timezone.now() + datetime.timedelta(minutes=60)
        auction = Auction.objects.create(
            title="A test auction", date_end=timeEnd, date_start=timeStart
        )
        self.assertIs(auction.closed, False)
        self.assertIs(auction.ending_soon, True)
        self.assertIs(auction.started, True)


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
            description="",
        )
        self.assertIs(testLot.ended, False)

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
            description="",
        )
        self.assertIs(testLot.ended, True)

    def test_lot_with_no_bids(self):
        time = timezone.now() + datetime.timedelta(days=30)
        user = User.objects.create(username="Test user")
        lot = Lot(
            lot_name="A lot with no bids",
            date_end=time,
            reserve_price=5,
            user=user,
            description="",
        )
        self.assertIs(lot.high_bid, 5)

    def test_lot_with_one_bids(self):
        time = timezone.now() + datetime.timedelta(days=30)
        lotuser = User.objects.create(username="thisismylot")
        lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=time,
            reserve_price=5,
            description="",
            user=lotuser,
            quantity=1,
        )
        user = User.objects.create(username="Test user")
        Bid.objects.create(user=user, lot_number=lot, amount=10)
        self.assertIs(lot.high_bidder.pk, user.pk)
        self.assertIs(lot.high_bid, 5)

    def test_lot_with_two_bids(self):
        time = timezone.now() + datetime.timedelta(days=30)
        lotuser = User.objects.create(username="thisismylot")
        lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=time,
            reserve_price=5,
            description="",
            user=lotuser,
            quantity=1,
        )
        userA = User.objects.create(username="Test user")
        userB = User.objects.create(username="Test user B")
        Bid.objects.create(user=userA, lot_number=lot, amount=10)
        Bid.objects.create(user=userB, lot_number=lot, amount=6)
        self.assertIs(lot.high_bidder.pk, userA.pk)
        self.assertIs(lot.high_bid, 7)

    def test_lot_with_two_changing_bids(self):
        time = timezone.now() + datetime.timedelta(days=30)
        lotuser = User.objects.create(username="thisismylot")
        lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=time,
            reserve_price=20,
            description="",
            user=lotuser,
            quantity=6,
        )
        jeff = User.objects.create(username="Jeff")
        gary = User.objects.create(username="Gary")
        jeffBid = Bid.objects.create(user=jeff, lot_number=lot, amount=20)
        self.assertIs(lot.high_bidder.pk, jeff.pk)
        self.assertIs(lot.high_bid, 20)
        garyBid = Bid.objects.create(user=gary, lot_number=lot, amount=20)
        self.assertIs(lot.high_bidder.pk, jeff.pk)
        self.assertIs(lot.high_bid, 20)
        # check the order
        jeffBid.last_bid_time = timezone.now()
        jeffBid.save()
        self.assertIs(lot.high_bidder.pk, gary.pk)
        self.assertIs(lot.high_bid, 20)
        garyBid.amount = 30
        garyBid.save()
        self.assertIs(lot.high_bidder.pk, gary.pk)
        self.assertIs(lot.high_bid, 21)
        garyBid.last_bid_time = timezone.now()
        garyBid.save()
        self.assertIs(lot.high_bidder.pk, gary.pk)
        self.assertIs(lot.high_bid, 21)
        jeffBid.amount = 30
        jeffBid.last_bid_time = timezone.now()
        jeffBid.save()
        self.assertIs(lot.high_bidder.pk, gary.pk)
        self.assertIs(lot.high_bid, 30)

    def test_lot_with_tie_bids(self):
        time = timezone.now() + datetime.timedelta(days=30)
        tenDaysAgo = timezone.now() - datetime.timedelta(days=10)
        fiveDaysAgo = timezone.now() - datetime.timedelta(days=5)
        lotuser = User.objects.create(username="thisismylot")
        lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=time,
            reserve_price=5,
            description="",
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
        self.assertIs(lot.high_bidder.pk, userB.pk)
        self.assertIs(lot.high_bid, 6)
        self.assertIs(lot.max_bid, 6)

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
            description="",
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
        self.assertIs(lot.high_bidder.pk, userB.pk)
        self.assertIs(lot.high_bid, 7)
        self.assertIs(lot.max_bid, 7)

    def test_lot_with_two_bids_one_after_end(self):
        time = timezone.now() + datetime.timedelta(days=30)
        afterEndTime = timezone.now() + datetime.timedelta(days=31)
        lotuser = User.objects.create(username="thisismylot")
        lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=time,
            reserve_price=5,
            description="",
            user=lotuser,
            quantity=1,
        )
        userA = User.objects.create(username="Test user")
        userB = User.objects.create(username="Test user B")
        bidA = Bid.objects.create(user=userA, lot_number=lot, amount=10)
        bidA.last_bid_time = afterEndTime
        bidA.save()
        Bid.objects.create(user=userB, lot_number=lot, amount=6)
        self.assertIs(lot.high_bidder.pk, userB.pk)
        self.assertIs(lot.high_bid, 5)

    def test_lot_with_one_bids_below_reserve(self):
        time = timezone.now() + datetime.timedelta(days=30)
        lotuser = User.objects.create(username="thisismylot")
        lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=time,
            reserve_price=5,
            description="",
            user=lotuser,
            quantity=1,
        )
        user = User.objects.create(username="Test user")
        Bid.objects.create(user=user, lot_number=lot, amount=2)
        self.assertIs(lot.high_bidder, False)
        self.assertIs(lot.high_bid, 5)


class ChatSubscriptionTests(TestCase):
    def test_chat_subscriptions(self):
        lotuser = User.objects.create(username="thisismylot")
        chatuser = User.objects.create(username="ichatonlots")
        my_lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=timezone.now() + datetime.timedelta(days=30),
            reserve_price=5,
            description="",
            user=lotuser,
            quantity=1,
        )
        my_lot_that_i_have_seen_all = Lot.objects.create(
            lot_name="seen all",
            date_end=timezone.now() + datetime.timedelta(days=30),
            reserve_price=5,
            description="",
            user=lotuser,
            quantity=1,
        )
        someone_elses_lot = Lot.objects.create(
            lot_name="Another test lot",
            date_end=timezone.now() + datetime.timedelta(days=30),
            reserve_price=5,
            description="",
            user=chatuser,
            quantity=1,
        )
        my_lot_that_is_unsubscribed = Lot.objects.create(
            lot_name="An unsubscribed lot",
            date_end=timezone.now() + datetime.timedelta(days=30),
            reserve_price=5,
            description="",
            user=lotuser,
            quantity=1,
        )
        sub = ChatSubscription.objects.get(lot=my_lot, user=lotuser)
        sub.last_seen = timezone.now() + datetime.timedelta(minutes=15)
        sub.save()
        sub = ChatSubscription.objects.get(
            lot=my_lot_that_is_unsubscribed, user=lotuser
        )
        sub.unsubscribed = True
        sub.save()
        ChatSubscription.objects.create(lot=someone_elses_lot, user=lotuser)
        data = lotuser.userdata
        self.assertIs(data.unnotified_subscriptions_count, 0)
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
        self.assertIs(data.subscriptions.count(), 3)
        self.assertIs(data.my_lot_subscriptions_count, 0)
        self.assertIs(data.other_lot_subscriptions_count, 0)
        self.assertIs(data.unnotified_subscriptions_count, 0)
        history = LotHistory.objects.create(
            user=chatuser,
            lot=my_lot,
            message="a chat in the future",
            changed_price=False,
        )
        history.timestamp = ten_minutes_in_the_future
        history.save()
        self.assertIs(data.unnotified_subscriptions_count, 0)
        history = LotHistory.objects.create(
            user=chatuser,
            lot=my_lot,
            message="a chat in the far future",
            changed_price=False,
        )
        history.timestamp = twenty_minutes_in_the_future
        history.save()
        self.assertIs(data.unnotified_subscriptions_count, 1)
        history = LotHistory.objects.create(
            user=chatuser,
            lot=someone_elses_lot,
            message="a chat in the far future",
            changed_price=False,
        )
        history.timestamp = twenty_minutes_in_the_future
        history.save()
        self.assertIs(data.other_lot_subscriptions_count, 1)
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
        self.assertIs(data.my_lot_subscriptions_count, 1)
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
        self.assertIs(data.my_lot_subscriptions_count, 1)
        self.assertIs(data.other_lot_subscriptions_count, 1)


class InvoiceModelTests(StandardTestCase):
    def test_invoices(self):
        self.assertEqual(self.invoice.auction, self.online_auction)

        self.assertEqual(self.invoiceB.flat_value_adjustments, 0)
        self.assertEqual(self.invoiceB.percent_value_adjustments, 0)

        self.assertEqual(self.invoiceB.total_sold, 0)
        self.assertEqual(self.invoiceB.total_bought, 30)
        self.assertEqual(self.invoiceB.subtotal, -30)
        self.assertEqual(self.invoiceB.tax, 7.5)
        self.assertEqual(self.invoiceB.net, -37.5)
        self.assertEqual(self.invoiceB.rounded_net, -37)
        self.assertEqual(self.invoiceB.absolute_amount, 37)
        self.assertEqual(self.invoiceB.lots_sold, 0)
        self.assertEqual(self.invoiceB.lots_sold_successfully_count, 0)
        self.assertEqual(self.invoiceB.unsold_lots, 0)
        self.assertEqual(self.invoiceB.lots_bought, 3)

        self.assertEqual(self.invoice.total_sold, 6.5)
        self.assertEqual(self.invoice.total_bought, 0)
        self.assertEqual(self.invoice.subtotal, 6.5)
        self.assertEqual(self.invoice.tax, 0)
        self.assertEqual(self.invoice.net, 6.5)
        self.assertEqual(self.invoice.rounded_net, 7)
        self.assertEqual(self.invoice.absolute_amount, 7)
        self.assertEqual(self.invoice.lots_sold, 4)
        self.assertEqual(self.invoice.lots_sold_successfully_count, 3)
        self.assertEqual(self.invoice.unsold_lots, 1)
        self.assertEqual(self.invoice.lots_bought, 0)
        self.assertEqual(self.invoiceB.location, self.location)
        self.assertEqual(self.invoiceB.contact_email, "test@example.com")
        self.assertTrue(self.invoiceB.is_online)
        self.assertEqual(self.invoiceB.unsold_lot_warning, "")
        self.assertEqual(
            str(self.invoice), f"{self.tos.name}'s invoice for {self.tos.auction}"
        )

        # adjustments
        self.adjustment_add.amount = 0
        self.adjustment_add.save()
        self.assertEqual(self.invoiceB.net, -27.5)
        self.adjustment_discount.amount = 0
        self.adjustment_discount.save()
        self.assertEqual(self.invoiceB.net, -37.5)
        self.adjustment_add_percent.amount = 0
        self.adjustment_add_percent.save()
        self.assertEqual(self.invoiceB.net, -34.5)
        self.adjustment_discount_percent.amount = 0
        self.adjustment_discount_percent.save()
        self.assertEqual(self.invoiceB.net, -37.5)


class LotPricesTests(TestCase):
    def setUp(self):
        time = timezone.now() - datetime.timedelta(days=2)
        timeStart = timezone.now() - datetime.timedelta(days=3)
        theFuture = timezone.now() + datetime.timedelta(days=3)
        self.user = User.objects.create_user(
            username="my_lot", password="testpassword", email="test@example.com"
        )
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
        self.location = PickupLocation.objects.create(
            name="location", auction=self.auction, pickup_time=theFuture
        )
        self.userB = User.objects.create_user(
            username="no_tos", password="testpassword"
        )
        self.tos = AuctionTOS.objects.create(
            user=self.user, auction=self.auction, pickup_location=self.location
        )
        self.tosB = AuctionTOS.objects.create(
            user=self.userB, auction=self.auction, pickup_location=self.location
        )
        self.lot = Lot.objects.create(
            lot_name="A test lot",
            auction=self.auction,
            auctiontos_seller=self.tos,
            quantity=1,
            description="",
            winning_price=10,
            auctiontos_winner=self.tosB,
            active=False,
        )
        self.unsold_lot = Lot.objects.create(
            lot_name="Unsold lot",
            reserve_price=10,
            description="",
            auction=self.auction,
            quantity=1,
            auctiontos_seller=self.tos,
            active=False,
        )
        self.sold_no_auction_lot = Lot.objects.create(
            lot_name="not in the auction",
            reserve_price=10,
            description="",
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
            description="",
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
        self.assertEqual(lot.your_cut, 5.5)
        unsold_lot = lots.filter(pk=self.unsold_lot.pk).first()
        self.assertEqual(unsold_lot.your_cut, -10)
        sold_no_auction_lot = lots.filter(pk=self.sold_no_auction_lot.pk).first()
        self.assertEqual(sold_no_auction_lot.your_cut, 10)
        unsold_no_auction_lot = lots.filter(pk=self.unsold_no_auction_lot.pk).first()
        self.assertEqual(unsold_no_auction_lot.your_cut, 0)

        self.auction.winning_bid_percent_to_club = 50
        self.auction.winning_bid_percent_to_club_for_club_members = 0
        self.auction.save()
        lot = lots.filter(pk=self.lot.pk).first()
        self.assertEqual(lot.your_cut, 3.0)
        unsold_lot = lots.filter(pk=self.unsold_lot.pk).first()
        self.assertEqual(unsold_lot.your_cut, -10)

        self.tos.is_club_member = True
        self.tos.save()
        lot = lots.filter(pk=self.lot.pk).first()
        self.assertEqual(lot.your_cut, 10)
        # print(lot.winning_price, lot.your_cut)
        unsold_lot = lots.filter(pk=self.unsold_lot.pk).first()
        self.assertEqual(unsold_lot.your_cut, -10)

        self.auction.winning_bid_percent_to_club_for_club_members = 50
        self.auction.pre_register_lot_discount_percent = 10
        self.auction.save()
        lot = lots.filter(pk=self.lot.pk).first()
        self.assertEqual(lot.your_cut, 5)
        unsold_lot = lots.filter(pk=self.unsold_lot.pk).first()
        self.assertEqual(unsold_lot.your_cut, -10)

        self.auction.lot_entry_fee_for_club_members = 1
        self.auction.save()
        lot = lots.filter(pk=self.lot.pk).first()
        self.assertEqual(lot.your_cut, 4)
        unsold_lot = lots.filter(pk=self.unsold_lot.pk).first()
        self.assertEqual(unsold_lot.your_cut, -10)

        self.lot.partial_refund_percent = 25
        self.lot.save()
        self.unsold_lot.partial_refund_percent = 25
        self.unsold_lot.save()

        lot = lots.filter(pk=self.lot.pk).first()
        self.assertEqual(lot.your_cut, 3.0)
        unsold_lot = lots.filter(pk=self.unsold_lot.pk).first()
        self.assertEqual(unsold_lot.your_cut, -10)

        self.lot.donation = True
        self.lot.save()
        lot = lots.filter(pk=self.lot.pk).first()
        self.assertEqual(lot.your_cut, 0)


class SetLotWinnerViewTest(TestCase):
    def setUp(self):
        time = timezone.now() - datetime.timedelta(days=2)
        timeStart = timezone.now() - datetime.timedelta(days=3)
        theFuture = timezone.now() + datetime.timedelta(days=3)
        self.user = User.objects.create_user(
            username="testuser", password="testpassword"
        )
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
        self.location = PickupLocation.objects.create(
            name="location", auction=self.auction, pickup_time=theFuture
        )
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
            description="",
        )
        self.lot2 = Lot.objects.create(
            custom_lot_number="124",
            lot_name="Another test lot",
            auction=self.auction,
            auctiontos_seller=self.seller,
            quantity=1,
            description="",
        )
        # self.invoice = Invoice.objects.create(auctiontos_user=self.seller)
        # self.invoiceB = Invoice.objects.create(auctiontos_user=self.tosB)

        # Create test client
        self.client = Client()

    def test_valid_form_submission_and_undo(self):
        self.client.login(username="testuser", password="testpassword")
        url = reverse("auction_lot_winners_images", kwargs={"slug": self.auction.slug})
        response = self.client.post(
            url,
            {
                "lot": self.lot.custom_lot_number,
                "winner": self.bidder.bidder_number,
                "winning_price": 100,
                "invoice": True,
                "auction": 5,  # this is not used anywhere, but still required
            },
        )
        self.assertEqual(
            response.status_code, 302
        )  # Should redirect after successful form submission
        updated_lot = Lot.objects.get(pk=self.lot.pk)
        self.assertEqual(updated_lot.auctiontos_winner, self.bidder)
        self.assertEqual(updated_lot.winning_price, 100)
        response = self.client.get(url, {"undo": self.lot.custom_lot_number})
        self.assertEqual(response.status_code, 200)
        updated_lot = Lot.objects.get(pk=self.lot.pk)
        self.assertIsNone(updated_lot.auctiontos_winner)
        self.assertIsNone(updated_lot.winning_price)

    def test_invalid_form_submission(self):
        url = reverse("auction_lot_winners_images", kwargs={"slug": self.auction.slug})
        self.client.login(username="testuser", password="testpassword")
        response = self.client.post(
            url,
            {
                "lot": self.lot.custom_lot_number,
                "winner": self.bidder.bidder_number,
                "winning_price": -10,  # Invalid winning price
                "invoice": True,
                "auction": 5,
            },
        )
        self.assertEqual(
            response.status_code, 200
        )  # Form should not be submitted successfully
        updated_lot = Lot.objects.get(pk=self.lot.pk)
        self.assertIsNone(updated_lot.auctiontos_winner)
        self.assertIsNone(updated_lot.winning_price)

    def test_seller_invoice_closed(self):
        self.invoice = Invoice.objects.get(auctiontos_user=self.seller)
        self.invoice.status = "READY"
        self.invoice.save()
        self.client.login(username="testuser", password="testpassword")
        url = reverse("auction_lot_winners_images", kwargs={"slug": self.auction.slug})
        response = self.client.post(
            url,
            {
                "lot": self.lot.custom_lot_number,
                "winner": self.bidder.bidder_number,
                "winning_price": 100,
                "invoice": True,
                "auction": 5,
            },
        )
        self.assertEqual(
            response.status_code, 200
        )  # Form should not be submitted successfully
        updated_lot = Lot.objects.get(pk=self.lot.pk)
        self.assertIsNone(updated_lot.auctiontos_winner)
        self.assertIsNone(updated_lot.winning_price)

    def test_winner_invoice_closed(self):
        self.invoice = Invoice.objects.create(auctiontos_user=self.bidder)
        self.invoice.status = "READY"
        self.invoice.save()
        self.client.login(username="testuser", password="testpassword")
        url = reverse("auction_lot_winners_images", kwargs={"slug": self.auction.slug})
        response = self.client.post(
            url,
            {
                "lot": self.lot.custom_lot_number,
                "winner": self.bidder.bidder_number,
                "winning_price": 100,
                "invoice": True,
                "auction": 5,
            },
        )
        self.assertEqual(
            response.status_code, 200
        )  # Form should not be submitted successfully
        updated_lot = Lot.objects.get(pk=self.lot.pk)
        self.assertIsNone(updated_lot.auctiontos_winner)
        self.assertIsNone(updated_lot.winning_price)

    def test_winner_not_found(self):
        self.client.login(username="testuser", password="testpassword")
        url = reverse("auction_lot_winners_images", kwargs={"slug": self.auction.slug})
        response = self.client.post(
            url,
            {
                "lot": self.lot.custom_lot_number,
                "winner": "55665",  # Invalid winner number
                "winning_price": 100,
                "invoice": True,
                "auction": 5,
            },
        )
        self.assertEqual(
            response.status_code, 200
        )  # Form should not be submitted successfully
        updated_lot = Lot.objects.get(pk=self.lot.pk)
        self.assertIsNone(updated_lot.auctiontos_winner)
        self.assertIsNone(updated_lot.winning_price)

    def test_lot_not_found(self):
        self.client.login(username="testuser", password="testpassword")
        url = reverse("auction_lot_winners_images", kwargs={"slug": self.auction.slug})
        response = self.client.post(
            url,
            {
                "lot": "invalid_lot_number",  # Invalid lot number
                "winner": self.bidder.bidder_number,
                "winning_price": 100,
                "invoice": True,
                "auction": 5,
            },
        )
        self.assertEqual(
            response.status_code, 200
        )  # Form should not be submitted successfully

    def test_lot_already_sold(self):
        self.lot.auctiontos_winner = self.bidder
        self.lot.winning_price = 100
        self.lot.save()
        self.client.login(username="testuser", password="testpassword")
        url = reverse("auction_lot_winners_images", kwargs={"slug": self.auction.slug})
        response = self.client.post(
            url,
            {
                "lot": self.lot.custom_lot_number,
                "winner": self.bidder.bidder_number,
                "winning_price": 200,  # Attempting to set winner for already sold lot with a different price
                "invoice": True,
                "auction": 5,
            },
        )
        self.assertEqual(
            response.status_code, 200
        )  # Form should not be submitted successfully
        updated_lot = Lot.objects.get(pk=self.lot.pk)
        self.assertEqual(
            updated_lot.auctiontos_winner, self.bidder
        )  # Lot winner should remain unchanged
        self.assertEqual(
            updated_lot.winning_price, 100
        )  # Winning price should remain unchanged


class LotRefundDialogTests(TestCase):
    def setUp(self):
        time = timezone.now() - datetime.timedelta(days=2)
        timeStart = timezone.now() - datetime.timedelta(days=3)
        theFuture = timezone.now() + datetime.timedelta(days=3)
        self.user = User.objects.create_user(
            username="testuser", password="testpassword"
        )
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
        self.location = PickupLocation.objects.create(
            name="location", auction=self.auction, pickup_time=theFuture
        )
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
            description="",
        )
        self.lot2 = Lot.objects.create(
            custom_lot_number="124",
            lot_name="Another test lot",
            auction=self.auction,
            auctiontos_seller=self.seller,
            quantity=1,
            description="",
        )
        self.client = Client()
        self.client.login(username="testuser", password="testpassword")
        self.lot_not_in_auction = Lot.objects.create(
            lot_name="not in auction",
            quantity=1,
            reserve_price=10,
            user=self.user,
            active=True,
            description="",
        )
        self.lot_url = reverse("lot_refund", kwargs={"pk": self.lot.pk})

    def test_lot_not_in_auction(self):
        response = self.client.get(
            reverse("lot_refund", kwargs={"pk": self.lot_not_in_auction.pk})
        )
        self.assertEqual(response.status_code, 404)

    def test_get_lot_refund_dialog(self):
        response = self.client.get(self.lot_url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "auctions/generic_admin_form.html")

    def test_post_lot_refund_dialog(self):
        data = {"partial_refund_percent": 50, "banned": False}
        response = self.client.post(self.lot_url, data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<script>location.reload();</script>")

        # Check if the lot was updated
        updated_lot = Lot.objects.get(pk=self.lot.pk)
        self.assertEqual(updated_lot.partial_refund_percent, 50)
        self.assertEqual(updated_lot.banned, False)
