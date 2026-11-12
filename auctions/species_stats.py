"""What the site's own sales say about one species, for its public page (``/species/<pk>/``).

Every number is counted from real lots: how many, since when, how many people sold and bought it, what
it went for, and whether it is listed more or less than a year ago. Only totals leave this module --
never a seller, a buyer or a lot -- because the page is public and the lots are spread over every club.

A nominal species counts its cultivars' lots too (a Halfmoon is a *Betta splendens*); a cultivar counts
only its own. Results are cached for :data:`CACHE_SECONDS`, since a species page is cheap to ask for
and the counting is not.
"""

from __future__ import annotations

import datetime
from collections import Counter, defaultdict
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Count, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from auctions import cloudflare_images
from auctions.helper_functions import get_currency_symbol
from auctions.models import Lot, LotImage, Species, UserData

#: How long one species' numbers are reused before they are counted again.
CACHE_SECONDS = 6 * 60 * 60

#: Years of sales the price guide is worked out over: older prices say little about today's.
PRICE_GUIDE_YEARS = 3

#: Fewest sales before a price is quoted at all: one sale is an anecdote.
MIN_SALES_FOR_PRICE = 3

#: Fewest different sellers before a price is quoted: one seller's prices are that person's, not a guide.
MIN_SELLERS_FOR_PRICE = 2

#: Fewest lots, over the two years compared, before a trend is called.
MIN_LOTS_FOR_TREND = 6

#: Picture sources that are the seller's own photo, preferred over "not my photo".
OWN_PHOTO_SOURCES = ("ACTUAL", "REPRESENTATIVE")

YEAR = datetime.timedelta(days=365)


def public_lots():
    """Lots that count anywhere public: not deleted, not removed, not in a deleted auction, and not in one
    whose creator keeps its stats to themselves (``Auction.make_stats_public``).
    """
    return (
        Lot.objects.filter(is_deleted=False, banned=False)
        .exclude(auction__is_deleted=True)
        .exclude(auction__make_stats_public=False)
    )


def lots_for(species):
    """The lots this species' page counts: its own, plus its cultivars' when it is a nominal species."""
    if species.parent_id or species.is_hybrid:
        return public_lots().filter(species=species)
    # An unapproved cultivar is private to whoever added it, and so are its lots.
    return public_lots().filter(Q(species=species) | Q(species__parent=species, species__approved=True))


def _quantile(ordered: list[Decimal], fraction: float) -> Decimal:
    """Linear-interpolated quantile of an already sorted, non-empty list."""
    position = (len(ordered) - 1) * fraction
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * Decimal(str(position - low))


def _money(value: Decimal) -> str:
    """A price for a sentence: ``12.50``, or ``8`` when it is whole."""
    value = Decimal(value).quantize(Decimal("0.01"))
    return f"{value:.0f}" if value == value.to_integral_value() else f"{value:.2f}"


def price_guide(
    prices: list[Decimal], per_each: list[Decimal] | None, currency: str, sellers: int = MIN_SELLERS_FOR_PRICE
) -> dict | None:
    """The middle of what a lot sold for, and the range most sales fell in; None under
    :data:`MIN_SALES_FOR_PRICE`, or when one seller made every sale (the "guide" would be their prices).
    """
    if len(prices) < MIN_SALES_FOR_PRICE or sellers < MIN_SELLERS_FOR_PRICE:
        return None
    ordered = sorted(prices)
    symbol = get_currency_symbol(currency)
    guide = {
        "currency": currency,
        "symbol": symbol,
        "sales": len(ordered),
        "median": _money(_quantile(ordered, 0.5)),
        "low": _money(_quantile(ordered, 0.25)),
        "high": _money(_quantile(ordered, 0.75)),
        "per_each": None,
    }
    # Each, only when some lots held more than one: otherwise it repeats the median.
    if per_each:
        guide["per_each"] = _money(_quantile(sorted(per_each), 0.5))
    return guide


def trend(recent: int, prior: int, first_seen, now) -> str:
    """ "new", "growing", "steady", "declining", or "" when there is too little to say."""
    if first_seen and first_seen >= now - YEAR:
        return "new"
    if recent + prior < MIN_LOTS_FOR_TREND:
        return ""
    if prior == 0 or recent >= prior * 1.25:
        return "growing"
    if recent <= prior * 0.8:
        return "declining"
    return "steady"


def _recent_counts_by_species(since) -> dict[int, int]:
    """Lots listed since *since*, per nominal species (a cultivar's lots count for its parent). Cached:
    every species page ranks against the same table.
    """
    key = f"species_stats:recent_counts:{since.date().isoformat()}"
    counts = cache.get(key)
    if counts is None:
        rows = (
            public_lots()
            .filter(date_posted__gte=since, species__isnull=False)
            .annotate(nominal=Coalesce("species__parent_id", "species_id"))
            .values("nominal")
            .annotate(lots=Count("pk"))
            .order_by()
        )
        counts = {row["nominal"]: row["lots"] for row in rows}
        cache.set(key, counts, CACHE_SECONDS)
    return counts


def popularity_rank(species, now) -> tuple[int, int] | None:
    """(rank, out of) among species by lots listed in the last year, or None. Nominal species only: a
    cultivar's lots are already part of its parent's count.
    """
    if species.parent_id or species.is_hybrid:
        return None
    counts = _recent_counts_by_species((now - YEAR).replace(hour=0, minute=0, second=0, microsecond=0))
    mine = counts.get(species.pk, 0)
    if not mine:
        return None
    return (sum(1 for lots in counts.values() if lots > mine) + 1, len(counts))


def representative_photo(species) -> str | None:
    """One picture of this species: a seller's own uploaded photo, from a lot in an auction shown to everyone,
    primary images first, then the newest. Never a pasted link (it would put someone else's host on the page)
    and never "not my photo". Not cached, so a removed lot's picture goes at once.
    """
    lots = lots_for(species).filter(Q(auction__isnull=True) | Q(auction__promote_this_auction=True))
    images = (
        LotImage.objects.filter(lot_number__in=lots.values("pk"), image_source__in=OWN_PHOTO_SOURCES)
        .exclude(image="", cloudflare_image_id="")
        .order_by("-is_primary", "-createdon")
    )
    # A few: a row can have no picture left (display_url None), which is cheaper to skip than to query.
    for image in images[:5]:
        url = cloudflare_images.image_url(image.image, image.cloudflare_image_id)
        if url:
            return url
    return None


def _person(user_id, tos_user_id, email, tos_id):
    """One key per person across auctions: the account where there is one, else the email, else the bidder row."""
    if user_id or tos_user_id:
        return ("user", user_id or tos_user_id)
    if email:
        return ("email", email.strip().lower())
    if tos_id:
        return ("tos", tos_id)
    return None


def _count(species, now) -> dict:
    """Count everything on the page, uncached."""
    rows = lots_for(species).values(
        "date_posted",
        "winning_price",
        "quantity",
        "i_bred_this_fish",
        "donation",
        "ended_unsold",
        "auction_id",
        "auction__club_id",
        "user_id",
        "auctiontos_seller_id",
        "auctiontos_seller__user_id",
        "auctiontos_seller__email",
        "winner_id",
        "auctiontos_winner_id",
        "auctiontos_winner__user_id",
        "auctiontos_winner__email",
        "auction__created_by_id",
    )
    sellers, buyers, auctions, clubs = set(), set(), set(), set()
    first_seen = last_seen = None
    lots = sold = unsold = bred = recent = prior = 0
    per_year = defaultdict(lambda: {"lots": 0, "sold": 0})
    # Keyed by whose currency the lot is in (Lot.currency: the auction's creator, else the seller), since
    # a currency is worked out per person, not stored on a column.
    priced_by_owner = defaultdict(list)
    price_since = now - PRICE_GUIDE_YEARS * YEAR
    for row in rows.iterator(chunk_size=2000):
        lots += 1
        posted = row["date_posted"]
        if posted:
            first_seen = posted if first_seen is None or posted < first_seen else first_seen
            last_seen = posted if last_seen is None or posted > last_seen else last_seen
            per_year[posted.year]["lots"] += 1
            if posted >= now - YEAR:
                recent += 1
            elif posted >= now - 2 * YEAR:
                prior += 1
        if row["i_bred_this_fish"]:
            bred += 1
        if row["auction_id"]:
            auctions.add(row["auction_id"])
        if row["auction__club_id"]:
            clubs.add(row["auction__club_id"])
        seller = _person(
            row["user_id"],
            row["auctiontos_seller__user_id"],
            row["auctiontos_seller__email"],
            row["auctiontos_seller_id"],
        )
        if seller:
            sellers.add(seller)
        buyer = _person(
            row["winner_id"],
            row["auctiontos_winner__user_id"],
            row["auctiontos_winner__email"],
            row["auctiontos_winner_id"],
        )
        price = row["winning_price"]
        if buyer and price and price > 0:
            sold += 1
            buyers.add(buyer)
            if posted:
                per_year[posted.year]["sold"] += 1
            # Donations sell for goodwill, not for what the fish is worth.
            if not row["donation"] and posted and posted >= price_since:
                quantity = max(row["quantity"] or 1, 1)
                owner = row["auction__created_by_id"] or row["user_id"]
                priced_by_owner[owner].append((price, quantity, seller))
        elif row["ended_unsold"]:
            unsold += 1

    priced = defaultdict(lambda: {"prices": [], "each": [], "multiples": False, "sellers": set()})
    owners = {data.user_id: data for data in UserData.objects.filter(user_id__in=[pk for pk in priced_by_owner if pk])}
    for owner, sales in priced_by_owner.items():
        currency = owners[owner].currency if owner in owners else "USD"
        bucket = priced[currency]
        for price, quantity, seller in sales:
            bucket["prices"].append(price)
            bucket["each"].append(price / quantity)
            bucket["multiples"] = bucket["multiples"] or quantity > 1
            if seller:
                bucket["sellers"].add(seller)
    guide = None
    if priced:
        # One currency: the one most of its sales were in. Mixing dollars and euros means nothing.
        currency = max(priced, key=lambda name: len(priced[name]["prices"]))
        bucket = priced[currency]
        each = bucket["each"] if bucket["multiples"] else None
        guide = price_guide(bucket["prices"], each, currency, len(bucket["sellers"]))
    rank = popularity_rank(species, now)
    return {
        "lots": lots,
        "sold": sold,
        "sell_through": round(100 * sold / (sold + unsold)) if sold + unsold else None,
        "sellers": len(sellers),
        "buyers": len(buyers),
        "auctions": len(auctions),
        "clubs": len(clubs),
        "bred_percent": round(100 * bred / lots) if lots else None,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "recent_lots": recent,
        "prior_lots": prior,
        "trend": trend(recent, prior, first_seen, now),
        "rank": rank[0] if rank else None,
        "ranked_species": rank[1] if rank else None,
        "years": [{"year": year, **per_year[year]} for year in sorted(per_year, reverse=True)],
        "price": guide,
    }


def species_stats(species, now=None) -> dict:
    """The page's numbers for *species*, cached for :data:`CACHE_SECONDS`."""
    key = f"species_stats:{species.pk}"
    stats = cache.get(key)
    if stats is None:
        stats = _count(species, now or timezone.now())
        cache.set(key, stats, CACHE_SECONDS)
    return {**stats, "photo": representative_photo(species)}


def varieties_with_lots(species) -> list[tuple[Species, int]]:
    """A nominal species' approved cultivars that have lots, most lots first."""
    if species.parent_id or species.is_hybrid:
        return []
    counts = Counter(
        dict(
            public_lots()
            .filter(species__parent=species, species__approved=True)
            .values_list("species_id")
            .annotate(lots=Count("pk"))
            .order_by()
        )
    )
    if not counts:
        return []
    by_pk = Species.objects.in_bulk(list(counts))
    return [(by_pk[pk], lots) for pk, lots in counts.most_common() if pk in by_pk]
