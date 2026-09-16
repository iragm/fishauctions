"""Celery configuration: the app, its beat schedule, and the self-scheduling tasks started at boot."""

import os

from celery import Celery
from celery.signals import worker_ready

# Constants
WORKER_READY_TASK_DELAY_SECONDS = 5  # Delay before starting self-scheduling tasks after worker is ready

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "fishauctions.settings")

app = Celery("fishauctions")

# A string, so the worker doesn't serialize the config to child processes; the CELERY namespace
# means every setting is prefixed CELERY_.
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
    # Fuse AR sightings into a 2D map and prune the observation buffer.
    "update_ar_positions": {
        "task": "auctions.tasks.update_ar_positions",
        "schedule": 60.0,  # Run every minute
    },
    # Welcome and print reminder emails - every 15 minutes
    "auctiontos_notifications": {
        "task": "auctions.tasks.auctiontos_notifications",
        "schedule": 900.0,  # Run every 15 minutes
    },
    # One-shot: fill in PageView.auction on lot views written before the beacon sent it, so the
    # `auction_id OR lot.auction_id` in Auction.page_views can go. Disables its own row when done.
    "backfill_page_view_auctions": {
        "task": "auctions.tasks.backfill_page_view_auctions",
        "schedule": 900.0,  # Run every 15 minutes
    },
    # Club lifecycle rollup and the outreach queue; nothing it measures moves faster than daily.
    "refresh_club_health": {
        "task": "auctions.tasks.refresh_club_health",
        "schedule": 86400.0,  # Run every 24 hours
    },
    # Send queued mail (post_office), retrying failures.
    "send_queued_mail": {
        "task": "post_office.tasks.send_queued_mail",
        "schedule": 600.0,  # Run every 10 minutes
    },
    # Two-way Google Calendar sync and Discord events for club events.
    "sync_club_calendars": {
        "task": "auctions.tasks.sync_club_calendars",
        "schedule": 900.0,  # Run every 15 minutes
    },
    # The backstop for club announcements: the view queues a countdown task for the exact moment
    # (announcements.GRACE_SECONDS), so a lost one costs a short delay. One indexed lookup.
    "send_scheduled_announcements": {
        "task": "auctions.tasks.send_scheduled_announcements",
        "schedule": 60.0,  # Run every minute
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
    # Weekly promo email; per-user scheduling via next_promo_email_at gives local-timezone delivery.
    "weekly_promo": {
        "task": "auctions.tasks.weekly_promo",
        "schedule": 3600.0,  # Run every hour
    },
    # Promo push notifications for nearby auctions, the push analogue of weekly_promo.
    "promo_push_notifications": {
        "task": "auctions.tasks.promo_push_notifications",
        "schedule": 3600.0,  # Run every hour
    },
    # Set user locations - every 2 hours
    "set_user_location": {
        "task": "auctions.tasks.set_user_location",
        "schedule": 7200.0,  # Run every 2 hours
    },
    # Deduplicate webpush notifications - every 24 hours
    "webpush_notifications_deduplicate": {
        "task": "auctions.tasks.webpush_notifications_deduplicate",
        "schedule": 86400.0,  # Run every 24 hours
    },
    # Merge duplicate user interest categories from request races.
    "deduplicate_user_interest": {
        "task": "auctions.tasks.deduplicate_user_interest",
        "schedule": 86400.0,  # Run every 24 hours
    },
    # Clean up old invoice notification tasks - every 24 hours
    "cleanup_old_invoice_notification_tasks": {
        "task": "auctions.tasks.cleanup_old_invoice_notification_tasks",
        "schedule": 86400.0,  # Run every 24 hours
    },
    # Discord roles for expired or renewed memberships.
    #
    # The four entries below were part of this task until they were split out: run in one body under
    # the soft time limit, a slow Discord sync silently skipped everything after it.
    "update_expired_membership_discord_roles": {
        "task": "auctions.tasks.update_expired_membership_discord_roles",
        "schedule": 86400.0,  # Run every 24 hours
    },
    # Zero the year-to-date award counters at the start of each year; a no-op on 364 days, and it
    # catches up whenever it runs.
    "reset_yearly_bap_counters": {
        "task": "auctions.tasks.reset_yearly_bap_counters",
        "schedule": 86400.0,  # Run every 24 hours
    },
    # Welcome letters for members who joined more than 24 hours ago.
    "send_club_member_welcome_emails": {
        "task": "auctions.tasks.send_club_member_welcome_emails",
        "schedule": 86400.0,  # Run every 24 hours
    },
    # "Your membership expires in 30 days" and "expires tomorrow".
    "send_membership_expiration_reminders": {
        "task": "auctions.tasks.send_membership_expiration_reminders",
        "schedule": 86400.0,  # Run every 24 hours
    },
    # Nightly Mailchimp/Brevo catch-up so lifecycle tags stay accurate.
    "backfill_marketing_contacts": {
        "task": "auctions.tasks.backfill_marketing_contacts",
        "schedule": 86400.0,  # Run every 24 hours
    },
    # Refresh Google Wallet passes for recently expired members.
    "refresh_google_wallet_membership_status": {
        "task": "auctions.tasks.refresh_google_wallet_membership_status",
        "schedule": 86400.0,  # Run every 24 hours
    },
    # The same for Apple Wallet: push updates to registered devices.
    "refresh_apple_wallet_membership_status": {
        "task": "auctions.tasks.refresh_apple_wallet_membership_status",
        "schedule": 86400.0,  # Run every 24 hours
    },
    # Flush expired JWT blacklist and outstanding tokens (mobile rotation writes one per refresh).
    "flush_expired_tokens": {
        "task": "auctions.tasks.flush_expired_tokens",
        "schedule": 86400.0,  # Run every 24 hours
    },
    # Delete accounts whose deletion grace period has expired.
    "delete_pending_accounts": {
        "task": "auctions.tasks.delete_pending_accounts",
        "schedule": 86400.0,  # Run every 24 hours
    },
    # Delete sent mail older than settings.MAIL_RETENTION_DAYS.
    "cleanup_mail": {
        "task": "auctions.tasks.cleanup_mail",
        "schedule": 86400.0,  # Run every 24 hours
    },
    # Move one local image to Cloudflare Images; a no-op unless CLOUDFLARE_IMAGES_* is set.
    "migrate_to_cloudflare_images": {
        "task": "auctions.tasks.migrate_to_cloudflare_images",
        "schedule": 60.0,  # Run every minute
    },
    # Expired OAuth tokens and stale clients from the MCP authorization server.
    "cleanup_oauth_tokens": {
        "task": "auctions.tasks.cleanup_oauth_tokens",
        "schedule": 86400.0,  # Run every 24 hours
    },
    # update_auction_stats is not here: it is self-scheduling, starting on worker_ready. This is the
    # watchdog for that chain, since a run killed by the hard time limit never re-arms itself.
    "ensure_auction_stats_task_scheduled": {
        "task": "auctions.tasks.ensure_auction_stats_task_scheduled",
        "schedule": 900.0,  # Run every 15 minutes
    },
}


@worker_ready.connect
def start_auction_stats_task(sender, **kwargs):
    """Start the self-scheduling auction stats task once the worker is ready; it reschedules itself."""
    # Schedule the task to run shortly after worker is fully ready
    from datetime import timedelta

    from django.utils import timezone

    from auctions.tasks import schedule_auction_stats_update

    schedule_auction_stats_update(timezone.now() + timedelta(seconds=WORKER_READY_TASK_DELAY_SECONDS))


@worker_ready.connect
def start_bap_recalculation_tasks(sender, **kwargs):
    """Bootstrap the self-scheduling BAP recalculation tasks when the worker is ready."""
    from datetime import timedelta

    from django.utils import timezone

    from auctions.tasks import bootstrap_bap_recalculation_tasks

    bootstrap_bap_recalculation_tasks(timezone.now() + timedelta(seconds=WORKER_READY_TASK_DELAY_SECONDS))


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    """Debug task for testing Celery configuration."""
    import logging

    logger = logging.getLogger(__name__)
    logger.info("Request: %s", self.request)
