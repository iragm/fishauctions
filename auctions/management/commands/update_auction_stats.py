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
        # Process only one auction per run, ordered by most overdue first
        auction = (
            Auction.objects.filter(
                Q(next_update_due__lte=now) | Q(next_update_due__isnull=True),
                is_deleted=False,
            )
            .order_by("next_update_due")
            .first()
        )

        if auction:
            try:
                logger.info("Recalculating stats for auction: %s (%s)", auction.title, auction.slug)
                
                # Set next_update_due before recalculating to prevent concurrent recalculations
                # This ensures that if the recalculation takes longer than a minute,
                # the cron job won't try to recalculate the same auction again
                auction.next_update_due = now + timezone.timedelta(minutes=5)
                auction.save(update_fields=["next_update_due"])
                
                auction.recalculate_stats()

                auction_websocket = get_channel_layer()
                async_to_sync(auction_websocket.group_send)(
                    f"auctions_{auction.pk}",
                    {
                        "type": "stats_updated",
                    },
                )
                logger.info("Successfully updated stats for auction: %s", auction.title)
            except Exception as e:
                logger.error("Failed to update stats for auction %s (%s): %s", auction.title, auction.slug, e)
                logger.exception(e)
