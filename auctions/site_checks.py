"""Checks for the site quietly not doing its job, which no error line would show. Read by ``site_health``.

An exception gets logged. These don't: a periodic task that stopped running, or keeps failing while
the others run every minute and keep beat's "last run" fresh; a form that answers 200 and saves
nothing, so signups or page views fall to a fraction of a normal day; lots past their end that nobody
closed; mail that failed or never left the queue.

- :func:`periodic_tasks`: every enabled beat entry that is overdue on its own schedule, and every task
  whose newest run failed. Celery's results are ignored here (``ignore_result``), so the outcome of each
  run is kept in the cache by :func:`connect_celery`'s signal handlers.
- :func:`rates`: the last day's page views, signups and emails against the same 24 hours one to four
  weeks back, so a weekend is compared with weekends. Bids and lots aren't here: they follow the
  auction calendar, and neither table has an index on its date to count them cheaply.
- :func:`invariants`: counts of rows in a state they shouldn't stay in.

Every check is a few bounded counts over an index (``auth_user`` is small enough without one) or a cache
read, and reports its own failure as its answer. :func:`rates` is kept for a few minutes, since a
routine may call ``site_health`` more than once a run.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

#: One key per outcome, never a read-modify-write: two runs finishing together can't erase each other.
_TASK_KEY = "site_checks:task:{}:{}"
#: How long a task's last outcome is kept. Longer than the slowest schedule worth watching.
TASK_KEY_SECONDS = 60 * 60 * 24 * 40
#: A task this late on its own schedule is overdue, at the least.
OVERDUE_GRACE = timedelta(minutes=15)


# --- periodic tasks ----------------------------------------------------------------------------------


def _record(name: str, outcome: str, error: str = "") -> None:
    if not name:
        return
    values = {_TASK_KEY.format(name, outcome): timezone.now().isoformat()}
    if outcome == "failed":
        values[_TASK_KEY.format(name, "error")] = error[:200]
    try:
        cache.set_many(values, timeout=TASK_KEY_SECONDS)
    except Exception:  # the task already ran; Redis being down must not fail it
        logger.debug("Couldn't record task outcome for %s", name, exc_info=True)


def _task_succeeded(sender=None, **kwargs) -> None:
    _record(getattr(sender, "name", ""), "ok")


def _task_failed(sender=None, exception=None, **kwargs) -> None:
    _record(getattr(sender, "name", ""), "failed", type(exception).__name__ if exception else "")


def connect_celery() -> None:
    """Keep each task's last success and failure. Called from ``AuctionsConfig.ready``."""
    from celery.signals import task_failure, task_success

    task_success.connect(_task_succeeded, weak=False, dispatch_uid="site_checks_task_success")
    task_failure.connect(_task_failed, weak=False, dispatch_uid="site_checks_task_failure")


def task_outcome(name: str) -> dict[str, str]:
    """``{"ok": iso, "failed": iso, "error": name}``, whichever are known."""
    keys = {field: _TASK_KEY.format(name, field) for field in ("ok", "failed", "error")}
    try:
        found = cache.get_many(list(keys.values()))
    except Exception:
        return {}
    return {field: found[key] for field, key in keys.items() if found.get(key)}


def _grace(schedule) -> timedelta:
    run_every = getattr(schedule, "run_every", None)
    if isinstance(run_every, timedelta):
        return max(OVERDUE_GRACE, 2 * run_every)
    return OVERDUE_GRACE


def periodic_tasks() -> dict[str, Any]:
    """Enabled beat entries that are overdue, and tasks whose newest run failed."""
    from django_celery_beat.models import PeriodicTask

    now = timezone.now()
    overdue, failing, checked = [], [], 0
    try:
        tasks = list(PeriodicTask.objects.filter(enabled=True, one_off=False, clocked__isnull=True))
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    for task in tasks:
        if task.start_time and task.start_time > now:
            continue
        if task.expires and task.expires < now:
            continue
        checked += 1
        try:
            schedule = task.schedule
            since = task.last_run_at or task.date_changed
            late = -schedule.remaining_estimate(since)
        except Exception as exc:
            # Can't tell lateness; the failure check below still runs.
            overdue.append({"task": task.name, "error": f"{type(exc).__name__}: {exc}"[:200]})
            late = None
        if late is not None and late > _grace(schedule):
            overdue.append(
                {
                    "task": task.name,
                    "last_run": task.last_run_at.isoformat() if task.last_run_at else None,
                    "late_minutes": int(late.total_seconds() // 60),
                }
            )
        outcome = task_outcome(task.task)
        if outcome.get("failed") and outcome["failed"] > outcome.get("ok", ""):
            failing.append({"task": task.name, "failed_at": outcome["failed"], "error": outcome.get("error", "")})
    return {"checked": checked, "overdue": overdue, "failing": failing}


# --- rates ---------------------------------------------------------------------------------------------

#: Weeks back the last day is compared with.
BASELINE_WEEKS = 4
#: A baseline under this many a day is too small to judge.
RATE_MIN_BASELINE = 20
#: Judged low under this share of the baseline, high over this multiple of it.
RATE_LOW = 0.25
RATE_HIGH = 5.0


def _counts(queryset, field: str, now) -> tuple[int, float]:
    """The last day's count and the mean of the same day 1..BASELINE_WEEKS weeks back.

    One bounded count per day, not one aggregate over the month: on ``PageView`` that reads five days of
    the index instead of twenty-nine."""
    day = timedelta(days=1)

    def count(end) -> int:
        return queryset.filter(**{f"{field}__gte": end - day, f"{field}__lt": end}).count()

    weeks = [count(now - timedelta(weeks=week)) for week in range(1, BASELINE_WEEKS + 1)]
    return count(now), sum(weeks) / BASELINE_WEEKS


def _sources():
    from django.contrib.auth.models import User
    from post_office.models import Email

    from auctions.models import PageView

    # (name, queryset, field): PageView.date_start and Email.created are indexed; auth_user is small.
    return [
        ("page_views", PageView.objects.all(), "date_start"),
        ("signups", User.objects.all(), "date_joined"),
        ("emails", Email.objects.all(), "created"),
    ]


_RATES_KEY = "site_checks:rates"
RATES_CACHE_SECONDS = 600


def rates() -> dict[str, Any]:
    """The last 24 hours of each count against the same weekday's average; ``unusual`` names the outliers."""
    try:
        cached = cache.get(_RATES_KEY)
    except Exception:
        cached = None
    if cached is not None:
        return cached
    now = timezone.now()
    found: dict[str, Any] = {}
    unusual: list[str] = []
    for name, queryset, field in _sources():
        try:
            current, baseline = _counts(queryset, field, now)
        except Exception as exc:
            found[name] = {"error": f"{type(exc).__name__}"}
            continue
        found[name] = {"last_24h": current, "usual": round(baseline, 1)}
        if baseline < RATE_MIN_BASELINE:
            continue
        if current < baseline * RATE_LOW or current > baseline * RATE_HIGH:
            unusual.append(f"{name.replace('_', ' ')} {current} in 24 hours, usually {round(baseline)}")
    found["unusual"] = unusual
    try:
        cache.set(_RATES_KEY, found, timeout=RATES_CACHE_SECONDS)
    except Exception:
        logger.debug("Couldn't cache the rates", exc_info=True)
    return found


# --- invariants ----------------------------------------------------------------------------------------

#: An online lot this far past its end and still open was missed by endauctions.
LOT_END_GRACE = timedelta(minutes=15)
#: Mail still queued this long after it was due never left.
MAIL_STUCK_AFTER = timedelta(hours=1)


def invariants(since) -> dict[str, Any]:
    """Rows that shouldn't stay as they are: each count should be 0."""
    from django.db.models import Q
    from post_office.models import STATUS, Email

    from auctions.models import Lot

    now = timezone.now()
    found: dict[str, Any] = {}
    try:
        found["online_lots_not_closed"] = (
            Lot.objects.filter(active=True, is_deleted=False, banned=False, deactivated=False)
            .filter(Q(auction__isnull=True) | Q(auction__is_online=True))
            .filter(date_end__lt=now - LOT_END_GRACE)
            .count()
        )
    except Exception as exc:
        found["online_lots_not_closed"] = {"error": f"{type(exc).__name__}"}
    try:
        found["emails_failed"] = Email.objects.filter(status=STATUS.failed, last_updated__gte=since).count()
        stuck = now - MAIL_STUCK_AFTER
        found["emails_stuck_in_queue"] = (
            Email.objects.filter(status__in=[STATUS.queued, STATUS.requeued], created__lt=stuck)
            .filter(Q(scheduled_time__isnull=True) | Q(scheduled_time__lt=stuck))
            .count()
        )
    except Exception as exc:
        found["emails_failed"] = {"error": f"{type(exc).__name__}"}
    found["problems"] = [
        f"{name.replace('_', ' ')}: {value}" for name, value in found.items() if isinstance(value, int) and value
    ]
    return found
