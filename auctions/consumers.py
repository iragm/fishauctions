"""The websocket half of the site: live bidding, chat, and "somebody else just bid".

One consumer per thing a browser can watch. :class:`LotConsumer` carries bids on an open lot, and
the permission checks at the top of this module are what stop a socket doing what the equivalent
POST would refuse. A dropped socket is invisible to the person using it, so test changes here with
the connection actually interrupted.
"""

# chat/consumers.py
import json
import logging
from decimal import Decimal

from asgiref.sync import async_to_sync
from channels.generic.websocket import WebsocketConsumer
from channels.layers import get_channel_layer
from django.utils import timezone

from .models import (
    Auction,
    ChatSubscription,
    Lot,
    LotHistory,
    User,
    UserBan,
)

try:
    from uvicorn.protocols.utils import ClientDisconnected
except ImportError:  # pragma: no cover - only uvicorn raises this

    class ClientDisconnected(Exception):
        """Placeholder so the handlers below still work under other ASGI servers."""


logger = logging.getLogger(__name__)


def check_chat_permissions(lot, user):
    """False when everything is OK, or a string error message. Call check_all_permissions first."""
    try:
        ban = user.userdata.banned_from_chat_until
        if ban:
            if ban > timezone.now():
                display = ban - timezone.now()
                if not display.days:
                    display = " later"
                else:
                    display = f"in {display.days} days"
                return f"You can't chat.  Try again {display}"
    except AttributeError:
        pass
    if not lot.chat_allowed:
        return "Chat is no longer allowed on this lot"
    return False


def check_all_permissions(lot, user):
    """Returns false if everything is OK, or a string error message"""
    # Admin-added lots often have no lot.user; fall back to the seller's linked account.
    seller_pk = lot.user_id or (lot.auctiontos_seller.user_id if lot.auctiontos_seller_id else None)
    if seller_pk and UserBan.objects.filter(banned_user=user.pk, user=seller_pk).first():
        return "This user has banned you from bidding on their lots"
    if lot.banned:
        return "This lot has been removed"
    if lot.auction and lot.auction.user_banned_by_admins(user):
        return "You don't have permission to bid in this auction"
    return False


def post_chat_message(lot, user, message):
    """Post a chat message on a lot: persist it, then push it to everyone watching.

    Shared by the websocket and the assistant's ``answer_question``, so both write the same row and
    broadcast the same event. Permission checks are the caller's job
    (:func:`check_all_permissions`, then :func:`check_chat_permissions`), because the websocket answers
    a failure with a toast and the action with a tool error.

    The broadcast is best-effort like ``place_bid_and_broadcast``: the row is written first.
    """
    history = LotHistory.objects.create(
        lot=lot,
        user=user,
        message=message,
        changed_price=False,
        current_price=lot.high_bid,
    )
    try:
        async_to_sync(get_channel_layer().group_send)(
            f"lot_{lot.pk}",
            {
                "type": "chat_message",
                "info": "CHAT",
                "message": message,
                "pk": user.pk,
                "username": str(user),
                "timestamp": history.timestamp.isoformat(),
            },
        )
    except Exception:
        logger.exception("Could not broadcast a chat message on lot %s", lot.pk)
    return history


def broadcast_bid_result(lot, user, result):
    """Push the outcome of a bid to the connected lot-page websockets.

    The broadcast half of placing a bid, kept separate from persistence: callers treat it as
    best-effort (see place_bid_and_broadcast) so an outage never loses a saved bid.
    """
    channel_layer = get_channel_layer()
    room_group_name = f"lot_{lot.pk}"
    user_room_name = f"private_user_{user.pk}_lot_{lot.pk}"
    current_high_bid = result["current_high_bid"]
    if isinstance(current_high_bid, Decimal):
        current_high_bid = float(current_high_bid)
    if result["send_to"] == "user":
        if result["type"] == "ERROR":
            async_to_sync(channel_layer.group_send)(
                user_room_name,
                {"type": "error_message", "error": result["message"]},
            )
        else:
            # sealed-bid success and "you raised your own proxy bid" land here
            async_to_sync(channel_layer.group_send)(
                user_room_name,
                {
                    "type": "chat_message",
                    "info": result["type"],
                    "message": result["message"],
                    "high_bidder_pk": result["high_bidder_pk"],
                    "high_bidder_name": result["high_bidder_name"],
                    "current_high_bid": current_high_bid,
                },
            )
    else:
        async_to_sync(channel_layer.group_send)(
            room_group_name,
            {
                "type": "chat_message",
                "info": result["type"],
                "message": result["message"],
                "high_bidder_pk": result["high_bidder_pk"],
                "high_bidder_name": result["high_bidder_name"],
                "current_high_bid": current_high_bid,
                "date_end": result["date_end"],
            },
        )


class LotConsumer(WebsocketConsumer):
    def connect(self):
        try:
            self.lot_number = self.scope["url_route"]["kwargs"]["lot_number"]
            self.user = self.scope["user"]
            self.room_group_name = f"lot_{self.lot_number}"
            self.user_room_name = f"private_user_{self.user.pk}_lot_{self.lot_number}"
            self.lot = Lot.objects.get(pk=self.lot_number)

            # Join room group
            async_to_sync(self.channel_layer.group_add)(self.room_group_name, self.channel_name)

            # Join private room for notifications only to this user
            async_to_sync(self.channel_layer.group_add)(self.user_room_name, self.channel_name)
            self.accept()
            # send the most recent history
            allHistory = LotHistory.objects.filter(lot=self.lot, removed=False).order_by("-timestamp")[:200]
            # send oldest first
            for history in reversed(allHistory):
                try:
                    if history.changed_price:
                        pk = -1
                        username = "System"
                    else:
                        pk = history.user.pk
                        username = str(history.user)

                    async_to_sync(self.channel_layer.group_send)(
                        self.user_room_name,
                        {
                            "type": "chat_message",
                            "pk": pk,
                            "info": "CHAT",
                            "message": history.message,
                            "username": username,
                            "timestamp": history.timestamp.isoformat(),
                        },
                    )
                except Exception as e:
                    logger.exception(e)
            try:
                owner_chat_notifications = False
                if self.lot.user:
                    subscription, created = ChatSubscription.objects.get_or_create(
                        user=self.lot.user,
                        lot=self.lot,
                        defaults={
                            "unsubscribed": not self.lot.user.userdata.email_me_when_people_comment_on_my_lots,
                        },
                    )
                    if not subscription.unsubscribed:
                        owner_chat_notifications = True
                if not owner_chat_notifications:
                    async_to_sync(self.channel_layer.group_send)(
                        self.user_room_name,
                        {
                            "type": "chat_message",
                            "pk": -1,
                            "info": "CHAT",
                            "message": "The creator of this lot has turned off email notifications when chat messages are posted.  You may not get a reply.",
                            "username": "System",
                            "timestamp": timezone.now().isoformat(),
                        },
                    )
            except Exception as e:
                logger.exception(e)
            # mark chat messages as seen when a user visits a lot page
            user_pk = None
            if self.lot.user:
                user_pk = self.lot.user.pk
            if self.lot.auctiontos_seller and self.lot.auctiontos_seller.user:
                user_pk = self.lot.auctiontos_seller.user
            if user_pk and self.user.pk == user_pk:
                logger.debug("lot owner is entering the chat, marking all chats as seen")
                LotHistory.objects.filter(lot=self.lot.pk, seen=False).update(seen=True)
            # this is for everyone else
            if self.user.pk:
                existing_subscription = ChatSubscription.objects.filter(lot=self.lot, user=self.user.pk).first()
                if existing_subscription:
                    logger.info("Marking all ChatSubscription seen last time now for user %s", self.user.pk)
                    existing_subscription.last_seen = timezone.now()
                    existing_subscription.last_notification_sent = timezone.now()
                    existing_subscription.save()
        except ClientDisconnected:
            # The tab closed or lost signal mid-handshake. Routine, so log below ERROR to keep it
            # out of mail_admins. ClientDisconnected subclasses OSError, so this stays above the
            # `except Exception`.
            logger.info("client went away before the lot websocket finished connecting")
        except Exception as e:
            logger.exception(e)

    def disconnect(self, close_code):
        # Leave room group
        async_to_sync(self.channel_layer.group_discard)(self.room_group_name, self.channel_name)
        # 'seen' drives lot notifications for the lot's owner.
        user_pk = None
        if self.lot.user:
            user_pk = self.lot.user.pk
        if self.lot.auctiontos_seller and self.lot.auctiontos_seller.user:
            user_pk = self.lot.auctiontos_seller.user
        if user_pk and self.user.pk == user_pk:
            logger.debug("lot owner is leaving the chat, marking all chats as seen")
            LotHistory.objects.filter(lot=self.lot.pk, seen=False).update(seen=True)
        # this is for everyone else
        if self.user.pk:
            existing_subscription = ChatSubscription.objects.filter(lot=self.lot, user=self.user.pk).first()
            if existing_subscription:
                logger.info("Marking all ChatSubscription seen last time now for user %s", self.user.pk)
                existing_subscription.last_seen = timezone.now()
                existing_subscription.last_notification_sent = timezone.now()
                existing_subscription.save()

    # Receive message from WebSocket
    def receive(self, text_data):
        text_data_json = json.loads(text_data)
        # This websocket is chat only: bids go through views.PlaceBid, so a stalled socket can't
        # lose one.
        if self.user.is_authenticated:
            try:
                # self.lot was fetched in connect() and held for as long as the page is open, so
                # everything cached on it (high_bid, high_bidder, ended, sold) must be dropped or a
                # message is filed at the price from before the last bid.
                self.lot.invalidate_cached_properties()
                error = check_all_permissions(self.lot, self.user)
                if error:
                    async_to_sync(self.channel_layer.group_send)(
                        self.user_room_name, {"type": "error_message", "error": error}
                    )
                else:
                    try:
                        message = text_data_json["message"]
                        error = check_chat_permissions(self.lot, self.user)
                        if error:
                            async_to_sync(self.channel_layer.group_send)(
                                self.user_room_name,
                                {"type": "error_message", "error": error},
                            )
                        else:
                            post_chat_message(self.lot, self.user, message)
                    except (KeyError, ValueError):
                        pass
            except Exception as e:
                logger.exception(e)

    # Send a toast error to a single user
    def error_message(self, event):
        error = event["error"]
        # Send error to WebSocket
        self.send(
            text_data=json.dumps(
                {
                    "error": error,
                }
            )
        )

    # Receive message from room group
    def chat_message(self, event):
        self.send(text_data=json.dumps(event))


class UserConsumer(WebsocketConsumer):
    """Ready to use, with the client code commented out in base.html.

    ``userdata.send_websocket_message`` messages one user, which would make a reasonable messaging
    system, but it doesn't seem worth it at the moment.
    """

    def connect(self):
        try:
            self.pk = self.scope["url_route"]["kwargs"]["user_pk"]
            user_for = User.objects.filter(pk=self.pk).first()
            self.user = self.scope["user"]
            self.user_notification_channel = f"user_{self.pk}"
            if not user_for or user_for != self.user:
                self.close()
                return
            else:
                self.accept()
                # Add to the group after accepting the connection
                async_to_sync(self.channel_layer.group_add)(self.user_notification_channel, self.channel_name)

        except ClientDisconnected:
            logger.info("client went away before the user websocket finished connecting")
            return
        except Exception as e:
            logger.exception(e)
            self.close()
            return

    def disconnect(self, close_code):
        # Leave room group
        async_to_sync(self.channel_layer.group_discard)(self.user_notification_channel, self.channel_name)
        logger.debug("disconnected")

    # Receive message from WebSocket
    def receive(self, text_data):
        text_data_json = json.loads(text_data)
        logger.info(text_data_json)

    def toast(self, event):
        message = event["message"]
        bg = event.get("bg", "info")
        self.send(text_data=json.dumps({"type": "toast", "message": message, "bg": bg}))


class AuctionConsumer(WebsocketConsumer):
    """Auction Admins only.  Catch signals to mark invoices paid"""

    def connect(self):
        try:
            self.pk = self.scope["url_route"]["kwargs"]["auction_pk"]
            auction = Auction.objects.filter(pk=self.pk).first()
            if not auction:
                self.close()
                return
            self.user = self.scope["user"]
            if self.user.is_anonymous:
                self.close()
                return
            if not auction.permission_check(self.user):
                self.close()
                return
            self.accept()
            async_to_sync(self.channel_layer.group_add)(f"auctions_{self.pk}", self.channel_name)

        except ClientDisconnected:
            logger.info("client went away before the auction websocket finished connecting")
            return
        except Exception as e:
            logger.exception(e)
            self.close()
            return

    def invoice_approved(self, event):
        """Step 1, NOT PAID YET"""
        self.send(text_data=json.dumps({"type": "invoice_approved", "pk": event["pk"]}))

    def capture_complete(self, event):
        """Good enough to hide the payment QR in the front end, but don't mark the invoice paid yet."""
        self.send(text_data=json.dumps({"type": "capture_complete", "pk": event["pk"]}))

    def invoice_paid(self, event):
        """When PayPal payment completes"""
        self.send(text_data=json.dumps({"type": "invoice_paid", "pk": event["pk"]}))

    def stats_updated(self, event):
        """When auction stats have been recalculated"""
        self.send(text_data=json.dumps({"type": "stats_updated"}))

    def queue_updated(self, event):
        """The in-person lot queue changed, so the queue and kiosk screens re-fetch and track winners set on
        another device.
        """
        self.send(text_data=json.dumps({"type": "queue_updated"}))

    def disconnect(self, close_code):
        # Leave room group
        async_to_sync(self.channel_layer.group_discard)(f"auctions_{self.pk}", self.channel_name)
        logger.debug("disconnected")
