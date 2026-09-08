"""The three usability measurements, in one place a dashboard can read.

USABILITY.md sets out three questions and says which source answers each.  This module is that
mapping in code:

**Reach** -- did anybody open this page?  ``PageView``, grouped by *route* rather than by URL.  The
raw column holds ``/auctions/springfield-2026/edit/``; a hundred auctions make a hundred rows of one
view each, and the question "does anybody open the auction settings page" cannot be asked of it at
all.  :func:`route_name` folds a path back onto the URL pattern that served it, using Django's own
resolver -- so the classifier cannot drift from ``urls.py``, which is the failure mode a
hand-written one has.

**Failure** -- did they submit it and get bounced?  ``FormFailure``
(:mod:`auctions.friction_models`), grouped by form.

**Adoption** -- did anybody change this setting, ever?  :mod:`auctions.field_adoption`.

Two caveats belong on the reach numbers wherever they are shown, and
:data:`REACH_CAVEATS` carries them so the dashboard cannot quietly drop them:
``pageView()`` is called by 38 of 247 templates, so a page that never opted in is *absent* rather
than *unvisited*; and it fires behind a two-second timer, so a page abandoned faster than that
records nothing.  Both biases run the same way -- toward pages people did **not** struggle with --
which is the opposite of what a usability pass wants, and is why the failure column exists.
"""

from __future__ import annotations

import functools
import logging
from datetime import timedelta

from django.db.models import Avg, Count, Q
from django.urls import Resolver404, resolve
from django.utils import timezone

logger = logging.getLogger(__name__)

REACH_CAVEATS = (
    "pageView() is called by 38 of 247 templates -- a page that never opted in reads as absent, not as unvisited.",
    "The beacon fires two seconds after load, so anything abandoned faster records nothing. Both "
    "biases run toward pages people did not struggle with.",
)

# A path nobody's URLconf claims. Kept as one bucket rather than dropped: a lot of these means the
# beacon is posting something the resolver does not recognise, which is a bug in the beacon.
UNROUTED = "(no matching url)"
# Distinct paths to classify in one report. Every lot page is its own path, so the tail is
# unbounded; the head is what a reach question is about.
MAX_PATHS = 5000


@functools.lru_cache(maxsize=4096)
def route_name(path: str) -> str:
    """The URL pattern name that serves ``path``, or :data:`UNROUTED`.

    Django's resolver rather than a pattern list of our own: the point of this is that a route
    renamed in ``urls.py`` cannot leave a stale classifier behind, and the only way to have that
    property is to ask the URLconf.

    Cached because a report classifies thousands of paths that fall into a few dozen routes, and
    ``resolve()`` walks the URLconf every time.
    """
    if not path or not path.startswith("/"):
        return UNROUTED
    try:
        match = resolve(path)
    except (Resolver404, Exception):
        return UNROUTED
    return match.url_name or UNROUTED


def reach_by_route(days=30, limit=60):
    """``[{route, views, pages}]`` -- how much traffic each URL pattern saw, biggest first.

    ``pages`` is how many distinct paths folded into that route: one for a singleton page like
    ``/account/``, and one per auction for the settings page, which is the number that says whether
    a route is reached by many organizers or by one enthusiastic one.
    """
    from auctions.models import PageView

    since = timezone.now() - timedelta(days=days)
    rows = (
        PageView.objects.filter(date_start__gte=since)
        .exclude(url="")
        .values("url")
        .annotate(views=Count("pk"))
        .order_by("-views")[:MAX_PATHS]
    )
    totals: dict[str, dict] = {}
    for row in rows:
        route = route_name(row["url"])
        bucket = totals.setdefault(route, {"route": route, "views": 0, "pages": 0})
        bucket["views"] += row["views"]
        bucket["pages"] += 1
    ordered = sorted(totals.values(), key=lambda bucket: -bucket["views"])
    return ordered[:limit]


def friction_by_form(days=30, limit=40):
    """``[{form_name, bounces, abandoned, people, unresolved, fields, ...}]``, worst first.

    Ordered by **abandoned plus unresolved** rather than by volume: a form that bounces a thousand
    times and is finished a thousand times is a strict validator and patient users, and a form
    fifty people edited and walked away from is what this campaign is looking for.

    Abandonments are counted separately from rejections because on this site they are the bigger
    number by construction -- nearly every field is optional and most of the rest are filled in on
    save, so the server refusing a submission is the unusual case. A form with rejections and no
    abandonments is one people can see how to fill in and keep getting wrong; one with abandonments
    and no rejections is one they cannot see how to fill in at all, and no validator will ever say
    so.
    """
    from auctions.models import FormFailure

    since = timezone.now() - timedelta(days=days)
    rows = (
        FormFailure.objects.filter(timestamp__gte=since)
        .values("form_name")
        .annotate(
            bounces=Count("pk", filter=Q(kind="rejected")),
            abandoned=Count("pk", filter=Q(kind="abandoned")),
            unresolved=Count("pk", filter=Q(kind="rejected", resolved=False)),
            people=Count("user", distinct=True),
            sessions=Count("session_id", distinct=True),
            gave_up_on_the_first_try=Count("pk", filter=Q(kind="rejected", resolved=False, attempt=1)),
            median_seconds=Avg("seconds_on_page", filter=Q(kind="abandoned")),
        )
        .order_by("-abandoned", "-unresolved", "-bounces")[:limit]
    )
    rows = list(rows)
    worst = worst_fields(since)
    abandoned_fields = worst_fields(since, kind="abandoned")
    for row in rows:
        row["people"] = max(row["people"], row["sessions"])
        row["fields"] = worst.get(row["form_name"], [])
        row["abandoned_fields"] = abandoned_fields.get(row["form_name"], [])
        row["completion"] = (
            round(100 * (row["bounces"] - row["unresolved"]) / row["bounces"], 1) if row["bounces"] else None
        )
        row["seconds_before_leaving"] = round(row["median_seconds"]) if row["median_seconds"] else None
    return rows


def worst_fields(since, limit=4, kind="rejected"):
    """``{form_name: [{field, code, count}, ...]}`` -- which field, and why, per form.

    For ``kind="abandoned"`` the "why" is always ``edited``: those rows carry the fields somebody
    changed and did not save, which is the closest thing there is to "the field they gave up on".
    It is not proof -- the field they could not work out may be one they never touched -- but a
    field that is edited and unsaved far more often than the others on the same form is the first
    place to look.

    One pass over the window in Python. ``field_errors`` is JSON and the counting is per key inside
    it, which is not a GROUP BY any database here can do without an expression index.
    """
    from auctions.models import FormFailure

    counts: dict[str, dict[tuple[str, str], int]] = {}
    rows = FormFailure.objects.filter(timestamp__gte=since, kind=kind).values_list("form_name", "field_errors")
    for form_name, field_errors in rows.iterator(chunk_size=2000):
        if not isinstance(field_errors, dict):
            continue
        bucket = counts.setdefault(form_name, {})
        for field, codes in field_errors.items():
            for code in codes or ["invalid"]:
                key = (field, code)
                bucket[key] = bucket.get(key, 0) + 1
    return {
        form_name: [
            {"field": field, "code": code, "count": count}
            for (field, code), count in sorted(bucket.items(), key=lambda item: -item[1])[:limit]
        ]
        for form_name, bucket in counts.items()
    }
