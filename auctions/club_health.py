"""Whether a club is still running auctions here, judged against its own cadence.

``Club.active`` is a hand-set flag for dissolved clubs, not a health signal.

The threshold is the club's own median gap between auctions: :attr:`ClubHealth.overdue_ratio` is
1.0 when due and 2.0 after missing one. Clubs with fewer than :data:`MIN_AUCTIONS_FOR_CADENCE` real
auctions are judged on stage instead. Test auctions are counted separately throughout.

:func:`due_for_checkin` is the outreach queue, worst first. Contacting a club writes
``Club.date_contacted`` and keeps it off the queue for :data:`CONTACT_COOLDOWN_DAYS`.

``ClubHealth`` is a nightly-rewritten table so clubs can be sorted by it; nothing hand-set can live
on it. The hand-set half is ``Club.outreach_stage``, also the map gate. :func:`ladder_position`
combines both and reports which half answered.
"""

from __future__ import annotations

import logging
import re
import statistics

from django.db import models
from django.utils import timezone

logger = logging.getLogger(__name__)

# AuctionEditForm's pattern, widened to match titles with spaces. A plain word boundary would catch
# "protest" and "contest".
_TEST_WORDS = "test|mock|trial|example|demo"
TEST_AUCTION_PATTERN = re.compile(
    rf"^({_TEST_WORDS})([-_\s]|$)|([-_\s])({_TEST_WORDS})([-_\s]|$)",
    re.IGNORECASE,
)
MIN_AUCTIONS_FOR_CADENCE = 3
# 1.0 is one median gap since the last auction; due means having missed one.
OVERDUE_RATIO_DUE = 2.0
# Missed three gaps.
DORMANT_RATIO = 4.0
# For clubs with no cadence, judged on elapsed time.
NO_CADENCE_DORMANT_DAYS = 400
# How long a club stays out of the queue after somebody contacts it.
CONTACT_COOLDOWN_DAYS = 90
# A gap this long is a restart, and would skew the median.
MAX_GAP_DAYS_FOR_CADENCE = 730

STAGE_CHOICES = (
    # A club record with nothing behind it; every discovered club starts here.
    ("empty", "No auctions"),
    # A ClubMember row or UserData.club points at it, but no auctions.
    ("aware", "Members here, no auctions"),
    # Tried it, never ran one for real.
    ("trial", "Test auctions only"),
    # One real auction, no cadence to judge yet.
    ("new", "First real auction"),
    ("active", "Running to its own schedule"),
    ("slipping", "Late against its own schedule"),
    ("dormant", "Stopped"),
)

# Club management tools a club has used, as (label, check).
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
    """A derived rollup of one club's use of the site, written by :func:`compute_club_health`. Nothing
    here is hand-maintained.
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
        """Whether there's anybody to contact."""
        return bool(self.club.contact_email or self.club.contact_email_member_id)


def is_test_auction(auction) -> bool:
    """Whether an auction is named like somebody trying the site out."""
    return bool(TEST_AUCTION_PATTERN.search(auction.slug or "") or TEST_AUCTION_PATTERN.search(auction.title or ""))


def median_gap(dates) -> float | None:
    """The club's median gap between auctions in days, or None without enough history."""
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
    """``(stage, reason)``: where the club is, and why it's in the queue.

    The reason names the numbers for whoever reads the queue. ``people_here`` separates ``aware`` from
    ``empty``.
    """
    if not real_auctions:
        if test_auctions:
            return "trial", f"Set the site up and ran {test_auctions} test auction(s), never a real one"
        if people_here:
            return "aware", f"{people_here} person/people here say they are in this club, no auctions yet"
        return "empty", "Club exists here but has never had an auction"
    if ratio is None:
        # No cadence yet, only elapsed time.
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
        # Removed members don't count.
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
    """How many people on this site say they're in this club: ClubMember rows plus ``UserData.club``, as
    distinct users, plus members without accounts.
    """
    from auctions.models import ClubMember, UserData

    users = set(
        ClubMember.objects.filter(club=club, is_deleted=False, user__isnull=False).values_list("user_id", flat=True)
    )
    users.update(UserData.objects.filter(club=club).values_list("user_id", flat=True))
    without_accounts = ClubMember.objects.filter(club=club, is_deleted=False, user__isnull=True).count()
    return len(users) + without_accounts


def _safely(check, club, counts):
    try:
        return bool(check(club, counts))
    except Exception:
        logger.exception("club health tool check failed for %s", club)
        return False


def _queue_decision(club, stage, reason) -> tuple[bool, str]:
    """Whether this club belongs on the outreach queue; recently contacted clubs don't."""
    if stage not in ("empty", "aware", "trial", "slipping", "dormant"):
        return False, ""
    if not club.active:
        # Dissolved.
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
    """The outreach queue, worst first, ordered by stage rather than ratio."""
    order = {"trial": 0, "aware": 1, "empty": 2, "slipping": 3, "dormant": 4}
    # Sort in Python before slicing: Meta.ordering sorts NULL ratios (trial, empty) last on MariaDB.
    rows = ClubHealth.objects.filter(due_for_checkin=True).select_related("club")
    ordered = sorted(rows, key=lambda row: (order.get(row.stage, 9), -(row.overdue_ratio or 0)))
    return ordered[:limit]


# The whole ladder in order, as (key, label, which half said so). The first three rungs are
# Club.outreach_stage; the rest come from classify().
#
# `aware` sits above `listed`, and `empty` isn't a rung: every prospect starts there. `dormant` and
# `slipping` sit above `new` (they ran real auctions) and below `active`.
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
# Club.outreach_stage -> rung.
HAND_RUNGS = {"prospect": "unaware", "contacted": "contacted", "listed": "listed"}
# Sentinel for "not looked up yet", distinct from None ("no rollup").
UNFETCHED = object()


def ladder_position(club, health=UNFETCHED) -> dict:
    """``{stage, label, source}``: the furthest rung this club has reached, and whether ``"hand"`` or
    ``"derived"`` said so.

    ``health`` is a rollup, ``None`` (no rollup), or unset (fetch it). Loops should pass ``None``
    explicitly to avoid a query per club.
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
    """``[{stage, label, source, clubs}]`` in ladder order, in two queries."""
    from auctions.models import Club

    stages = dict(ClubHealth.objects.values_list("club_id", "stage"))
    counts = dict.fromkeys(LADDER_RANK, 0)
    for club in Club.objects.all().only("id", "outreach_stage").iterator(chunk_size=500):
        stage = stages.get(club.pk)
        rung = ladder_position(club, _StageOnly(stage) if stage else None)
        counts[rung["stage"]] += 1
    return [{"stage": key, "label": label, "source": half, "clubs": counts[key]} for key, label, half in LADDER]


#: Months of ladder history shown; outreach progress is a year-over-year question.
LADDER_HISTORY_MONTHS = 12


class ClubLadderSnapshot(models.Model):
    """How many clubs stood on each rung, on the first of one month.

    ``ClubHealth`` only holds today, so this is the ladder as a trend: one row per rung per month,
    never purged.
    """

    #: The first of the month; the nightly task updates the month's row in place.
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
    """Record this month's ladder counts, replacing the month's row if present, so it's safe to run
    nightly.
    """
    from auctions.models import ClubLadderSnapshot as Snapshot

    now = when or timezone.now()
    month = now.date().replace(day=1)
    for row in ladder_counts():
        Snapshot.objects.update_or_create(month=month, stage=row["stage"], defaults={"clubs": row["clubs"]})
    return month


def ladder_history(months=LADDER_HISTORY_MONTHS):
    """``{"months": [...], "rows": [{stage, label, source, counts: [...]}]}``: the ladder as a trend.

    A month with no snapshot is a gap (None), not a zero.
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
    def __init__(self, stage):
        self.stage = stage
