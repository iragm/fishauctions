import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from auctions.models import Auction

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Update cached statistics for auctions whose next_update_due is past due"

    def handle(self, *args, **options):
        now = timezone.now()
        # Find auctions that need stats recalculation
        auctions = Auction.objects.filter(
            Q(next_update_due__lte=now) | Q(next_update_due__isnull=True),
            is_deleted=False,
        )

        count = auctions.count()
        if count == 0:
            logger.info("No auctions need stats updates at this time")
            return

        logger.info("Updating stats for %d auction(s)", count)

        for auction in auctions:
            try:
                logger.info("Recalculating stats for auction: %s (%s)", auction.title, auction.slug)
                auction.recalculate_stats()
                logger.info("Successfully updated stats for auction: %s", auction.title)
                
                # Send WebSocket notification to connected users
                channel_layer = get_channel_layer()
                async_to_sync(channel_layer.group_send)(
                    f"auctions_{auction.pk}",
                    {
                        "type": "stats_updated",
                        "auction_pk": auction.pk,
                    }
                )
                logger.debug("Sent stats_updated notification for auction: %s", auction.pk)
            except Exception as e:
                logger.error("Failed to update stats for auction %s (%s): %s", auction.title, auction.slug, e)
                logger.exception(e)
                # Continue with other auctions even if one fails
                continue

        logger.info("Completed stats update for %d auction(s)", count)
