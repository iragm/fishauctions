# chat/consumers.py
import json
from asgiref.sync import async_to_sync
from channels.generic.websocket import WebsocketConsumer
from .models import Lot, Bid, UserInterestCategory, LotHistory
from post_office import mail
from django.contrib.sites.models import Site
from django.conf import settings
from django.utils import timezone
def check_bidding_permissions(lot, user):
    """
    Returns false if everything is OK, or a string error message
    call check_all_permissions first
    """
    if lot.ended:
        return "Bidding on this lot has ended"
    if lot.user.pk == user.pk:
        return "You can't bid on your own lot"
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
    try:
        ban = UserBan.objects.get(banned_user=user.pk, user=lot.user.pk)
        return "This user has banned you from bidding on their lots"
    except:
        pass
    try:
        ban = UserBan.objects.get(banned_user=user.pk, user=lot.auction.created_by.pk)
        return "The owner of this auction has banned you from bidding"
    except:
        pass
    if lot.banned:
        return "This lot has been banned"
    return False

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
    #if True:
        amount = int(amount)
        result = {  "type": "ERROR",
                    "message": "Override this message",
                    "send_to": 'user',
                    "high_bidder_pk": None,
                    "high_bidder_name": None,
                    "current_high_bid": None,
                    'winner': None,
                }
        if lot.ended:
            result['message'] = "Bidding has ended"
            return result
        if lot.winner:
            result['message'] = "This lot has already been sold"
            return result
        originalHighBidder = lot.high_bidder
        originalBid = lot.high_bid
        originalMaxBid = lot.max_bid
        bid, created = Bid.objects.get_or_create(
                    user = user,
                    lot_number = lot,
                    defaults={'amount': amount},
            )
        bid.last_bid_time = timezone.now()
        # also update category interest, max one per bid
        interest, interestCreated = UserInterestCategory.objects.get_or_create(
            category=lot.species_category,
            user=user,
            defaults={ 'interest': settings.BID_WEIGHT }
            )
        if not interestCreated:
            interest.interest += settings.BID_WEIGHT
            interest.save()
        if lot.sealed_bid:
            bid.was_high_bid = True
            bid.amount = amount
            bid.save()
            result['type'] = "INFO"
            result['message'] = "Bid placed!  You can change your bid at any time until the auction ends"
            result["send_to"] = 'user'
            #result["high_bidder_pk"] = user.pk
            #result["high_bidder_name"] = str(user)
            result["current_high_bid"] = bid.amount
            return result
        else:
            if not created:
                if amount <= bid.amount:
                    result['message'] = f"Bid more than your proxy bid (${bid.amount})"
                    return result
                else:
                    bid.amount = amount
                    # bid.amount now contains the actual bid, regardless of whether it was new or not
                    bid.save()
            # from here on, lot.high_bidder and lot.high_bid will include the current bid
            if lot.buy_now_price and not originalHighBidder:
                if bid.amount >= lot.buy_now_price:
                    lot.winner = user
                    lot.winning_price = lot.buy_now_price
                    lot.date_end = timezone.now()
                    lot.watch_warning_email_sent = True
                    lot.save()
                    result['send_to'] = 'everyone'
                    result['high_bidder_pk'] = user.pk   
                    result['high_bidder_name'] = str(user)
                    result['type'] = "LOT_END_WINNER"
                    result['message'] = f"{user} bought this lot now!!"
                    result["current_high_bid"] = lot.buy_now_price
                    bid.was_high_bid = True
                    bid.save()
                    LotHistory.objects.create(
                        lot = lot,
                        user = user,
                        message = result['message'],
                        changed_price = True,
                        current_price=result["current_high_bid"],
                    )
                    return result
            if (not originalHighBidder) and (lot.high_bidder.pk == user.pk):
                result['send_to'] = 'everyone'
                result['type'] = "NEW_HIGH_BIDDER"
                result['message'] = f"{user} has placed the first bid on this lot!"
                result["current_high_bid"] = lot.reserve_price
                result['high_bidder_pk'] = user.pk   
                result['high_bidder_name'] = str(user)
                bid.was_high_bid = True
                LotHistory.objects.create(
                    lot = lot,
                    user = user,
                    message = result['message'],
                    changed_price = True,
                    current_price=result["current_high_bid"],
                    )
                bid.save()
                return result
            if bid.amount <= originalBid: # changing this to < would allow bumping without being the high bidder
                # there's a high bidder already
                bid.save() # save the current bid regardless
                result['message'] = f"You can't bid less than ${originalBid + 1}"
                return result
            # if we get to this point, the user has bid >= the high bid
            bid.was_high_bid = True
            bid.save()
            if lot.high_bidder.pk == user.pk:
                try:
                    if originalHighBidder.pk == lot.high_bidder.pk:
                        # user is upping their own price, don't tell other people about it
                        result['type'] = "INFO"
                        result['message'] = f"You've raised your proxy bid to ${bid.amount}"
                        return result
                except:
                    pass
                
                # bidder has changed!!
                result['type'] = "NEW_HIGH_BIDDER"
                result['message'] = f"{lot.high_bidder} is now the high bidder at ${lot.high_bid}!"
                result['high_bidder_pk'] = lot.high_bidder.pk
                result['high_bidder_name'] = str(lot.high_bidder)
                result["current_high_bid"] = lot.high_bid
                result['send_to'] = 'everyone'
                # email the old one
                current_site = Site.objects.get_current()
                print('sending emialk')
                mail.send(
                    originalHighBidder.email,
                    template='outbid_notification',
                    context={'name': originalHighBidder.first_name, 'domain': current_site.domain, 'lot': lot},
                )
                LotHistory.objects.create(
                    lot = lot,
                    user = user,
                    message = result['message'],
                    changed_price = True,
                    current_price=result["current_high_bid"],
                    )
                return result
            result['type'] = "NEW_HIGH_BID"
            result["current_high_bid"] = lot.high_bid
            result['message'] = f"{user} bumped the price up to ${lot.high_bid}!"
            result['send_to'] = 'everyone'
            LotHistory.objects.create(
                lot = lot,
                user = user,
                message = result['message'],
                changed_price = True,
                current_price=result["current_high_bid"],
                )
            return result
    except Exception as e:
        print(e)

class LotConsumer(WebsocketConsumer):
    def connect(self):
        try:
            self.lot_number = self.scope['url_route']['kwargs']['lot_number']
            self.user = self.scope["user"]
            self.room_group_name = f'lot_{self.lot_number}'
            self.user_room_name = f"private_user_{self.user.pk}_lot_{self.lot_number}"
            self.lot = Lot.objects.get(pk=self.lot_number)

            # Join room group
            async_to_sync(self.channel_layer.group_add)(
                self.room_group_name,
                self.channel_name
            )

            # Join private room for notifications only to this user
            async_to_sync(self.channel_layer.group_add)(
                self.user_room_name,
                self.channel_name
            )
            self.accept()
            # send the most recent history
            allHistory = LotHistory.objects.filter(lot = self.lot).order_by('-timestamp')[:200]
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
                            'type': 'chat_message',
                            'pk': pk,
                            'info': "CHAT",
                            'message': history.message,
                            'username': username
                        }
                    )
                except Exception as e:
                    print(e)
        except Exception as e:
            print(e)
    def disconnect(self, close_code):
        # Leave room group
        async_to_sync(self.channel_layer.group_discard)(
            self.room_group_name,
            self.channel_name
        )
        if self.user.pk == self.lot.user.pk:
            #print("lot owner is leaving the chat, marking all chats as seen")
            LotHistory.objects.filter(lot=self.lot.pk, seen=False).update(seen=True)

    # Receive message from WebSocket
    def receive(self, text_data):
        text_data_json = json.loads(text_data)
        #print(self.user)
        #print(text_data_json)
        if self.user.is_authenticated:
            try:
                error = check_all_permissions(self.lot, self.user)
                if error:
                    if error:
                        async_to_sync(self.channel_layer.group_send)(
                            self.user_room_name,
                            {
                                'type': 'error_message',
                                'error': error
                            }
                        )
                else:                
                    try:
                        message = text_data_json['message']
                        error = check_chat_permissions(self.lot, self.user)
                        if error:
                            async_to_sync(self.channel_layer.group_send)(
                                self.user_room_name,
                                {
                                    'type': 'error_message',
                                    'error': error
                                }
                            )
                        else:
                            # try:
                            if True:
                                LotHistory.objects.create(
                                    lot = self.lot,
                                    user = self.user,
                                    message = message,
                                    changed_price = False,
                                    current_price=self.lot.high_bid,
                                    )
                            # except Exception as e:
                            #     print(e)
                            async_to_sync(self.channel_layer.group_send)(
                                self.room_group_name,
                                {
                                    'type': 'chat_message',
                                    'info': 'CHAT',
                                    'message': message,
                                    'pk':self.user.pk,
                                    'username': str(self.user),
                                }
                            )
                    except:
                        pass
                    try:
                        # handle bids
                        amount = text_data_json['bid']
                        error = check_bidding_permissions(self.lot, self.user)
                        if error:
                            async_to_sync(self.channel_layer.group_send)(
                                self.user_room_name,
                                {
                                    'type': 'error_message',
                                    'error': error
                                }
                            )
                        else:
                            result = bid_on_lot(self.lot, self.user, amount)
                            if result['send_to'] == 'user':
                                if result['type'] == "ERROR":
                                    async_to_sync(self.channel_layer.group_send)(
                                        self.user_room_name,
                                        {
                                            'type': 'error_message',
                                            'error': result['message']
                                        }
                                    )
                                else:
                                    # I think just the sealed bid success and upping your own bids go here
                                    async_to_sync(self.channel_layer.group_send)(
                                        self.user_room_name,
                                        {
                                            'type': 'chat_message',
                                            'info': result["type"],
                                            'message': result["message"],
                                            'high_bidder_pk': result["high_bidder_pk"],
                                            'high_bidder_name': result["high_bidder_name"],
                                            'current_high_bid': result["current_high_bid"],
                                        }
                                    )
                            else:
                                async_to_sync(self.channel_layer.group_send)(
                                    self.room_group_name,
                                    {
                                        'type': 'chat_message',
                                        'info': result["type"],
                                        'message': result["message"],
                                        'high_bidder_pk': result["high_bidder_pk"],
                                        'high_bidder_name': result["high_bidder_name"],
                                        'current_high_bid': result["current_high_bid"],
                                    }
                                )
                    except:
                        pass
            except Exception as e:
                print(e)
        else:
            pass
            #print("user is not authorized")

    # Send a toast error to a single user
    def error_message(self, event):
        error = event['error']
        # Send error to WebSocket
        self.send(text_data=json.dumps({
            'error': error,
        }))

    # Receive message from room group
    def chat_message(self, event):
        self.send(text_data=json.dumps(event))
