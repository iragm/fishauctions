"""
Celery configuration for fishauctions project.

This module sets up Celery for handling asynchronous tasks and periodic tasks.
"""

import os

from celery import Celery
from celery.schedules import crontab

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
    # Process invoice notifications that are due - every 10 seconds
    "process_invoice_notifications": {
        "task": "auctions.tasks.process_invoice_notifications",
        "schedule": 10.0,  # Run every 10 seconds to catch notifications after 15s delay
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
    # Send notifications about unread chats - daily at 10:00
    "email_unseen_chats": {
        "task": "auctions.tasks.email_unseen_chats",
        "schedule": crontab(hour=10, minute=0),
    },
    # Weekly promo email - Wednesday at 9:30
    "weekly_promo": {
        "task": "auctions.tasks.weekly_promo",
        "schedule": crontab(day_of_week=3, hour=9, minute=30),
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
    # Deduplicate webpush notifications - daily at 10:00
    "webpush_notifications_deduplicate": {
        "task": "auctions.tasks.webpush_notifications_deduplicate",
        "schedule": crontab(hour=10, minute=0),
    },
    # Update cached auction statistics - every minute
    "update_auction_stats": {
        "task": "auctions.tasks.update_auction_stats",
        "schedule": 60.0,  # Run every minute
    },
}


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    """Debug task for testing Celery configuration."""
    import logging

    logger = logging.getLogger(__name__)
    logger.info("Request: %s", self.request)
