# chat/consumers.py
import datetime
import json

from asgiref.sync import async_to_sync
from channels.generic.websocket import WebsocketConsumer
from django.conf import settings
from django.contrib.sites.models import Site
from django.db.models import Q
from django.utils import timezone
from post_office import mail

from .models import (
    AuctionTOS,
    Bid,
    ChatSubscription,
    Invoice,
    Lot,
    LotHistory,
    UserBan,
    UserData,
    UserInterestCategory,
)


def check_bidding_permissions(lot, user):
    """
    Returns false if everything is OK, or a string error message
    call check_all_permissions first
    """
    if lot.ended:
        return "Bidding on this lot has ended"
    if lot.user and lot.user.pk == user.pk:
        return "You can't bid on your own lot"
    if lot.auction:
        tos = AuctionTOS.objects.filter(Q(user=user) | Q(email=user.email), auction=lot.auction).first()
        if not tos:
            return "You haven't joined this auction"
        else:
            if not tos.bidding_allowed:
                return "This auction requires admin approval before you can bid"
    return False


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
    except:
        pass
    if not lot.chat_allowed:
        return "Chat is no longer allowed on this lot"
    return False


def check_all_permissions(lot, user):
    """Returns false if everything is OK, or a string error message"""
    if UserBan.objects.filter(banned_user=user.pk, user=lot.user.pk).first():
        return "This user has banned you from bidding on their lots"
    if lot.banned:
        return "This lot has been removed"
    if lot.auction and UserBan.objects.filter(banned_user=user.pk, user=lot.auction.created_by.pk).first():
        return "You don't have permission to bid in this auction"
    return False


def reset_lot_end_time(lot):
    """When bid are placed at the last minute on a lot, we need to bump up the end time.  Call this function any time a bid that changes the price is placed on a lot.
    Return ms-format timestamp for use on the lot page"""
    if lot.within_dynamic_end_time:
        new_end_time = timezone.now() + datetime.timedelta(minutes=15)
        if new_end_time > lot.hard_end:
            new_end_time = lot.hard_end
        if lot.date_end != new_end_time:
            lot.date_end = new_end_time
            lot.save()
            # ms - to be parsed by js and set to local time on view_lot_images.html
            return int(lot.date_end.timestamp() * 1000)
    return None


def bid_on_lot(lot, user, amount):
    """
    Check permissions to make sure the user isn't banned before calling this function
    this will return the following:
    {
        "type": "INFO", # ERROR, INFO, NEW_HIGH_BID, NEW_HIGH_BIDDER, LOT_END_WINNER, ENDED_NO_WINNER
        "message": "string",
        "send_to": 'user' or 'everyone'
        "high_bidder_pk": 1 # pk of high bidder (or winner) or None.  Used to update DOM
        "high_bidder_name": 'user' # name of high bidder or None.  Used to update DOM
        "current_high_bid": 5 # current bid or None.  Can be the winning price.  Used to update DOM
        }
    """
    try:
        # if True:
        amount = int(amount)
        result = {
            "type": "ERROR",
            "message": "Override this message",
            "send_to": "user",
            "high_bidder_pk": None,
            "high_bidder_name": None,
            "current_high_bid": None,
            "winner": None,
            "date_end": None,
        }
        if lot.ended:
            result["message"] = "Bidding has ended"
            return result
        if lot.bidding_error:
            result["message"] = lot.bidding_error
            return result
        if lot.winner or lot.auctiontos_winner:
            result["message"] = "This lot has already been sold"
            return result
        if lot.auction:
            invoice = Invoice.objects.filter(auctiontos_user__user=user, auction=lot.auction).first()
            if invoice and invoice.status != "DRAFT":
                result["message"] = (
                    "Your invoice for this auction is not open.  An administrator can reopen it and allow you to bid."
                )
                return result

        originalHighBidder = lot.high_bidder
        originalBid = lot.high_bid
        bid, created = Bid.objects.get_or_create(
            user=user,
            lot_number=lot,
            defaults={"amount": amount},
        )
        # also update category interest, max one per bid
        interest, interestCreated = UserInterestCategory.objects.get_or_create(
            category=lot.species_category,
            user=user,
            defaults={"interest": settings.BID_WEIGHT},
        )
        userData, userdataCreated = UserData.objects.get_or_create(
            user=user,
            defaults={},
        )
        userData.has_bid = True
        if userData.username_visible:
            user_string = str(user)
        else:
            user_string = "Anonymous"
        userData.save()
        if not interestCreated:
            interest.interest += settings.BID_WEIGHT
            interest.save()
        if lot.sealed_bid:
            bid.was_high_bid = True
            bid.amount = amount
            bid.last_bid_time = timezone.now()
            bid.save()
            result["type"] = "INFO"
            result["message"] = "Bid placed!  You can change your bid at any time until the auction ends"
            result["send_to"] = "user"
            # result["high_bidder_pk"] = user.pk
            # result["high_bidder_name"] = str(user)
            result["current_high_bid"] = bid.amount
            return result
        else:
            if not created:
                if amount <= bid.amount:
                    result["message"] = f"Bid more than your proxy bid (${bid.amount})"
                    # print(f"{user_string} tried to bid on {lot} less than their original bid of ${originalBid}")
                    return result
                else:
                    bid.last_bid_time = timezone.now()
                    bid.amount = amount
                    # bid.amount now contains the actual bid, regardless of whether it was new or not
                    bid.save()
            # from here on, lot.high_bidder and lot.high_bid will include the current bid
            if lot.buy_now_price and not originalHighBidder:
                if bid.amount >= lot.buy_now_price:
                    lot.winner = user
                    if lot.auction:
                        auctiontos_winner = AuctionTOS.objects.filter(auction=lot.auction, user=user).first()
                        if auctiontos_winner:
                            lot.auctiontos_winner = auctiontos_winner
                            invoice, created = Invoice.objects.get_or_create(
                                auctiontos_user=lot.auctiontos_winner,
                                auction=lot.auction,
                                defaults={},
                            )
                            invoice.recalculate
                    lot.winning_price = lot.buy_now_price
                    lot.buy_now_used = True
                    # this next line makes the lot end immediately after buy now is used
                    # I have put it in and taken it out a few times now, it is controversial because it causes lots to "disappear" when sold
                    # see also lot.ended - setting this is needed to make buy now lots go into invoices immediately
                    lot.date_end = timezone.now()
                    lot.watch_warning_email_sent = True
                    lot.save()
                    result["send_to"] = "everyone"
                    result["high_bidder_pk"] = user.pk
                    result["high_bidder_name"] = user_string
                    result["type"] = "LOT_END_WINNER"
                    result["message"] = f"{user_string} bought this lot now!!"
                    result["current_high_bid"] = lot.buy_now_price
                    bid.was_high_bid = True
                    bid.last_bid_time = lot.date_end
                    bid.save()
                    LotHistory.objects.create(
                        lot=lot,
                        user=user,
                        message=result["message"],
                        changed_price=True,
                        current_price=result["current_high_bid"],
                        bid_amount=amount,
                    )
                    return result
            if (not originalHighBidder) and (lot.high_bidder.pk == user.pk):
                result["send_to"] = "everyone"
                result["type"] = "NEW_HIGH_BIDDER"
                result["message"] = f"{user} has placed the first bid on this lot"
                result["current_high_bid"] = lot.reserve_price
                result["high_bidder_pk"] = user.pk
                result["high_bidder_name"] = user_string
                bid.was_high_bid = True
                LotHistory.objects.create(
                    lot=lot,
                    user=user,
                    message=result["message"],
                    changed_price=True,
                    current_price=result["current_high_bid"],
                    bid_amount=amount,
                )
                bid.last_bid_time = timezone.now()
                bid.save()
                return result
            if bid.amount <= originalBid:  # changing this to < would allow bumping without being the high bidder
                # there's a high bidder already
                bid.save()  # save the current bid regardless
                # print(f"{user_string} tried to bid on {lot} less than the current bid of ${originalBid}")
                result["message"] = f"You can't bid less than ${originalBid + 1}"
                return result
            if bid.amount > originalBid + 1:
                userData, userdataCreated = UserData.objects.get_or_create(
                    user=user,
                    defaults={},
                )
                userData.has_used_proxy_bidding = True
                userData.save()
            # if we get to this point, the user has bid >= the high bid
            bid.was_high_bid = True
            bid.last_bid_time = timezone.now()
            bid.save()
            if lot.high_bidder.pk == user.pk:
                try:
                    if originalHighBidder.pk == lot.high_bidder.pk:
                        # user is upping their own price, don't tell other people about it
                        result["type"] = "INFO"
                        result["message"] = f"You've raised your proxy bid to ${bid.amount}"
                        # print(f"{user_string} has raised their bid on {lot} to ${bid.amount}")
                        return result
                except:
                    pass
                # New high bidder!  If we get to this point, the user has bid against someone else and changed the price
                result["date_end"] = reset_lot_end_time(lot)
                result["type"] = "NEW_HIGH_BIDDER"
                result["message"] = f"{lot.high_bidder_display} is now the high bidder at ${lot.high_bid}"
                if result["date_end"]:
                    result["message"] += ". End time extended!"
                result["high_bidder_pk"] = lot.high_bidder.pk
                result["high_bidder_name"] = str(lot.high_bidder_display)
                result["current_high_bid"] = lot.high_bid
                result["send_to"] = "everyone"
                # email the old one
                current_site = Site.objects.get_current()
                # print(f'{originalHighBidder.username} has been outbid!')
                mail.send(
                    originalHighBidder.email,
                    template="outbid_notification",
                    context={
                        "name": originalHighBidder.first_name,
                        "domain": current_site.domain,
                        "lot": lot,
                    },
                )
                LotHistory.objects.create(
                    lot=lot,
                    user=user,
                    message=result["message"],
                    changed_price=True,
                    current_price=result["current_high_bid"],
                    bid_amount=amount,
                )
                return result
            # bumped up against a proxy bid
            result["date_end"] = reset_lot_end_time(lot)
            result["type"] = "NEW_HIGH_BID"
            result["current_high_bid"] = lot.high_bid
            result["message"] = (
                f"{user_string} bumped the price up to ${lot.high_bid}.  {lot.high_bidder_display} is still the high bidder."
            )
            if result["date_end"]:
                result["message"] += "  End time extended!"
            result["send_to"] = "everyone"
            LotHistory.objects.create(
                lot=lot,
                user=user,
                message=result["message"],
                changed_price=True,
                current_price=result["current_high_bid"],
                bid_amount=amount,
            )
            return result
    except Exception as e:
        print(e)


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
                        },
                    )
                except Exception as e:
                    print(e)
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
                        },
                    )
            except Exception as e:
                print(e)

        except Exception as e:
            print(e)

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
            # print("lot owner is leaving the chat, marking all chats as seen")
            LotHistory.objects.filter(lot=self.lot.pk, seen=False).update(seen=True)
        # this is for everyone else
        if self.user.pk:
            existing_subscription = ChatSubscription.objects.filter(lot=self.lot, user=self.user.pk).first()
            if existing_subscription:
                print(f"Marking all ChatSubscription seen last time now for user {self.user.pk}")
                existing_subscription.last_seen = timezone.now()
                existing_subscription.last_notification_sent = timezone.now()
                existing_subscription.save()

    # Receive message from WebSocket
    def receive(self, text_data):
        text_data_json = json.loads(text_data)
        # print(self.user)
        # print(text_data_json)
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
                            # try:
                            if True:
                                LotHistory.objects.create(
                                    lot=self.lot,
                                    user=self.user,
                                    message=message,
                                    changed_price=False,
                                    current_price=self.lot.high_bid,
                                )
                            # except Exception as e:
                            #     print(e)
                            async_to_sync(self.channel_layer.group_send)(
                                self.room_group_name,
                                {
                                    "type": "chat_message",
                                    "info": "CHAT",
                                    "message": message,
                                    "pk": self.user.pk,
                                    "username": str(self.user),
                                },
                            )
                    except:
                        pass
                    try:
                        # handle bids
                        amount = text_data_json["bid"]
                        error = check_bidding_permissions(self.lot, self.user)
                        if error:
                            async_to_sync(self.channel_layer.group_send)(
                                self.user_room_name,
                                {"type": "error_message", "error": error},
                            )
                        else:
                            result = bid_on_lot(self.lot, self.user, amount)
                            if result["send_to"] == "user":
                                if result["type"] == "ERROR":
                                    async_to_sync(self.channel_layer.group_send)(
                                        self.user_room_name,
                                        {
                                            "type": "error_message",
                                            "error": result["message"],
                                        },
                                    )
                                else:
                                    # I think just the sealed bid success and upping your own bids go here
                                    async_to_sync(self.channel_layer.group_send)(
                                        self.user_room_name,
                                        {
                                            "type": "chat_message",
                                            "info": result["type"],
                                            "message": result["message"],
                                            "high_bidder_pk": result["high_bidder_pk"],
                                            "high_bidder_name": result["high_bidder_name"],
                                            "current_high_bid": result["current_high_bid"],
                                        },
                                    )
                            else:
                                async_to_sync(self.channel_layer.group_send)(
                                    self.room_group_name,
                                    {
                                        "type": "chat_message",
                                        "info": result["type"],
                                        "message": result["message"],
                                        "high_bidder_pk": result["high_bidder_pk"],
                                        "high_bidder_name": result["high_bidder_name"],
                                        "current_high_bid": result["current_high_bid"],
                                        "date_end": result["date_end"],
                                    },
                                )
                    except:
                        pass
            except Exception as e:
                print(e)
        else:
            pass
            # print("user is not authorized")

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
