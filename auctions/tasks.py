"""
Celery tasks for the auctions app.

This module contains all Celery tasks that were previously run as cron jobs.
Each task wraps a management command to maintain backward compatibility.
"""

from celery import shared_task
from django.contrib.sites.models import Site
from django.core.management import call_command
from django.db import transaction
from django.utils import timezone
from post_office import mail


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
def process_invoice_notifications(self):
    """
    Process invoice notifications that are due.

    This task checks for invoices where:
    - invoice_notification_due is in the past
    - email_sent is False
    - auction exists
    - status is not DRAFT

    For each qualifying invoice, it sends an email notification if:
    - The auction creator is trusted
    - The auction allows invoice ready notifications
    - The user has an email address

    The task uses select_for_update to prevent duplicate processing if
    the task runs multiple times simultaneously.
    """
    from auctions.models import AuctionHistory, Invoice

    now = timezone.now()

    # Use select_for_update to prevent race conditions
    with transaction.atomic():
        invoices = (
            Invoice.objects.select_for_update(skip_locked=True)
            .filter(
                invoice_notification_due__lte=now,
                email_sent=False,
                auction__isnull=False,
            )
            .exclude(status="DRAFT")
        )

        for invoice in invoices:
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


@shared_task(bind=True, ignore_result=True)
def update_auction_stats(self):
    """
    Update cached auction statistics for auctions whose next_update_due is past due.

    Previously run every minute via cron.
    """
    call_command("update_auction_stats")
