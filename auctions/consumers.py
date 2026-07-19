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

logger = logging.getLogger(__name__)


def check_chat_permissions(lot, user):
    """
    Returns false if everything is OK, or a string error message
    call check_all_permissions first
    """
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
    # admin-added lots often have no lot.user; fall back to the seller's linked account
    seller_pk = lot.user_id or (lot.auctiontos_seller.user_id if lot.auctiontos_seller_id else None)
    if seller_pk and UserBan.objects.filter(banned_user=user.pk, user=seller_pk).first():
        return "This user has banned you from bidding on their lots"
    if lot.banned:
        return "This lot has been removed"
    if lot.auction and lot.auction.user_banned_by_admins(user):
        return "You don't have permission to bid in this auction"
    return False


def broadcast_bid_result(lot, user, result):
    """Push the outcome of a bid to the connected lot-page websockets.

    This is the *broadcast* half of placing a bid, kept separate from persistence
    on purpose: callers must treat it as best-effort (see place_bid_and_broadcast)
    so a channel-layer outage can never lose a bid that was already saved. The
    message shapes here match what LotConsumer.receive() historically sent.
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
        except Exception as e:
            logger.exception(e)

    def disconnect(self, close_code):
        # Leave room group
        async_to_sync(self.channel_layer.group_discard)(self.room_group_name, self.channel_name)
        # bit redundant, but 'seen' is used for lot notifications for the owner of a given lot
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
        # This websocket handles chat only. Bids go through the HTTP endpoint
        # (views.PlaceBid -> bidding.place_bid_and_broadcast) so a down/stalled
        # socket can never silently lose a bid.
        if self.user.is_authenticated:
            try:
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
                            if True:
                                history = LotHistory.objects.create(
                                    lot=self.lot,
                                    user=self.user,
                                    message=message,
                                    changed_price=False,
                                    current_price=self.lot.high_bid,
                                )
                            async_to_sync(self.channel_layer.group_send)(
                                self.room_group_name,
                                {
                                    "type": "chat_message",
                                    "info": "CHAT",
                                    "message": message,
                                    "pk": self.user.pk,
                                    "username": str(self.user),
                                    "timestamp": history.timestamp.isoformat(),
                                },
                            )
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
    """This is ready to use and corresponding code to connect added (commented out) to base.html
    You can use userdata.send_websocket_message to message the user, like this:
        result = {
            "type": "toast",
            "message": "Hello world!",
        }
        user.userdata.send_websocket_message(result)
    It would make a good messaging system for some stuff like chat messages,
    but at this time it does not seem like a good idea
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

                # Send a message after accepting the connection
                # async_to_sync(self.channel_layer.group_send)(
                #     self.user_notification_channel,
                #     {"type": "toast", "message": 'Welcome!', 'bg': 'success'},
                # )
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

        except Exception as e:
            logger.exception(e)
            self.close()
            return

    def invoice_approved(self, event):
        """Step 1, NOT PAID YET"""
        self.send(text_data=json.dumps({"type": "invoice_approved", "pk": event["pk"]}))

    def capture_complete(self, event):
        """This is good enough to send to the front end and hide payment QR
        but don't mark invoice paid just yet"""
        self.send(text_data=json.dumps({"type": "capture_complete", "pk": event["pk"]}))

    def invoice_paid(self, event):
        """When PayPal payment completes"""
        self.send(text_data=json.dumps({"type": "invoice_paid", "pk": event["pk"]}))

    def stats_updated(self, event):
        """When auction stats have been recalculated"""
        self.send(text_data=json.dumps({"type": "stats_updated"}))

    def disconnect(self, close_code):
        # Leave room group
        async_to_sync(self.channel_layer.group_discard)(f"auctions_{self.pk}", self.channel_name)
        logger.debug("disconnected")
