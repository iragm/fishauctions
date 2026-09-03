"""
Tests for Celery tasks.

This module tests that Celery tasks properly call their corresponding management commands.
"""

import datetime
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from django_celery_beat.models import PeriodicTask

from auctions import tasks
from auctions.models import Auction, AuctionHistory, AuctionTOS, Club, Invoice, PickupLocation
from auctions.test_support import isolated_cache


# isolated_cache is required, not tidiness: endauctions and compute_user_flow_all now take a cache
# lock, and --parallel workers share one Redis. Without it, two workers running these at the same
# moment would have one of them correctly skip its run and fail its own assertion.
@isolated_cache("celery-tasks")
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

    def test_schedule_bap_recalculation_preserves_shared_clocked_schedule(self):
        """Rescheduling one club should not delete a shared schedule used by another club."""
        from django_celery_beat.models import ClockedSchedule, PeriodicTask

        run_at = timezone.now()
        shared_schedule = ClockedSchedule.objects.create(clocked_time=run_at)
        club_one_task_name = f"{tasks.BAP_RECALCULATION_TASK_PREFIX}1"
        club_two_task_name = f"{tasks.BAP_RECALCULATION_TASK_PREFIX}2"

        PeriodicTask.objects.create(
            name=club_one_task_name,
            task="auctions.tasks.recalculate_club_bap_points",
            clocked=shared_schedule,
            one_off=True,
            enabled=True,
            kwargs='{"club_pk": 1}',
        )
        other_task = PeriodicTask.objects.create(
            name=club_two_task_name,
            task="auctions.tasks.recalculate_club_bap_points",
            clocked=shared_schedule,
            one_off=True,
            enabled=True,
            kwargs='{"club_pk": 2}',
        )

        tasks.schedule_bap_recalculation(1, run_at=run_at + datetime.timedelta(days=1))

        other_task.refresh_from_db()
        self.assertEqual(other_task.clocked_id, shared_schedule.id)
        self.assertTrue(ClockedSchedule.objects.filter(id=shared_schedule.id).exists())

    def test_schedule_bap_recalculation_reuses_existing_clocked_schedule_for_same_time(self):
        """Rescheduling a club at the same time should keep using the existing schedule row."""
        from django_celery_beat.models import ClockedSchedule, PeriodicTask

        run_at = timezone.now()
        schedule = ClockedSchedule.objects.create(clocked_time=run_at)
        task_name = f"{tasks.BAP_RECALCULATION_TASK_PREFIX}1"

        PeriodicTask.objects.create(
            name=task_name,
            task="auctions.tasks.recalculate_club_bap_points",
            clocked=schedule,
            one_off=True,
            enabled=False,
            kwargs='{"club_pk": 1}',
        )

        tasks.schedule_bap_recalculation(1, run_at=run_at)

        self.assertTrue(ClockedSchedule.objects.filter(id=schedule.id).exists())
        task = PeriodicTask.objects.get(name=task_name)
        self.assertEqual(task.clocked_id, schedule.id)
        self.assertTrue(task.enabled)

    @patch("auctions.tasks.schedule_bap_recalculation")
    def test_bootstrap_bap_recalculation_tasks_schedules_enabled_clubs(self, mock_schedule):
        """BAP-enabled clubs should get a scheduled task on worker startup bootstrap."""
        now = timezone.now()
        Club.objects.create(name="Bootstrap Club", enable_breeder_award_program=True)
        future_run = now + datetime.timedelta(days=7)
        club_with_future_run = Club.objects.create(
            name="Bootstrap Future Club",
            enable_breeder_award_program=True,
            next_bap_recalculation=future_run,
        )
        Club.objects.create(name="Non-BAP Club", enable_breeder_award_program=False)

        tasks.bootstrap_bap_recalculation_tasks(run_at=now)

        mock_schedule.assert_any_call(club_with_future_run.pk, run_at=future_run)
        self.assertEqual(mock_schedule.call_count, 1)

    @patch("auctions.tasks.schedule_bap_recalculation")
    def test_bootstrap_bap_recalculation_tasks_runs_overdue_clubs_immediately(self, mock_schedule):
        """Past-due BAP clubs should be rescheduled to run immediately on startup."""
        now = timezone.now()
        overdue_run = now - datetime.timedelta(days=1)
        club = Club.objects.create(
            name="Bootstrap Overdue Club",
            enable_breeder_award_program=True,
            next_bap_recalculation=overdue_run,
        )

        tasks.bootstrap_bap_recalculation_tasks(run_at=now)

        mock_schedule.assert_called_once_with(club.pk, run_at=now)

    @patch("auctions.tasks.bootstrap_bap_recalculation_tasks")
    def test_worker_ready_starts_bap_bootstrap(self, mock_bootstrap):
        """The worker_ready hook should bootstrap BAP self-scheduling tasks."""
        from fishauctions.celery import WORKER_READY_TASK_DELAY_SECONDS, start_bap_recalculation_tasks

        now = timezone.now()

        with patch("django.utils.timezone.now", return_value=now):
            start_bap_recalculation_tasks(sender=None)

        mock_bootstrap.assert_called_once_with(now + datetime.timedelta(seconds=WORKER_READY_TASK_DELAY_SECONDS))


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


class FixedDatabaseSchedulerTestCase(TestCase):
    """Test case for the custom FixedDatabaseScheduler."""

    def test_get_crontab_exclude_query_returns_empty(self):
        """Test that _get_crontab_exclude_query returns an empty Q() object."""
        from django.db.models import Q

        from fishauctions.custom_scheduler import FixedDatabaseScheduler

        # Create scheduler instance - but don't let it initialize fully
        # We just want to test the method override
        scheduler = object.__new__(FixedDatabaseScheduler)

        # Call the overridden method
        result = scheduler._get_crontab_exclude_query()

        # Verify it returns an empty Q() object
        self.assertIsInstance(result, Q)
        self.assertEqual(str(result), str(Q()))

    def test_crontab_tasks_not_filtered_by_hour(self):
        """Test that crontab tasks are loaded regardless of their scheduled hour."""
        from django_celery_beat.models import CrontabSchedule

        # Create a crontab schedule for a time far from current hour
        # This would be filtered out by the buggy scheduler
        crontab = CrontabSchedule.objects.create(
            minute="30",
            hour="3",  # 3 AM - likely far from test execution time
            day_of_week="*",
            day_of_month="*",
            month_of_year="*",
        )

        # Create an enabled periodic task with this crontab
        PeriodicTask.objects.create(
            name="test_crontab_task_scheduler",
            task="auctions.tasks.endauctions",
            crontab=crontab,
            enabled=True,
        )

        # Import after creating the task to avoid initialization issues
        from fishauctions.custom_scheduler import FixedDatabaseScheduler

        # Test that the fixed scheduler's query method returns empty Q
        scheduler_obj = object.__new__(FixedDatabaseScheduler)
        exclude_query = scheduler_obj._get_crontab_exclude_query()

        # The exclude query should be empty, meaning no crontab tasks are excluded
        from django.db.models import Q

        self.assertEqual(str(exclude_query), str(Q()))


@isolated_cache("celery-locks")
class OverlapLockTestCase(TestCase):
    """The two tasks that must never run twice at once."""

    def setUp(self):
        # A lock deliberately taken by one test would otherwise still be held by the next one --
        # these tests are exactly the ones that leave locks behind.
        from django.core.cache import cache

        cache.delete(tasks.ENDAUCTIONS_LOCK_KEY)
        cache.delete(tasks.USER_FLOW_LOCK_KEY)

    @patch("auctions.tasks.call_command")
    def test_endauctions_skips_a_tick_it_is_already_running(self, mock_call_command):
        """The beat fires this every 60 seconds and the soft time limit is 300, so a slow run --
        which is a run at the moment a big auction ends -- overlaps the next one. Two runs read the
        same active lots and both see `sold` as False, so both send the lot-ended message and both
        write invoices."""
        from django.core.cache import cache

        cache.add(tasks.ENDAUCTIONS_LOCK_KEY, "1", timeout=60)
        tasks.endauctions()
        mock_call_command.assert_not_called()

    @patch("auctions.tasks.call_command")
    def test_endauctions_releases_the_lock_when_the_command_raises(self, mock_call_command):
        """A lock held by a crashed run would stop the auction ending for as long as it lasts."""
        from django.core.cache import cache

        boom = "the command blew up"
        mock_call_command.side_effect = RuntimeError(boom)
        with self.assertRaises(RuntimeError):
            tasks.endauctions()
        self.assertIsNone(cache.get(tasks.ENDAUCTIONS_LOCK_KEY))

    @patch("auctions.tasks._compute_user_flow_all")
    def test_a_second_user_flow_request_is_dropped_rather_than_queued(self, mock_compute):
        """It holds a worker slot for as long as it takes (time_limit=None) and the worker runs at
        concurrency=2, so two presses of the admin button used to stop every other task on the
        site -- endauctions included."""
        from django.core.cache import cache

        cache.add(tasks.USER_FLOW_LOCK_KEY, "1", timeout=60)
        tasks.compute_user_flow_all()
        mock_compute.assert_not_called()

    @patch("auctions.tasks._compute_user_flow_all")
    def test_the_user_flow_lock_is_released_afterwards(self, mock_compute):
        from django.core.cache import cache

        tasks.compute_user_flow_all()
        mock_compute.assert_called_once()
        self.assertIsNone(cache.get(tasks.USER_FLOW_LOCK_KEY))


@isolated_cache("celery-ytd")
class YearlyBapResetTestCase(TestCase):
    """The reset used to be an `if today is January 1` branch in the middle of another task."""

    def setUp(self):
        self.club = Club.objects.create(name="YTD Club", enable_breeder_award_program=True)

    def _member(self, **kwargs):
        from auctions.models import ClubMember

        return ClubMember.objects.create(club=self.club, name="Breeder", bap_points_ytd=12, **kwargs)

    def test_it_catches_up_when_it_missed_the_first_of_january(self):
        """The whole point. Nothing here is a date check: a club whose recorded year is behind the
        current one is reset whenever this next runs, however late."""
        member = self._member()
        tasks.reset_yearly_bap_counters()
        member.refresh_from_db()
        self.assertEqual(member.bap_points_ytd, 0)

    def test_it_does_not_zero_the_same_club_twice(self):
        """It runs daily and is a no-op on 364 of them; a second run must not wipe points earned
        since the first."""
        member = self._member()
        tasks.reset_yearly_bap_counters()
        member.bap_points_ytd = 5
        member.save(update_fields=["bap_points_ytd"])
        tasks.reset_yearly_bap_counters()
        member.refresh_from_db()
        self.assertEqual(member.bap_points_ytd, 5)

    def test_a_club_without_the_program_is_left_alone(self):
        other = Club.objects.create(name="No BAP", enable_breeder_award_program=False)
        from auctions.models import ClubMember

        member = ClubMember.objects.create(club=other, name="Somebody", bap_points_ytd=7)
        tasks.reset_yearly_bap_counters()
        member.refresh_from_db()
        self.assertEqual(member.bap_points_ytd, 7)

    def test_the_year_is_recorded_on_the_club(self):
        self._member()
        tasks.reset_yearly_bap_counters()
        self.club.refresh_from_db()
        self.assertEqual(self.club.bap_ytd_reset_year, timezone.localtime().year)


@isolated_cache("celery-stats-watchdog")
class AuctionStatsWatchdogTestCase(TestCase):
    """update_auction_stats is not on the beat; it re-arms itself at the end of every run.

    A run killed by the hard time limit never reaches that call, and beat has already disabled the
    one-off row that fired it, so the chain simply stops.
    """

    @patch("auctions.tasks.schedule_auction_stats_update")
    def test_it_re_arms_when_the_task_row_is_gone(self, mock_schedule):
        PeriodicTask.objects.filter(name=tasks.AUCTION_STATS_TASK_NAME).delete()
        tasks.ensure_auction_stats_task_scheduled()
        mock_schedule.assert_called_once()

    @staticmethod
    def _arm(run_at, *, enabled=True):
        """The row the real scheduler would leave behind, built without it.

        `schedule_auction_stats_update` is what these tests patch, so calling it here would record
        a call and create nothing.
        """
        from django_celery_beat.models import ClockedSchedule

        schedule, _ = ClockedSchedule.objects.get_or_create(clocked_time=run_at)
        PeriodicTask.objects.filter(name=tasks.AUCTION_STATS_TASK_NAME).delete()
        return PeriodicTask.objects.create(
            name=tasks.AUCTION_STATS_TASK_NAME,
            task="auctions.tasks.update_auction_stats",
            clocked=schedule,
            one_off=True,
            enabled=enabled,
        )

    @patch("auctions.tasks.schedule_auction_stats_update")
    def test_it_re_arms_when_the_row_is_disabled(self, mock_schedule):
        """Which is exactly the state django-celery-beat leaves a fired one-off row in."""
        self._arm(timezone.now() + datetime.timedelta(minutes=5), enabled=False)
        tasks.ensure_auction_stats_task_scheduled()
        mock_schedule.assert_called_once()

    @patch("auctions.tasks.schedule_auction_stats_update")
    def test_it_re_arms_when_the_scheduled_time_is_long_past(self, mock_schedule):
        self._arm(timezone.now() - datetime.timedelta(seconds=tasks.STATS_WATCHDOG_GRACE_SECONDS + 60))
        tasks.ensure_auction_stats_task_scheduled()
        mock_schedule.assert_called_once()

    @patch("auctions.tasks.schedule_auction_stats_update")
    def test_a_healthy_chain_is_left_alone(self, mock_schedule):
        """One indexed lookup every 15 minutes and nothing else, or the watchdog would be fighting
        the task it is watching."""
        self._arm(timezone.now() + datetime.timedelta(minutes=5))
        tasks.ensure_auction_stats_task_scheduled()
        mock_schedule.assert_not_called()


class PerItemIsolationTestCase(TestCase):
    """One failing row must not stop the rest of the list, and must not make the whole task retry
    it from the top -- which is what the wallet tasks used to do."""

    class _FakeTask:
        """Stands in for a bound Celery task. `self.retry(exc=...)` returns the exception for the
        caller to raise, which is how Celery's own retry is used in this file."""

        def __init__(self):
            self.retried_with = None

        def retry(self, exc=None):
            self.retried_with = exc
            return exc

    def test_every_item_is_attempted_even_after_a_failure(self):
        import requests

        seen = []
        message = "nope"

        def do_one(item):
            seen.append(item)
            if item == "b":
                raise requests.RequestException(message)

        task = self._FakeTask()
        with self.assertRaises(RuntimeError):
            tasks._per_item(task, "wallet refresh", ["a", "b", "c"], do_one)
        self.assertEqual(seen, ["a", "b", "c"], "c was skipped -- one bad row stopped the list")
        self.assertIn("b: nope", str(task.retried_with))

    def test_nothing_is_raised_when_every_item_succeeds(self):
        task = self._FakeTask()
        self.assertIsNone(tasks._per_item(task, "label", [1, 2, 3], lambda item: None))
        self.assertIsNone(task.retried_with)


class OrphanedPeriodicTaskTestCase(TestCase):
    """DatabaseScheduler only ever writes beat_schedule *into* the database.

    A row that leaves beat_schedule -- or one created by hand for code that was never written --
    keeps being dispatched forever and reaches the worker as NotRegistered.
    """

    def _scheduler(self):
        from fishauctions.celery import app
        from fishauctions.custom_scheduler import FixedDatabaseScheduler

        scheduler = object.__new__(FixedDatabaseScheduler)
        scheduler.app = app
        return scheduler

    @staticmethod
    def _row(name, task, **kwargs):
        """PeriodicTask insists on a schedule of some kind, even for a row nobody will run."""
        from django_celery_beat.models import IntervalSchedule

        interval, _ = IntervalSchedule.objects.get_or_create(every=1, period=IntervalSchedule.HOURS)
        return PeriodicTask.objects.create(name=name, task=task, interval=interval, enabled=True, **kwargs)

    def test_a_row_that_is_not_in_beat_schedule_is_removed(self):
        self._row("send_club_event_reminders", "auctions.tasks.send_club_event_reminders")
        self._scheduler()._prune_orphaned_entries()
        self.assertFalse(PeriodicTask.objects.filter(name="send_club_event_reminders").exists())

    def test_a_row_that_is_in_beat_schedule_survives(self):
        self._row("endauctions", "auctions.tasks.endauctions")
        self._scheduler()._prune_orphaned_entries()
        self.assertTrue(PeriodicTask.objects.filter(name="endauctions").exists())

    def test_one_off_rows_are_left_alone(self):
        """Auction stats, invoice notifications and BAP recalculations are scheduled at runtime and
        are not supposed to be in beat_schedule."""
        self._row("invoice_notification_999", "auctions.tasks.send_invoice_notification", one_off=True)
        self._scheduler()._prune_orphaned_entries()
        self.assertTrue(PeriodicTask.objects.filter(name="invoice_notification_999").exists())

    def test_celerys_own_row_survives(self):
        self._row("celery.backend_cleanup", "celery.backend_cleanup")
        self._scheduler()._prune_orphaned_entries()
        self.assertTrue(PeriodicTask.objects.filter(name="celery.backend_cleanup").exists())

    def test_every_beat_entry_names_a_task_that_exists(self):
        """The other half: a beat entry whose task was renamed or deleted is dispatched forever and
        never runs. This is what would have caught send_club_event_reminders at review time."""
        from fishauctions.celery import app

        app.loader.import_default_modules()
        missing = sorted(entry["task"] for entry in app.conf.beat_schedule.values() if entry["task"] not in app.tasks)
        self.assertEqual(missing, [], "these beat_schedule entries name tasks that do not exist")
