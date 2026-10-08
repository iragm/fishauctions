"""Do in-person auctions whose lots and people are added early gross more?

:func:`early_adds` is one row per promoted in-person auction: the share of its lots, and of its people,
added more than ``days`` before it started, and its gross. People are left out for club-managed
auctions, whose rows are made from the member list all at once. :func:`summarize` gives the
correlation and splits the auctions at the median share: gross grows with the number of lots, so the
split shows lots and gross per lot beside gross.
"""

from __future__ import annotations

import datetime
import statistics
from dataclasses import dataclass

from django.db.models import Count, DateTimeField, ExpressionWrapper, F, Q, Sum
from django.utils import timezone

#: The fewest lots an auction needs to be plotted.
MIN_LOTS = 10


@dataclass
class AuctionPoint:
    pk: int
    slug: str
    title: str
    date_start: datetime.datetime
    lots: int
    early_lots: int
    people: int
    early_people: int
    gross: float

    @property
    def early_lots_pct(self):
        return round(100 * self.early_lots / self.lots, 1)

    @property
    def early_people_pct(self):
        return round(100 * self.early_people / self.people, 1) if self.people else None

    @property
    def gross_per_lot(self):
        return self.gross / self.lots


def _cutoff(days, field):
    return ExpressionWrapper(F(field) - datetime.timedelta(days=days), output_field=DateTimeField())


def early_adds(days: int = 5) -> list[AuctionPoint]:
    """Promoted, finished in-person auctions with at least :data:`MIN_LOTS` lots, oldest first."""
    from auctions.models import Auction, AuctionTOS, Lot

    auctions = {
        auction.pk: auction
        for auction in Auction.objects.filter(
            is_online=False,
            is_deleted=False,
            promote_this_auction=True,
            date_start__lt=timezone.now() - datetime.timedelta(days=1),
        ).only("pk", "slug", "title", "date_start", "club", "manage_users_through_club")
    }
    lots = (
        Lot.objects.filter(auction__in=auctions, is_deleted=False, banned=False)
        .values("auction")
        .annotate(
            n=Count("pk"),
            early=Count("pk", filter=Q(date_posted__lt=_cutoff(days, "auction__date_start"))),
            gross=Sum("winning_price"),
        )
        .order_by()
    )
    people = {
        row["auction"]: row
        for row in AuctionTOS.objects.filter(auction__in=auctions)
        .values("auction")
        .annotate(n=Count("pk"), early=Count("pk", filter=Q(createdon__lt=_cutoff(days, "auction__date_start"))))
        .order_by()
    }
    points = []
    for row in lots:
        auction = auctions[row["auction"]]
        if row["n"] < MIN_LOTS or not row["gross"]:
            continue
        joined = {} if auction.is_club_managed else people.get(auction.pk, {})
        points.append(
            AuctionPoint(
                pk=auction.pk,
                slug=auction.slug,
                title=auction.title,
                date_start=auction.date_start,
                lots=row["n"],
                early_lots=row["early"],
                people=joined.get("n", 0),
                early_people=joined.get("early", 0),
                gross=float(row["gross"]),
            )
        )
    return sorted(points, key=lambda point: point.date_start)


def summarize(points: list[AuctionPoint], attribute: str) -> dict | None:
    """Pearson r between ``attribute`` and gross (and gross per lot), and the auctions split at its median."""
    pairs = [(getattr(point, attribute), point) for point in points if getattr(point, attribute) is not None]
    if len(pairs) < 3:
        return None
    xs = [x for x, _ in pairs]

    def r(ys):
        try:
            return statistics.correlation(xs, ys)
        except statistics.StatisticsError:
            # Every x, or every y, the same.
            return None

    median = statistics.median(xs)

    def half(chosen):
        return {
            "auctions": len(chosen),
            "gross": statistics.median(point.gross for point in chosen) if chosen else None,
            "lots": statistics.median(point.lots for point in chosen) if chosen else None,
            "gross_per_lot": statistics.median(point.gross_per_lot for point in chosen) if chosen else None,
        }

    return {
        "n": len(pairs),
        "r": r([point.gross for _, point in pairs]),
        "r_per_lot": r([point.gross_per_lot for _, point in pairs]),
        "median": median,
        "above": half([point for x, point in pairs if x > median]),
        "below": half([point for x, point in pairs if x <= median]),
    }
