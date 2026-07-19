"""Re-solve AR lot positions for auctions with fresh sightings, and prune the observation buffer.

Runs on a 60 s beat (the map is an admin overview, not a live tracker). Each pass:

1. deletes ``LotObservation`` rows older than 24 h (the rolling buffer's hard cap),
2. re-solves every auction that was flagged dirty by the observations endpoint — plus, as a DB
   safety net against a lost cache flag, any auction that still has live observations or existing
   positions — via :func:`auctions.ar_mapping.update_positions_for_auction`.

Solving an auction with no surviving observations simply deletes its stale positions, so a table
that emptied out cleans itself up.
"""

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Solve flagged auctions' AR lot positions and prune observations older than 24 h."

    def handle(self, *args, **options):
        from auctions.ar_mapping import WINDOW_HOURS, update_positions_for_auction
        from auctions.mobile.services import ar as ar_service
        from auctions.models import Auction, LotObservation, LotPosition

        cutoff = timezone.now() - timedelta(hours=WINDOW_HOURS)
        pruned, _ = LotObservation.objects.filter(captured_at__lt=cutoff).delete()

        pks = ar_service.drain_dirty_auction_pks()
        pks |= set(LotObservation.objects.values_list("auction_id", flat=True).distinct())
        pks |= set(LotPosition.objects.values_list("auction_id", flat=True).distinct())

        solved_auctions = 0
        solved_lots = 0
        for auction in Auction.objects.filter(pk__in=pks):
            solved_lots += update_positions_for_auction(auction)
            solved_auctions += 1

        logger.info(
            "update_ar_positions: pruned %s observation(s); solved %s lot(s) across %s auction(s)",
            pruned,
            solved_lots,
            solved_auctions,
        )
