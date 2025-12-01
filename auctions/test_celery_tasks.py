"""
Tests for Celery tasks.

This module tests that Celery tasks properly call their corresponding management commands.
"""

from unittest.mock import patch

from django.test import TestCase

from auctions import tasks


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

        # Run the task
        tasks.update_auction_stats()

        # Verify the auction was processed (next_update_due should be updated)
        auction.refresh_from_db()
        self.assertIsNotNone(auction.next_update_due)
        self.assertGreater(auction.next_update_due, timezone.now())

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

    def test_cleanup_old_auction_stats_tasks(self):
        """Test that cleanup_old_auction_stats_tasks removes old tasks."""
        from datetime import timedelta

        from django.utils import timezone
        from django_celery_beat.models import ClockedSchedule, PeriodicTask

        # Create an old task (more than 24 hours ago)
        old_time = timezone.now() - timedelta(hours=48)
        schedule = ClockedSchedule.objects.create(clocked_time=old_time)
        PeriodicTask.objects.create(
            name=tasks.AUCTION_STATS_TASK_NAME,
            task="auctions.tasks.update_auction_stats",
            clocked=schedule,
            one_off=True,
        )

        # Run the cleanup task
        tasks.cleanup_old_auction_stats_tasks()

        # Verify the old task was deleted
        task = PeriodicTask.objects.filter(name=tasks.AUCTION_STATS_TASK_NAME).first()
        self.assertIsNone(task)
