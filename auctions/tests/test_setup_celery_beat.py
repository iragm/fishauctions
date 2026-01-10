"""Tests for the setup_celery_beat management command."""

from django.core.management import call_command
from django.test import TestCase
from django_celery_beat.models import IntervalSchedule, PeriodicTask


class SetupCeleryBeatCommandTestCase(TestCase):
    """Test case for the setup_celery_beat management command."""

    def test_creates_interval_tasks_with_start_time(self):
        """Test that interval-based tasks are created with start_time set."""
        # Run the setup command
        call_command("setup_celery_beat")

        # Check that interval-based tasks have start_time set
        interval_tasks = PeriodicTask.objects.filter(interval__isnull=False)
        self.assertGreater(interval_tasks.count(), 0, "Should create interval-based tasks")

        for task in interval_tasks:
            with self.subTest(task=task.name):
                self.assertIsNotNone(
                    task.start_time, f"Interval task {task.name} should have start_time set"
                )

    def test_updates_existing_interval_tasks_without_start_time(self):
        """Test that existing interval tasks without start_time get updated."""
        # Create an interval schedule matching set_user_location (7200 seconds = 2 hours)
        interval = IntervalSchedule.objects.create(every=7200, period=IntervalSchedule.SECONDS)

        # Create set_user_location task without start_time (simulating existing task)
        task = PeriodicTask.objects.create(
            name="set_user_location",
            task="auctions.tasks.set_user_location",
            interval=interval,
            enabled=True,
        )

        # Verify start_time is None before running setup
        self.assertIsNone(task.start_time, "Task should not have start_time before setup")

        # Run the setup command
        call_command("setup_celery_beat")

        # Refresh and verify the task now has start_time set
        task.refresh_from_db()
        self.assertIsNotNone(
            task.start_time,
            "set_user_location should have start_time set after setup command runs",
        )

    def test_crontab_tasks_do_not_get_start_time(self):
        """Test that crontab-based tasks do not get start_time set by the command."""
        # Run the setup command
        call_command("setup_celery_beat")

        # Check that crontab-based tasks exist and verify start_time behavior
        crontab_tasks = PeriodicTask.objects.filter(crontab__isnull=False, interval__isnull=True)
        
        # Should have at least one crontab task (e.g., email_unseen_chats, weekly_promo)
        self.assertGreater(crontab_tasks.count(), 0, "Should have crontab-based tasks")

        for task in crontab_tasks:
            with self.subTest(task=task.name):
                # Verify the task has a crontab schedule
                self.assertIsNotNone(task.crontab, f"Crontab task {task.name} should have crontab set")
                # Verify that our command does not set start_time for crontab tasks
                # (start_time should be None since we only set it for interval tasks)
                self.assertIsNone(
                    task.start_time,
                    f"Crontab task {task.name} should not have start_time set by setup command",
                )

    def test_all_expected_tasks_created(self):
        """Test that all expected tasks from celery.py are created."""
        # Run the setup command
        call_command("setup_celery_beat")

        # Expected tasks from fishauctions/celery.py
        expected_tasks = [
            "endauctions",
            "sendnotifications",
            "auctiontos_notifications",
            "send_queued_mail",
            "auction_emails",
            "email_unseen_chats",
            "weekly_promo",
            "set_user_location",
            "remove_duplicate_views",
            "webpush_notifications_deduplicate",
            "cleanup_old_invoice_notification_tasks",
        ]

        for task_name in expected_tasks:
            with self.subTest(task=task_name):
                task = PeriodicTask.objects.filter(name=task_name).first()
                self.assertIsNotNone(task, f"Task {task_name} should exist")
                self.assertTrue(task.enabled, f"Task {task_name} should be enabled")
