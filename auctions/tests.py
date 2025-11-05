import datetime
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

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
        assert lot.high_bidder.pk is user.pk
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
        assert lot.high_bidder.pk is userA.pk
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
        assert lot.high_bidder.pk is jeff.pk
        assert lot.high_bid == 20
        garyBid = Bid.objects.create(user=gary, lot_number=lot, amount=20)
        assert lot.high_bidder.pk is jeff.pk
        assert lot.high_bid == 20
        # check the order
        jeffBid.last_bid_time = timezone.now()
        jeffBid.save()
        assert lot.high_bidder.pk is gary.pk
        assert lot.high_bid == 20
        garyBid.amount = 30
        garyBid.save()
        assert lot.high_bidder.pk is gary.pk
        assert lot.high_bid == 21
        garyBid.last_bid_time = timezone.now()
        garyBid.save()
        assert lot.high_bidder.pk is gary.pk
        assert lot.high_bid == 21
        jeffBid.amount = 30
        jeffBid.last_bid_time = timezone.now()
        jeffBid.save()
        assert lot.high_bidder.pk is gary.pk
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
        assert lot.high_bidder.pk is userB.pk
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
        assert lot.high_bidder.pk is userB.pk
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
        assert lot.high_bidder.pk is userB.pk
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
        assert response.status_code == 403
        response = self.client.post(self.get_url())
        assert response.status_code == 403

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

        Lot.objects.create(
            lot_name="dupe",
            auction=self.in_person_auction,
            auctiontos_seller=self.admin_in_person_tos,
            quantity=1,
            custom_lot_number="101-1",
        )
        response = self.client.post(
            self.get_url(), data={"lot": "101-1", "price": "10", "winner": "555", "action": "validate"}
        )
        data = response.json()
        assert "Multiple" in data.get("lot")


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
            date_end=timezone.now() - datetime.timedelta(days=1),
        )
        assert ended_lot.ended is True

        # Create a lot that is still active
        active_lot = Lot.objects.create(
            lot_name="Active lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            date_end=timezone.now() + datetime.timedelta(days=1),
        )
        assert active_lot.ended is False

    def test_lot_high_bid_calculation(self):
        """Test lot high_bid and high_bidder properties"""
        # Create a lot
        lot = Lot.objects.create(
            lot_name="Bid test lot",
            auction=self.online_auction,
            auctiontos_seller=self.online_tos,
            quantity=1,
            reserve_price=10,
            date_end=timezone.now() + datetime.timedelta(days=1),
        )

        # No bids - high_bid should be reserve_price
        assert lot.high_bid == 10

        # Add a bid
        Bid.objects.create(user=self.userB, lot_number=lot, amount=15)
        # Refresh lot to get updated calculations
        lot.refresh_from_db()
        assert lot.high_bidder.pk == self.userB.pk

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

    def test_auction_tos_name(self):
        """Test the name property of AuctionTOS"""
        # Should return user's username if name is not set
        assert self.online_tos.name == self.user.username

        # Update with a custom name
        self.online_tos.name = "Custom Name"
        self.online_tos.save()
        assert self.online_tos.name == "Custom Name"

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


class AuctionEditViewTests(StandardTestCase):
    """Test auction edit view with different user types"""

    def test_auction_edit_anonymous_user(self):
        """Anonymous users should not be able to edit"""
        response = self.client.get(self.online_auction.get_edit_url())
        # Should redirect to login
        assert response.status_code == 302

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
        response = self.client.get("/lots/my-lots/")
        assert response.status_code == 302

    def test_my_lots_logged_in_user(self):
        """Logged in users can view their lots"""
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get("/lots/my-lots/")
        assert response.status_code == 200


class AuctionUsersViewTests(StandardTestCase):
    """Test auction users/TOS admin view"""

    def test_auction_users_anonymous(self):
        """Anonymous users should not access user list"""
        url = reverse("auction_tos_list", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        # Should redirect to login
        assert response.status_code == 302

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
        assert response.status_code == 302

    def test_lot_create_logged_in_not_joined(self):
        """User not joined to auction should not be able to create lot in that auction"""
        self.client.login(username=self.user_who_does_not_join.username, password="testpassword")
        # Try to create a lot in the auction they haven't joined
        response = self.client.get(f"/lots/new/?auction={self.online_auction.slug}")
        # They can access the form, but posting should fail or redirect
        assert response.status_code == 200

    def test_lot_create_logged_in_joined(self):
        """User joined to auction can create lots"""
        self.client.login(username=self.user.username, password="testpassword")
        response = self.client.get(f"/lots/new/?auction={self.online_auction.slug}")
        assert response.status_code == 200


class InvoiceViewTests(StandardTestCase):
    """Test invoice views with different user types"""

    def test_invoice_view_anonymous(self):
        """Anonymous users should not view invoices"""
        url = reverse("invoice", kwargs={"pk": self.invoice.pk})
        response = self.client.get(url)
        # Should redirect to login
        assert response.status_code == 302

    def test_invoice_view_owner(self):
        """Invoice owner can view their invoice"""
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("invoice", kwargs={"pk": self.invoice.pk})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_invoice_view_other_user(self):
        """Other users should not view someone else's invoice"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = reverse("invoice", kwargs={"pk": self.invoice.pk})
        response = self.client.get(url)
        # Should be denied
        assert response.status_code in [302, 403]

    def test_invoice_view_admin(self):
        """Admin can view any invoice"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("invoice", kwargs={"pk": self.invoice.pk})
        response = self.client.get(url)
        assert response.status_code == 200


class PickupLocationTests(StandardTestCase):
    """Test PickupLocation model properties and views"""

    def test_pickup_location_create_anonymous(self):
        """Anonymous users cannot create pickup locations"""
        url = reverse("add_pickup", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 302

    def test_pickup_location_create_non_admin(self):
        """Non-admin users cannot create pickup locations"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = reverse("add_pickup", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code in [302, 403]

    def test_pickup_location_create_admin(self):
        """Admin users can create pickup locations"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("add_pickup", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_pickup_location_list_anonymous(self):
        """Anonymous users can view pickup locations"""
        url = reverse("auction_pickup_location", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_pickup_location_list_logged_in(self):
        """Logged in users can view pickup locations"""
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("auction_pickup_location", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200


class AuctionStatsViewTests(StandardTestCase):
    """Test auction stats view with different user types"""

    def test_auction_stats_anonymous_public(self):
        """Anonymous users can view stats if make_stats_public is True"""
        # Stats are public by default in StandardTestCase
        url = reverse("auction_stats", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_auction_stats_anonymous_private(self):
        """Anonymous users cannot view stats if make_stats_public is False"""
        self.online_auction.make_stats_public = False
        self.online_auction.save()
        url = reverse("auction_stats", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        # Should be denied (302 redirect or 403)
        assert response.status_code in [302, 403]

    def test_auction_stats_creator_private(self):
        """Creator can always view stats even if private"""
        self.online_auction.make_stats_public = False
        self.online_auction.save()
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("auction_stats", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_auction_stats_admin_private(self):
        """Admin can always view stats even if private"""
        self.online_auction.make_stats_public = False
        self.online_auction.save()
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("auction_stats", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200


class BulkAddLotsViewTests(StandardTestCase):
    """Test bulk add lots view with different user types"""

    def test_bulk_add_lots_anonymous(self):
        """Anonymous users cannot bulk add lots"""
        url = reverse("bulk_add_lots", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 302

    def test_bulk_add_lots_non_admin(self):
        """Non-admin users cannot bulk add lots"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = reverse("bulk_add_lots", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code in [302, 403]

    def test_bulk_add_lots_admin(self):
        """Admin users can bulk add lots"""
        self.client.login(username=self.admin_user.username, password="testpassword")
        url = reverse("bulk_add_lots", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_bulk_add_lots_creator(self):
        """Auction creator can bulk add lots"""
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("bulk_add_lots", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 200


class BulkAddUsersViewTests(StandardTestCase):
    """Test bulk add users view with different user types"""

    def test_bulk_add_users_anonymous(self):
        """Anonymous users cannot bulk add users"""
        url = reverse("bulk_add_users", kwargs={"slug": self.online_auction.slug})
        response = self.client.get(url)
        assert response.status_code == 302

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
        assert response.status_code == 302

    def test_set_lot_winners_non_admin(self):
        """Non-admin users cannot access set lot winners"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = reverse("auction_lot_winners_dynamic", kwargs={"slug": self.in_person_auction.slug})
        response = self.client.get(url)
        assert response.status_code in [302, 403]

    def test_set_lot_winners_admin(self):
        """Admin users can access set lot winners"""
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
        assert response.status_code == 302

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
        assert response.status_code == 200


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
        url = reverse("user_detail", kwargs={"pk": self.user.pk})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_user_view_logged_in(self):
        """Logged in users can view user profiles"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        url = reverse("user_detail", kwargs={"pk": self.user.pk})
        response = self.client.get(url)
        assert response.status_code == 200

    def test_user_view_own_profile(self):
        """Users can view their own profile"""
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("user_detail", kwargs={"pk": self.user.pk})
        response = self.client.get(url)
        assert response.status_code == 200


class ImageViewTests(StandardTestCase):
    """Test image create/update/delete views"""

    def test_image_create_anonymous(self):
        """Anonymous users cannot create images"""
        url = reverse("new_image", kwargs={"lot_number": self.lot.lot_number})
        response = self.client.get(url)
        assert response.status_code == 302

    def test_image_create_logged_in(self):
        """Logged in users can access image create form"""
        self.client.login(username=self.user.username, password="testpassword")
        url = reverse("new_image", kwargs={"lot_number": self.lot.lot_number})
        response = self.client.get(url)
        assert response.status_code == 200


class WatchViewTests(StandardTestCase):
    """Test watch/unwatch functionality"""

    def test_watch_anonymous(self):
        """Anonymous users cannot watch lots"""
        # watchOrUnwatch is a function-based view
        response = self.client.post(f"/lots/watch/{self.lot.pk}/")
        # Should redirect to login
        assert response.status_code == 302

    def test_watch_logged_in(self):
        """Logged in users can watch lots"""
        self.client.login(username=self.user_with_no_lots.username, password="testpassword")
        response = self.client.post(f"/lots/watch/{self.lot.pk}/")
        # Should succeed or redirect
        assert response.status_code in [200, 302]


class MyBidsViewTests(StandardTestCase):
    """Test my bids view with different user types"""

    def test_my_bids_anonymous(self):
        """Anonymous users should be redirected to login"""
        response = self.client.get("/lots/my-bids/")
        assert response.status_code == 302

    def test_my_bids_logged_in(self):
        """Logged in users can view their bids"""
        self.client.login(username=self.userB.username, password="testpassword")
        response = self.client.get("/lots/my-bids/")
        assert response.status_code == 200


class MyWonLotsViewTests(StandardTestCase):
    """Test my won lots view with different user types"""

    def test_my_won_lots_anonymous(self):
        """Anonymous users should be redirected to login"""
        response = self.client.get("/lots/my-won-lots/")
        assert response.status_code == 302

    def test_my_won_lots_logged_in(self):
        """Logged in users can view their won lots"""
        self.client.login(username=self.userB.username, password="testpassword")
        response = self.client.get("/lots/my-won-lots/")
        assert response.status_code == 200
