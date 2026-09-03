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

import logging

from django.db.models import Q
from django_celery_beat.schedulers import DatabaseScheduler

logger = logging.getLogger(__name__)

#: PeriodicTask names that are allowed to exist without being in ``app.conf.beat_schedule``.
#: ``celery.backend_cleanup`` is Celery's own, and the one-off rows are created at runtime by
#: ``schedule_auction_stats_update`` / ``schedule_invoice_notification`` / ``schedule_bap_recalculation``.
NOT_FROM_BEAT_SCHEDULE = {"celery.backend_cleanup"}


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

    def setup_schedule(self):
        """Sync ``beat_schedule`` into the database, then delete the rows that left it.

        ``DatabaseScheduler`` only ever writes entries *into* the PeriodicTask table. A task
        renamed or removed from ``beat_schedule`` -- or created by hand in the Django admin for
        something that was never written -- keeps its row, keeps being dispatched on its interval
        forever, and reaches the worker as ``NotRegistered``: an hourly error for a feature nobody
        is maintaining, with nothing anywhere saying so. That is not hypothetical; it is how
        ``send_club_event_reminders`` came to be dispatched 214 times against code that has never
        existed in this repository.

        One-off rows are left alone -- those are the runtime-scheduled ones (auction stats, invoice
        notifications, BAP recalculations) and are not supposed to be in ``beat_schedule``.
        """
        super().setup_schedule()
        try:
            self._prune_orphaned_entries()
        except Exception:
            # Beat starting is more important than this tidying up.
            logger.exception("Could not reconcile PeriodicTask rows against beat_schedule")

    def _prune_orphaned_entries(self):
        from django_celery_beat.models import PeriodicTask

        known = set(self.app.conf.beat_schedule) | NOT_FROM_BEAT_SCHEDULE
        orphans = PeriodicTask.objects.filter(one_off=False).exclude(name__in=known)
        for name, task in orphans.values_list("name", "task"):
            logger.warning("Removing orphaned periodic task %s (%s): it is not in beat_schedule", name, task)
        orphans.delete()
