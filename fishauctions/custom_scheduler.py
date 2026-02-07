"""
Custom Celery Beat Scheduler to work around django-celery-beat 2.8.1 bug.

The DatabaseScheduler in django-celery-beat 2.8.1 has an overly aggressive
optimization that excludes crontab tasks based on a narrow time window. This
causes crontab tasks to not run if their scheduled hour is outside a ±2 hour
window of the current server hour.

This custom scheduler disables that optimization by overriding the methods
that filter out crontab tasks.

Bug reference: django-celery-beat issue with _get_crontab_exclude_query
"""

from django.db.models import Q
from django_celery_beat.schedulers import DatabaseScheduler


class FixedDatabaseScheduler(DatabaseScheduler):
    """
    Custom DatabaseScheduler that disables the crontab filtering optimization.

    This ensures all enabled periodic tasks are loaded into the schedule,
    regardless of their scheduled time.
    """

    def enabled_models_qs(self):
        """
        Return queryset of enabled periodic tasks without filtering crontab tasks.

        This overrides the parent method to remove the _get_crontab_exclude_query
        filter that was causing crontab tasks to be excluded.
        """
        # Only exclude clocked tasks that are far in the future
        from datetime import timedelta

        from django.utils.timezone import now

        SCHEDULE_SYNC_MAX_INTERVAL = 300  # 5 minutes

        next_schedule_sync = now() + timedelta(seconds=SCHEDULE_SYNC_MAX_INTERVAL)

        exclude_clock_tasks_query = Q(clocked__isnull=False, clocked__clocked_time__gt=next_schedule_sync)

        # Don't exclude any crontab tasks - let the scheduler evaluate them all
        # This fixes the bug where crontab tasks outside the current hour window
        # were being excluded from the schedule

        # Fetch all enabled tasks except clocked tasks that are far in the future
        return self.Model.objects.enabled().exclude(exclude_clock_tasks_query)
