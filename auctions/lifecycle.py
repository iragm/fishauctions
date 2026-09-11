"""Phase 9: the people who are not running the auction.

``docs/phase_9.md`` sets out four instruments and this module is all four of them.  Phases 0-8
measured the organizer; these measure the buyer, the seller, and the person who never made an
account and whose first contact with this site is an invoice email.  Nothing here writes a row
except :class:`~auctions.models.SignInStitch`, and nothing here is a new kind of tracking: every
query below reads ``PageView``, ``AuctionTOS``, ``Lot`` and ``Invoice`` rows that already exist and
in ``PageView``'s case go back to 2020.

**Milestones, not a funnel** (:func:`milestone_reach`).  ``usability_report.buyer_funnel`` reports
seven stages in a fixed order, and most of them do not have one.  A seller adds lots and never opens
one; a buyer opens forty and never adds one; the non-user starts at "viewed the invoice" and has no
earlier row at all.  A strict funnel reports *did it differently* as *dropped out*.  So the events
here are a set each person reached, with the handful of genuine prerequisites written down as
prerequisites (:data:`MILESTONES`), and the report is a reach rate rather than a step-to-step
conversion.  Bidding is deliberately demoted: online bidding is about 5% of auctions and two clubs
do most of it, and **the terminal milestone is a paid invoice**, which exists for every auction type.

**Lapsing is counted in auctions, not in days** (:func:`lapsed_participants`).  An ending cannot be
observed, only an absence, and no query separates "left the hobby" from "waiting for the spring
auction".  What fixes that is the unit: a club running four auctions a year makes a two-month gap
meaningless and a club running one makes it enormous, so the same *number of days* means opposite
things and the same *number of missed auctions* means the same thing.  That is the whole of the
normalisation, and it is why :data:`LAPSED_AFTER_AUCTIONS` can be a constant without being a global
cutoff in disguise.  Anyone whose last auction is the club's most recent one is **right-censored** --
not lapsed, just not asked yet -- and is excluded rather than counted as retained, which is the
mistake that makes every retention number look better than it is.

**One person's sessions, read in order** (:func:`session_timeline`).  The highest-value instrument
here and the one that needs no new table.  At n=dozens, twenty real sessions read end to end teach
more than any aggregate.  The anonymous seam is the real limitation and
:class:`~auctions.models.SignInStitch` is the only honest key across it; :func:`stitched_sessions`
is where that key is spent, and :func:`stitching_began` is the date before which it does not exist,
which any before-and-after number has to be shown next to.

**The club is its own control** (:func:`club_cohorts`).  Same members, same venue, same rules, one
auction to the next -- as close to an experiment as this site will ever get, and the reason
``docs/phase_9.md`` rules out A/B testing rather than merely deferring it.  Two bidders in one
auction are not independent samples; the effective n is closer to the number of auctions than to the
number of bidders.

**What blocks the club half on today's data:** only about one auction in five has a ``club``.  Every
query in :func:`club_cohorts` groups by club, so it can see a fifth of the site until
``/admin-unlinked-auctions/`` has been worked down.  :func:`club_coverage` returns that fraction so
the page can say so rather than quietly reporting a fifth of the answer.
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
    """One thing a person can have reached, and the one thing that has to come first.

    ``after`` is a real prerequisite, not a position in a list: you have an invoice before you pay
    it and you join before you win.  Where there is no prerequisite it is ``None``, and the pair is
    then reported as two independent reach rates rather than as a conversion between them --
    which is the whole difference between this and a funnel.
    """

    key: str
    label: str
    after: str | None = None
    #: Shown under the label on the dashboard when the number needs a caveat to be read correctly.
    note: str = ""


#: The conversions worth counting, in the order they are usually *reported* -- which is not an
#: order anybody moves through.  Read ``after`` for what is actually ordered.
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

#: How many of a club's own auctions have to pass before somebody counts as lapsed.  Two, and the
#: unit is what does the work: see the module docstring.  A club that runs one auction a year and a
#: club that runs six are compared on the same number here, and a number of *days* could not do
#: that for both.
LAPSED_AFTER_AUCTIONS = 2

#: Auctions per club on the cohort page.  A club's history read across more than this stops being
#: something an organizer looks at and starts being a chart.
COHORT_AUCTIONS = 6

#: Rows in one session timeline.  A session with more pages than this is a crawler or somebody who
#: left a tab open for a week, and either way the first page of it is the part worth reading.
TIMELINE_LIMIT = 300

#: A gap longer than this inside one session is a different visit, not a slow reader.  Used only to
#: mark the break when the timeline is read; nothing splits a session on it.
VISIT_GAP = datetime.timedelta(minutes=30)


def stitching_began():
    """The datetime before which no anonymous half of anybody's visit can be attributed.

    :class:`~auctions.models.SignInStitch` only works forwards, so a retention number computed
    across this date is a stitched period compared against an unstitched one.  Returned as the
    timestamp of the oldest stitch rather than a constant, because a constant would go stale on the
    day somebody restored a database or reran a fixture, and ``None`` when nothing has been stitched
    yet -- which is the honest answer on a fresh install.
    """
    from auctions.models import SignInStitch

    oldest = SignInStitch.objects.order_by("createdon").values_list("createdon", flat=True).first()
    return oldest


def stitched_sessions(user):
    """Every anonymous session key this user was holding when they signed in.

    The one place the stitch is spent.  Anything read through it is that person's own earlier
    browsing; anything read without it is two people as far as this site can tell.
    """
    from auctions.models import SignInStitch

    return list(SignInStitch.objects.filter(user=user).values_list("session_id", flat=True))


# ---------------------------------------------------------------------------------------------
# 9a -- milestones
# ---------------------------------------------------------------------------------------------


def _reach(rows, key):
    return {row[key]: row["people"] for row in rows}


def milestone_reach(auctions):
    """``{auction_pk: {milestone_key: people}}`` for the auctions given.

    One ``GROUP BY`` per milestone over the whole set rather than one query per auction, the same
    shape as ``usability_report.buyer_funnel``, and for the same reason: this page shows several
    auctions and a per-auction loop would be forty queries.

    The ``PageView`` queries carry a date floor that is not cosmetic.  They match an auction as
    ``pageview.auction_id OR lot.auction_id`` -- an OR across a join no single index can serve --
    so unbounded each one is a full scan of the largest table on the site, which is the exact shape
    behind a past production incident.  The OR itself only covers rows written before 2026-09-09;
    ``tasks.backfill_page_view_auctions`` is retiring it.
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
    # The rules page is the auction's own page and nothing else: a lot page also carries the
    # auction FK now (7a.2), so "viewed something belonging to this auction" is not the question.
    # Matched on the route rather than on the path, because the path holds a slug.
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


#: The route that serves an auction's own page -- its rules, its dates, its "join this auction"
#: button.  Named here rather than matched as a path prefix because ``route_name`` classifies with
#: Django's own resolver, so a route renamed in ``urls.py`` cannot leave this reading the wrong
#: pages the way a hand-written prefix would.
RULES_ROUTE = "auction_main"


def _rules_page_views(views):
    """People who opened an auction's own page, as opposed to one of its lots.

    Since 7a.2 a lot page carries its auction's FK too, so "a view belonging to this auction" is
    not the question -- ``lot_number__isnull=True`` and the route together are.

    Counted in Python over the (auction, actor, url) rows because the route is not a column: there
    is no ``GROUP BY`` for "the URL pattern that served this path" in any database here, and
    ``route_name`` is memoised across the thousands of paths that fold into it.
    """
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
    """A club's auctions, oldest first -- the ruler everything below is measured against.

    Oldest first because "N auctions since the one they last took part in" is a count forwards from
    a position in this list, and reversing it at every call site is how an off-by-one gets in.
    """
    from auctions.models import Auction

    query = Auction.objects.filter(club=club, is_deleted=False).order_by("date_start", "pk")
    if limit:
        query = query[:limit]
    return list(query)


@dataclass
class Cohort:
    """One auction inside a club's history, and the four numbers ``docs/phase_9.md`` asked for."""

    auction: object
    new_people: int = 0
    new_who_participated: int = 0
    returning: int = 0
    lapsed: int = 0
    #: ``None`` for the club's most recent auction: everybody is right-censored there and a rate
    #: computed anyway would be reported as perfect retention.
    lapsed_is_measurable: bool = True
    referrers: list = field(default_factory=list)

    @property
    def participation_rate(self):
        """What share of the new arrivals actually bought or sold something."""
        if not self.new_people:
            return None
        return round(100 * self.new_who_participated / self.new_people, 1)


def _participants(auction_ids):
    """``{auction_pk: {identity}}`` -- who took part, by the identity that survives having no account.

    Keyed on the user id where there is one and on the ``AuctionTOS`` email otherwise, because the
    non-user persona is the point: somebody who brings lots on a piece of paper has an
    ``AuctionTOS`` an organizer typed in, no ``User``, and is the same person at the next auction
    only through that email.  A row with neither is that auction's own bidder number, which is
    unique inside the auction and therefore never matches across two -- correct, and deliberately
    so: there is nothing there to match on.
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
    """``{auction_pk: {identity}}`` -- narrowed to the people who bought or sold, not merely joined.

    "Participated" is per persona and it is never "logged in": a buyer won a lot, a seller entered
    one.  Both are rows an organizer created at the table as often as the person did themselves,
    which is exactly why this is the honest measure of a room.
    """
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
    """The identities who took part before ``auction`` and had not come back ``after`` auctions later.

    The churn definition and the "who did we lose this time" report are the same query, which is
    the point of measuring in auctions: the organizer's question -- *who was here last time and is
    not here now* -- and the campaign's question are one thing rather than two reports that
    disagree.

    Returns ``(identities, measurable)``.  ``measurable`` is ``False`` when there have not been
    ``after`` auctions since this one, because everybody is then **right-censored**: not lapsed,
    just not asked yet.  Counting them as retained is what makes retention numbers look better
    than they are, and counting them as lost is worse.
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
    """``[Cohort]`` for a club's most recent auctions, oldest first.

    Read across a club's history these four numbers say whether it is growing, replacing its
    members, or quietly shrinking behind a flat headline attendance -- and the pattern worth
    catching (people come, then spend less, then stop coming) is a trend in *participation rate*
    and *lapsed* together, not a single number.
    """
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
    """``{auction_pk: [{referrer, people}]}`` -- how the people at each auction first got there.

    Per person rather than the per-view count ``usability_report.funnel_referrers`` reports: a
    cohort question is "where did these forty people come from", and one visitor refreshing a lot
    page thirty times is one answer to it, not thirty.
    """
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
    """``{linked, total, percent}`` -- the fraction of auctions any club query here can see.

    Not a footnote.  Every number :func:`club_cohorts` returns is computed from the auctions that
    have a ``club``, and most do not, so a page showing them without this is reporting a fifth of
    the site as if it were the site.
    """
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
    #: True when :data:`VISIT_GAP` has passed -- the same session, but a different sitting.
    new_visit: bool = False


def session_timeline(session_id=None, user=None, limit=TIMELINE_LIMIT):
    """``[Step]`` -- what one person opened, in order, with the elapsed gap between each page.

    A page and a limit, not a chart.  This is the instrument ``docs/phase_9.md`` calls the
    highest-value item in the phase, and the argument for it is about sample size rather than
    about features: at n=dozens, twenty real sessions read end to end teach more than any
    aggregate, and nothing else here lets anybody read one.

    Given a ``user``, this returns their signed-in rows **and** the anonymous rows from every
    session they were holding when they signed in (:func:`stitched_sessions`) -- which for the
    buyer persona is the half of the visit that matters, because they arrive from a club's Facebook
    page anonymously and only become a user at the point of paying.  Before
    :func:`stitching_began` there are no such rows to find, and their absence is not evidence that
    the person did not browse.
    """
    from auctions.models import PageView

    query = PageView.objects.select_related("auction", "lot_number", "user").order_by("date_start", "pk")
    if user is not None:
        keys = stitched_sessions(user)
        query = query.filter(Q(user=user) | Q(session_id__in=keys)) if keys else query.filter(user=user)
    elif session_id:
        query = query.filter(session_id=session_id)
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


def busiest_sessions(limit=25):
    """``[{session_id, user_id, pages}]`` -- somewhere to start reading.

    The index in front of :func:`session_timeline`, because that instrument is useless without a
    way to find a session to open.  Ordered by page count, which is **not** the same as ordered by
    interest: the sessions worth reading are usually short and end somewhere odd.  This is a list
    to click, not an answer.

    Rows with neither a user nor a session key are dropped -- there is nothing to open them by.
    """
    from auctions.models import PageView

    rows = PageView.objects.values("session_id", "user_id").annotate(pages=Count("pk")).order_by("-pages")[: limit * 4]
    found = []
    for row in rows:
        if not row["session_id"] and not row["user_id"]:
            continue
        found.append({"session_id": row["session_id"], "user_id": row["user_id"], "pages": row["pages"]})
        if len(found) >= limit:
            break
    return found


# ---------------------------------------------------------------------------------------------
# 9c -- the median member, shown to their own club
# ---------------------------------------------------------------------------------------------


def median_member(auction):
    """The participant at the 50th percentile of this auction, by lots bought plus lots sold.

    **Median, and a real person.**  The mean of this distribution is meaningless -- a handful of
    power users dominate every total -- and an averaged journey is nobody's journey, which reads as
    fiction to any organizer who knows their own members.  So this returns one ``AuctionTOS`` and
    the caller shows *their* session.

    Ranked on lots rather than on money because money is a different question (an auction's
    takings) and because a club whose median member sells three cheap plants and one that sells one
    expensive fish are the same amount of participation.  Ties are broken by bidder number, which
    is stable, so the same auction returns the same person twice running.
    """
    from auctions.models import AuctionTOS

    rows = (
        AuctionTOS.objects.filter(auction=auction)
        .annotate(
            # Both reverse accessors are named after the forward field on Lot, which is why the
            # seller side reads as "auctiontos_seller" here rather than as "lots".
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
    """``{tos, score, sold, bought, timeline}`` -- the median member and what they actually did.

    The output most likely to change what a club does, because it is the only one here a volunteer
    running a fish auction will read without being asked to.  It is a person and a sequence, not a
    statistic.

    The timeline is empty for somebody with no ``User`` -- the non-user persona, whose first
    contact with this site is an invoice email.  That emptiness is the finding rather than a gap in
    the data, and the page says so: a club whose median member has no rows here ran an auction the
    site never spoke to.
    """
    tos = median_member(auction)
    if tos is None:
        return None
    sold = tos.lots_qs.count() if hasattr(tos, "lots_qs") else 0
    bought = tos.bought_lots_qs.count() if hasattr(tos, "bought_lots_qs") else 0
    timeline = session_timeline(user=tos.user) if tos.user_id else []
    return {"tos": tos, "sold": sold, "bought": bought, "score": sold + bought, "timeline": timeline}


def unreached_share(auction):
    """What share of the room this site never spoke to, as an honest proxy.

    ``docs/phase_9.md`` asks for this next to any buyer funnel and it is the one number here that
    is about the people no instrument can see: somebody who does not use a computer is by
    construction invisible to every query above, and quietly reporting a funnel that omits them is
    the failure mode.

    The proxy is an ``AuctionTOS`` with no user attached whose invoice was never opened.  It is a
    floor, not a count: somebody with no account who did open their invoice is not in it.
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
