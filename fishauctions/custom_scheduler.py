"""Celery Beat scheduler working around a django-celery-beat 2.8.1 bug.

Its ``DatabaseScheduler`` excludes crontab tasks whose scheduled hour is outside a ±2 hour window of
the current server hour, so they never run. This subclass disables that optimization and prunes
periodic tasks that have left ``beat_schedule``.
"""

import logging

from django.db.models import Q
from django_celery_beat.schedulers import DatabaseScheduler

logger = logging.getLogger(__name__)

#: PeriodicTask names allowed to exist without being in ``app.conf.beat_schedule``:
#: ``celery.backend_cleanup`` is Celery's own, and the one-off rows are created at runtime.
NOT_FROM_BEAT_SCHEDULE = {"celery.backend_cleanup"}


class FixedDatabaseScheduler(DatabaseScheduler):
    """DatabaseScheduler with the crontab filtering optimization disabled, so every enabled crontab task is
    loaded whatever its scheduled time.
    """

    def _get_crontab_exclude_query(self, *args, **kwargs):
        """Return an empty Q(), so no crontab task is excluded and the parent's other filtering is preserved."""
        return Q()

    def setup_schedule(self):
        """Sync ``beat_schedule`` into the database, then delete the rows that left it.

        ``DatabaseScheduler`` only ever writes entries *into* PeriodicTask, so a task renamed or removed from
        ``beat_schedule`` keeps its row, keeps being dispatched, and reaches the worker as ``NotRegistered``
        -- an hourly error for a feature nobody maintains. That is how ``send_club_event_reminders`` came to
        be dispatched 214 times against code that has never existed here.

        One-off rows are left alone: they are runtime-scheduled and not supposed to be in ``beat_schedule``.
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
