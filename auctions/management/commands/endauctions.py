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
            # Core: mark inactive and set winner/price. Everything else is "extra"
            # and must be guarded so it cannot prevent the lot from being sold.
            try:
                lot.active = False
                if not lot.sold:
                    lot.send_lot_end_message()
                lot.save()
            except Exception as e:
                logger.warning('Unable to set winner on "%s":', lot)
                logger.exception(e)
                continue

            try:
                lot.create_update_invoices()
            except Exception:
                logger.exception("create_update_invoices failed for lot %s", lot.pk)

            try:
                lot.send_non_auction_lot_emails()
            except Exception:
                logger.exception("send_non_auction_lot_emails failed for lot %s", lot.pk)

            relist = False
            sendNoRelistWarning = False
            try:
                relist, sendNoRelistWarning = lot.process_relist_logic()
            except Exception:
                logger.exception("process_relist_logic failed for lot %s", lot.pk)

            if sendNoRelistWarning:
                try:
                    current_site = Site.objects.get_current()
                    mail.send(
                        lot.user.email,
                        template="lot_ended_relist",
                        context={"domain": current_site.domain, "lot": lot},
                    )
                except Exception:
                    logger.exception("Failed to send relist warning email for lot %s", lot.pk)

            if relist:
                try:
                    lot.relist_lot()
                except Exception:
                    logger.exception("relist_lot failed for lot %s", lot.pk)

            try:
                lot.auto_award_bap_points()
            except Exception:
                logger.exception("auto_award_bap_points failed for lot %s", lot.pk)
        else:
            # note: once again, lots that are part of an in-person auction are not included here
            try:
                lot.send_ending_very_soon_message()
            except Exception as e:
                logger.warning('Unable to send ending-soon message for "%s":', lot)
                logger.exception(e)


class Command(BaseCommand):
    help = "Sets the winner, and winning price on all ended lots.  Send lot ending soon and lot ended messages to websocket connected users.  Sets active to false on lots"

    def handle(self, *args, **options):
        lots = Lot.objects.filter(active=True, is_deleted=False, banned=False, deactivated=False)
        declare_winners_on_lots(lots)
