"""The ``endauctions`` command and the websocket layer that tells everyone what happened.

This is the job that closes lots and creates invoices. It runs on a 60-second beat under a 300-second
limit and takes a cache lock, because two runs both seeing a lot as unsold both invoice it.
"""

import datetime
import unittest
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from auctions.models import (
    Auction,
    AuctionTOS,
    Bid,
    Lot,
    LotHistory,
    LotImage,
    PickupLocation,
)
from auctions.tests import CHANNELS_TESTING_AVAILABLE, StandardTestCase


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


class WebsocketClientDisconnectTests(TestCase):
    """A user closing the tab (or losing signal) mid-handshake makes uvicorn raise
    ClientDisconnected out of accept().  That's routine, so it must not be logged at ERROR:
    auctions.consumers is wired to mail_admins in settings.LOGGING, and it would email admins."""

    def test_client_disconnected_during_connect_is_not_logged_as_an_error(self):
        import logging as logging_module

        from auctions.consumers import ClientDisconnected, LotConsumer

        user = User.objects.create_user(username="ws_gone", password="testpassword", email="gone@example.com")
        lot = Lot.objects.create(
            lot_name="A test lot",
            date_end=timezone.now() + datetime.timedelta(days=3),
            reserve_price=5,
            user=user,
            quantity=1,
        )

        class FakeChannelLayer:
            async def group_add(self, group, channel):
                return None

        consumer = LotConsumer()
        consumer.scope = {"url_route": {"kwargs": {"lot_number": lot.pk}}, "user": user}
        consumer.channel_name = "test.channel"
        consumer.channel_layer = FakeChannelLayer()

        with (
            patch.object(LotConsumer, "accept", side_effect=ClientDisconnected),
            self.assertLogs("auctions.consumers", level="INFO") as captured,
        ):
            consumer.connect()
        self.assertFalse([record for record in captured.records if record.levelno >= logging_module.ERROR])


@unittest.skipUnless(CHANNELS_TESTING_AVAILABLE, "channels.testing requires daphne (test-only dependency)")
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
        # ``promote_this_auction`` is spelled out on both fixture auctions because the model's
        # default is False (an auction is not on the public list until somebody puts it there),
        # and several things this fixture is used to test are scoped to promoted auctions --
        # notably ``models.guess_category``, which excludes lots in unpromoted auctions. Leaving
        # it to the default made those tests depend on a column default rather than on a fixture.
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
            promote_this_auction=True,
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
            promote_this_auction=True,
        )
        self.location = PickupLocation.objects.create(
            name="location", auction=self.online_auction, pickup_time=theFuture
        )
        self.in_person_location = PickupLocation.objects.create(
            name="location", auction=self.in_person_auction, pickup_time=theFuture
        )
        # Every fixture participant gets an explicit bidder number. AuctionTOS.save() auto-assigns
        # with randint(1, 999) when the number is left blank, so a fixture row that generates its own
        # can land on a number a test hard-codes ("88", "70", ...) and fail that test roughly one run
        # in 500: the auction already holds that number under a different name. These are kept out of
        # the range tests pick their own numbers from.
        self.in_person_buyer = AuctionTOS.objects.create(
            user=self.user_with_no_lots,
            auction=self.in_person_auction,
            pickup_location=self.in_person_location,
            bidder_number="555",
        )
        self.userB = User.objects.create_user(username="no_tos", password="testpassword")
        self.admin_online_tos = AuctionTOS.objects.create(
            user=self.admin_user,
            auction=self.online_auction,
            pickup_location=self.location,
            is_admin=True,
            bidder_number="501",
        )
        self.admin_in_person_tos = AuctionTOS.objects.create(
            user=self.admin_user,
            auction=self.in_person_auction,
            pickup_location=self.in_person_location,
            is_admin=True,
            bidder_number="502",
        )
        self.online_tos = AuctionTOS.objects.create(
            user=self.user, auction=self.online_auction, pickup_location=self.location, bidder_number="503"
        )
        self.in_person_tos = AuctionTOS.objects.create(
            user=self.user, auction=self.in_person_auction, pickup_location=self.location, bidder_number="504"
        )
        self.tosB = AuctionTOS.objects.create(
            user=self.userB, auction=self.online_auction, pickup_location=self.location, bidder_number="505"
        )
        self.tosC = AuctionTOS.objects.create(
            user=self.user_with_no_lots, auction=self.online_auction, pickup_location=self.location, bidder_number="506"
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
