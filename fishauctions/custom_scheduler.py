"""
Custom Celery Beat Scheduler to work around django-celery-beat 2.8.1 bug.

The DatabaseScheduler in django-celery-beat 2.8.1 has an overly aggressive
optimization that excludes crontab tasks based on a narrow time window. This
causes crontab tasks to not run if their scheduled hour is outside a ±2 hour
window of the current server hour.

This custom scheduler disables that optimization by overriding the specific
method responsible for crontab exclusion.

Bug reference: django-celery-beat issue with _get_crontab_exclude_query
"""

from django.db.models import Q
from django_celery_beat.schedulers import DatabaseScheduler


class FixedDatabaseScheduler(DatabaseScheduler):
    """
    Custom DatabaseScheduler that disables the crontab filtering optimization.

    This ensures all enabled crontab periodic tasks are loaded into the schedule,
    regardless of their scheduled time, while preserving all other parent filtering
    behavior.
    """

    def _get_crontab_exclude_query(self, *args, **kwargs):
        """
        Disable the crontab exclusion optimization from the parent scheduler.

        By returning an empty Q(), all crontab-based periodic tasks remain
        eligible for scheduling, while all other filtering behavior defined
        in the parent DatabaseScheduler is preserved.

        This fixes the bug in django-celery-beat 2.8.1 where crontab tasks
        outside a ±2 hour window of the current server hour were excluded.
        """
        return Q()
