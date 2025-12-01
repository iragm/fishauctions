"""
Management command to set up Celery Beat periodic tasks in the database.

This command creates PeriodicTask entries in the django-celery-beat database
from the beat_schedule defined in fishauctions/celery.py.
"""

from django.core.management.base import BaseCommand
from django_celery_beat.models import CrontabSchedule, IntervalSchedule, PeriodicTask

from fishauctions.celery import app


class Command(BaseCommand):
    help = "Set up Celery Beat periodic tasks in the database from celery.py beat_schedule"

    def handle(self, *args, **options):
        """Create or update periodic tasks from the beat schedule."""
        beat_schedule = app.conf.beat_schedule
        created_count = 0
        updated_count = 0
        skipped_count = 0
        deleted_count = 0

        # Get names of tasks that should exist
        expected_task_names = set(beat_schedule.keys())

        # Delete tasks that are no longer in the beat_schedule
        existing_tasks = PeriodicTask.objects.filter(
            name__in=[
                "endauctions",
                "sendnotifications",
                "auctiontos_notifications",
                "email_invoice",
                "send_queued_mail",
                "auction_emails",
                "email_unseen_chats",
                "weekly_promo",
                "set_user_location",
                "remove_duplicate_views",
                "webpush_notifications_deduplicate",
                "update_auction_stats",  # Old task that should be removed
            ]
        )
        for task in existing_tasks:
            if task.name not in expected_task_names:
                self.stdout.write(self.style.ERROR(f"  ✗ Deleting removed task: {task.name}"))
                task.delete()
                deleted_count += 1

        for task_name, task_config in beat_schedule.items():
            task_path = task_config["task"]
            schedule = task_config["schedule"]

            self.stdout.write(f"Processing task: {task_name}")

            # Determine if it's an interval or crontab schedule
            if isinstance(schedule, float) or isinstance(schedule, int):
                # Interval schedule (in seconds)
                interval, created = IntervalSchedule.objects.get_or_create(
                    every=int(schedule), period=IntervalSchedule.SECONDS
                )
                schedule_obj = interval
                schedule_type = "interval"
            else:
                # Crontab schedule
                crontab, created = CrontabSchedule.objects.get_or_create(
                    minute=schedule.minute,
                    hour=schedule.hour,
                    day_of_week=schedule.day_of_week,
                    day_of_month=schedule.day_of_month,
                    month_of_year=schedule.month_of_year,
                )
                schedule_obj = crontab
                schedule_type = "crontab"

            # Create or update the periodic task
            task, task_created = PeriodicTask.objects.get_or_create(
                name=task_name,
                defaults={
                    "task": task_path,
                    schedule_type: schedule_obj,
                    "enabled": True,
                },
            )

            if task_created:
                self.stdout.write(self.style.SUCCESS(f"  ✓ Created task: {task_name}"))
                created_count += 1
            else:
                # Update existing task
                updated = False
                if task.task != task_path:
                    task.task = task_path
                    updated = True
                if schedule_type == "interval" and task.interval != schedule_obj:
                    task.interval = schedule_obj
                    task.crontab = None
                    updated = True
                elif schedule_type == "crontab" and task.crontab != schedule_obj:
                    task.crontab = schedule_obj
                    task.interval = None
                    updated = True

                if updated:
                    task.save()
                    self.stdout.write(self.style.WARNING(f"  ↻ Updated task: {task_name}"))
                    updated_count += 1
                else:
                    self.stdout.write(f"  - Skipped (no changes): {task_name}")
                    skipped_count += 1

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Summary:"))
        self.stdout.write(self.style.SUCCESS(f"  Created: {created_count}"))
        self.stdout.write(self.style.WARNING(f"  Updated: {updated_count}"))
        self.stdout.write(f"  Skipped: {skipped_count}")
        if deleted_count > 0:
            self.stdout.write(self.style.ERROR(f"  Deleted: {deleted_count}"))
        self.stdout.write(f"  Total in schedule: {len(beat_schedule)}")
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS("Periodic tasks are now visible in Django Admin → Periodic Tasks → Periodic tasks")
        )
