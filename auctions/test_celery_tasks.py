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
    def test_email_invoice_task(self, mock_call_command):
        """Test that email_invoice task calls the management command."""
        tasks.email_invoice()
        mock_call_command.assert_called_once_with("email_invoice")

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

    @patch("auctions.tasks.schedule_next_auction_stats_update")
    @patch("auctions.tasks.async_to_sync")
    @patch("auctions.tasks.get_channel_layer")
    def test_update_auction_stats_task_with_auction(self, mock_channel, mock_async, mock_schedule):
        """Test that update_auction_stats task processes an auction and schedules next run."""
        from django.utils import timezone

        from auctions.models import Auction

        # Create an auction that needs stats update
        auction = Auction.objects.create(
            title="Test Auction",
            is_deleted=False,
            next_update_due=timezone.now() - timezone.timedelta(minutes=10),
        )

        # Run the task
        tasks.update_auction_stats()

        # Verify the auction was processed (next_update_due should be updated)
        auction.refresh_from_db()
        self.assertIsNotNone(auction.next_update_due)
        self.assertGreater(auction.next_update_due, timezone.now())

        # Verify the task schedules itself
        mock_schedule.assert_called_once()

    @patch("auctions.tasks.schedule_next_auction_stats_update")
    def test_update_auction_stats_task_no_auction(self, mock_schedule):
        """Test that update_auction_stats task handles no auctions gracefully."""
        # Run the task with no auctions needing update
        tasks.update_auction_stats()

        # Verify the task still schedules itself
        mock_schedule.assert_called_once()

    @patch("auctions.tasks.update_auction_stats")
    def test_schedule_next_auction_stats_update_with_auction(self, mock_task):
        """Test that schedule_next_auction_stats_update schedules the task correctly."""
        from django.utils import timezone

        from auctions.models import Auction

        # Create an auction with a future update due date
        future_time = timezone.now() + timezone.timedelta(hours=2)
        Auction.objects.create(
            title="Test Auction",
            is_deleted=False,
            next_update_due=future_time,
        )

        # Call the scheduling function
        tasks.schedule_next_auction_stats_update()

        # Verify the task was scheduled
        mock_task.apply_async.assert_called_once()

    @patch("auctions.tasks.update_auction_stats")
    def test_schedule_next_auction_stats_update_no_auction(self, mock_task):
        """Test that schedule_next_auction_stats_update schedules fallback when no auctions."""
        # Call the scheduling function with no auctions
        tasks.schedule_next_auction_stats_update()

        # Verify the task was scheduled with fallback delay
        mock_task.apply_async.assert_called_once()
        call_kwargs = mock_task.apply_async.call_args[1]
        self.assertEqual(call_kwargs["countdown"], tasks.STATS_UPDATE_FALLBACK_DELAY_SECONDS)
