"""The numbers the help guides quote: facts from every auction on the site, and from the reader's own.

``site_stats`` is a dict of one-line facts counted once a day from real sales, so "a lot with a photo sells
more often" comes with this site's own percentage. The rest are the tables and charts some guides draw:
``in_person_photos``, ``rules_chart`` (rules length against ``AuctionTOS.time_spent_reading_rules``),
``online_timing``, ``seller_rank``, ``bid_amounts`` and ``bred_species``. All are counted by the
``refresh_help_stats`` task (:func:`refresh`), never by a page: the guides are public and crawled, and these
are scans of the lots table. A page that finds nothing counted asks for a count and reads the words
meanwhile. ``auction_facts`` reads an auction's ``cached_stats`` (see ``Auction.recalculate_stats``), so a
guide never waits on a count. All return ``{}``, or leave a key out, when there isn't enough to say, and the
guides fall back to words.
"""

from __future__ import annotations

import datetime
import html
import logging
import re
import statistics
import zoneinfo

from django.core.cache import cache
from django.db import transaction
from django.db.models import (
    Avg,
    BooleanField,
    Case,
    Count,
    DateTimeField,
    Exists,
    ExpressionWrapper,
    F,
    OuterRef,
    Q,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Floor
from django.utils import timezone

logger = logging.getLogger(__name__)

SITE_CACHE_KEY = "help_site_stats_v1"
#: Counted daily, kept for three: a missed run leaves the last count up rather than the words alone.
SITE_CACHE_SECONDS = 60 * 60 * 24 * 3
#: Held while a count asked for by a page is queued, so a crawl asks once.
REFRESH_QUEUED_KEY = "help_stats_refresh_queued"
#: Also how long a page waits to ask again after a count failed.
REFRESH_QUEUED_SECONDS = 60 * 60 * 6
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

RULES_CACHE_KEY = "help_rules_chart_v1"
#: Longer than this between opening the rules and clicking Join is a tab left open, not reading.
READ_CAP_SECONDS = 10 * 60
#: The fewest timed joins an auction's average is plotted from.
MIN_READS = 10
#: The fewest auctions the chart is drawn from, after outliers go.
MIN_RULES_AUCTIONS = 10


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
    """Count everything the guides quote: each site-wide count here, and the rules guide's usage badges
    (``field_adoption``). ``run(label, step)`` runs each count, so one failing can leave the others.
    """
    from auctions import field_adoption

    failed = []

    def run_one(label, step):
        def counted():
            try:
                step()
            except BaseException:
                failed.append(label)
                raise

        run(label, counted)

    try:
        run_one("site stats", lambda: cache.set(SITE_CACHE_KEY, _site_stats(), SITE_CACHE_SECONDS))
        run_one("in-person photos", lambda: cache.set(PHOTO_CACHE_KEY, _in_person_photos(), SITE_CACHE_SECONDS))
        for label, key, name in (
            ("rules chart", RULES_CACHE_KEY, "_rules_chart"),
            ("online timing", TIMING_CACHE_KEY, "_online_timing"),
            ("seller rank", SELLER_RANK_CACHE_KEY, "_seller_rank"),
            ("bid amounts", BID_AMOUNTS_CACHE_KEY, "_bid_amounts"),
            ("bred species", BRED_CACHE_KEY, "_bred_species"),
        ):
            # Looked up when run, so a test can patch the count.
            run_one(label, lambda key=key, name=name: cache.set(key, globals()[name](), SITE_CACHE_SECONDS))
        run_one(
            "field adoption",
            lambda: cache.set(
                field_adoption.CACHE_KEY, field_adoption.auction_field_adoption(use_cache=False), SITE_CACHE_SECONDS
            ),
        )
    finally:
        # A failed count (an error, or the time limit) holds the guard, so pages that find it missing don't
        # rerun every count back to back. Done, a page that finds a count missing may ask again.
        if failed:
            cache.set(REFRESH_QUEUED_KEY, True, REFRESH_QUEUED_SECONDS)
        else:
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

    facts.update(_joining(min_lots))
    facts.update(_feedback(lots, min_lots))
    facts.update(_voice(min_lots))
    facts.update(_invoice_opening(min_lots))
    return facts


def _recent_auctions():
    """Auctions the site-wide counts look at: started in the last :data:`SITE_YEARS`, and over a fortnight ago."""
    from auctions.models import Auction

    now = timezone.now()
    return Auction.objects.filter(
        is_deleted=False,
        date_start__gte=now - datetime.timedelta(days=365 * SITE_YEARS),
        date_start__lt=now - datetime.timedelta(days=14),
    )


def _before(field, **delta):
    return ExpressionWrapper(F(field) - datetime.timedelta(**delta), output_field=DateTimeField())


def _joining(min_people) -> dict:
    """How late people join: in person, on the day (from 12 hours before the start); online, in the last day
    before the end. Anybody added after that day, or after the end, isn't counted at all. Club-managed
    auctions are left out: their people are made from the member list at once.
    """
    from auctions.models import AuctionTOS

    people = AuctionTOS.objects.filter(
        auction__in=_recent_auctions().exclude(manage_users_through_club=True, club__isnull=False)
    )
    facts = {}
    for online, key, late, end in (
        (
            False,
            "in_person_join_on_day",
            _before("auction__date_start", hours=12),
            _before("auction__date_start", hours=-12),
        ),
        (True, "online_join_last_day", _before("auction__date_end", days=1), F("auction__date_end")),
    ):
        counts = people.filter(auction__is_online=online, createdon__lte=end).aggregate(
            n=Count("pk"), late=Count("pk", filter=Q(createdon__gte=late))
        )
        if counts["n"] >= min_people:
            facts[key] = _pct(counts["late"], counts["n"])
    return facts


def _feedback(lots, min_lots) -> dict:
    """Buyers rating sellers (``Lot.feedback_rating``): how many of the ratings are +1, and how many buyers
    rate anything at all.
    """
    bought = lots.filter(auctiontos_winner__isnull=False, winning_price__isnull=False)
    ratings = bought.exclude(feedback_rating=0).aggregate(n=Count("pk"), good=Count("pk", filter=Q(feedback_rating=1)))
    buyers = bought.values("auctiontos_winner").distinct().order_by().count()
    raters = bought.exclude(feedback_rating=0).values("auctiontos_winner").distinct().order_by().count()
    facts = {}
    if ratings["n"] >= min_lots:
        facts["feedback_positive"] = _pct(ratings["good"], ratings["n"])
    if buyers >= min_lots:
        facts["feedback_left"] = _pct(raters, buyers)
    return facts


def _voice(min_commands) -> dict:
    """Setting winners by voice: the share of commands matched and not corrected before saving."""
    from auctions.models import VoiceCommandLog

    counts = VoiceCommandLog.objects.aggregate(
        n=Count("pk"), right=Count("pk", filter=~Q(slot="") & Q(corrected_to=""))
    )
    return {"voice_right": _pct(counts["right"], counts["n"])} if counts["n"] >= min_commands else {}


def _invoice_opening(min_invoices) -> dict:
    """Invoice emails: the share whose link is opened after it's sent, and the median hours until it is."""
    from auctions.models import Invoice

    sent = list(
        Invoice.objects.filter(
            email_sent_on__gte=timezone.now() - datetime.timedelta(days=365 * SITE_YEARS), auction__isnull=False
        ).values_list("email_sent_on", "opened_on")
    )
    if len(sent) < min_invoices:
        return {}
    hours = [(opened - emailed).total_seconds() / 3600 for emailed, opened in sent if opened and opened >= emailed]
    facts = {"invoice_opened": _pct(len(hours), len(sent))}
    if hours:
        median = statistics.median(hours)
        facts["invoice_open_hours"] = round(median) if median >= 1 else round(median, 1)
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


def rules_chart() -> dict:
    """``{"points": [[words, seconds], ...], "auctions": n}``, as last counted by :func:`refresh`."""
    return _counted(RULES_CACHE_KEY)


_TAG = re.compile(r"<[^>]*>")


def visible_words(rules_html: str) -> int:
    """Words a reader sees. Each tag becomes a space, so ``</p><p>`` doesn't join two words into one. A regex,
    not ``strip_tags``: that is an HTML parser, and this runs over every auction's rules once a day.
    """
    return len(html.unescape(_TAG.sub(" ", rules_html or "")).split())


def _inside_fences(values):
    """Tukey's fences: (low, high) past which a value is an outlier."""
    q1, _q2, q3 = statistics.quantiles(values, n=4)
    spread = 1.5 * (q3 - q1)
    return q1 - spread, q3 + spread


def _rules_chart(min_reads: int = MIN_READS, min_auctions: int = MIN_RULES_AUCTIONS) -> dict:
    """One point per auction with rules and ``min_reads`` people who joined on the site: its rules' word count
    (to the nearest 10, so no point names an auction) against their average seconds from opening the rules
    to clicking Join. Outliers on either axis are dropped.
    """
    from auctions.models import Auction, AuctionTOS

    seconds = {
        row["auction"]: row["mean"]
        for row in AuctionTOS.objects.exclude(manually_added=True)
        .filter(
            time_spent_reading_rules__gt=0,
            time_spent_reading_rules__lte=READ_CAP_SECONDS,
            auction__is_deleted=False,
        )
        .values("auction")
        .annotate(n=Count("pk"), mean=Avg("time_spent_reading_rules"))
        .filter(n__gte=min_reads)
        .order_by()
    }
    points = [
        (visible_words(text), float(seconds[pk]))
        for pk, text in Auction.objects.filter(pk__in=list(seconds)).values_list("pk", "summernote_description")
    ]
    points = [(words, mean) for words, mean in points if words]
    if len(points) < max(min_auctions, 4):
        return {}
    words_low, words_high = _inside_fences([words for words, _ in points])
    seconds_low, seconds_high = _inside_fences([mean for _, mean in points])
    kept = sorted(
        (round(words, -1) or 10, round(mean))
        for words, mean in points
        if words_low <= words <= words_high and seconds_low <= mean <= seconds_high
    )
    if len(kept) < min_auctions:
        return {}
    return {"points": [list(point) for point in kept], "auctions": len(kept)}


# --- tables and charts --------------------------------------------------------

TIMING_CACHE_KEY = "help_online_timing_v1"
SELLER_RANK_CACHE_KEY = "help_seller_rank_v1"
BID_AMOUNTS_CACHE_KEY = "help_bid_amounts_v1"
BRED_CACHE_KEY = "help_bred_species_v1"
#: The fewest auctions a row of the online timing tables is quoted from.
MIN_TIMING_AUCTIONS = 5
#: The bid chart's last dollar.
BID_CHART_DOLLARS = 60
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
#: (label, most days) for the length table, shortest first.
LENGTHS = (("3 days or less", 3), ("4 to 7 days", 7), ("8 to 14 days", 14), ("More than 2 weeks", None))
#: (label, first, last) for a seller's lots in one auction, in the order they added them.
SELLER_RANKS = (
    ("1st to 10th", 1, 10),
    ("11th to 20th", 11, 20),
    ("21st to 30th", 21, 30),
    ("After the 30th", 31, None),
)


def online_timing() -> dict:
    """``{"weekdays": rows, "lengths": rows}``, rows ``{"label", "auctions", "sold"}``, as last counted."""
    return _counted(TIMING_CACHE_KEY)


def seller_rank() -> dict:
    """``{"rows": [{"label", "lots", "sold", "median"}]}``, as last counted."""
    return _counted(SELLER_RANK_CACHE_KEY)


def bid_amounts() -> dict:
    """``{"percents": [...], "bids", "round_pct", "favourite"}``, as last counted."""
    return _counted(BID_AMOUNTS_CACHE_KEY)


def bred_species() -> dict:
    """``{"rows": [{"name", "lots"}]}``, as last counted."""
    return _counted(BRED_CACHE_KEY)


def _dollar_auctions(auctions) -> set[int]:
    """The pks of ``auctions`` priced in dollars, US or Canadian. A currency is its creator's, worked out
    once per creator.
    """
    from auctions.models import UserData

    by_creator: dict = {}
    for pk, creator in auctions.values_list("pk", "created_by"):
        by_creator.setdefault(creator, []).append(pk)
    dollars = set(by_creator.pop(None, []))
    for userdata in UserData.objects.filter(user__in=list(by_creator)):
        if userdata.currency in ("USD", "CAD"):
            dollars.update(by_creator.pop(userdata.user_id))
    return dollars


def _local(when, zone_name):
    try:
        zone = zoneinfo.ZoneInfo(zone_name) if zone_name else timezone.get_default_timezone()
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        zone = timezone.get_default_timezone()
    return when.astimezone(zone)


def _online_timing(min_lots: int = MIN_AUCTION_LOTS, min_auctions: int = MIN_TIMING_AUCTIONS) -> dict:
    """The share of lots sold at online auctions with ``min_lots`` lots or more: typical (median) auction by
    the weekday it ended, in its creator's time zone, and by how long bidding ran.
    """
    from auctions.models import Lot

    auctions = {
        row["pk"]: row
        for row in _recent_auctions()
        .filter(is_online=True, date_end__isnull=False)
        .values("pk", "date_start", "date_end", "created_by__userdata__timezone")
    }
    counts = (
        Lot.objects.filter(auction__in=list(auctions), is_deleted=False, banned=False)
        .values("auction")
        .annotate(n=Count("pk"), sold=Count("winning_price"))
        .order_by()
    )
    by_day: dict = {}
    by_length: dict = {}
    for row in counts:
        if row["n"] < min_lots:
            continue
        auction = auctions[row["auction"]]
        sold = row["sold"] / row["n"]
        day = _local(auction["date_end"], auction["created_by__userdata__timezone"]).weekday()
        by_day.setdefault(WEEKDAYS[day], []).append(sold)
        days = round((auction["date_end"] - auction["date_start"]).total_seconds() / 86400)
        label = next(label for label, most in LENGTHS if most is None or days <= most)
        by_length.setdefault(label, []).append(sold)

    def rows(groups, order):
        return [
            {"label": label, "auctions": len(groups[label]), "sold": round(100 * statistics.median(groups[label]))}
            for label in order
            if len(groups.get(label, [])) >= min_auctions
        ]

    facts = {}
    weekdays, lengths = rows(by_day, WEEKDAYS), rows(by_length, [label for label, _most in LENGTHS])
    if len(weekdays) >= 2:
        facts["weekdays"] = weekdays
    if len(lengths) >= 2:
        facts["lengths"] = lengths
    return facts


def _seller_rank(min_lots: int = MIN_LOTS, min_seller_lots: int = 20) -> dict:
    """Does a seller's 30th lot do worse than their first? Sellers with ``min_seller_lots`` or more lots in one
    auction, their lots in the order they added them: the share sold, and the median price in dollars.
    """
    from auctions.models import Lot

    lots = Lot.objects.filter(
        auction__in=_dollar_auctions(_recent_auctions()),
        is_deleted=False,
        banned=False,
        auctiontos_seller__isnull=False,
    )
    # Only the big sellers' lots come back to Python, in the order they added them.
    big = (
        lots.values("auctiontos_seller")
        .annotate(n=Count("pk"))
        .filter(n__gte=min_seller_lots)
        .order_by()
        .values_list("auctiontos_seller", flat=True)
    )
    sellers: dict = {}
    for seller, price in (
        lots.filter(auctiontos_seller__in=list(big))
        .order_by("auctiontos_seller", "pk")
        .values_list("auctiontos_seller", "winning_price")
    ):
        sellers.setdefault(seller, []).append(price)
    groups: dict = {label: [] for label, _first, _last in SELLER_RANKS}
    for prices in sellers.values():
        if len(prices) < min_seller_lots:
            continue
        for rank, price in enumerate(prices, start=1):
            label = next(
                label for label, first, last in SELLER_RANKS if rank >= first and (last is None or rank <= last)
            )
            groups[label].append(price)
    rows = []
    for label, _first, _last in SELLER_RANKS:
        prices = groups[label]
        sold = sorted(float(price) for price in prices if price is not None)
        if len(prices) < min_lots or not sold:
            continue
        rows.append(
            {
                "label": label,
                "lots": len(prices),
                "sold": _pct(len(sold), len(prices)),
                "median": statistics.median(sold),
            }
        )
    return {"rows": rows} if len(rows) >= 2 else {}


def _bid_amounts(min_bids: int = MIN_LOTS * 5, dollars: int = BID_CHART_DOLLARS) -> dict:
    """How much people bid: each person's top bid on each lot (``Bid.amount``), in whole dollars, as a share of
    all of them from $1 to ``dollars``. Online, and dollar auctions only.
    """
    from auctions.models import Bid

    auctions = _dollar_auctions(_recent_auctions().filter(is_online=True))
    per_dollar = (
        Bid.objects.filter(lot_number__auction__in=auctions, is_deleted=False, amount__gte=1, amount__lt=dollars + 1)
        .annotate(dollar=Floor("amount"))
        .values("dollar")
        .annotate(n=Count("pk"))
        .order_by()
    )
    counts = [0] * dollars
    for row in per_dollar:
        counts[int(row["dollar"]) - 1] += row["n"]
    total = sum(counts)
    if total < min_bids:
        return {}
    round_numbers = sum(count for dollar, count in enumerate(counts, start=1) if dollar % 5 == 0)
    favourite = max(range(dollars), key=lambda i: counts[i]) + 1
    return {
        "percents": [round(100 * count / total, 2) for count in counts],
        "bids": total,
        "round_pct": _pct(round_numbers, total),
        "favourite": favourite,
    }


def _bred_species(top: int = 10, min_lots: int = 3) -> dict:
    """The species most often sold here as bred by the seller (``Lot.i_bred_this_fish``), from the site-wide
    species list only: a club's own additions stay the club's.
    """
    from auctions.models import Lot

    rows = (
        Lot.objects.filter(
            auction__in=_recent_auctions(),
            is_deleted=False,
            banned=False,
            i_bred_this_fish=True,
            species__isnull=False,
            species__club__isnull=True,
        )
        .values("species__common_name", "species__scientific_name")
        .annotate(lots=Count("pk"))
        .filter(lots__gte=min_lots)
        .order_by("-lots", "species__scientific_name")[:top]
    )
    picked = [
        {"name": row["species__common_name"] or row["species__scientific_name"], "lots": row["lots"]} for row in rows
    ]
    return {"rows": picked} if len(picked) >= 3 else {}


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
