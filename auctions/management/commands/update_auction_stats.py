import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from auctions.models import Auction

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Update cached statistics for auctions whose next_update_due is past due"

    def handle(self, *args, **options):
        now = timezone.now()
        # Find auctions that need stats recalculation
        auctions = Auction.objects.filter(
            is_deleted=False,
            next_update_due__lte=now,
        )
        
        count = auctions.count()
        if count == 0:
            logger.info("No auctions need stats updates at this time")
            return
        
        logger.info(f"Updating stats for {count} auction(s)")
        
        for auction in auctions:
            try:
                logger.info(f"Recalculating stats for auction: {auction.title} ({auction.slug})")
                auction.recalculate_stats()
                logger.info(f"Successfully updated stats for auction: {auction.title}")
            except Exception as e:
                logger.error(f"Failed to update stats for auction {auction.title} ({auction.slug}): {e}")
                logger.exception(e)
                # Continue with other auctions even if one fails
                continue
        
        logger.info(f"Completed stats update for {count} auction(s)")
