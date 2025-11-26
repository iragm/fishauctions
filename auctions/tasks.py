"""
Celery tasks for the auctions app.

This module contains all Celery tasks that were previously run as cron jobs.
Each task wraps a management command to maintain backward compatibility.
"""

from celery import shared_task
from django.core.management import call_command


@shared_task(bind=True, ignore_result=True)
def endauctions(self):
    """
    Set the winner and winning price on all ended lots.
    Send lot ending soon and lot ended messages to websocket connected users.
    Sets active to false on lots.

    Previously run every minute via cron.
    """
    call_command("endauctions")


@shared_task(bind=True, ignore_result=True)
def sendnotifications(self):
    """
    Send notifications about watched items.

    Previously run every 15 minutes via cron.
    """
    call_command("sendnotifications")


@shared_task(bind=True, ignore_result=True)
def auctiontos_notifications(self):
    """
    Welcome and print reminder emails.

    Previously run every 15 minutes via cron.
    """
    call_command("auctiontos_notifications")


@shared_task(bind=True, ignore_result=True)
def email_invoice(self):
    """
    Email users about invoices.

    Previously run every 15 minutes via cron.
    """
    call_command("email_invoice")


@shared_task(bind=True, ignore_result=True)
def auction_emails(self):
    """
    Send auction-related drip marketing emails.

    Previously run every 4 minutes via cron.
    """
    call_command("auction_emails")


@shared_task(bind=True, ignore_result=True)
def email_unseen_chats(self):
    """
    Send notifications about unread chat messages.

    Previously run daily at 10:00 via cron.
    """
    call_command("email_unseen_chats")


@shared_task(bind=True, ignore_result=True)
def weekly_promo(self):
    """
    Send weekly promotional email advertising auctions and lots near you.

    Previously run weekly on Wednesday at 9:30 via cron.
    """
    call_command("weekly_promo")


@shared_task(bind=True, ignore_result=True)
def set_user_location(self):
    """
    Set user lat/long based on their IP address.

    Previously run every 2 hours via cron.
    """
    call_command("set_user_location")


@shared_task(bind=True, ignore_result=True)
def remove_duplicate_views(self):
    """
    Remove duplicate page views.

    Previously run every 15 minutes via cron.
    """
    call_command("remove_duplicate_views")


@shared_task(bind=True, ignore_result=True)
def webpush_notifications_deduplicate(self):
    """
    Deduplicate web push notification subscriptions.

    Previously run daily at 10:00 via cron.
    """
    call_command("webpush_notifications_deduplicate")


def schedule_next_auction_stats_update():
    """
    Schedule the next auction stats update based on when the next auction update is due.

    This function finds the earliest `next_update_due` timestamp among all auctions
    and schedules the update_auction_stats task to run at that time. If no auctions
    need updates, it schedules a fallback check in 1 hour.
    """
    from django.utils import timezone

    from auctions.models import Auction

    now = timezone.now()

    # Find the next auction that needs updating
    next_auction = (
        Auction.objects.filter(is_deleted=False, next_update_due__isnull=False).order_by("next_update_due").first()
    )

    if next_auction and next_auction.next_update_due:
        # Calculate delay until the next update is due
        delay_seconds = max(0, (next_auction.next_update_due - now).total_seconds())
        # Cap the delay at 1 hour to ensure we check periodically for new auctions
        delay_seconds = min(delay_seconds, 3600)
    else:
        # No auctions with scheduled updates, check again in 1 hour
        delay_seconds = 3600

    # Schedule the task with countdown
    update_auction_stats.apply_async(countdown=delay_seconds)


@shared_task(bind=True, ignore_result=True)
def update_auction_stats(self):
    """
    Update cached auction statistics for auctions whose next_update_due is past due.

    This task is self-scheduling: it processes one auction, then schedules itself
    to run again when the next auction's stats are due, rather than running on a
    fixed periodic interval.
    """
    import logging

    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    from django.db.models import Q
    from django.utils import timezone

    from auctions.models import Auction

    logger = logging.getLogger(__name__)
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
            # This ensures that if the recalculation takes longer than expected,
            # subsequent task runs won't try to recalculate the same auction again
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

    # Schedule the next run based on when the next auction update is due
    schedule_next_auction_stats_update()
