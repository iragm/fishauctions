"""
Tests for Celery tasks.

This module tests that Celery tasks properly call their corresponding management commands.
"""

import datetime
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from django_celery_beat.models import CrontabSchedule, IntervalSchedule, PeriodicTask

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

    @patch("auctions.tasks.schedule_auction_stats_update")
    @patch("channels.layers.get_channel_layer")
    def test_update_auction_stats_task_with_auction(self, mock_channel, mock_schedule):
        """Test that update_auction_stats task processes an auction and schedules next run."""
        import datetime

        from django.utils import timezone

        from auctions.models import Auction

        # Create an auction that needs stats update (providing required date_start field)
        now = timezone.now()
        auction = Auction.objects.create(
            title="Test Auction",
            is_deleted=False,
            next_update_due=now - timezone.timedelta(minutes=10),
            date_start=now - datetime.timedelta(days=1),
        )

        # Mock the channel layer to track WebSocket sends
        # The channel layer is used by Django Channels to send messages to WebSocket groups
        # We mock it to verify the message is sent without requiring a real Redis connection
        mock_channel_layer = mock_channel.return_value

        # Run the task
        tasks.update_auction_stats()

        # Verify the auction was processed (next_update_due should be updated)
        auction.refresh_from_db()
        self.assertIsNotNone(auction.next_update_due)
        self.assertGreater(auction.next_update_due, timezone.now())

        # Verify WebSocket message was sent
        mock_channel_layer.group_send.assert_called_once()
        call_args = mock_channel_layer.group_send.call_args
        self.assertEqual(call_args[0][0], f"auctions_{auction.pk}")  # Channel name
        self.assertEqual(call_args[0][1]["type"], "stats_updated")  # Message type

        # Verify the task schedules itself
        mock_schedule.assert_called_once()

    @patch("auctions.tasks.schedule_auction_stats_update")
    def test_update_auction_stats_task_no_auction(self, mock_schedule):
        """Test that update_auction_stats task handles no auctions gracefully."""
        # Run the task with no auctions needing update
        tasks.update_auction_stats()

        # Verify the task still schedules itself
        mock_schedule.assert_called_once()

    def test_schedule_auction_stats_update_creates_task(self):
        """Test that schedule_auction_stats_update creates a PeriodicTask."""
        from django_celery_beat.models import PeriodicTask

        # Call the scheduling function
        tasks.schedule_auction_stats_update()

        # Verify the task was created
        task = PeriodicTask.objects.filter(name=tasks.AUCTION_STATS_TASK_NAME).first()
        self.assertIsNotNone(task)
        self.assertTrue(task.one_off)
        self.assertTrue(task.enabled)
        self.assertEqual(task.task, "auctions.tasks.update_auction_stats")

    def test_schedule_auction_stats_update_recreates_disabled_task(self):
        """Test that schedule_auction_stats_update recreates a disabled task."""
        from django.utils import timezone
        from django_celery_beat.models import ClockedSchedule, PeriodicTask

        # Create a disabled task (simulating what happens after a one-off task runs)
        old_schedule = ClockedSchedule.objects.create(clocked_time=timezone.now())
        old_task = PeriodicTask.objects.create(
            name=tasks.AUCTION_STATS_TASK_NAME,
            task="auctions.tasks.update_auction_stats",
            clocked=old_schedule,
            one_off=True,
            enabled=False,  # Disabled as would happen after running
        )
        old_task_id = old_task.id
        old_schedule_id = old_schedule.id

        # Call the scheduling function
        tasks.schedule_auction_stats_update()

        # Verify the old task was deleted
        self.assertFalse(PeriodicTask.objects.filter(id=old_task_id).exists())

        # Verify the old schedule was cleaned up
        self.assertFalse(ClockedSchedule.objects.filter(id=old_schedule_id).exists())

        # Verify a new task was created and is enabled
        new_task = PeriodicTask.objects.filter(name=tasks.AUCTION_STATS_TASK_NAME).first()
        self.assertIsNotNone(new_task)
        self.assertNotEqual(new_task.id, old_task_id)  # Different task
        self.assertTrue(new_task.enabled)  # Enabled!
        self.assertTrue(new_task.one_off)
        self.assertEqual(new_task.task, "auctions.tasks.update_auction_stats")

    def test_schedule_auction_stats_update_ensures_single_task(self):
        """Test that there is always exactly one task after scheduling."""
        from django_celery_beat.models import PeriodicTask

        # Call the scheduling function multiple times
        tasks.schedule_auction_stats_update()
        tasks.schedule_auction_stats_update()
        tasks.schedule_auction_stats_update()

        # Verify there is exactly one task
        task_count = PeriodicTask.objects.filter(name=tasks.AUCTION_STATS_TASK_NAME).count()
        self.assertEqual(task_count, 1, "There should be exactly one auction stats update task")

        # Verify the task is enabled
        task = PeriodicTask.objects.get(name=tasks.AUCTION_STATS_TASK_NAME)
        self.assertTrue(task.enabled)
        self.assertTrue(task.one_off)


class SendInvoiceNotificationTaskTestCase(TestCase):
    """Test case for the send_invoice_notification task."""

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
    def test_sends_email_for_invoice(self, mock_mail_send):
        """Test that the task sends an email for an invoice."""
        # Create an invoice
        invoice = Invoice.objects.create(
            auctiontos_user=self.tos_with_email,
            auction=self.auction,
            status="UNPAID",
            email_sent=False,
        )

        # Run the task
        tasks.send_invoice_notification(invoice.pk)

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
    def test_does_not_send_email_twice(self, mock_mail_send):
        """Test that the task does not send email for invoices already marked as sent."""
        # Create an invoice already marked as sent
        invoice = Invoice.objects.create(
            auctiontos_user=self.tos_with_email,
            auction=self.auction,
            status="UNPAID",
            email_sent=True,  # Already sent
        )

        # Run the task
        tasks.send_invoice_notification(invoice.pk)

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
            email_sent=False,
        )

        # Run the task
        tasks.send_invoice_notification(invoice.pk)

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
            email_sent=False,
        )

        # Run the task
        tasks.send_invoice_notification(invoice.pk)

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
            email_sent=False,
        )

        # Run the task
        tasks.send_invoice_notification(invoice.pk)

        # Check that email was NOT sent
        mock_mail_send.assert_not_called()

        # Refresh invoice from database
        invoice.refresh_from_db()

        # Check that invoice was marked as sent (to prevent re-processing)
        assert invoice.email_sent is True

    @patch("auctions.tasks.mail.send")
    def test_does_not_send_email_for_draft_invoice(self, mock_mail_send):
        """Test that the task does not process draft invoices."""
        # Create a draft invoice
        invoice = Invoice.objects.create(
            auctiontos_user=self.tos_with_email,
            auction=self.auction,
            status="DRAFT",  # Draft status
            email_sent=False,
        )

        # Run the task
        tasks.send_invoice_notification(invoice.pk)

        # Check that email was NOT sent
        mock_mail_send.assert_not_called()

        # Refresh invoice from database
        invoice.refresh_from_db()

        # Check that invoice was NOT marked as sent
        assert invoice.email_sent is False

    @patch("auctions.tasks.mail.send")
    def test_handles_deleted_invoice(self, mock_mail_send):
        """Test that the task handles deleted invoices gracefully."""
        # Run the task with a non-existent invoice ID
        tasks.send_invoice_notification(99999)

        # Check that email was NOT sent (and no error was raised)
        mock_mail_send.assert_not_called()

    @patch("auctions.tasks.mail.send")
    def test_cleans_up_periodic_task_after_sending(self, mock_mail_send):
        """Test that the task cleans up its PeriodicTask entry after execution."""
        # Create an invoice
        invoice = Invoice.objects.create(
            auctiontos_user=self.tos_with_email,
            auction=self.auction,
            status="UNPAID",
            email_sent=False,
        )

        # Schedule the notification (creates a PeriodicTask)
        run_at = timezone.now() + datetime.timedelta(seconds=15)
        tasks.schedule_invoice_notification(invoice.pk, run_at)

        # Verify the task exists
        task_name = f"invoice_notification_{invoice.pk}"
        assert PeriodicTask.objects.filter(name=task_name).exists()

        # Run the task
        tasks.send_invoice_notification(invoice.pk)

        # Verify the PeriodicTask was cleaned up
        assert not PeriodicTask.objects.filter(name=task_name).exists()

    @patch("auctions.tasks.mail.send")
    def test_cleans_up_periodic_task_for_deleted_invoice(self, mock_mail_send):
        """Test that cleanup happens even when invoice is deleted."""
        # Schedule a notification for a non-existent invoice
        run_at = timezone.now() + datetime.timedelta(seconds=15)
        tasks.schedule_invoice_notification(99999, run_at)

        # Verify the task exists
        task_name = "invoice_notification_99999"
        assert PeriodicTask.objects.filter(name=task_name).exists()

        # Run the task with the non-existent invoice ID
        tasks.send_invoice_notification(99999)

        # Verify the PeriodicTask was cleaned up
        assert not PeriodicTask.objects.filter(name=task_name).exists()


class ScheduleInvoiceNotificationTestCase(TestCase):
    """Test case for schedule_invoice_notification and cancel_invoice_notification functions."""

    def setUp(self):
        """Set up test data."""
        time = timezone.now() - datetime.timedelta(days=2)
        timeStart = timezone.now() - datetime.timedelta(days=3)
        theFuture = timezone.now() + datetime.timedelta(days=3)

        self.user = User.objects.create_user(username="test_user", password="testpassword", email="test@example.com")
        self.auction = Auction.objects.create(
            created_by=self.user,
            title="Test Auction",
            is_online=True,
            date_end=time,
            date_start=timeStart,
        )
        self.location = PickupLocation.objects.create(name="Test Location", auction=self.auction, pickup_time=theFuture)
        self.tos = AuctionTOS.objects.create(
            user=self.user,
            auction=self.auction,
            pickup_location=self.location,
            email="test@example.com",
        )
        self.invoice = Invoice.objects.create(
            auctiontos_user=self.tos,
            auction=self.auction,
            status="UNPAID",
        )

    def test_schedule_creates_periodic_task(self):
        """Test that schedule_invoice_notification creates a PeriodicTask."""
        run_at = timezone.now() + datetime.timedelta(seconds=15)

        tasks.schedule_invoice_notification(self.invoice.pk, run_at)

        # Check that the task was created
        task_name = f"invoice_notification_{self.invoice.pk}"
        task = PeriodicTask.objects.get(name=task_name)
        assert task.task == "auctions.tasks.send_invoice_notification"
        assert task.one_off is True
        assert task.enabled is True

    def test_schedule_updates_existing_task(self):
        """Test that schedule_invoice_notification updates an existing task."""
        run_at1 = timezone.now() + datetime.timedelta(seconds=15)
        run_at2 = timezone.now() + datetime.timedelta(seconds=30)

        # Schedule twice
        tasks.schedule_invoice_notification(self.invoice.pk, run_at1)
        tasks.schedule_invoice_notification(self.invoice.pk, run_at2)

        # Check that only one task exists
        task_name = f"invoice_notification_{self.invoice.pk}"
        count = PeriodicTask.objects.filter(name=task_name).count()
        assert count == 1

    def test_cancel_deletes_periodic_task(self):
        """Test that cancel_invoice_notification deletes a PeriodicTask."""
        run_at = timezone.now() + datetime.timedelta(seconds=15)

        # Schedule then cancel
        tasks.schedule_invoice_notification(self.invoice.pk, run_at)
        tasks.cancel_invoice_notification(self.invoice.pk)

        # Check that the task was deleted
        task_name = f"invoice_notification_{self.invoice.pk}"
        count = PeriodicTask.objects.filter(name=task_name).count()
        assert count == 0

    def test_cancel_handles_nonexistent_task(self):
        """Test that cancel_invoice_notification handles non-existent tasks gracefully."""
        # Cancel without scheduling first (should not raise an error)
        tasks.cancel_invoice_notification(99999)


class CleanupOldInvoiceNotificationTasksTestCase(TestCase):
    """Test case for the cleanup_old_invoice_notification_tasks task."""

    def setUp(self):
        """Set up test data."""
        time = timezone.now() - datetime.timedelta(days=2)
        timeStart = timezone.now() - datetime.timedelta(days=3)
        theFuture = timezone.now() + datetime.timedelta(days=3)

        self.user = User.objects.create_user(username="test_user", password="testpassword", email="test@example.com")
        self.auction = Auction.objects.create(
            created_by=self.user,
            title="Test Auction",
            is_online=True,
            date_end=time,
            date_start=timeStart,
        )
        self.location = PickupLocation.objects.create(name="Test Location", auction=self.auction, pickup_time=theFuture)
        self.tos = AuctionTOS.objects.create(
            user=self.user,
            auction=self.auction,
            pickup_location=self.location,
            email="test@example.com",
        )
        self.invoice = Invoice.objects.create(
            auctiontos_user=self.tos,
            auction=self.auction,
            status="UNPAID",
        )

    def test_deletes_old_tasks(self):
        """Test that old invoice notification tasks are deleted."""
        # Schedule a task with a clocked time more than 24 hours ago
        old_time = timezone.now() - datetime.timedelta(hours=25)
        tasks.schedule_invoice_notification(self.invoice.pk, old_time)

        # Verify the task exists
        task_name = f"invoice_notification_{self.invoice.pk}"
        assert PeriodicTask.objects.filter(name=task_name).exists()

        # Run the cleanup task
        tasks.cleanup_old_invoice_notification_tasks()

        # Verify the task was deleted
        assert not PeriodicTask.objects.filter(name=task_name).exists()

    def test_keeps_recent_tasks(self):
        """Test that recent invoice notification tasks are not deleted."""
        # Schedule a task with a clocked time less than 24 hours ago
        recent_time = timezone.now() - datetime.timedelta(hours=12)
        tasks.schedule_invoice_notification(self.invoice.pk, recent_time)

        # Verify the task exists
        task_name = f"invoice_notification_{self.invoice.pk}"
        assert PeriodicTask.objects.filter(name=task_name).exists()

        # Run the cleanup task
        tasks.cleanup_old_invoice_notification_tasks()

        # Verify the task still exists
        assert PeriodicTask.objects.filter(name=task_name).exists()

        # Clean up
        PeriodicTask.objects.filter(name=task_name).delete()

    def test_keeps_future_tasks(self):
        """Test that future invoice notification tasks are not deleted."""
        # Schedule a task for the future
        future_time = timezone.now() + datetime.timedelta(seconds=15)
        tasks.schedule_invoice_notification(self.invoice.pk, future_time)

        # Verify the task exists
        task_name = f"invoice_notification_{self.invoice.pk}"
        assert PeriodicTask.objects.filter(name=task_name).exists()

        # Run the cleanup task
        tasks.cleanup_old_invoice_notification_tasks()

        # Verify the task still exists
        assert PeriodicTask.objects.filter(name=task_name).exists()

        # Clean up
        PeriodicTask.objects.filter(name=task_name).delete()


class SetupCeleryBeatCommandTestCase(TestCase):
    """Test case for the setup_celery_beat management command."""

    def test_reenables_disabled_tasks(self):
        """Test that setup_celery_beat re-enables tasks that were manually disabled."""
        # Create a crontab schedule for weekly_promo (Wednesday at 9:30)
        crontab, _ = CrontabSchedule.objects.get_or_create(
            minute="30",
            hour="9",
            day_of_week="3",
            day_of_month="*",
            month_of_year="*",
        )

        # Create a disabled weekly_promo task (simulating manual disable in Django admin)
        task = PeriodicTask.objects.create(
            name="weekly_promo",
            task="auctions.tasks.weekly_promo",
            crontab=crontab,
            enabled=False,  # Manually disabled
        )

        # Verify the task is disabled
        self.assertFalse(task.enabled)

        # Run the setup_celery_beat command
        out = StringIO()
        call_command("setup_celery_beat", stdout=out)

        # Refresh the task from database
        task.refresh_from_db()

        # Verify the task is now enabled
        self.assertTrue(task.enabled, "setup_celery_beat should re-enable disabled tasks")

        # Verify the command output mentions the update
        output = out.getvalue()
        self.assertIn("weekly_promo", output)

    def test_creates_missing_tasks(self):
        """Test that setup_celery_beat creates tasks that don't exist."""
        # Delete all periodic tasks to start fresh
        PeriodicTask.objects.filter(name="endauctions").delete()

        # Verify the task doesn't exist
        self.assertFalse(PeriodicTask.objects.filter(name="endauctions").exists())

        # Run the setup_celery_beat command
        out = StringIO()
        call_command("setup_celery_beat", stdout=out)

        # Verify the task was created
        task = PeriodicTask.objects.get(name="endauctions")
        self.assertTrue(task.enabled)
        self.assertEqual(task.task, "auctions.tasks.endauctions")

        # Verify the command output mentions the creation
        output = out.getvalue()
        self.assertIn("Created task: endauctions", output)

    def test_updates_schedule_when_changed(self):
        """Test that setup_celery_beat updates the schedule when it changes."""
        # Create an interval schedule (wrong schedule for weekly_promo)
        interval, _ = IntervalSchedule.objects.get_or_create(every=3600, period=IntervalSchedule.SECONDS)

        # Create a weekly_promo task with wrong schedule
        task = PeriodicTask.objects.create(
            name="weekly_promo",
            task="auctions.tasks.weekly_promo",
            interval=interval,
            enabled=True,
        )

        # Verify the task has the wrong schedule type
        self.assertIsNotNone(task.interval)
        self.assertIsNone(task.crontab)

        # Run the setup_celery_beat command
        out = StringIO()
        call_command("setup_celery_beat", stdout=out)

        # Refresh the task from database
        task.refresh_from_db()

        # Verify the task now has a crontab schedule
        self.assertIsNone(task.interval)
        self.assertIsNotNone(task.crontab)
        # CrontabSchedule stores minute/hour/day_of_week as sets in some versions
        self.assertIn("30", task.crontab.minute)
        self.assertIn("9", task.crontab.hour)
        self.assertIn("3", task.crontab.day_of_week)

        # Verify the command output mentions the update
        output = out.getvalue()
        self.assertIn("Updated task: weekly_promo", output)

    def test_keeps_enabled_tasks_enabled(self):
        """Test that setup_celery_beat doesn't disable already-enabled tasks."""
        # Create a crontab schedule for weekly_promo
        crontab, _ = CrontabSchedule.objects.get_or_create(
            minute="30",
            hour="9",
            day_of_week="3",
            day_of_month="*",
            month_of_year="*",
        )

        # Create an enabled weekly_promo task (correct state)
        task = PeriodicTask.objects.create(
            name="weekly_promo",
            task="auctions.tasks.weekly_promo",
            crontab=crontab,
            enabled=True,  # Already enabled
        )

        # Run the setup_celery_beat command
        out = StringIO()
        call_command("setup_celery_beat", stdout=out)

        # Refresh the task from database
        task.refresh_from_db()

        # Verify the task is still enabled
        self.assertTrue(task.enabled)

        # The command should skip or update the task since it's already correct
        # It's OK if it shows as skipped or updated - both are correct behaviors
        output = out.getvalue()
        self.assertTrue(
            "Skipped (no changes): weekly_promo" in output or "Updated task: weekly_promo" in output,
            f"Expected task to be skipped or updated, got: {output}",
        )
