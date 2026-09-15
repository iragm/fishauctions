"""Phase 9: measuring buyers, sellers and people with no account (``docs/phase_9.md``).

Reads existing ``PageView``, ``AuctionTOS``, ``Lot`` and ``Invoice`` rows; only
:class:`~auctions.models.SignInStitch` is written.

* **Milestones, not a funnel** (:func:`milestone_reach`): people reach different things in different
  orders, so report reach rates with only real prerequisites. The terminal milestone is a paid
  invoice; bidding is demoted (online bidding is ~5% of auctions).
* **Lapsing counts missed auctions, not days** (:func:`lapsed_participants`), so clubs with different
  schedules compare. Anyone whose last auction is the club's latest is right-censored and excluded.
* **Session timelines** (:func:`session_timeline`): read real sessions end to end. ``SignInStitch``
  bridges the anonymous seam, from :func:`stitching_began` on.
* **The club is its own control** (:func:`club_cohorts`). Only ~1 auction in 5 has a club, so
  :func:`club_coverage` reports the fraction visible.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field

from django.db.models import Count, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from auctions.usability_report import route_name

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Milestone:
    """One milestone and its real prerequisite. ``after=None`` reports two independent reach rates."""

    key: str
    label: str
    after: str | None = None
    #: A caveat shown under the label.
    note: str = ""


#: The milestones, in report order; ``after`` holds what is really ordered.
MILESTONES: list[Milestone] = [
    Milestone("arrived", "First page view"),
    Milestone(
        "read_rules",
        "Read the auction rules",
        note="The one page that answers 'am I allowed to do this'.",
    ),
    Milestone("lot_page", "Viewed a first lot"),
    Milestone(
        "joined",
        "Joined the auction",
        note="An AuctionTOS row, whether self-service or typed in at the door by an organizer.",
    ),
    Milestone(
        "added_lots",
        "Added lots",
        after="joined",
        note="The seller's entire path. A seller who never opens a lot page is not a drop-out.",
    ),
    Milestone(
        "won",
        "Won something",
        after="joined",
        note="A bid that won, or a winner set by an organizer at the table.",
    ),
    Milestone("invoice_opened", "Viewed the invoice", after="joined"),
    Milestone("paid", "Paid", after="invoice_opened"),
]

#: Club auctions missed before someone counts as lapsed.
LAPSED_AFTER_AUCTIONS = 2

#: Auctions per club on the cohort page.
COHORT_AUCTIONS = 6

#: Rows in one session timeline.
TIMELINE_LIMIT = 300

#: A gap this long marks a new visit in the timeline display; nothing is split on it.
VISIT_GAP = datetime.timedelta(minutes=30)

#: Look-back for the session index, which is a GROUP BY over PageView (never purged) on page load.
SESSION_INDEX_DAYS = 14

#: Session key characters exposed. PageView stores live anonymous session keys, which must not
#: reach logs, history or Referer.
SESSION_KEY_PREFIX = 12


def stitching_began():
    """When stitching began (oldest ``SignInStitch``), or None. Numbers across this date compare stitched
    with unstitched periods.
    """
    from auctions.models import SignInStitch

    oldest = SignInStitch.objects.order_by("createdon").values_list("createdon", flat=True).first()
    return oldest


def stitched_sessions(user):
    """Every anonymous session key this user held when signing in."""
    from auctions.models import SignInStitch

    return list(SignInStitch.objects.filter(user=user).values_list("session_id", flat=True))


# ---------------------------------------------------------------------------------------------
# 9a -- milestones
# ---------------------------------------------------------------------------------------------


def _reach(rows, key):
    return {row[key]: row["people"] for row in rows}


def milestone_reach(auctions):
    """``{auction_pk: {milestone_key: people}}``, one GROUP BY per milestone.

    PageView queries carry a date floor: the ``auction_id OR lot.auction_id`` match can't use an index,
    and unbounded it caused a production incident. The OR covers rows before 2026-09-09 until
    ``tasks.backfill_page_view_auctions`` retires it.
    """
    from auctions.models import AuctionTOS, Invoice, Lot, PageView
    from auctions.usability_report import FUNNEL_LOOKBACK_DAYS, _actor

    if not auctions:
        return {}
    ids = [auction.pk for auction in auctions]
    actor = _actor()
    starts = [
        auction.date_start or auction.date_end for auction in auctions if (auction.date_start or auction.date_end)
    ]
    floor = (min(starts) if starts else timezone.now()) - datetime.timedelta(days=FUNNEL_LOOKBACK_DAYS)

    auction_key = Coalesce("auction_id", "lot_number__auction_id")
    views = PageView.objects.filter(date_start__gte=floor).filter(Q(auction__in=ids) | Q(lot_number__auction__in=ids))

    arrived = _reach(
        views.annotate(auction_key=auction_key).values("auction_key").annotate(people=Count(actor, distinct=True)),
        "auction_key",
    )
    # The auction's own page by route: lot pages carry the auction FK too.
    read_rules = _reach(_rules_page_views(views), "auction_key")
    lot_pages = _reach(
        PageView.objects.filter(date_start__gte=floor, lot_number__auction__in=ids)
        .values("lot_number__auction")
        .annotate(people=Count(actor, distinct=True)),
        "lot_number__auction",
    )
    joined = _reach(
        AuctionTOS.objects.filter(auction__in=ids).values("auction").annotate(people=Count("pk")),
        "auction",
    )
    added = _reach(
        Lot.objects.filter(auctiontos_seller__auction__in=ids, is_deleted=False)
        .values("auctiontos_seller__auction")
        .annotate(people=Count("auctiontos_seller", distinct=True)),
        "auctiontos_seller__auction",
    )
    won = _reach(
        Lot.objects.filter(auction__in=ids, is_deleted=False, auctiontos_winner__isnull=False)
        .values("auction")
        .annotate(people=Count("auctiontos_winner", distinct=True)),
        "auction",
    )
    opened = _reach(
        Invoice.objects.filter(auctiontos_user__auction__in=ids, opened=True)
        .values("auctiontos_user__auction")
        .annotate(people=Count("pk")),
        "auctiontos_user__auction",
    )
    paid = _reach(
        Invoice.objects.filter(auctiontos_user__auction__in=ids, status="PAID")
        .values("auctiontos_user__auction")
        .annotate(people=Count("pk")),
        "auctiontos_user__auction",
    )
    found = {}
    for pk in ids:
        found[pk] = {
            "arrived": arrived.get(pk, 0),
            "read_rules": read_rules.get(pk, 0),
            "lot_page": lot_pages.get(pk, 0),
            "joined": joined.get(pk, 0),
            "added_lots": added.get(pk, 0),
            "won": won.get(pk, 0),
            "invoice_opened": opened.get(pk, 0),
            "paid": paid.get(pk, 0),
        }
    return found


#: The route that serves an auction's own page, matched via the resolver so a rename can't break
#: it.
RULES_ROUTE = "auction_main"


def _rules_page_views(views):
    """People who opened an auction's own page. Counted in Python, since the route isn't a column."""
    rows = (
        views.annotate(auction_key=Coalesce("auction_id", "lot_number__auction_id"))
        .filter(lot_number__isnull=True)
        .exclude(url__isnull=True)
        .exclude(url="")
        .values_list("auction_key", "url", "user_id", "session_id")
    )
    seen: dict[int, set] = {}
    for auction_key, url, user_id, session_id in rows.iterator(chunk_size=2000):
        if auction_key is None or route_name(url) != RULES_ROUTE:
            continue
        actor = f"u{user_id}" if user_id else session_id
        if actor:
            seen.setdefault(auction_key, set()).add(actor)
    return [{"auction_key": key, "people": len(actors)} for key, actors in seen.items()]


# ---------------------------------------------------------------------------------------------
# 9a -- lapsing, counted in the club's own auctions
# ---------------------------------------------------------------------------------------------


def club_auctions(club, limit=None):
    """A club's auctions, oldest first, the ruler lapsing is counted on."""
    from auctions.models import Auction

    query = Auction.objects.filter(club=club, is_deleted=False).order_by("date_start", "pk")
    if limit:
        query = query[:limit]
    return list(query)


@dataclass
class Cohort:
    """One auction in a club's history and its cohort numbers."""

    auction: object
    new_people: int = 0
    new_who_participated: int = 0
    returning: int = 0
    lapsed: int = 0
    #: False for the latest auction, where everyone is right-censored.
    lapsed_is_measurable: bool = True
    referrers: list = field(default_factory=list)

    @property
    def participation_rate(self):
        """What share of the new arrivals actually bought or sold something."""
        if not self.new_people:
            return None
        return round(100 * self.new_who_participated / self.new_people, 1)


def _participants(auction_ids):
    """``{auction_pk: {identity}}``: user id, else ``AuctionTOS`` email (so people with no account match
    across auctions). A row with neither never matches, correctly.
    """
    from auctions.models import AuctionTOS

    rows = AuctionTOS.objects.filter(auction__in=auction_ids).values_list("auction_id", "user_id", "email", "pk")
    found: dict[int, set] = {}
    for auction_id, user_id, email, pk in rows.iterator(chunk_size=2000):
        if user_id:
            identity = f"u{user_id}"
        elif email:
            identity = f"e{email.strip().lower()}"
        else:
            identity = f"t{pk}"
        found.setdefault(auction_id, set()).add(identity)
    return found


def _did_something(auction_ids):
    """``{auction_pk: {identity}}`` narrowed to people who bought or sold."""
    from auctions.models import AuctionTOS, Lot

    sold = set(
        Lot.objects.filter(auctiontos_seller__auction__in=auction_ids, is_deleted=False).values_list(
            "auctiontos_seller_id", flat=True
        )
    )
    bought = set(
        Lot.objects.filter(auctiontos_winner__auction__in=auction_ids, is_deleted=False).values_list(
            "auctiontos_winner_id", flat=True
        )
    )
    active_tos = {pk for pk in sold | bought if pk}
    rows = AuctionTOS.objects.filter(pk__in=active_tos).values_list("auction_id", "user_id", "email", "pk")
    found: dict[int, set] = {}
    for auction_id, user_id, email, pk in rows.iterator(chunk_size=2000):
        if user_id:
            identity = f"u{user_id}"
        elif email:
            identity = f"e{email.strip().lower()}"
        else:
            identity = f"t{pk}"
        found.setdefault(auction_id, set()).add(identity)
    return found


def lapsed_participants(club, auction, after=LAPSED_AFTER_AUCTIONS):
    """Identities who took part before ``auction`` and hadn't returned ``after`` auctions later.

    Returns ``(identities, measurable)``; not measurable when fewer than ``after`` auctions have passed.
    """
    history = club_auctions(club)
    try:
        index = [item.pk for item in history].index(auction.pk)
    except ValueError:
        return set(), False
    later = history[index + 1 : index + 1 + after]
    if len(later) < after:
        return set(), False
    took_part = _participants([item.pk for item in history[: index + 1]] + [item.pk for item in later])
    here = took_part.get(auction.pk, set())
    came_back: set = set()
    for item in later:
        came_back |= took_part.get(item.pk, set())
    return here - came_back, True


def club_cohorts(club, limit=COHORT_AUCTIONS):
    """``[Cohort]`` for a club's recent auctions, oldest first."""
    history = club_auctions(club)
    if not history:
        return []
    shown = history[-limit:]
    ids = [item.pk for item in history]
    took_part = _participants(ids)
    did = _did_something([item.pk for item in shown])
    referrers = _cohort_referrers(shown)

    cohorts = []
    for position, auction in enumerate(history):
        if auction not in shown:
            continue
        before: set = set()
        for earlier in history[:position]:
            before |= took_part.get(earlier.pk, set())
        here = took_part.get(auction.pk, set())
        new_people = here - before
        lapsed, measurable = lapsed_participants(club, auction)
        cohorts.append(
            Cohort(
                auction=auction,
                new_people=len(new_people),
                new_who_participated=len(new_people & did.get(auction.pk, set())),
                returning=len(here & before),
                lapsed=len(lapsed),
                lapsed_is_measurable=measurable,
                referrers=referrers.get(auction.pk, []),
            )
        )
    return cohorts


def _cohort_referrers(auctions, limit=4):
    """``{auction_pk: [{referrer, people}]}``: how people first arrived, counted per person, not per view."""
    from django.contrib.sites.models import Site

    from auctions.models import PageView
    from auctions.usability_report import FUNNEL_LOOKBACK_DAYS

    if not auctions:
        return {}
    ids = [auction.pk for auction in auctions]
    starts = [
        auction.date_start or auction.date_end for auction in auctions if (auction.date_start or auction.date_end)
    ]
    floor = (min(starts) if starts else timezone.now()) - datetime.timedelta(days=FUNNEL_LOOKBACK_DAYS)
    rows = (
        PageView.objects.filter(date_start__gte=floor)
        .filter(Q(auction__in=ids) | Q(lot_number__auction__in=ids))
        .exclude(referrer__isnull=True)
        .exclude(referrer__exact="")
        .exclude(referrer__startswith=Site.objects.get_current().domain)
        .annotate(auction_key=Coalesce("auction_id", "lot_number__auction_id"))
        .values_list("auction_key", "referrer", "user_id", "session_id")
    )
    people: dict[int, dict[str, set]] = {}
    for auction_key, referrer, user_id, session_id in rows.iterator(chunk_size=2000):
        if auction_key is None:
            continue
        actor = f"u{user_id}" if user_id else session_id
        if not actor:
            continue
        people.setdefault(auction_key, {}).setdefault(referrer, set()).add(actor)
    return {
        key: [
            {"referrer": referrer, "people": len(actors)}
            for referrer, actors in sorted(buckets.items(), key=lambda item: -len(item[1]))[:limit]
        ]
        for key, buckets in people.items()
    }


def club_coverage():
    """``{linked, total, percent}``: the fraction of auctions any club query can see."""
    from auctions.models import Auction

    total = Auction.objects.filter(is_deleted=False).count()
    linked = Auction.objects.filter(is_deleted=False, club__isnull=False).count()
    return {
        "linked": linked,
        "total": total,
        "percent": round(100 * linked / total, 1) if total else 0,
    }


# ---------------------------------------------------------------------------------------------
# 9b -- one person's pages, in order, with the gaps
# ---------------------------------------------------------------------------------------------


@dataclass
class Step:
    """One page in a timeline, and how long the person sat on the one before it."""

    view: object
    gap: datetime.timedelta | None
    route: str
    #: True after :data:`VISIT_GAP`.
    new_visit: bool = False


def session_timeline(session_id=None, user=None, limit=TIMELINE_LIMIT):
    """``[Step]``: what one person opened, in order, with the gap between pages.

    Given a ``user``, includes anonymous rows from sessions stitched at sign-in. Before
    :func:`stitching_began` none exist, which isn't evidence they didn't browse.
    """
    from auctions.models import PageView

    query = PageView.objects.select_related("auction", "lot_number", "user").order_by("date_start", "pk")
    if user is not None:
        keys = stitched_sessions(user)
        query = query.filter(Q(user=user) | Q(session_id__in=keys)) if keys else query.filter(user=user)
    elif session_id:
        # A prefix, never the key (SESSION_KEY_PREFIX); a full key is cut to it.
        if len(session_id) < SESSION_KEY_PREFIX:
            return []
        query = query.filter(session_id__startswith=session_id[:SESSION_KEY_PREFIX])
    else:
        return []
    steps: list[Step] = []
    previous = None
    for view in query[:limit]:
        gap = (view.date_start - previous) if previous else None
        steps.append(
            Step(
                view=view,
                gap=gap,
                route=route_name(view.url or ""),
                new_visit=bool(gap and gap >= VISIT_GAP),
            )
        )
        previous = view.date_start
    return steps


def busiest_sessions(limit=25, days=SESSION_INDEX_DAYS):
    """``[{session_id, user_id, pages}]``: sessions to start reading, by page count (not interest).

    Bounded to :data:`SESSION_INDEX_DAYS`; rows with no user or session are dropped; keys are prefixed.
    """
    from auctions.models import PageView

    floor = timezone.now() - datetime.timedelta(days=days)
    rows = (
        PageView.objects.filter(date_start__gte=floor)
        .values("session_id", "user_id")
        .annotate(pages=Count("pk"))
        .order_by("-pages")[: limit * 4]
    )
    found = []
    for row in rows:
        if not row["session_id"] and not row["user_id"]:
            continue
        found.append(
            {
                "session_id": (row["session_id"] or "")[:SESSION_KEY_PREFIX],
                "user_id": row["user_id"],
                "pages": row["pages"],
            }
        )
        if len(found) >= limit:
            break
    return found


# ---------------------------------------------------------------------------------------------
# 9c -- the median member, shown to their own club
# ---------------------------------------------------------------------------------------------


def median_member(auction):
    """The participant at the 50th percentile by lots bought plus sold: a real person, not an average.
    Ties broken by bidder number, so it's stable.
    """
    from auctions.models import AuctionTOS

    rows = (
        AuctionTOS.objects.filter(auction=auction)
        .annotate(
            # Reverse accessors are named after Lot's forward fields.
            sold=Count("auctiontos_seller", filter=Q(auctiontos_seller__is_deleted=False), distinct=True),
            bought=Count("auctiontos_winner", filter=Q(auctiontos_winner__is_deleted=False), distinct=True),
        )
        .order_by("pk")
    )
    scored = sorted(((row.sold + row.bought, row.pk, row) for row in rows), key=lambda item: (item[0], item[1]))
    if not scored:
        return None
    return scored[len(scored) // 2][2]


def median_member_story(auction):
    """``{tos, score, sold, bought, timeline}`` for the median member. An empty timeline (no account) is a
    finding: the site never spoke to them.
    """
    tos = median_member(auction)
    if tos is None:
        return None
    sold = tos.lots_qs.count() if hasattr(tos, "lots_qs") else 0
    bought = tos.bought_lots_qs.count() if hasattr(tos, "bought_lots_qs") else 0
    timeline = session_timeline(user=tos.user) if tos.user_id else []
    return {"tos": tos, "sold": sold, "bought": bought, "score": sold + bought, "timeline": timeline}


def unreached_share(auction):
    """Share of the room the site never reached: participants with no user whose invoice was never opened.
    A floor, not a count.
    """
    from auctions.models import AuctionTOS, Invoice

    joined = AuctionTOS.objects.filter(auction=auction).count()
    if not joined:
        return None
    opened = set(
        Invoice.objects.filter(auctiontos_user__auction=auction, opened=True).values_list(
            "auctiontos_user_id", flat=True
        )
    )
    silent = AuctionTOS.objects.filter(auction=auction, user__isnull=True).exclude(pk__in=opened).count()
    return {"joined": joined, "silent": silent, "percent": round(100 * silent / joined, 1)}
