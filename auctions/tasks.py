"""
Celery tasks for the auctions app.

This module contains all Celery tasks that were previously run as cron jobs.
Each task wraps a management command to maintain backward compatibility.
"""

import json

from celery import shared_task
from django.contrib.sites.models import Site
from django.core.management import call_command
from django_celery_beat.models import ClockedSchedule, PeriodicTask
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
