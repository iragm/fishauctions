"""
Celery configuration for fishauctions project.

This module sets up Celery for handling asynchronous tasks and periodic tasks.
"""

import os

from celery import Celery
from celery.signals import worker_ready

# Constants
WORKER_READY_TASK_DELAY_SECONDS = 5  # Delay before starting self-scheduling tasks after worker is ready

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fishauctions.settings")

app = Celery("fishauctions")

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
# - namespace='CELERY' means all celery-related configuration keys
#   should have a `CELERY_` prefix.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Load task modules from all registered Django apps.
app.autodiscover_tasks()

# Configure Celery Beat schedule for periodic tasks
app.conf.beat_schedule = {
    # End auctions and declare winners - every minute
    "endauctions": {
        "task": "auctions.tasks.endauctions",
        "schedule": 60.0,  # Run every minute
    },
    # Send notifications about watched items - every 15 minutes
    "sendnotifications": {
        "task": "auctions.tasks.sendnotifications",
        "schedule": 900.0,  # Run every 15 minutes
    },
    # Welcome and print reminder emails - every 15 minutes
    "auctiontos_notifications": {
        "task": "auctions.tasks.auctiontos_notifications",
        "schedule": 900.0,  # Run every 15 minutes
    },
    # Send queued mail (post_office) - every 10 minutes (retry failed emails)
    "send_queued_mail": {
        "task": "post_office.tasks.send_queued_mail",
        "schedule": 600.0,  # Run every 10 minutes
    },
    # Send auction emails - every 4 minutes
    "auction_emails": {
        "task": "auctions.tasks.auction_emails",
        "schedule": 240.0,  # Run every 4 minutes
    },
    # Send notifications about unread chats - every 24 hours
    "email_unseen_chats": {
        "task": "auctions.tasks.email_unseen_chats",
        "schedule": 86400.0,  # Run every 24 hours
    },
    # Weekly promo email - every 24 hours (per-user scheduling via next_promo_email_at)
    "weekly_promo": {
        "task": "auctions.tasks.weekly_promo",
        "schedule": 86400.0,  # Run every 24 hours
    },
    # Set user locations - every 2 hours
    "set_user_location": {
        "task": "auctions.tasks.set_user_location",
        "schedule": 7200.0,  # Run every 2 hours
    },
    # Remove duplicate page views - every 15 minutes
    "remove_duplicate_views": {
        "task": "auctions.tasks.remove_duplicate_views",
        "schedule": 900.0,  # Run every 15 minutes
    },
    # Deduplicate webpush notifications - every 24 hours
    "webpush_notifications_deduplicate": {
        "task": "auctions.tasks.webpush_notifications_deduplicate",
        "schedule": 86400.0,  # Run every 24 hours
    },
    # Clean up old invoice notification tasks - every 24 hours
    "cleanup_old_invoice_notification_tasks": {
        "task": "auctions.tasks.cleanup_old_invoice_notification_tasks",
        "schedule": 86400.0,  # Run every 24 hours
    },
    # Note: update_auction_stats is NOT in beat_schedule as it's self-scheduling.
    # It starts on worker_ready and schedules itself based on when the next
    # auction's stats are due for update.
}


@worker_ready.connect
def start_auction_stats_task(sender, **kwargs):
    """
    Start the self-scheduling auction stats update task when the worker is ready.

    This ensures the task begins running after the worker starts up, and then
    it will continue to schedule itself based on when the next auction update is due.
    """
    # Schedule the task to run shortly after worker is fully ready
    from datetime import timedelta

    from django.utils import timezone

    from auctions.tasks import schedule_auction_stats_update

    schedule_auction_stats_update(timezone.now() + timedelta(seconds=WORKER_READY_TASK_DELAY_SECONDS))


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    """Debug task for testing Celery configuration."""
    import logging

    logger = logging.getLogger(__name__)
    logger.info("Request: %s", self.request)
