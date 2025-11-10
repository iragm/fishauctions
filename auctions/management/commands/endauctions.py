import logging

from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand
from post_office import mail

from auctions.models import Lot

logger = logging.getLogger(__name__)


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
                if not lot.sold:
                    # Send lot end message (winner or no winner) and create LotHistory
                    lot.send_lot_end_message()

                # Update invoices
                lot.create_update_invoices

                # Send emails for non-auction lots
                lot.send_non_auction_lot_emails()

                # Handle automatic relisting
                relist, sendNoRelistWarning = lot.process_relist_logic()

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
                    lot.relist_lot()
            except Exception as e:
                logger.warning('Unable to set winner on "%s":', lot)
                logger.exception(e)
        else:
            # note: once again, lots that are part of an in-person auction are not included here
            lot.send_ending_very_soon_message()


class Command(BaseCommand):
    help = "Sets the winner, and winning price on all ended lots.  Send lot ending soon and lot ended messages to websocket connected users.  Sets active to false on lots"

    def handle(self, *args, **options):
        lots = Lot.objects.filter(active=True, is_deleted=False, banned=False, deactivated=False)
        declare_winners_on_lots(lots)
