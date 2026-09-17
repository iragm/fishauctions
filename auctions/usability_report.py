"""The usability measurements for the dashboard.

* Reach: ``PageView`` grouped by route, via :func:`route_name` and Django's resolver.
* Failure: ``FormFailure`` (:mod:`auctions.friction_models`), grouped by form.
* Adoption: :mod:`auctions.field_adoption`.
* :func:`buyer_funnel`: where buyers stop, from existing rows.

``base_page_view.html`` records a view on every page extending ``base.html``, with no timer.
"""

from __future__ import annotations

import functools
import logging
import statistics
from datetime import timedelta

from django.db.models import Case, CharField, Count, Q, Value, When
from django.db.models.functions import Cast, Coalesce, Concat
from django.urls import Resolver404, resolve
from django.utils import timezone

logger = logging.getLogger(__name__)

# Paths no URLconf claims; many of these means a beacon bug.
UNROUTED = "(no matching url)"
# Distinct paths to classify per report.
MAX_PATHS = 5000


@functools.lru_cache(maxsize=4096)
def route_name(path: str) -> str:
    """The URL pattern name serving ``path``, or :data:`UNROUTED`. Uses Django's resolver so it can't
    drift from ``urls.py``; cached.
    """
    if not path or not path.startswith("/"):
        return UNROUTED
    try:
        match = resolve(path)
    except (Resolver404, Exception):
        return UNROUTED
    return match.url_name or UNROUTED


def reach_by_route(days=30, limit=60):
    """``[{route, views, pages}]``, biggest first. ``pages`` counts distinct paths per route."""
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

    Ordered by abandoned plus unresolved, not volume. Abandonments are counted separately because most
    fields are optional, so walking away is more common than rejection.
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
        )
        .order_by("-abandoned", "-unresolved", "-bounces")[:limit]
    )
    rows = list(rows)
    worst = worst_fields(since)
    abandoned_fields = worst_fields(since, kind="abandoned")
    durations = abandoned_durations(since)
    for row in rows:
        row["people"] = max(row["people"], row["sessions"])
        row["fields"] = worst.get(row["form_name"], [])
        row["abandoned_fields"] = abandoned_fields.get(row["form_name"], [])
        row["completion"] = (
            round(100 * (row["bounces"] - row["unresolved"]) / row["bounces"], 1) if row["bounces"] else None
        )
        row["seconds_before_leaving"] = durations.get(row["form_name"])
    return rows


def abandoned_durations(since):
    """``{form_name: median seconds before leaving}``. Median, since tabs left open skew a mean."""
    from auctions.models import FormFailure

    seconds: dict[str, list[int]] = {}
    rows = FormFailure.objects.filter(
        timestamp__gte=since, kind="abandoned", seconds_on_page__isnull=False
    ).values_list("form_name", "seconds_on_page")
    for form_name, value in rows.iterator(chunk_size=2000):
        seconds.setdefault(form_name, []).append(value)
    return {form_name: round(statistics.median(values)) for form_name, values in seconds.items() if values}


def worst_fields(since, limit=4, kind="rejected"):
    """``{form_name: [{field, code, count}, ...]}``: which fields fail, and why.

    For ``kind="abandoned"`` the code is ``edited``: fields changed and not saved. Counted in Python
    because ``field_errors`` is JSON.
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


# Auctions shown. Stages aren't date-cut, since people arrive weeks before paying.
FUNNEL_AUCTIONS = 8
# Referrers kept per auction.
FUNNEL_REFERRERS = 4
# How far before an auction's start its page views are scanned; bounds the query.
FUNNEL_LOOKBACK_DAYS = 90


def _actor():
    """One person, with or without an account: ``u<user_id>`` or the session key.

    Browsing anonymously then signing in counts as two. ``Case`` because ``Concat`` turns NULL into "".
    """
    return Case(
        When(user_id__isnull=False, then=Concat(Value("u"), Cast("user_id", CharField()))),
        default="session_id",
        output_field=CharField(),
    )


def _by_auction(rows, key):
    return {row[key]: row["people"] for row in rows}


def buyer_funnel(days=180, limit=FUNNEL_AUCTIONS):
    """``[{auction, stages: [{stage, people}], referrers: [...]}]``: where buyers stop.

    One GROUP BY per stage across all shown auctions. Bid is ``None`` for in-person auctions. Opened an
    invoice can be lower than paid. Joined includes organizer-added rows.

    The PageView queries need the date floor: the ``auction_id OR lot.auction_id`` match can't use one
    index, so unbounded it full-scans PageView. The OR is only for rows before 2026-09-09 (see
    ``tasks.backfill_page_view_auctions``).
    """
    from auctions.models import Auction, AuctionTOS, Bid, Invoice, Lot, PageView

    since = timezone.now() - timedelta(days=days)
    auctions = list(Auction.objects.exclude(is_deleted=True).filter(date_end__gte=since).order_by("-date_end")[:limit])
    if not auctions:
        return []
    ids = [auction.pk for auction in auctions]
    actor = _actor()
    auction_key = Coalesce("auction_id", "lot_number__auction_id")
    starts = [
        auction.date_start or auction.date_end for auction in auctions if (auction.date_start or auction.date_end)
    ]
    floor = (min(starts) if starts else since) - timedelta(days=FUNNEL_LOOKBACK_DAYS)

    arrived = _by_auction(
        PageView.objects.filter(date_start__gte=floor)
        .filter(Q(auction__in=ids) | Q(lot_number__auction__in=ids))
        .annotate(auction_key=auction_key)
        .values("auction_key")
        .annotate(people=Count(actor, distinct=True)),
        "auction_key",
    )
    lot_pages = _by_auction(
        PageView.objects.filter(date_start__gte=floor, lot_number__auction__in=ids)
        .values("lot_number__auction")
        .annotate(people=Count(actor, distinct=True)),
        "lot_number__auction",
    )
    joined = _by_auction(
        AuctionTOS.objects.filter(auction__in=ids).values("auction").annotate(people=Count("pk")),
        "auction",
    )
    bid = _by_auction(
        Bid.objects.filter(lot_number__auction__in=ids, is_deleted=False)
        .values("lot_number__auction")
        .annotate(people=Count("user", distinct=True)),
        "lot_number__auction",
    )
    won = _by_auction(
        Lot.objects.filter(auction__in=ids, is_deleted=False, auctiontos_winner__isnull=False)
        .values("auction")
        .annotate(people=Count("auctiontos_winner", distinct=True)),
        "auction",
    )
    opened = _by_auction(
        Invoice.objects.filter(auctiontos_user__auction__in=ids, opened=True)
        .values("auctiontos_user__auction")
        .annotate(people=Count("pk")),
        "auctiontos_user__auction",
    )
    paid = _by_auction(
        Invoice.objects.filter(auctiontos_user__auction__in=ids, status="PAID")
        .values("auctiontos_user__auction")
        .annotate(people=Count("pk")),
        "auctiontos_user__auction",
    )
    referrers = funnel_referrers(ids, floor)

    report = []
    for auction in auctions:
        pk = auction.pk
        stages = [
            {"stage": "Arrived", "people": arrived.get(pk, 0)},
            {"stage": "Opened a lot", "people": lot_pages.get(pk, 0)},
            {"stage": "Joined", "people": joined.get(pk, 0)},
            {"stage": "Bid", "people": bid.get(pk, 0) if auction.is_online else None},
            {"stage": "Won something", "people": won.get(pk, 0)},
            {"stage": "Opened an invoice", "people": opened.get(pk, 0)},
            {"stage": "Paid", "people": paid.get(pk, 0)},
        ]
        report.append({"auction": auction, "stages": stages, "referrers": referrers.get(pk, [])})
    return report


def funnel_referrers(auction_ids, since, limit=FUNNEL_REFERRERS):
    """``{auction_pk: [{referrer, views}]}``, excluding our own domain. ``since`` is required: the same
    OR query would otherwise full-scan PageView.
    """
    from django.contrib.sites.models import Site

    from auctions.models import PageView

    rows = (
        PageView.objects.filter(date_start__gte=since)
        .filter(Q(auction__in=auction_ids) | Q(lot_number__auction__in=auction_ids))
        .exclude(referrer__isnull=True)
        .exclude(referrer__exact="")
        .exclude(referrer__startswith=Site.objects.get_current().domain)
        .annotate(auction_key=Coalesce("auction_id", "lot_number__auction_id"))
        .values("auction_key", "referrer")
        .annotate(views=Count("pk"))
        .order_by("-views")[:MAX_PATHS]
    )
    found: dict[int, list] = {}
    for row in rows:
        bucket = found.setdefault(row["auction_key"], [])
        if len(bucket) < limit:
            bucket.append({"referrer": row["referrer"], "views": row["views"]})
    return found
