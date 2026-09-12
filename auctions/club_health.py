"""Whether a club is still running auctions here, judged against its own cadence.

``Club.active`` is a hand-set boolean defaulting True, flipped when a club dissolves and has to
come off the map.  It is not a health signal and was never meant to be one, so a club that quietly
stops using the site keeps reading as active for ever.  That is the wrong way round: a dormant
bidder is one person, a dormant club is an organizer, its members and a recurring auction that
stops appearing -- and nothing on the site said so.

**The threshold is the club's own gap between auctions, not a number.**  A club running monthly
that has missed two is in trouble; a club running one big auction a year is healthy at eight
months.  One global cutoff gets both wrong, which is the likeliest reason nobody trusted ``active``
enough to maintain it.  So :func:`compute_club_health` measures the club's own median gap and asks
how many of those have gone by -- :attr:`ClubHealth.overdue_ratio`, where 1.0 is "due now" and 2.0
is "has missed one".  A club with fewer than :data:`MIN_AUCTIONS_FOR_CADENCE` real auctions has no
cadence to deviate from, and is judged on its stage instead.

**A threshold with no trigger is another chart nobody opens.**  So the rollup ends in a queue:
:func:`due_for_checkin` is the list of clubs to contact, ordered worst first, and contacting one
writes ``Club.date_contacted`` -- the outreach field that already existed and had nothing feeding
it.  A club stays out of the queue for :data:`CONTACT_COOLDOWN_DAYS` after it is contacted, so the
queue is a worklist rather than a standing complaint.

Test auctions are counted separately from real ones throughout.  A club whose only auction is
called "test-auction" has not run an auction here; before this, it was indistinguishable from one
that had, and it is exactly the club worth reaching out to -- somebody set the site up, tried it,
and stopped.

The rollup is a table rather than a set of properties because the questions asked of it are
"which clubs" rather than "this club": sorting every club by how overdue it is cannot be done from
a property, and recomputing on read would mean a dozen aggregates per club per page.

**The ladder has two halves and only one of them can be derived.**  Everything above is computed
from rows and is rewritten nightly, so nothing hand-set can live on ``ClubHealth`` -- it would be
overwritten.  The other half is what nobody can query: has anybody here heard of this club, written
to it, decided to publish it.  That is ``Club.outreach_stage``, hand-set, and it is also the map
gate.  :func:`ladder_position` puts the two on one order and says which half answered, because the
useful reading is always the furthest-along of the two: a club we have merely contacted may already
be running auctions here, and a club we have listed may have done nothing at all.
"""

from __future__ import annotations

import logging
import re
import statistics

from django.db import models
from django.utils import timezone

logger = logging.getLogger(__name__)

# The same shape AuctionEditForm refuses to promote, widened by one character class: that one is
# matched against a slug, and this is matched against the title as well, where the separator is a
# space. A word boundary alone would be wrong -- it would catch "protest" and "contest".
_TEST_WORDS = "test|mock|trial|example|demo"
TEST_AUCTION_PATTERN = re.compile(
    rf"^({_TEST_WORDS})([-_\s]|$)|([-_\s])({_TEST_WORDS})([-_\s]|$)",
    re.IGNORECASE,
)
# Below this, a club has no cadence: one auction says nothing about when the next one was due.
MIN_AUCTIONS_FOR_CADENCE = 3
# What "due" means for a club with a cadence. 1.0 is one whole median gap since the last auction --
# not late yet. Late is having missed one.
OVERDUE_RATIO_DUE = 2.0
# Twice as late again: the club has missed three of its own gaps and is not coming back on its own.
DORMANT_RATIO = 4.0
# A club with no cadence yet is judged on plain elapsed time, because there is nothing else.
NO_CADENCE_DORMANT_DAYS = 400
# How long a club stays out of the queue after somebody contacts it.
CONTACT_COOLDOWN_DAYS = 90
# A gap this long is a restart, not a cadence; including it drags the median toward "annual" for a
# club that ran monthly, took a year off, and came back monthly.
MAX_GAP_DAYS_FOR_CADENCE = 730

STAGE_CHOICES = (
    # Nothing at all: a club record with nobody and nothing behind it. Every club found by club
    # discovery starts here, which is why this rung carries no information on the ladder below.
    ("empty", "No auctions"),
    # Somebody here says they are in this club -- a ClubMember row, or a UserData.club pointing at
    # it -- and the club has never run an auction. Derived, so it separates a club that has heard of
    # this site from one that has not without anybody being asked.
    ("aware", "Members here, no auctions"),
    # Tried it, never ran one for real. The most reachable club on this list.
    ("trial", "Test auctions only"),
    # One real auction, no cadence to judge yet.
    ("new", "First real auction"),
    ("active", "Running to its own schedule"),
    ("slipping", "Late against its own schedule"),
    ("dormant", "Stopped"),
)

# Which club management tools a club has actually used, as (label, how to tell). Each is a plain
# question about rows or settings, kept here rather than in the model so adding one is a line.
TOOL_CHECKS = (
    ("members", lambda club, counts: counts["members"] > 0),
    ("breeder award program", lambda club, counts: club.enable_breeder_award_program),
    ("membership dues", lambda club, counts: club.membership_system != "none"),
    ("announcements", lambda club, counts: counts["announcements"] > 0),
    ("club events", lambda club, counts: counts["events"] > 0),
    ("donations", lambda club, counts: club.enable_donation_tracking),
    ("api keys", lambda club, counts: counts["api_keys"] > 0),
    ("discord", lambda club, counts: bool(club.discord_server_id)),
    ("google calendar", lambda club, counts: bool(club.google_calendar_id)),
    ("mailing list", lambda club, counts: bool(club.mailchimp_audience_id or club.brevo_list_id)),
    ("treasurer ledger", lambda club, counts: counts["money"] > 0),
)


class ClubHealth(models.Model):
    """A derived rollup of one club's use of the site. Written by :func:`compute_club_health`.

    Every column here is computed; nothing on this row is hand-maintained, which is the whole point
    -- ``Club.active``, ``date_contacted`` and ``date_contacted_for_in_person_auctions`` are the
    hand-maintained ones and between them they answered none of these questions.
    """

    club = models.OneToOneField("auctions.Club", on_delete=models.CASCADE, related_name="health")
    stage = models.CharField(max_length=20, choices=STAGE_CHOICES, default="empty", db_index=True)
    real_auctions = models.PositiveIntegerField(default=0)
    real_auctions.help_text = "Auctions that are not named like a test"
    test_auctions = models.PositiveIntegerField(default=0)
    first_auction = models.DateTimeField(null=True, blank=True)
    last_auction = models.DateTimeField(null=True, blank=True)
    days_since_last_auction = models.IntegerField(null=True, blank=True)
    median_gap_days = models.FloatField(null=True, blank=True)
    median_gap_days.help_text = "This club's own cadence: the median gap between its real auctions"
    overdue_ratio = models.FloatField(null=True, blank=True)
    overdue_ratio.help_text = "Gaps elapsed since the last auction. 1.0 is due, 2.0 has missed one"
    members = models.PositiveIntegerField(default=0)
    people_here = models.PositiveIntegerField(default=0)
    people_here.help_text = "People on this site who say they are in this club, by either link"
    tools_used = models.JSONField(default=list, blank=True)
    tools_used.help_text = "Which club management features this club has actually used"
    due_for_checkin = models.BooleanField(default=False, db_index=True)
    checkin_reason = models.CharField(max_length=200, blank=True, default="")
    computed_on = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-overdue_ratio"]
        verbose_name_plural = "Club health"

    def __str__(self):
        return f"{self.club}: {self.get_stage_display()}"

    @property
    def is_reachable(self):
        """Whether there is anybody to contact. A queue entry with no address is not a task."""
        return bool(self.club.contact_email or self.club.contact_email_member_id)


def is_test_auction(auction) -> bool:
    """Whether an auction is named like somebody trying the site out."""
    return bool(TEST_AUCTION_PATTERN.search(auction.slug or "") or TEST_AUCTION_PATTERN.search(auction.title or ""))


def median_gap(dates) -> float | None:
    """The club's own cadence in days, or None when there is not enough history to have one.

    Median rather than mean: a club that ran four auctions two weeks apart and then one eighteen
    months later has a cadence of two weeks and a gap, and a mean would report ten months, which
    describes neither.
    """
    dates = sorted(date for date in dates if date)
    if len(dates) < MIN_AUCTIONS_FOR_CADENCE:
        return None
    gaps = [
        (later - earlier).days
        for earlier, later in zip(dates, dates[1:], strict=False)
        if 0 < (later - earlier).days <= MAX_GAP_DAYS_FOR_CADENCE
    ]
    if not gaps:
        return None
    return float(statistics.median(gaps))


def classify(real_auctions, test_auctions, days_since_last, cadence, ratio, people_here=0) -> tuple[str, str]:
    """``(stage, reason)`` -- where this club is in its life here, and why it is in the queue.

    The reason is written for whoever opens the queue and has to decide what to say, so it names
    the numbers that put the club there rather than restating the stage.

    ``people_here`` is what separates ``aware`` from ``empty``, and it matters because those are two
    completely different conversations: one club has members on this site and has never run an
    auction, the other is a name and a location somebody typed in.
    """
    if not real_auctions:
        if test_auctions:
            return "trial", f"Set the site up and ran {test_auctions} test auction(s), never a real one"
        if people_here:
            return "aware", f"{people_here} person/people here say they are in this club, no auctions yet"
        return "empty", "Club exists here but has never had an auction"
    if ratio is None:
        # No cadence yet: one or two auctions, so all we have is elapsed time.
        if days_since_last is not None and days_since_last > NO_CADENCE_DORMANT_DAYS:
            return "dormant", f"One-off: {real_auctions} auction(s), last one {days_since_last} days ago"
        return "new", f"{real_auctions} auction(s) so far, no cadence yet"
    late = f"Runs about every {round(cadence)} days, silent for {days_since_last}"
    if ratio >= DORMANT_RATIO:
        return "dormant", late
    if ratio >= OVERDUE_RATIO_DUE:
        return "slipping", late
    return "active", ""


def compute_club_health(club) -> ClubHealth:
    """Recompute one club's rollup and save it. Safe to call as often as you like."""
    from auctions.models import Auction

    auctions = list(
        Auction.objects.filter(club=club, is_deleted=False)
        .only("id", "slug", "title", "date_start", "date_end")
        .order_by("date_start")
    )
    real = [auction for auction in auctions if not is_test_auction(auction)]
    tests = [auction for auction in auctions if is_test_auction(auction)]
    dates = [auction.date_start for auction in real if auction.date_start]
    last = dates[-1] if dates else None
    days_since = (timezone.now() - last).days if last else None
    cadence = median_gap(dates)
    ratio = round(days_since / cadence, 2) if (cadence and days_since is not None and cadence > 0) else None
    people_here = _people_here(club)
    stage, reason = classify(len(real), len(tests), days_since, cadence, ratio, people_here)
    counts = {
        # Removed members are not members. Counting them makes a club that has emptied out look
        # staffed, and puts "members" in tools_used for a club that stopped using the feature --
        # which is exactly the club this triage is trying to notice.
        "members": club.members.filter(is_deleted=False).count(),
        "announcements": club.announcements.count(),
        "events": club.events.count(),
        "api_keys": club.api_keys.count(),
        "money": club.money.count(),
    }
    tools = [label for label, used in TOOL_CHECKS if _safely(used, club, counts)]
    due, queue_reason = _queue_decision(club, stage, reason)
    health, _created = ClubHealth.objects.update_or_create(
        club=club,
        defaults={
            "stage": stage,
            "real_auctions": len(real),
            "test_auctions": len(tests),
            "first_auction": dates[0] if dates else None,
            "last_auction": last,
            "days_since_last_auction": days_since,
            "median_gap_days": cadence,
            "overdue_ratio": ratio,
            "members": counts["members"],
            "people_here": people_here,
            "tools_used": tools,
            "due_for_checkin": due,
            "checkin_reason": queue_reason[:200],
        },
    )
    return health


def _people_here(club) -> int:
    """How many people on this site say they are in this club.

    Two links, because they are set in two different places and either one on its own would miss
    half the answer: a ``ClubMember`` row, which the club's own admin creates, and ``UserData.club``,
    which the member sets on their own contact-info page. Counted as distinct users, since somebody
    with both is one person.

    The third signal USABILITY.md names -- an ``AuctionTOS`` belonging to one of these people in
    somebody else's auction -- is the same set of people seen doing something rather than a wider
    set, so it says "warm", not "aware", and adds nothing to this rung.
    """
    from auctions.models import ClubMember, UserData

    users = set(
        ClubMember.objects.filter(club=club, is_deleted=False, user__isnull=False).values_list("user_id", flat=True)
    )
    users.update(UserData.objects.filter(club=club).values_list("user_id", flat=True))
    # A ClubMember with no account is still a person this club put on the site.
    without_accounts = ClubMember.objects.filter(club=club, is_deleted=False, user__isnull=True).count()
    return len(users) + without_accounts


def _safely(check, club, counts):
    try:
        return bool(check(club, counts))
    except Exception:
        logger.exception("club health tool check failed for %s", club)
        return False


def _queue_decision(club, stage, reason) -> tuple[bool, str]:
    """Whether this club belongs on the outreach queue right now.

    A club contacted recently comes off it whatever its numbers say: the queue is a worklist, and a
    club that has already had the email this quarter is not a task until the cooldown is up.
    """
    if stage not in ("empty", "aware", "trial", "slipping", "dormant"):
        return False, ""
    if not club.active:
        # `active` is False only when a club has dissolved. There is nobody to check in with.
        return False, ""
    contacted = club.date_contacted
    if contacted and (timezone.now() - contacted).days < CONTACT_COOLDOWN_DAYS:
        return False, reason
    return True, reason


def refresh_all(queryset=None):
    """Recompute every club's rollup. Returns how many rows were written."""
    from auctions.models import Club

    queryset = Club.objects.all() if queryset is None else queryset
    written = 0
    for club in queryset.iterator(chunk_size=200):
        try:
            compute_club_health(club)
            written += 1
        except Exception:
            logger.exception("could not compute club health for %s", club)
    return written


def due_for_checkin(limit=100):
    """The outreach queue: clubs to contact, worst first.

    Ordered by stage rather than by ratio, because the stages are not degrees of the same thing --
    a club that set the site up and never ran an auction is a different conversation from one that
    ran twelve and stopped, and the first is much more likely to be recoverable.
    """
    order = {"trial": 0, "aware": 1, "empty": 2, "slipping": 3, "dormant": 4}
    # Ordered first, then cut. Slicing the queryset would apply Meta.ordering -- "-overdue_ratio" --
    # and a trial or empty club has no ratio at all: NULLs sort last under DESC on MariaDB, so the
    # two stages this queue is meant to lead with are the two the slice would throw away.
    rows = ClubHealth.objects.filter(due_for_checkin=True).select_related("club")
    ordered = sorted(rows, key=lambda row: (order.get(row.stage, 9), -(row.overdue_ratio or 0)))
    return ordered[:limit]


# The whole ladder in order, as (key, label, which half said so). The first three rungs are the
# hand-set half (Club.outreach_stage); the rest are derived from rows by classify() above.
#
# Two orderings are deliberate and are the judgement in this list:
#
# * `aware` sits above `listed`, and `empty` is not on the ladder at all. Approving a club is the
#   last thing this site does before the club does anything, and "a club row with nothing behind
#   it" is the absence of a signal rather than a rung -- every prospect starts there, so ranking it
#   would report a club nobody has heard of as further along than one we just contacted.
# * `dormant` and `slipping` sit above `new` and below `active`: both ran real auctions, which is
#   further than a club with its first, and neither is running to a schedule, which is the top.
LADDER = (
    ("unaware", "Not contacted", "hand"),
    ("contacted", "Contacted, no reply yet", "hand"),
    ("listed", "Approved and listed", "hand"),
    ("aware", "Members here, no auctions", "derived"),
    ("trial", "Test auctions only", "derived"),
    ("new", "First real auction", "derived"),
    ("dormant", "Stopped", "derived"),
    ("slipping", "Late against its own schedule", "derived"),
    ("active", "Running to its own schedule", "derived"),
)
LADDER_RANK = {key: rank for rank, (key, _label, _half) in enumerate(LADDER)}
LADDER_LABELS = {key: label for key, label, _half in LADDER}
# Club.outreach_stage -> the rung it means. Kept here rather than on the model because the ladder is
# this module's subject and the model's job is to store the field.
HAND_RUNGS = {"prospect": "unaware", "contacted": "contacted", "listed": "listed"}
# "the caller has not looked this up", as distinct from "there is no rollup" (None).
UNFETCHED = object()


def ladder_position(club, health=UNFETCHED) -> dict:
    """``{stage, label, source}`` -- the furthest-along rung this club has reached, and who says so.

    ``source`` is ``"hand"`` when the answer is the field somebody set on the club and ``"derived"``
    when it is the rollup, which is the part worth showing: a club sitting at ``listed`` because
    nothing has been derived is a different problem from one sitting at ``listed`` because that is
    genuinely as far as it got.

    ``health`` distinguishes three states, which is why the default is a sentinel rather than
    ``None``: a rollup, *no* rollup (``None``, and the caller has already looked), and "not looked
    yet", which is the only one that fetches. A caller in a loop passes what it has -- including
    ``None`` -- and gets no query. Getting that wrong is one SELECT per club with no rollup, which
    is every club club discovery is about to add.
    """
    hand = HAND_RUNGS.get(getattr(club, "outreach_stage", ""), "unaware")
    if health is UNFETCHED:
        health = getattr(club, "health", None)
    derived = getattr(health, "stage", None)
    best, source = hand, "hand"
    if derived in LADDER_RANK and LADDER_RANK[derived] > LADDER_RANK[best]:
        best, source = derived, "derived"
    return {"stage": best, "label": LADDER_LABELS[best], "source": source}


def ladder_counts():
    """``[{stage, label, source, clubs}]`` in ladder order -- how many clubs are on each rung.

    Two queries whatever the number of clubs: one for the clubs and one for the rollups, rather
    than a join or a fetch per row. A club with no rollup is passed ``None`` explicitly, which is
    what keeps it two -- see :func:`ladder_position`.
    """
    from auctions.models import Club

    stages = dict(ClubHealth.objects.values_list("club_id", "stage"))
    counts = dict.fromkeys(LADDER_RANK, 0)
    for club in Club.objects.all().only("id", "outreach_stage").iterator(chunk_size=500):
        stage = stages.get(club.pk)
        rung = ladder_position(club, _StageOnly(stage) if stage else None)
        counts[rung["stage"]] += 1
    return [{"stage": key, "label": label, "source": half, "clubs": counts[key]} for key, label, half in LADDER]


#: How many months of ladder history the dashboard reads back.  Twelve, because the question 8f
#: exists to answer -- is the outreach working -- is a year-over-year one, and a longer window on a
#: chart of a few dozen clubs is a wider chart rather than a better answer.
LADDER_HISTORY_MONTHS = 12


class ClubLadderSnapshot(models.Model):
    """How many clubs stood on each rung, on the first of one month.

    The one part of phase 8f that is code.  Everything else in the outreach loop is a person with a
    queue: the email is drafted per club and sent by hand (settled twice), and marking a club
    contacted already writes ``Club.date_contacted`` and a stall reason.  What was missing is the
    only way to tell whether any of that is working, which is the ladder **as a trend** --
    "unaware -> aware -> trial -> ran a real auction" is a story about movement and a single column
    of counts cannot show movement at all.

    It needs its own table because :class:`ClubHealth` cannot answer it.  That rollup is a
    ``OneToOneField`` recomputed nightly from scratch, so it holds only today: the moment a club
    moves up a rung, every trace of where it used to be is gone.  This is the cheapest possible fix
    -- one row per rung per month, so about a hundred rows a year whatever happens to the number of
    clubs, and nothing recomputes or purges it.

    Monthly rather than nightly for the same reason ``ClubHealth`` is nightly: nothing this
    measures moves faster.  Outreach is somebody writing letters, and a club replies in weeks.
    """

    #: Midnight on the first of the month this snapshot describes -- the key, so a task that runs
    #: every night writes the month's row once and then updates it in place.
    month = models.DateField(db_index=True)
    stage = models.CharField(max_length=20, db_index=True)
    clubs = models.PositiveIntegerField(default=0)
    computed_on = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["month", "stage"], name="one_ladder_snapshot_per_month_stage")]
        ordering = ["-month", "stage"]
        verbose_name = "Club ladder snapshot"

    def __str__(self):
        return f"{self.month:%b %Y}: {self.clubs} on {self.stage}"


def snapshot_ladder(when=None):
    """Record this month's ladder counts, replacing the month's row if it is already there.

    Idempotent on purpose, and keyed on the month rather than on the day, so the nightly task can
    call it unconditionally: every run after the first in a month overwrites that month's row with
    a fresher count, and the row is final once the month is over.  A task that had to know whether
    it was the first of the month would silently record nothing on the month it was deployed.
    """
    from auctions.models import ClubLadderSnapshot as Snapshot

    now = when or timezone.now()
    month = now.date().replace(day=1)
    for row in ladder_counts():
        Snapshot.objects.update_or_create(month=month, stage=row["stage"], defaults={"clubs": row["clubs"]})
    return month


def ladder_history(months=LADDER_HISTORY_MONTHS):
    """``{"months": [...], "rows": [{stage, label, source, counts: [...]}]}`` -- the ladder as a trend.

    Shaped for a table rather than returned raw: a rung is a row and a month is a column, and a
    month with no snapshot is a gap in that row rather than a zero.  A zero would say every club
    left that rung; the truth is that nobody was looking.
    """
    from auctions.models import ClubLadderSnapshot as Snapshot

    found = sorted({row.month for row in Snapshot.objects.only("month")}, reverse=True)[:months]
    if not found:
        return {"months": [], "rows": []}
    found = sorted(found)
    counts = {(row.month, row.stage): row.clubs for row in Snapshot.objects.filter(month__in=found)}
    return {
        "months": found,
        "rows": [
            {
                "stage": key,
                "label": label,
                "source": half,
                "counts": [counts.get((month, key)) for month in found],
            }
            for key, label, half in LADDER
        ],
    }


class _StageOnly:
    """Just enough of a ClubHealth for ladder_position, so ladder_counts needs no second fetch."""

    def __init__(self, stage):
        self.stage = stage
