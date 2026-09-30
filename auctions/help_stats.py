"""The numbers the help guides quote: facts from every auction on the site, and from the reader's own.

``site_stats`` is counted once a day, from real sales, so "a lot with a photo sells more often" comes with
this site's own percentage. ``in_person_photos`` is what a photo is worth at the big in-person auctions,
also daily. Both are counted by the ``refresh_help_stats`` task (:func:`refresh`), never by a page: the
guides are public and crawled, and these are scans of the lots table. A page that finds nothing counted
asks for a count and reads the words meanwhile. ``auction_facts`` reads an auction's ``cached_stats`` (see
``Auction.recalculate_stats``), so a guide never waits on a count. All return ``{}``, or leave a key out,
when there isn't enough to say, and the guides fall back to words.
"""

from __future__ import annotations

import datetime
import logging

from django.core.cache import cache
from django.db import transaction
from django.db.models import Avg, BooleanField, Case, Count, Exists, F, OuterRef, Q, Sum, Value, When
from django.utils import timezone

logger = logging.getLogger(__name__)

SITE_CACHE_KEY = "help_site_stats_v1"
#: Counted daily, kept for three: a missed run leaves the last count up rather than the words alone.
SITE_CACHE_SECONDS = 60 * 60 * 24 * 3
#: Held while a count asked for by a page is queued, so a crawl asks once.
REFRESH_QUEUED_KEY = "help_stats_refresh_queued"
REFRESH_QUEUED_SECONDS = 60 * 15
#: The fewest lots a site-wide percentage is quoted from.
MIN_LOTS = 200
#: The fewest lots, and people, an auction's own percentages are quoted from.
MIN_AUCTION_LOTS = 20
MIN_AUCTION_PEOPLE = 10
#: How far back site-wide stats look.
SITE_YEARS = 3

PHOTO_CACHE_KEY = "help_in_person_photos_v1"
#: The in-person auctions whose photo prices are quoted: promoted ones that sold at least this much.
PHOTO_MIN_GROSS = 5000
#: The fewest sold lots a photo price is quoted from.
PHOTO_MIN_LOTS = 30


def _pct(part, whole):
    return round(100 * part / whole) if whole else None


def site_stats() -> dict:
    """Site-wide facts, as last counted by :func:`refresh`."""
    return _counted(SITE_CACHE_KEY)


def _counted(key) -> dict:
    """What :func:`refresh` last stored under ``key``, or ``{}`` (asking for a count) when there's nothing."""
    try:
        value = cache.get(key)
    except Exception as e:
        logger.warning("help stats unreadable: %r", e)
        return {}
    if value is None:
        request_refresh()
        return {}
    return value


def request_refresh():
    """Queue :func:`refresh` unless one is queued already."""
    try:
        if not cache.add(REFRESH_QUEUED_KEY, True, REFRESH_QUEUED_SECONDS):
            return
    except Exception:
        return
    from auctions.tasks import refresh_help_stats

    transaction.on_commit(refresh_help_stats.delay)


def _run(label, step):
    step()


def refresh(run=_run):
    """Count everything the guides quote: :func:`site_stats`, :func:`in_person_photos` and the rules guide's
    usage badges (``field_adoption``). ``run(label, step)`` runs each count, so one failing can leave the others.
    """
    from auctions import field_adoption

    run("site stats", lambda: cache.set(SITE_CACHE_KEY, _site_stats(), SITE_CACHE_SECONDS))
    run("in-person photos", lambda: cache.set(PHOTO_CACHE_KEY, _in_person_photos(), SITE_CACHE_SECONDS))
    run(
        "field adoption",
        lambda: cache.set(
            field_adoption.CACHE_KEY, field_adoption.auction_field_adoption(use_cache=False), SITE_CACHE_SECONDS
        ),
    )
    cache.delete(REFRESH_QUEUED_KEY)


def _site_stats(min_lots: int = MIN_LOTS) -> dict:
    from auctions.models import Lot, LotImage

    now = timezone.now()
    lots = Lot.objects.filter(
        auction__isnull=False,
        auction__is_deleted=False,
        is_deleted=False,
        banned=False,
        auction__date_start__gte=now - datetime.timedelta(days=365 * SITE_YEARS),
        auction__date_start__lt=now - datetime.timedelta(days=14),
    )
    facts: dict = {}

    rows = (
        lots.annotate(has_photo=Exists(LotImage.objects.filter(lot_number=OuterRef("pk"))))
        .values("auction__is_online", "has_photo")
        .annotate(n=Count("pk"), sold=Count("winning_price"), average=Avg("winning_price"))
        .order_by()
    )
    by = {(row["auction__is_online"], row["has_photo"]): row for row in rows}

    def combined(online=None, photo=None):
        """Lots, lots sold and the average price, over the rows matching whichever of the two is given."""
        picked = [
            row
            for (is_online, has_photo), row in by.items()
            if online in (None, is_online) and photo in (None, has_photo)
        ]
        n = sum(row["n"] for row in picked)
        sold = sum(row["sold"] for row in picked)
        total = sum((row["average"] or 0) * row["sold"] for row in picked)
        return n, sold, (total / sold if sold else None)

    n_all, sold_all, _ = combined()
    if n_all >= min_lots:
        facts["lots"] = n_all
    photo_n, photo_sold, photo_avg = combined(photo=True)
    bare_n, bare_sold, bare_avg = combined(photo=False)
    if photo_n >= min_lots and bare_n >= min_lots:
        facts["photo_sell_rate"] = _pct(photo_sold, photo_n)
        facts["no_photo_sell_rate"] = _pct(bare_sold, bare_n)
        if photo_avg and bare_avg and photo_avg > bare_avg:
            facts["photo_price_premium"] = round(100 * (photo_avg / bare_avg - 1))
    for online, key in ((True, "online"), (False, "in_person")):
        n, sold, _ = combined(online=online)
        if n >= min_lots:
            facts[f"{key}_unsold"] = _pct(n - sold, n)

    online_sold = lots.filter(auction__is_online=True, winning_price__isnull=False)
    sold_count = online_sold.count()
    if sold_count >= min_lots:
        late = online_sold.filter(date_end__gt=F("auction__date_end")).count()
        facts["online_extended"] = _pct(late, sold_count)

    # In person: lots the seller typed in themselves, against lots typed in at the desk.
    self_added = (
        lots.filter(auction__is_online=False, winning_price__isnull=False)
        .annotate(
            by_seller=Case(
                When(Q(added_by__isnull=False) & Q(added_by=F("user")), then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            )
        )
        .values("by_seller")
        .annotate(n=Count("pk"), average=Avg("winning_price"))
        .order_by()
    )
    split = {row["by_seller"]: row for row in self_added}
    seller, desk = split.get(True), split.get(False)
    if seller and desk and seller["n"] >= min_lots and desk["n"] >= min_lots and desk["average"]:
        difference = round(100 * (seller["average"] / desk["average"] - 1))
        if difference > 0:
            facts["self_added_premium"] = difference

    # In person: sales recorded once and never corrected (``Lot.sales_recorded``).
    recorded = lots.filter(auction__is_online=False, winning_price__isnull=False, sales_recorded__gte=1).aggregate(
        n=Count("pk"), once=Count("pk", filter=Q(sales_recorded=1))
    )
    if recorded["n"] >= min_lots:
        facts["recorded_once"] = _pct(recorded["once"], recorded["n"])
    return facts


def in_person_photos() -> dict:
    """What a photo is worth at a big in-person auction, as last counted by :func:`refresh`."""
    return _counted(PHOTO_CACHE_KEY)


def _in_person_photos(min_gross: int = PHOTO_MIN_GROSS, min_lots: int = PHOTO_MIN_LOTS) -> dict:
    """Median prices of lots sold with no photo, one, and more, at promoted in-person auctions that sold
    ``min_gross`` or more. Dollars only, so no mixing of currencies.
    """
    from auctions.models import Auction, Lot, median_value

    sold = Lot.objects.filter(
        is_deleted=False,
        banned=False,
        winning_price__isnull=False,
        auction__is_online=False,
        auction__is_deleted=False,
        auction__promote_this_auction=True,
        auction__date_start__lt=timezone.now(),
    )
    big = (
        sold.values("auction")
        .annotate(total=Sum("winning_price"))
        .filter(total__gte=min_gross)
        .order_by()
        .values_list("auction", flat=True)
    )
    # An auction's currency is its creator's, worked out in Python; there are only a handful of these.
    auctions = [
        auction.pk
        for auction in Auction.objects.filter(pk__in=list(big)).select_related("created_by__userdata")
        if auction.currency == "USD"
    ]
    if not auctions:
        return {}
    lots = sold.filter(auction__in=auctions).annotate(photos=Count("lotimage"))
    medians = {}
    rows = []
    for label, subset in (
        ("No photo", lots.filter(photos=0)),
        ("One photo", lots.filter(photos=1)),
        ("More than one", lots.filter(photos__gt=1)),
        ("Any photo", lots.filter(photos__gt=0)),
    ):
        count = subset.count()
        if count < min_lots:
            continue
        medians[label] = median_value(subset, "winning_price")
        if label != "Any photo":
            rows.append({"label": label, "lots": count, "median": round(medians[label])})
    facts: dict = {"auctions": len(auctions), "min_gross": min_gross, "rows": rows}
    bare, photo = medians.get("No photo"), medians.get("Any photo")
    if bare and photo and photo > bare:
        facts["premium"] = round(100 * (photo / bare - 1))
    return facts if len(rows) >= 2 else {}


# --- one auction ---------------------------------------------------------------


def _series(stats, key):
    chart = (stats or {}).get(key) or {}
    return chart.get("labels") or [], chart.get("data") or []


def auction_facts(auction) -> dict:
    """What an auction's cached stats say, in the terms the guides use. Empty without cached stats."""
    stats = getattr(auction, "cached_stats", None)
    if not stats:
        return {}
    facts: dict = {"currency": auction.currency_symbol}
    try:
        misc = stats.get("misc") or {}
        club = misc.get("club_stats") or {}
        if club.get("gross"):
            facts["gross"] = round(club["gross"])
        if club.get("total_lots"):
            facts["lots"] = club["total_lots"]
        if club.get("checked_in"):
            facts["people"] = club["checked_in"]
        if misc.get("number_of_lots_with_scanned_qr"):
            facts["qr_scans"] = misc["number_of_lots_with_scanned_qr"]
        accuracy = misc.get("bid_recorder_accuracy") or {}
        if accuracy.get("lots", 0) >= MIN_AUCTION_LOTS:
            facts["recorded_once"] = accuracy["percent"]
    except Exception:
        logger.exception("help facts: misc")
    try:
        _labels, data = _series(stats, "lot_sell_prices")
        counts = data[0] if data else []
        if counts and sum(counts) >= MIN_AUCTION_LOTS:
            facts["unsold_pct"] = _pct(counts[0], sum(counts))
    except Exception:
        logger.exception("help facts: sell prices")
    try:
        labels, data = _series(stats, "previous_auctions")
        counts = data[0] if data else []
        if counts and sum(counts) >= MIN_AUCTION_PEOPLE:
            facts["first_timers"] = counts[0]
            facts["first_timers_pct"] = _pct(counts[0], sum(counts))
    except Exception:
        logger.exception("help facts: previous auctions")
    if not auction.is_online:
        try:
            _labels, data = _series(stats, "auctioneer_speed")
            points = [point["y"] for point in (data[0] if data else []) if point.get("y")]
            if len(points) >= 20:
                minutes = sum(points) / len(points)
                facts["lots_per_hour"] = round(60 / minutes) if minutes else None
        except Exception:
            logger.exception("help facts: speed")
        try:
            _labels, data = _series(stats, "attrition")
            points = sorted((point["x"], point["y"]) for point in (data[0] if data else []))
            if len(points) >= 30:
                third = len(points) // 3
                # x counts minutes back from the end, so the smallest x sold last.
                late = [price for _x, price in points[:third]]
                early = [price for _x, price in points[-third:]]
                early_avg, late_avg = sum(early) / len(early), sum(late) / len(late)
                if early_avg and late_avg < early_avg:
                    facts["late_price_drop"] = round(100 * (1 - late_avg / early_avg))
        except Exception:
            logger.exception("help facts: attrition")
    try:
        labels, data = _series(stats, "feature_use")
        values = dict(zip(labels, data[0] if data else []))
        for label, key in (
            ("Mobile app", "app_pct"),
            ("Watch", "watch_pct"),
            ("Proxy bidding", "proxy_pct"),
            ("View invoice", "viewed_invoice_pct"),
        ):
            if values.get(label):
                facts[key] = values[label]
    except Exception:
        logger.exception("help facts: feature use")
    return facts


def stats_auction(user, auction=None, is_admin=False):
    """The auction whose numbers a guide quotes to ``user``: ``auction`` if they run it and it's over, else
    the latest auction they ran that's over. None if there isn't one with stats.
    """
    if not (user and user.is_authenticated):
        return None
    if auction is not None and is_admin and auction.cached_stats and auction.pretty_much_over:
        return auction
    from auctions.models import Auction, AuctionTOS

    now = timezone.now()
    admin_of = AuctionTOS.objects.filter(user=user, is_admin=True).values("auction")
    candidates = (
        Auction.objects.filter(
            is_deleted=False,
            cached_stats__isnull=False,
            date_start__gte=now - datetime.timedelta(days=400),
            date_start__lt=now,
        )
        .filter(Q(created_by=user) | Q(pk__in=admin_of))
        .order_by("-date_start")[:5]
    )
    for candidate in candidates:
        if candidate.pretty_much_over:
            return candidate
    return None
