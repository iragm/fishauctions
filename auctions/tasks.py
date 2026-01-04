"""
Celery tasks for the auctions app.

This module contains all Celery tasks that were previously run as cron jobs.
Each task wraps a management command to maintain backward compatibility.
"""

import json
import logging

from celery import shared_task
from django.contrib.sites.models import Site
from django.core.management import call_command
from django_celery_beat.models import ClockedSchedule, PeriodicTask
from post_office import mail

# Constants for update_auction_stats scheduling
STATS_UPDATE_LOCK_MINUTES = 5  # Minutes to lock auction before recalculation to prevent concurrent updates
STATS_UPDATE_MAX_DELAY_SECONDS = 3600  # Maximum delay (1 hour) before checking for new auctions
STATS_UPDATE_FALLBACK_DELAY_SECONDS = 3600  # Fallback delay when no auctions need updates
AUCTION_STATS_TASK_NAME = "auction_stats_update"  # Name for the one-off scheduled task

logger = logging.getLogger(__name__)


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


def get_invoice_notification_task_name(invoice_pk):
    """Generate a unique task name for an invoice notification."""
    return f"invoice_notification_{invoice_pk}"


def schedule_invoice_notification(invoice_pk, run_at):
    """
    Schedule a one-off task to send an invoice notification.

    Uses django-celery-beat's ClockedSchedule and PeriodicTask to schedule
    a task to run at a specific time. If a task already exists for this
    invoice, it will be updated with the new scheduled time.

    Args:
        invoice_pk: The primary key of the invoice
        run_at: datetime when the notification should be sent
    """
    schedule, _ = ClockedSchedule.objects.get_or_create(clocked_time=run_at)

    task_name = get_invoice_notification_task_name(invoice_pk)

    PeriodicTask.objects.update_or_create(
        name=task_name,
        defaults={
            "task": "auctions.tasks.send_invoice_notification",
            "clocked": schedule,
            "one_off": True,
            "enabled": True,
            "kwargs": json.dumps({"invoice_pk": invoice_pk}),
        },
    )


def cancel_invoice_notification(invoice_pk):
    """
    Cancel a scheduled invoice notification task.

    Args:
        invoice_pk: The primary key of the invoice
    """
    task_name = get_invoice_notification_task_name(invoice_pk)
    PeriodicTask.objects.filter(name=task_name).delete()


@shared_task(bind=True, ignore_result=True)
def send_invoice_notification(self, invoice_pk):
    """
    Send an invoice notification for a specific invoice.

    This task is scheduled as a one-off task when an invoice status changes
    to "ready" or "paid". It will:
    - Check if the invoice still needs notification (not already sent, not draft)
    - Send email if conditions are met (trusted user, has email, notifications enabled)
    - Mark the invoice as email_sent=True
    - Add history entry if email was sent
    - Clean up the PeriodicTask entry after execution

    The task is idempotent - if called multiple times or after the notification
    is already sent, it will simply do nothing.
    """
    from auctions.models import AuctionHistory, Invoice

    try:
        invoice = Invoice.objects.get(pk=invoice_pk)
    except Invoice.DoesNotExist:
        # Invoice was deleted, clean up and return
        _cleanup_invoice_notification_task(invoice_pk)
        return

    # Check if notification is still needed
    if invoice.email_sent:
        # Already sent, clean up and return
        _cleanup_invoice_notification_task(invoice_pk)
        return

    if invoice.status == "DRAFT":
        # Invoice was set back to open, clean up and return
        _cleanup_invoice_notification_task(invoice_pk)
        return

    if not invoice.auction:
        # No auction associated, mark as sent to prevent reprocessing
        invoice.email_sent = True
        invoice.invoice_notification_due = None
        invoice.save()
        _cleanup_invoice_notification_task(invoice_pk)
        return

    should_send_email = (
        invoice.auction.created_by.userdata.is_trusted
        and invoice.auction.email_users_when_invoices_ready
        and invoice.auctiontos_user.email
    )

    if should_send_email:
        email = invoice.auctiontos_user.email
        subject = f"Your invoice for {invoice.label} is ready"
        if invoice.status == "PAID":
            subject = f"Thanks for being part of {invoice.label}"
        contact_email = invoice.auction.created_by.email
        current_site = Site.objects.get_current()
        mail.send(
            email,
            headers={"Reply-to": contact_email},
            template="invoice_ready",
            context={
                "subject": subject,
                "name": invoice.auctiontos_user.name,
                "domain": current_site.domain,
                "location": invoice.location,
                "invoice": invoice,
                "reply_to_email": contact_email,
            },
        )
        # Add history entry about the email being sent
        AuctionHistory.objects.create(
            auction=invoice.auction,
            user=None,
            action=f"Invoice notification email sent to {invoice.auctiontos_user.name} ({email})",
            applies_to="INVOICES",
        )

    # Mark as sent regardless of whether we actually sent an email
    # This prevents re-processing invoices that can't receive emails
    invoice.email_sent = True
    invoice.invoice_notification_due = None
    invoice.save()

    # Clean up the PeriodicTask entry now that we're done
    _cleanup_invoice_notification_task(invoice_pk)


def _cleanup_invoice_notification_task(invoice_pk):
    """
    Remove the PeriodicTask entry for an invoice notification.

    This is called after the task runs to clean up the database.
    """
    task_name = get_invoice_notification_task_name(invoice_pk)
    PeriodicTask.objects.filter(name=task_name).delete()


@shared_task(bind=True, ignore_result=True)
def cleanup_old_invoice_notification_tasks(self):
    """
    Clean up old invoice notification PeriodicTask entries from the database.

    This task runs daily to remove any invoice_notification_* tasks that are
    more than 24 hours old. These tasks should normally be cleaned up after
    execution, but this provides a safety net for any orphaned entries.
    """
    from datetime import timedelta

    from django.utils import timezone

    cutoff_time = timezone.now() - timedelta(hours=24)

    # Find and delete old invoice notification tasks
    # The clocked schedule's clocked_time indicates when the task was scheduled to run
    old_tasks = PeriodicTask.objects.filter(
        name__startswith="invoice_notification_",
        clocked__clocked_time__lt=cutoff_time,
    )
    old_tasks.delete()


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


def schedule_auction_stats_update(run_at=None):
    """
    Schedule a one-off task to update auction stats.

    Uses django-celery-beat's ClockedSchedule and PeriodicTask to schedule
    a task to run at a specific time. Deletes and recreates the task to ensure
    it's properly enabled and picked up by celery-beat.

    Args:
        run_at: datetime when the update should run. If None, runs immediately.
    """
    from datetime import timedelta

    from django.utils import timezone

    if run_at is None:
        run_at = timezone.now()

    # Cap the delay to ensure we check periodically for new auctions
    max_run_at = timezone.now() + timedelta(seconds=STATS_UPDATE_MAX_DELAY_SECONDS)
    if run_at > max_run_at:
        run_at = max_run_at

    schedule, _ = ClockedSchedule.objects.get_or_create(clocked_time=run_at)

    # Delete the existing task if it exists to ensure clean state
    # This prevents issues with one-off tasks being disabled by celery-beat
    old_tasks = PeriodicTask.objects.filter(name=AUCTION_STATS_TASK_NAME)
    old_schedule_ids = [task.clocked_id for task in old_tasks if task.clocked_id]
    old_tasks.delete()

    # Clean up orphaned ClockedSchedule objects from previous runs
    if old_schedule_ids:
        ClockedSchedule.objects.filter(id__in=old_schedule_ids).delete()

    # Create a fresh task that's guaranteed to be enabled
    task = PeriodicTask.objects.create(
        name=AUCTION_STATS_TASK_NAME,
        task="auctions.tasks.update_auction_stats",
        clocked=schedule,
        one_off=True,
        enabled=True,
    )

    logger.info(
        "Scheduled auction stats update task (id=%s) to run at %s", task.id, run_at.strftime("%Y-%m-%d %H:%M:%S %Z")
    )


@shared_task(bind=True, ignore_result=True)
def update_auction_stats(self):
    """
    Update cached auction statistics for auctions whose next_update_due is past due.

    This task is self-scheduling: it processes one auction, then schedules itself
    to run again when the next auction's stats are due, rather than running on a
    fixed periodic interval.
    """
    from datetime import timedelta

    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    from django.utils import timezone

    from auctions.models import Auction

    now = timezone.now()

    logger.info("Auction stats update task started at %s", now.strftime("%Y-%m-%d %H:%M:%S %Z"))

    # Process only one auction per run, ordered by most overdue first
    # Only process auctions that have next_update_due set and are past due
    auction = (
        Auction.objects.filter(
            next_update_due__lte=now,
            is_deleted=False,
        )
        .order_by("next_update_due")
        .first()
    )

    if auction:
        logger.info("Found auction needing stats update: %s (id=%s)", auction.title, auction.pk)
        try:
            logger.info("Recalculating stats for auction: %s (%s)", auction.title, auction.slug)

            # Set next_update_due before recalculating to prevent concurrent recalculations
            # This ensures that if the recalculation takes longer than expected,
            # subsequent task runs won't try to recalculate the same auction again
            auction.next_update_due = now + timedelta(minutes=STATS_UPDATE_LOCK_MINUTES)
            auction.save(update_fields=["next_update_due"])

            auction.recalculate_stats()

            # Send WebSocket notification to users viewing the stats page
            # This is a best-effort notification - if it fails, we don't want to fail the entire stats update
            try:
                logger.info("Sending WebSocket notification for auction: %s", auction.title)
                auction_websocket = get_channel_layer()
                async_to_sync(auction_websocket.group_send)(
                    f"auctions_{auction.pk}",
                    {
                        "type": "stats_updated",
                    },
                )
                logger.info("Successfully sent WebSocket notification for auction: %s", auction.title)
            except Exception as websocket_error:
                # Log the error but don't fail the stats update
                logger.error("Failed to send WebSocket notification for auction %s: %s", auction.title, websocket_error)

            logger.info("Successfully updated stats for auction: %s", auction.title)
        except Exception as e:
            logger.error("Failed to update stats for auction %s (%s): %s", auction.title, auction.slug, e)
            logger.exception(e)
    else:
        logger.info("No auctions need stats update at this time")

    # Schedule the next run based on when the next auction update is due
    next_auction = (
        Auction.objects.filter(is_deleted=False, next_update_due__isnull=False).order_by("next_update_due").first()
    )

    if next_auction and next_auction.next_update_due:
        logger.info(
            "Scheduling next stats update for auction '%s' at %s",
            next_auction.title,
            next_auction.next_update_due.strftime("%Y-%m-%d %H:%M:%S %Z"),
        )
        schedule_auction_stats_update(next_auction.next_update_due)
    else:
        # No auctions with scheduled updates, check again later
        fallback_time = now + timedelta(seconds=STATS_UPDATE_FALLBACK_DELAY_SECONDS)
        logger.info(
            "No auctions need stats update, checking again at %s", fallback_time.strftime("%Y-%m-%d %H:%M:%S %Z")
        )
        schedule_auction_stats_update(fallback_time)
