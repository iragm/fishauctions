"""Recording a lot's sale: the one code path for the set-winners page, the palette and the app's offline sync.

A sale is written under the lot's row lock, so two clerks (or a clerk and an offline sync) can't both
win it. Anything that must wait for the sale to commit (the running-total push, bap points) runs
``on_commit``, and never inside the lock.
"""

import logging

from django.db import transaction
from django.utils import timezone

from auctions.models import Lot
from auctions.notifications import notify_running_total

logger = logging.getLogger(__name__)


class AlreadySold(Exception):
    """The lot already has a winner and price, so a sale that didn't force it is refused. ``lot`` is the
    row as it is now, locked for the caller to compare against.
    """

    def __init__(self, lot):
        super().__init__(f"Lot {lot.pk} is already sold")
        self.lot = lot


def is_sold(lot):
    """Exactly :attr:`Lot.sold`: a winner and a truthy price. A $0 "winner" is not a sale, since clubs enter
    0 to mean the lot ended unsold.
    """
    return lot.sold


def locked_lot(lot):
    """The lot's current row, locked until the enclosing transaction ends. Call inside ``transaction.atomic``."""
    return Lot._base_manager.select_for_update().get(pk=lot.pk)


def record_sale(lot, winning_tos, winning_price, user, *, force=False, sold_at=None):
    """Set ``winning_tos`` as the winner of ``lot`` at ``winning_price``. Raises :class:`AlreadySold` when the
    lot is already sold, unless ``force`` (the page's "Ignore errors and save") takes it off the old winner.
    """
    with transaction.atomic():
        fresh = locked_lot(lot)
        if is_sold(fresh) and not force:
            raise AlreadySold(fresh)
        # A force over an earlier sale takes the lot off the old winner's and seller's invoices, which then
        # need their totals recalculated; only setting a winner recalculates, so whoever loses the lot would
        # keep a stale total.
        stale_invoices = [invoice for invoice in (fresh.winner_invoice, fresh.sellers_invoice) if invoice]
        lot.auctiontos_winner = winning_tos
        lot.winning_price = winning_price
        lot.date_end = sold_at or timezone.now()
        lot.active = False
        lot.save()
        if (
            lot.auction
            and lot.auction.use_check_in_mode
            and lot.auctiontos_seller
            and not lot.auctiontos_seller.checked_in
        ):
            seller = lot.auctiontos_seller
            seller.checked_in = timezone.now()
            update_fields = ["checked_in"]
            if not seller.bidding_allowed:
                seller.bidding_allowed = True
                update_fields.append("bidding_allowed")
            seller.save(update_fields=update_fields)
            lot.auction.create_history(
                applies_to="USERS",
                action=f"Checked in {seller.name} (lot sold)",
                user=user,
            )
        try:
            lot.add_winner_message(user, winning_tos, winning_price)
        except Exception:
            logger.exception("add_winner_message failed for lot %s", lot.pk)
        for invoice in {invoice.pk: invoice for invoice in stale_invoices}.values():
            try:
                invoice.recalculate()
            except Exception:
                logger.exception("Recalculating the previous invoices failed for lot %s", lot.pk)
        transaction.on_commit(lambda: _after_commit(lot))


def check_in_winner(winner, auction, user, action):
    """Check a bidder in when a sale is recorded in check-in mode, with the history line ``action``."""
    winner.checked_in = timezone.now()
    update_fields = ["checked_in"]
    if not winner.bidding_allowed:
        winner.bidding_allowed = True
        update_fields.append("bidding_allowed")
    winner.save(update_fields=update_fields)
    auction.create_history(applies_to="USERS", action=action, user=user)


def _after_commit(lot):
    """Side effects that must see the committed sale: the winner's running-total push, and bap points."""
    try:
        notify_running_total(lot)
    except Exception:
        logger.exception("notify_running_total failed for lot %s", lot.pk)
    if lot.auction and lot.auction.club and not lot.bap_points_awarded and not lot.manually_approved:
        try:
            lot.auto_award_bap_points()
        except Exception:
            logger.exception("auto_award_bap_points failed for lot %s", lot.pk)
