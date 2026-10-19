"""When the server and each container last started, and the longest stretch with no page views.

After an outage this tells a reboot (the host's boot time moved), a restart (only container start
times moved) and a freeze (nothing moved, but the page views stop) apart, from ``site_health`` on
``/mcp/admin/``, without anyone logging in to the server.

- The host's boot time is ``btime`` in ``/proc/stat``, which a container shares with its host.
- A container started when its PID 1 did. The web container reads its own; each Celery container
  writes its own into the cache as it starts (:func:`record_start`, from ``fishauctions.celery``),
  since the web container can't see the others. The database and Redis report their own uptime.
- The quiet stretches are the longest gaps between ``PageView`` rows in the last day, the stretch up to
  now included. The logs can't answer it: at night they go quiet for an hour with nothing wrong.
  One query over the ``date_start`` index, never a scan of the table, which is kept forever.
"""

from __future__ import annotations

import heapq
import os
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path
from typing import Any

from django.core.cache import cache
from django.utils import timezone

#: How far back the quiet stretch is looked for.
QUIET_WINDOW = timedelta(hours=24)
#: How many of the longest stretches with no page views are reported.
QUIET_GAPS = 3
#: A stretch with no page views at least this long is named in ``site_health``'s summary.
QUIET_WARN = timedelta(minutes=10)
#: The Celery containers, by the ``LOG_SERVICE_NAME`` each runs with.
CELERY_SERVICES = ("celery", "documents", "beat")
_CACHE_KEY = "uptime:started:{}"


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def host_booted() -> datetime | None:
    """When the host last booted, from ``btime`` in ``/proc/stat``."""
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return datetime.fromtimestamp(int(line.split()[1]), tz=dt_timezone.utc)
    except (OSError, ValueError, IndexError):
        pass
    return None


def container_started() -> datetime | None:
    """When this container started: its PID 1's start time, in clock ticks after boot."""
    booted = host_booted()
    if booted is None:
        return None
    try:
        stat = Path("/proc/1/stat").read_text()
        # The command name is in parentheses and may hold spaces; the fields after it are fixed.
        ticks = int(stat.rsplit(")", 1)[1].split()[19])
        return booted + timedelta(seconds=ticks / os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError):
        return None


def record_start(service: str) -> None:
    """Write this container's start time into the cache for the web container to read."""
    try:
        cache.set(_CACHE_KEY.format(service), _iso(container_started()), timeout=None)
    except Exception:  # a worker must start whether or not Redis is up
        pass


def _celery_started() -> dict[str, Any]:
    try:
        found = cache.get_many([_CACHE_KEY.format(service) for service in CELERY_SERVICES])
    except Exception as exc:  # the cache being down is the answer, not a failure of this tool
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {service: found.get(_CACHE_KEY.format(service)) for service in CELERY_SERVICES}


def _database_started() -> str | None:
    from django.db import connection

    if connection.vendor != "mysql":
        return None
    try:
        with connection.cursor() as cursor:
            cursor.execute("SHOW GLOBAL STATUS LIKE 'Uptime'")
            row = cursor.fetchone()
    except Exception:  # unreadable is the answer, not a failure of this tool
        return None
    return _iso(timezone.now() - timedelta(seconds=int(row[1]))) if row else None


def _redis_started() -> str | None:
    from django.conf import settings

    try:
        import redis

        client = redis.Redis.from_url(settings.CELERY_BROKER_URL, socket_timeout=2, socket_connect_timeout=2)
        return _iso(timezone.now() - timedelta(seconds=int(client.info("server")["uptime_in_seconds"])))
    except Exception:  # unreadable is the answer, not a failure of this tool
        return None


def longest_quiet(now: datetime | None = None) -> list[dict[str, Any]]:
    """The :data:`QUIET_GAPS` longest gaps between page views within :data:`QUIET_WINDOW`, longest
    first, the gap up to ``now`` included. Several, so a lull at night doesn't hide a freeze by day."""
    from auctions.models import PageView

    now = now or timezone.now()
    since = now - QUIET_WINDOW
    gaps: list[tuple[timedelta, datetime, datetime]] = []
    previous = since
    views = PageView.objects.filter(date_start__gte=since, date_start__lte=now).order_by("date_start")
    for moment in [*views.values_list("date_start", flat=True).iterator(), now]:
        heapq.heappush(gaps, (moment - previous, previous, moment))
        if len(gaps) > QUIET_GAPS:
            heapq.heappop(gaps)
        previous = moment
    return [
        {"minutes": int(gap.total_seconds() // 60), "from": _iso(start), "to": _iso(end)}
        for gap, start, end in sorted(gaps, reverse=True)
    ]


def uptime() -> dict[str, Any]:
    """Boot and start times for ``site_health``, as ISO timestamps; ``None`` where one can't be read."""
    return {
        "host_booted": _iso(host_booted()),
        "containers_started": {"django": _iso(container_started()), **_celery_started()},
        "database_started": _database_started(),
        "redis_started": _redis_started(),
        "longest_without_page_views_24h": longest_quiet(),
    }
