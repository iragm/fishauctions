import datetime

from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand
from django.utils import timezone
from easy_thumbnails.files import get_thumbnailer
from post_office import mail

from auctions.models import Lot, LotHistory, LotImage


def declare_winners_on_lots(lots):
    """Set the winner and winning price on all lots"""
    for lot in lots:
        if lot.ended:
            # note - lots that are part of in-person auctions will not get here
            # if they are active, they always have lot.ended = False, and if they are sold,
            # the method that sells them should set active=False, so they won't be filtered here
            # But, see https://github.com/iragm/fishauctions/issues/116
            try:
                lot.active = False
                # lots with a winner or auctiontos winner and winning price are "sold"
                if lot.sold:
                    # lots will be bought via buy now will get here
                    # as well as in-person auction lots whose winner has been set manually
                    # We still need to make those active=False, done above, and then save, below
                    # I don't think the above is true anymore, lots bought with buy now should also be made inactive
                    pass
                else:
                    info = None
                    if lot.high_bidder:
                        lot.sell_to_online_high_bidder
                        info = "LOT_END_WINNER"
                        bidder = lot.high_bidder
                        high_bidder_pk = lot.high_bidder.pk
                        high_bidder_name = str(lot.high_bidder_display)
                        current_high_bid = lot.high_bid
                        message = f"Won by {lot.high_bidder_display}"
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
                        lot.send_websocket_message(result)
                        LotHistory.objects.create(
                            lot=lot,
                            user=bidder,
                            message=message,
                            changed_price=True,
                            current_price=lot.high_bid,
                        )
                lot.create_update_invoices
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
                    if (not lot.winner) and lot.relist_if_not_sold and (not lot.relist_countdown):
                        sendNoRelistWarning = True
                    if lot.winner and lot.relist_if_sold and lot.relist_countdown:
                        lot.relist_countdown -= 1
                        relist = True
                    if (not lot.winner) and lot.relist_if_not_sold and lot.relist_countdown:
                        # no need to relist unsold lots, just decrement the countdown
                        lot.relist_countdown -= 1
                        lot.date_end = timezone.now() + datetime.timedelta(days=lot.lot_run_duration)
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
                    lot.date_end = timezone.now() + datetime.timedelta(days=lot.lot_run_duration)
                    lot.active = True
                    lot.winner = None
                    lot.winning_price = None
                    lot.seller_invoice = None
                    lot.buyer_invoice = None
                    lot.buy_now_used = False
                    lot.save()
                    for location in Lot.objects.get(lot_number=originalPk).shipping_locations.all():
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
                lot.send_websocket_message(result)


class Command(BaseCommand):
    help = "Sets the winner, and winning price on all ended lots.  Send lot ending soon and lot ended messages to websocket connected users.  Sets active to false on lots"

    def handle(self, *args, **options):
        lots = Lot.objects.filter(active=True, is_deleted=False, banned=False, deactivated=False)
        declare_winners_on_lots(lots)
