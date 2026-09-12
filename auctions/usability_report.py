"""The usability measurements, in one place a dashboard can read.

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

:func:`buyer_funnel` is the fourth thing here and the only one about buyers rather than organizers.
It needs no model of its own: every stage of "arrived, looked at a lot, joined, bid, won, opened the
invoice, paid" already has a row, and the beacon now records the arrivals of people who never signed
in at all.

The reach numbers used to carry two caveats, both of them about the beacon rather than about this
module: it was called by 38 templates out of 247, so a page that never opted in read as *absent*
rather than *unvisited*, and it fired behind a two-second timer, so anything abandoned faster
recorded nothing.  Both biases ran toward pages people did **not** struggle with, which is the
opposite of what a usability pass wants.  ``base_page_view.html`` now records one view on every page
that extends ``base.html``, with no timer, so neither is true and neither is shown.
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
    """``{form_name: median seconds spent before leaving}``.

    Median rather than the ``Avg`` this used to be. The distribution has a tail made entirely of
    tabs somebody left open over lunch, and one of those moves a mean by minutes -- so a form
    people bailed out of in fifteen seconds reads as one they wrestled with for ten minutes, which
    is the opposite diagnosis.
    """
    from auctions.models import FormFailure

    seconds: dict[str, list[int]] = {}
    rows = FormFailure.objects.filter(
        timestamp__gte=since, kind="abandoned", seconds_on_page__isnull=False
    ).values_list("form_name", "seconds_on_page")
    for form_name, value in rows.iterator(chunk_size=2000):
        seconds.setdefault(form_name, []).append(value)
    return {form_name: round(statistics.median(values)) for form_name, values in seconds.items() if values}


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


# One funnel per auction, over that auction's whole life. The window picks which auctions are worth
# looking at; it deliberately does not cut the stages, because people arrive weeks before they pay
# and a funnel sliced by date reports that as a drop-off.
FUNNEL_AUCTIONS = 8
# Referrers to keep per auction. The tail of this is one visit each from a hundred link shorteners.
FUNNEL_REFERRERS = 4
# How far before an auction opens its page views can start. Lots are listed and links are shared
# ahead of the start date, so the floor is the earliest auction on the page minus this. It exists to
# bound the scan, not to describe behaviour -- see the comment on the arrived query.
FUNNEL_LOOKBACK_DAYS = 90


def _actor():
    """One person, whether or not they have an account.

    ``PageView`` stores a signed-in view as ``user=<id>, session_id=NULL`` and an anonymous one as
    ``user=NULL, session_id=<key>`` (``views/ajax.py``), so neither column alone counts people. The
    ``u`` prefix keeps a user id from colliding with a session key that happens to be digits.

    Somebody who browsed anonymously and then signed in is two actors here. That is the honest
    answer for a funnel: the site cannot tell that those two were the same person either.

    ``Case`` rather than ``Coalesce(Concat(...), session_id)``: Django's ``Concat`` folds a NULL
    argument to an empty string, so every anonymous row came out as the same ``"u"`` and a whole
    auction's anonymous visitors counted as one person.
    """
    return Case(
        When(user_id__isnull=False, then=Concat(Value("u"), Cast("user_id", CharField()))),
        default="session_id",
        output_field=CharField(),
    )


def _by_auction(rows, key):
    return {row[key]: row["people"] for row in rows}


def buyer_funnel(days=180, limit=FUNNEL_AUCTIONS):
    """``[{auction, stages: [{stage, people}], referrers: [...]}]`` -- where buyers stop.

    Seven queries for every auction on the page rather than seven per auction: each stage is one
    ``GROUP BY`` over the whole set.

    **Bid** is ``None`` rather than zero for an in-person auction, which is about 95% of them: the
    bidding happens in a room, and the first row it leaves is the winner on the lot. A zero there
    would read as nobody bidding.

    **Opened an invoice** is a flag the invoice page sets, so it is low by construction for an
    auction whose invoices were printed and handed over at the door. It is the one stage on this
    list that can be smaller than the one after it.

    ``joined`` counts ``AuctionTOS`` rows, which includes the ones an organizer typed in at the
    door. That is a real join -- somebody turned up -- but it is not a self-service one, which is
    why the arrival stages above it can legitimately be smaller than it.

    The two ``PageView`` queries carry a date floor and it is not cosmetic. They match an auction as
    ``pageview.auction_id OR lot.auction_id`` -- an OR across a join, which MariaDB cannot serve
    from one index -- so unbounded, each is a full scan of the largest and least-purged table on the
    site. A full scan of ``PageView`` is the exact shape behind a past production incident, and this
    page is one an admin opens casually. ``date_start`` is indexed, and no view of an auction can
    predate the auction by more than :data:`FUNNEL_LOOKBACK_DAYS`. The OR itself is only there for
    rows written before 2026-09-09, which ``tasks.backfill_page_view_auctions`` is working through
    -- see ``Auction.page_views``.
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
    """``{auction_pk: [{referrer, views}]}`` -- how the people who arrived got there.

    ``referrer`` is stored already cleaned (``views/ajax.py:clean_referrer`` folds every Facebook
    and Google host onto one name), so this is a plain ``GROUP BY``. Our own domain is excluded:
    a link from one page of this site to another is navigation, not arrival.

    ``since`` has no default on purpose: this is the same OR-across-a-join as the arrival query, and
    the only thing standing between it and a full scan of ``PageView`` is that bound. A caller that
    does not know its floor has not thought about the size of this table.
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
