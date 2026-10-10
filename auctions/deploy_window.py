"""Whether now is a quiet time to deploy production: traffic against its usual lows, and auctions in play.

Advice, never a block. ``site_health`` on ``/mcp/admin/`` carries it, so an agent asked to deploy reads
it first and says "probably not a good time" when it isn't; ``manage.py deploy_window`` prints it.

Traffic is page views (``PageView`` rows) in the last hour, set against every hour of the week before
it. "Near the lows" means at most :data:`LOW_FACTOR` times the week's :data:`LOW_PERCENTILE`th
percentile hour, plus :data:`LOW_SLACK` so a dead-quiet week doesn't make ten views look busy. An
auction is in play when an online one ends within :data:`ENDING_SOON` (or ended within the hour,
since late bids extend lots by up to one) or an in-person one is running: started within
:data:`IN_PERSON_LENGTH`, or starts within :data:`ENDING_SOON`. A restart drops their websockets.

The traffic is one query: a conditional count per hour over the ``date_start`` index, never a scan of
the table, which is kept forever.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

#: Hours of history the current hour is compared with.
HISTORY_HOURS = 7 * 24
LOW_PERCENTILE = 25
LOW_FACTOR = 1.5
LOW_SLACK = 10
ENDING_SOON = timedelta(hours=2)
#: Online bidding can run this long past an auction's end on lots that got late bids.
LATE_BIDS = timedelta(hours=1)
#: How long an in-person auction is assumed to run after it starts.
IN_PERSON_LENGTH = timedelta(hours=6)


def _percentile(values: list[int], percent: int) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * percent / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def hourly_views(now=None) -> list[int]:
    """Page views in each of the last ``HISTORY_HOURS + 1`` hours; index 0 is the hour ending now."""
    from auctions.models import PageView

    now = now or timezone.now()
    edges = [now - timedelta(hours=hours) for hours in range(HISTORY_HOURS + 2)]
    counts = PageView.objects.filter(date_start__gte=edges[-1], date_start__lt=now).aggregate(
        **{
            f"h{hours}": Count("pk", filter=Q(date_start__gte=edges[hours + 1], date_start__lt=edges[hours]))
            for hours in range(HISTORY_HOURS + 1)
        }
    )
    return [counts[f"h{hours}"] for hours in range(HISTORY_HOURS + 1)]


def quietest_hour(history: list[int], now=None) -> int | None:
    """The local hour of day (0-23) whose hours averaged fewest views over the history, or None if empty."""
    now = now or timezone.now()
    totals: dict[int, list[int]] = {}
    for hours_ago, views in enumerate(history, start=1):
        hour = timezone.localtime(now - timedelta(hours=hours_ago)).hour
        totals.setdefault(hour, []).append(views)
    if not totals or not any(sum(views) for views in totals.values()):
        return None
    return min(totals, key=lambda hour: (sum(totals[hour]) / len(totals[hour]), hour))


def auctions_in_play(now=None) -> list[dict[str, Any]]:
    """Auctions a deploy would interrupt, soonest first: their title, slug and why."""
    from auctions.models import Auction, Lot

    now = now or timezone.now()
    has_lots = Exists(Lot.objects.filter(auction=OuterRef("pk"), is_deleted=False))
    online = Q(is_online=True, date_end__gte=now - LATE_BIDS, date_end__lte=now + ENDING_SOON)
    in_person = Q(is_online=False, date_start__gte=now - IN_PERSON_LENGTH, date_start__lte=now + ENDING_SOON)
    found = []
    for auction in Auction.objects.filter(online | in_person, is_deleted=False).filter(has_lots):
        if auction.is_online:
            minutes = round((auction.date_end - now).total_seconds() / 60)
            why = f"online, ends in {minutes} minutes" if minutes >= 0 else f"online, ended {-minutes} minutes ago"
            when = auction.date_end
        else:
            minutes = round((auction.date_start - now).total_seconds() / 60)
            why = (
                f"in person, starts in {minutes} minutes"
                if minutes >= 0
                else f"in person, started {-minutes} minutes ago"
            )
            when = auction.date_start
        found.append({"title": auction.title, "slug": auction.slug, "why": why, "_when": abs(when - now)})
    found.sort(key=lambda item: item.pop("_when"))
    return found


def deploy_window(now=None) -> dict[str, Any]:
    """The facts and a verdict: ``quiet`` or ``busy``, with the reasons it is busy."""
    now = now or timezone.now()
    views = hourly_views(now)
    current, history = views[0], views[1:]
    low = _percentile(history, LOW_PERCENTILE)
    ceiling = low * LOW_FACTOR + LOW_SLACK
    in_play = auctions_in_play(now)
    reasons = []
    if current > ceiling:
        reasons.append(f"{current} page views in the last hour; the week's quiet hours see about {round(low)}")
    if in_play:
        reasons.append(f"{len(in_play)} auction{'s' if len(in_play) != 1 else ''} in play")
    quiet_at = quietest_hour(history, now)
    return {
        "verdict": "busy" if reasons else "quiet",
        "reasons": reasons,
        "views_last_hour": current,
        "typical_low_per_hour": round(low),
        "busiest_hour_last_week": max(history, default=0),
        "usually_quietest_at": f"{quiet_at:02d}:00 {timezone.localtime(now).tzname()}"
        if quiet_at is not None
        else None,
        "auctions_in_play": in_play,
    }


def summary(window: dict[str, Any]) -> str:
    """One sentence for a person deciding whether to deploy."""
    if window["verdict"] == "quiet":
        return "Traffic is near its lows; a fine time to deploy."
    text = "Probably not a good time to deploy: " + "; ".join(window["reasons"]) + "."
    if window["usually_quietest_at"]:
        text += f" It's usually quietest around {window['usually_quietest_at']}."
    return text
