import logging

from django.contrib.sites.models import Site
from django.core.management.base import BaseCommand
from post_office import mail

from auctions.models import Auction, Lot

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


def _club_awards_unsold_lots(club):
    """True when this club auto-awards BAP points to eligible unsold lots.

    That requires the breeder award program on, automatic awarding on, and the "only sold lots"
    restriction off. When all three hold, unsold lots should get points at wind-down the same way
    sold lots get them at sale time. auto_award_bap_points() enforces full eligibility itself and
    is idempotent, so this is only a cheap pre-filter to avoid touching lots that can never award."""
    return bool(club and club.enable_breeder_award_program and club.auto_add_points and not club.only_sold_lots)


def deactivate_pretty_much_over_lots():
    """Wind down auctions that are pretty_much_over: award BAP for unsold lots, then deactivate.

    Once an auction has been fully wound down for 24h+ (Auction.pretty_much_over), its still-active
    lots are stray: they clutter the default /lots/ browse view (which shows active=True). We flip
    them inactive so they drop out of that view. This is safe:
      * Lot.sold depends only on winner/auctiontos_winner + winning_price, never on active, so these
        lots can still be marked sold later.
      * "View lots for an auction" uses ?status=all, which skips the active filter, so they still
        show there.
    We also auto-award BAP for eligible *unsold* lots first, for clubs that award unsold lots. This
    is the moment in-person auction lots (which never flow through declare_winners_on_lots) finally
    get their points; auto_award_bap_points() is idempotent so already-awarded lots are untouched.
    """
    active_auction_ids = (
        Lot.objects.filter(active=True, is_deleted=False, banned=False, deactivated=False, auction__isnull=False)
        .values_list("auction_id", flat=True)
        .distinct()
    )
    over_auction_ids = [
        auction.pk for auction in Auction.objects.filter(pk__in=active_auction_ids) if auction.pretty_much_over
    ]
    if not over_auction_ids:
        return
    lots = Lot.objects.filter(
        auction_id__in=over_auction_ids,
        active=True,
        is_deleted=False,
        banned=False,
        deactivated=False,
    ).select_related("auction", "auction__club")
    for lot in lots:
        club = lot.auction.club if lot.auction else None
        if not lot.sold and _club_awards_unsold_lots(club) and not lot.bap_points_awarded and not lot.manually_approved:
            try:
                lot.auto_award_bap_points()
            except Exception:
                logger.exception("auto_award_bap_points failed for unsold lot %s", lot.pk)
    # Bulk-deactivate in one query; these lots are already wound down, so no per-lot save side effects.
    Lot.objects.filter(
        auction_id__in=over_auction_ids,
        active=True,
        is_deleted=False,
        banned=False,
        deactivated=False,
    ).update(active=False)


class Command(BaseCommand):
    help = "Sets the winner, and winning price on all ended lots.  Send lot ending soon and lot ended messages to websocket connected users.  Sets active to false on lots"

    def handle(self, *args, **options):
        lots = Lot.objects.filter(active=True, is_deleted=False, banned=False, deactivated=False)
        declare_winners_on_lots(lots)
        deactivate_pretty_much_over_lots()
