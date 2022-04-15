import datetime
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from auctions.models import Lot, LotHistory, LotImage
import channels.layers
from asgiref.sync import async_to_sync
from easy_thumbnails.files import get_thumbnailer
from post_office import mail
from django.contrib.sites.models import Site

def sendWarning(lot_number, result):
    channel_layer = channels.layers.get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f'lot_{lot_number}', result
    )

class Command(BaseCommand):
    help = 'Sets the winner, and winning price on all ended lots.  Send lot ending soon and lot ended messages to websocket connected users.  Sets active to false'

    def handle(self, *args, **options):
        lots = Lot.objects.filter(active=True)
        for lot in lots:
            if lot.ended:
                try:
                    lot.active = False
                    if lot.winner and lot.winning_price:
                        pass # some lots will be bought via buy now.  We still need to make those inactive
                    else:
                        if lot.high_bidder and not lot.banned:
                            lot.winner = lot.high_bidder
                            lot.winning_price = lot.high_bid
                            self.stdout.write(self.style.SUCCESS(f'{lot} has been won by {lot.high_bidder} for ${lot.high_bid}'))
                            info = 'LOT_END_WINNER'
                            bidder = lot.high_bidder
                            high_bidder_pk = lot.high_bidder.pk
                            high_bidder_name = str(lot.high_bidder)
                            current_high_bid = lot.high_bid
                            message = f"Won by {lot.high_bidder}"
                        else:
                            high_bidder_pk = None
                            high_bidder_name = None
                            current_high_bid = None
                            message = f"This lot did not sell"
                            bidder = None
                            info = 'ENDED_NO_WINNER'
                            self.stdout.write(self.style.SUCCESS(f'{lot} did not sell'))
                        result = {
                            'type': 'chat_message',
                            'info': info,
                            'message': message,
                            'high_bidder_pk': high_bidder_pk,
                            'high_bidder_name': high_bidder_name,
                            'current_high_bid': current_high_bid,
                        }
                        sendWarning(lot.lot_number, result)
                        LotHistory.objects.create(
                            lot = lot,
                            user = bidder,
                            message = message,
                            changed_price = True,
                            current_price=lot.high_bid,
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
                    lot.save()
                    if sendNoRelistWarning:
                        current_site = Site.objects.get_current()
                        mail.send(
                            lot.user.email,
                            template='lot_ended_relist',
                            context={'domain': current_site.domain, "lot": lot},
                        )
                    if relist:
                        originalImages = LotImage.objects.filter(lot_number=lot.pk)
                        originalPk = lot.pk
                        lot.pk = None # create a new, duplicate lot
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
                            newImage = LotImage.objects.create(createdon=originalImage.createdon, lot_number=lot, image_source=originalImage.image_source, is_primary=originalImage.is_primary)
                            newImage.image = get_thumbnailer(originalImage.image)
                            # if the original lot sold, this picture sure isn't of the actual item
                            if originalImage.image_source == "ACTUAL":
                                newImage.image_source = 'REPRESENTATIVE'
                            newImage.save()
                except Exception as e:
                    self.stdout.write(self.style.ERROR('Unable to set winner on "%s"' % lot))
                    self.stdout.write(e)
            else:
                if lot.ending_very_soon and not lot.winner:
                    result = {
                        'type': 'chat_message',
                        'info': 'CHAT',
                        'message': "Bidding ends in less than a minute!!",
                        'pk': -1,
                        'username': "System",
                        }
                    sendWarning(lot.lot_number, result)
