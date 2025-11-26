"""
Tests for Celery tasks.

This module tests that Celery tasks properly call their corresponding management commands.
"""

import datetime
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from auctions import tasks
from auctions.models import Auction, AuctionHistory, AuctionTOS, Invoice, PickupLocation


class CeleryTasksTestCase(TestCase):
    """Test case for Celery tasks."""

    @patch("auctions.tasks.call_command")
    def test_endauctions_task(self, mock_call_command):
        """Test that endauctions task calls the management command."""
        tasks.endauctions()
        mock_call_command.assert_called_once_with("endauctions")

    @patch("auctions.tasks.call_command")
    def test_sendnotifications_task(self, mock_call_command):
        """Test that sendnotifications task calls the management command."""
        tasks.sendnotifications()
        mock_call_command.assert_called_once_with("sendnotifications")

    @patch("auctions.tasks.call_command")
    def test_auctiontos_notifications_task(self, mock_call_command):
        """Test that auctiontos_notifications task calls the management command."""
        tasks.auctiontos_notifications()
        mock_call_command.assert_called_once_with("auctiontos_notifications")

    @patch("auctions.tasks.call_command")
    def test_auction_emails_task(self, mock_call_command):
        """Test that auction_emails task calls the management command."""
        tasks.auction_emails()
        mock_call_command.assert_called_once_with("auction_emails")

    @patch("auctions.tasks.call_command")
    def test_email_unseen_chats_task(self, mock_call_command):
        """Test that email_unseen_chats task calls the management command."""
        tasks.email_unseen_chats()
        mock_call_command.assert_called_once_with("email_unseen_chats")

    @patch("auctions.tasks.call_command")
    def test_weekly_promo_task(self, mock_call_command):
        """Test that weekly_promo task calls the management command."""
        tasks.weekly_promo()
        mock_call_command.assert_called_once_with("weekly_promo")

    @patch("auctions.tasks.call_command")
    def test_set_user_location_task(self, mock_call_command):
        """Test that set_user_location task calls the management command."""
        tasks.set_user_location()
        mock_call_command.assert_called_once_with("set_user_location")

    @patch("auctions.tasks.call_command")
    def test_remove_duplicate_views_task(self, mock_call_command):
        """Test that remove_duplicate_views task calls the management command."""
        tasks.remove_duplicate_views()
        mock_call_command.assert_called_once_with("remove_duplicate_views")

    @patch("auctions.tasks.call_command")
    def test_webpush_notifications_deduplicate_task(self, mock_call_command):
        """Test that webpush_notifications_deduplicate task calls the management command."""
        tasks.webpush_notifications_deduplicate()
        mock_call_command.assert_called_once_with("webpush_notifications_deduplicate")

    @patch("auctions.tasks.call_command")
    def test_update_auction_stats_task(self, mock_call_command):
        """Test that update_auction_stats task calls the management command."""
        tasks.update_auction_stats()
        mock_call_command.assert_called_once_with("update_auction_stats")


class ProcessInvoiceNotificationsTaskTestCase(TestCase):
    """Test case for the process_invoice_notifications task."""

    def setUp(self):
        """Set up test data."""
        time = timezone.now() - datetime.timedelta(days=2)
        timeStart = timezone.now() - datetime.timedelta(days=3)
        theFuture = timezone.now() + datetime.timedelta(days=3)

        # Create a trusted user (auction creator)
        self.trusted_user = User.objects.create_user(
            username="trusted_user", password="testpassword", email="trusted@example.com"
        )
        self.trusted_user.userdata.is_trusted = True
        self.trusted_user.userdata.save()

        # Create an auction
        self.auction = Auction.objects.create(
            created_by=self.trusted_user,
            title="Test Auction",
            is_online=True,
            date_end=time,
            date_start=timeStart,
            email_users_when_invoices_ready=True,
        )

        # Create a pickup location
        self.location = PickupLocation.objects.create(name="Test Location", auction=self.auction, pickup_time=theFuture)

        # Create a user with email
        self.user_with_email = User.objects.create_user(
            username="user_with_email", password="testpassword", email="user@example.com"
        )
        self.tos_with_email = AuctionTOS.objects.create(
            user=self.user_with_email,
            auction=self.auction,
            pickup_location=self.location,
            email="user@example.com",
        )

        # Create a user without email
        self.user_without_email = User.objects.create_user(
            username="user_without_email", password="testpassword", email="noemail@example.com"
        )
        self.tos_without_email = AuctionTOS.objects.create(
            user=self.user_without_email,
            auction=self.auction,
            pickup_location=self.location,
            email="",  # No email
        )

    @patch("auctions.tasks.mail.send")
    def test_sends_email_for_due_invoice(self, mock_mail_send):
        """Test that the task sends an email for invoices that are due."""
        # Create an invoice with notification due in the past
        invoice = Invoice.objects.create(
            auctiontos_user=self.tos_with_email,
            auction=self.auction,
            status="UNPAID",
            invoice_notification_due=timezone.now() - datetime.timedelta(seconds=10),
            email_sent=False,
        )

        # Run the task
        tasks.process_invoice_notifications()

        # Check that email was sent
        mock_mail_send.assert_called_once()

        # Refresh invoice from database
        invoice.refresh_from_db()

        # Check that invoice was marked as sent
        assert invoice.email_sent is True
        assert invoice.invoice_notification_due is None

        # Check that history was created
        history = AuctionHistory.objects.filter(auction=self.auction, applies_to="INVOICES").first()
        assert history is not None
        assert "Invoice notification email sent" in history.action

    @patch("auctions.tasks.mail.send")
    def test_does_not_send_email_for_future_notification(self, mock_mail_send):
        """Test that the task does not send email for invoices with future notification due."""
        # Create an invoice with notification due in the future
        invoice = Invoice.objects.create(
            auctiontos_user=self.tos_with_email,
            auction=self.auction,
            status="UNPAID",
            invoice_notification_due=timezone.now() + datetime.timedelta(seconds=30),
            email_sent=False,
        )

        # Run the task
        tasks.process_invoice_notifications()

        # Check that email was NOT sent
        mock_mail_send.assert_not_called()

        # Refresh invoice from database
        invoice.refresh_from_db()

        # Check that invoice was NOT marked as sent
        assert invoice.email_sent is False
        assert invoice.invoice_notification_due is not None

    @patch("auctions.tasks.mail.send")
    def test_does_not_send_email_twice(self, mock_mail_send):
        """Test that the task does not send email for invoices already marked as sent."""
        # Create an invoice already marked as sent
        Invoice.objects.create(
            auctiontos_user=self.tos_with_email,
            auction=self.auction,
            status="UNPAID",
            invoice_notification_due=timezone.now() - datetime.timedelta(seconds=10),
            email_sent=True,  # Already sent
        )

        # Run the task
        tasks.process_invoice_notifications()

        # Check that email was NOT sent
        mock_mail_send.assert_not_called()

    @patch("auctions.tasks.mail.send")
    def test_does_not_send_email_for_user_without_email(self, mock_mail_send):
        """Test that the task does not send email for users without email address."""
        # Create an invoice for user without email
        invoice = Invoice.objects.create(
            auctiontos_user=self.tos_without_email,
            auction=self.auction,
            status="UNPAID",
            invoice_notification_due=timezone.now() - datetime.timedelta(seconds=10),
            email_sent=False,
        )

        # Run the task
        tasks.process_invoice_notifications()

        # Check that email was NOT sent
        mock_mail_send.assert_not_called()

        # Refresh invoice from database
        invoice.refresh_from_db()

        # Check that invoice was marked as sent (to prevent re-processing)
        assert invoice.email_sent is True
        assert invoice.invoice_notification_due is None

    @patch("auctions.tasks.mail.send")
    def test_does_not_send_email_for_untrusted_auction_creator(self, mock_mail_send):
        """Test that the task does not send email when auction creator is not trusted."""
        # Make the auction creator untrusted
        self.trusted_user.userdata.is_trusted = False
        self.trusted_user.userdata.save()

        # Create an invoice
        invoice = Invoice.objects.create(
            auctiontos_user=self.tos_with_email,
            auction=self.auction,
            status="UNPAID",
            invoice_notification_due=timezone.now() - datetime.timedelta(seconds=10),
            email_sent=False,
        )

        # Run the task
        tasks.process_invoice_notifications()

        # Check that email was NOT sent
        mock_mail_send.assert_not_called()

        # Refresh invoice from database
        invoice.refresh_from_db()

        # Check that invoice was marked as sent (to prevent re-processing)
        assert invoice.email_sent is True

    @patch("auctions.tasks.mail.send")
    def test_does_not_send_email_when_notifications_disabled(self, mock_mail_send):
        """Test that the task does not send email when auction has notifications disabled."""
        # Disable notifications on auction
        self.auction.email_users_when_invoices_ready = False
        self.auction.save()

        # Create an invoice
        invoice = Invoice.objects.create(
            auctiontos_user=self.tos_with_email,
            auction=self.auction,
            status="UNPAID",
            invoice_notification_due=timezone.now() - datetime.timedelta(seconds=10),
            email_sent=False,
        )

        # Run the task
        tasks.process_invoice_notifications()

        # Check that email was NOT sent
        mock_mail_send.assert_not_called()

        # Refresh invoice from database
        invoice.refresh_from_db()

        # Check that invoice was marked as sent (to prevent re-processing)
        assert invoice.email_sent is True

    @patch("auctions.tasks.mail.send")
    def test_does_not_send_email_for_draft_invoice(self, mock_mail_send):
        """Test that the task does not process draft invoices."""
        # Create a draft invoice with notification due in the past
        invoice = Invoice.objects.create(
            auctiontos_user=self.tos_with_email,
            auction=self.auction,
            status="DRAFT",  # Draft status
            invoice_notification_due=timezone.now() - datetime.timedelta(seconds=10),
            email_sent=False,
        )

        # Run the task
        tasks.process_invoice_notifications()

        # Check that email was NOT sent
        mock_mail_send.assert_not_called()

        # Refresh invoice from database
        invoice.refresh_from_db()

        # Check that invoice was NOT marked as sent
        assert invoice.email_sent is False
