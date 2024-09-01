import datetime

import channels.layers
from asgiref.sync import async_to_sync
from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand
from django.db.models import F
from django.utils import timezone
from easy_thumbnails.files import get_thumbnailer
from post_office import mail

from auctions.models import AuctionTOS, Invoice, Lot, LotHistory, LotImage


def sendWarning(lot_number, result):
    channel_layer = channels.layers.get_channel_layer()
    async_to_sync(channel_layer.group_send)(f"lot_{lot_number}", result)


def fix_seller_info(part_of_auction):
    """This function is really not needed - it's been run once to update all existing lots.
    Fill out the user if auctiontos_seller is set, or auctiontos_seller if the user is set."""
    # first, let's fill out the seller info (1 of 2)
    needs_auctiontos_seller_filled_out = part_of_auction.filter(
        auctiontos_seller__isnull=True, auction__auctiontos__user=F("user")
    )
    for lot in needs_auctiontos_seller_filled_out:
        auctionTOS = AuctionTOS.objects.filter(
            user=lot.user, auction=lot.auction
        ).first()
        lot.auctiontos_seller = auctionTOS
        lot.save()  # presave receiver will make an invoice for this lot/seller

    # next, let's fill out the user info (2 of 2)
    # user when auctiontos filled out - handy for when users are part of an auction (auctiontos__user__isnull=True), but then later sign up
    # User is set for auctiontos on user sign in event - see models.user_logged_in_callback()
    needs_user_filled_out = part_of_auction.filter(
        auctiontos_seller__isnull=False,
        user__isnull=True,
        auctiontos_seller__user__isnull=False,
        auction__auctiontos=F("auctiontos_seller"),
    )
    for lot in needs_user_filled_out:
        user = lot.auctiontos_seller.user
        print("setting user based on auction tos:")
        print(lot, lot.auctiontos_seller, user)
        lot.user = user
        lot.save()


def declare_winners_on_lots(lots):
    """Set the winner and winning price on all lots"""
    for lot in lots:
        if lot.ended:
            # note - lots that are part of in-person auctions will only get here if a winner and price is set
            # they always have lot.ended = False
            # But, see https://github.com/iragm/fishauctions/issues/116
            try:
                lot.active = False
                # lots with a winner or auctiontos winner and winning price are "sold"
                if lot.sold:
                    # lots will be bought via buy now will get here
                    # as well as in-person auction lots whose winner has been set manually
                    # We still need to make those active=False, done above, and then save, below
                    pass
                else:
                    info = None
                    if lot.high_bidder:  # and not lot.sold: # not sure what that was here, we already filter this in the if above
                        lot.winner = lot.high_bidder
                        lot.winning_price = lot.high_bid
                        info = "LOT_END_WINNER"
                        bidder = lot.high_bidder
                        high_bidder_pk = lot.high_bidder.pk
                        high_bidder_name = str(lot.high_bidder_display)
                        current_high_bid = lot.high_bid
                        message = f"Won by {lot.high_bidder_display}"
                        lot.save()
                    # at this point, the lot should have a winner filled out if it's sold.  If it still doesn't:
                    if not lot.sold:
                        high_bidder_pk = None
                        high_bidder_name = None
                        current_high_bid = None
                        message = "This lot did not sell"
                        bidder = None
                        info = "ENDED_NO_WINNER"
                    result = {
                        "type": "chat_message",
                        "info": info,
                        "message": message,
                        "high_bidder_pk": high_bidder_pk,
                        "high_bidder_name": high_bidder_name,
                        "current_high_bid": current_high_bid,
                    }
                    if info:
                        sendWarning(lot.lot_number, result)
                        LotHistory.objects.create(
                            lot=lot,
                            user=bidder,
                            message=message,
                            changed_price=True,
                            current_price=lot.high_bid,
                        )
                # if this is part of an auction, update invoices
                if lot.sold and lot.auction:
                    if lot.auctiontos_winner:
                        auctiontos_winner = lot.auctiontos_winner
                    else:
                        # look for the TOS and create the invoice
                        auctiontos_winner = AuctionTOS.objects.filter(
                            auction=lot.auction, user=lot.high_bidder
                        ).first()
                        if auctiontos_winner:
                            lot.auctiontos_winner = auctiontos_winner
                            lot.save()
                    if lot.auctiontos_winner:
                        invoice, created = Invoice.objects.get_or_create(
                            auctiontos_user=lot.auctiontos_winner,
                            auction=lot.auction,
                            defaults={},
                        )
                        invoice.recalculate
                    if lot.auctiontos_seller:
                        invoice, created = Invoice.objects.get_or_create(
                            auctiontos_user=lot.auctiontos_seller,
                            auction=lot.auction,
                            defaults={},
                        )
                        invoice.recalculate
                # logic to email winner and buyer for lots not in an auction
                if lot.winner and not lot.auction:
                    current_site = Site.objects.get_current()
                    # email the winner first
                    mail.send(
                        lot.winner.email,
                        headers={"Reply-to": lot.user.email},
                        template="non_auction_lot_winner",
                        context={"lot": lot, "domain": current_site.domain},
                    )
                    # now, email the seller
                    mail.send(
                        lot.user.email,
                        headers={"Reply-to": lot.winner.email},
                        template="non_auction_lot_seller",
                        context={"lot": lot, "domain": current_site.domain},
                    )
                # automatic relisting of lots
                relist = False
                sendNoRelistWarning = False
                if not lot.auction:
                    if lot.winner and lot.relist_if_sold and (not lot.relist_countdown):
                        sendNoRelistWarning = True
                    if (
                        (not lot.winner)
                        and lot.relist_if_not_sold
                        and (not lot.relist_countdown)
                    ):
                        sendNoRelistWarning = True
                    if lot.winner and lot.relist_if_sold and lot.relist_countdown:
                        lot.relist_countdown -= 1
                        relist = True
                    if (
                        (not lot.winner)
                        and lot.relist_if_not_sold
                        and lot.relist_countdown
                    ):
                        # no need to relist unsold lots, just decrement the countdown
                        lot.relist_countdown -= 1
                        lot.date_end = timezone.now() + datetime.timedelta(
                            days=lot.lot_run_duration
                        )
                        lot.active = True
                        lot.seller_invoice = None
                        lot.buyer_invoice = None
                # this is needed for any changes made above, as well as in-person and buy now auction lots
                lot.save()
                if sendNoRelistWarning:
                    current_site = Site.objects.get_current()
                    mail.send(
                        lot.user.email,
                        template="lot_ended_relist",
                        context={"domain": current_site.domain, "lot": lot},
                    )
                if relist:
                    originalImages = LotImage.objects.filter(lot_number=lot.pk)
                    originalPk = lot.pk
                    lot.pk = None  # create a new, duplicate lot
                    lot.date_end = timezone.now() + datetime.timedelta(
                        days=lot.lot_run_duration
                    )
                    lot.active = True
                    lot.winner = None
                    lot.winning_price = None
                    lot.seller_invoice = None
                    lot.buyer_invoice = None
                    lot.buy_now_used = False
                    lot.save()
                    for location in Lot.objects.get(
                        lot_number=originalPk
                    ).shipping_locations.all():
                        lot.shipping_locations.add(location)
                    for originalImage in originalImages:
                        newImage = LotImage.objects.create(
                            createdon=originalImage.createdon,
                            lot_number=lot,
                            image_source=originalImage.image_source,
                            is_primary=originalImage.is_primary,
                        )
                        newImage.image = get_thumbnailer(originalImage.image)
                        # if the original lot sold, this picture sure isn't of the actual item
                        if originalImage.image_source == "ACTUAL":
                            newImage.image_source = "REPRESENTATIVE"
                        newImage.save()
            except Exception as e:
                print(f'Unable to set winner on "{lot}":')
                print(e)
        else:
            # note: once again, lots that are part of an in-person auction are not included here
            if lot.ending_very_soon and not lot.sold:
                result = {
                    "type": "chat_message",
                    "info": "CHAT",
                    "message": "Bidding ends in less than a minute!!",
                    "pk": -1,
                    "username": "System",
                }
                sendWarning(lot.lot_number, result)


def fix_winner_info(part_of_auction):
    """set winner if auctiontos_winner is set, and auctiontos_winner if winner is set. It's a good idea to run this AFTER declaring winners"""
    # set auctiontos_winner if only winner is set
    needs_auctiontos_winner_filled_out = part_of_auction.filter(
        winner_isnull=False,
        auctiontos_winner__isnull=True,
        auction__auctiontos__user=F("winner"),
    )
    for lot in needs_auctiontos_winner_filled_out:
        auctionTOS = AuctionTOS.objects.filter(
            user=lot.winner, auction=lot.auction
        ).first()
        lot.auctiontos_winner = auctionTOS
        lot.save()
        invoice, created = Invoice.objects.get_or_create(
            auctiontos_user=lot.auctiontos_winner, auction=lot.auction, defaults={}
        )

    # declate winners where there is already an auctiontos_winner (hopefully rare - but see models.user_logged_in_callback()
    needs_winner_filled_out = part_of_auction.filter(
        auctiontos_winner__isnull=False,
        winner__isnull=True,
        auctiontos_winner__user__isnull=False,
        auction__auctiontos=F("auctiontos_winner"),
    )
    for lot in needs_winner_filled_out:
        winner = lot.auctiontos_seller.user
        print("setting winner based on auction tos:")
        print(lot, lot.auctiontos_winner, winner)
        # fixme - this code is not yet fully tested, no saving yet!!
        # lot.winner = winner
        # lot.save()


class Command(BaseCommand):
    help = "Sets the winner, and winning price on all ended lots.  Send lot ending soon and lot ended messages to websocket connected users.  Sets active to false on lots"

    def handle(self, *args, **options):
        lots = Lot.objects.filter(is_deleted=False, banned=False, deactivated=False)
        # part_of_auction = lots.filter(auction__isnull=False)

        # fix_seller_info(part_of_auction)
        declare_winners_on_lots(lots.filter(active=True))
        # fix_winner_info(part_of_auction)
