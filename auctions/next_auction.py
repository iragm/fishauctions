"""Asking an auction's creator to run the next one, automatically, at about the time it would be due.

Two emails, both to ``Auction.created_by`` and both one sentence and one button that copies the auction
(``/auctions/new/?copy=<slug>``):

* ``next_auction`` (:func:`send_next_auction_emails`). After a **real** auction, it goes when the next one
  should be getting set up: :func:`expected_gap` after the auction wound down, less a lead of half that gap
  (at most :data:`MAX_LEAD`). The gap is the club's own median gap between real auctions
  (:func:`auctions.club_health.median_gap`), or the creator's when there's no club, or
  :data:`DEFAULT_GAP` (three weeks) when there isn't enough history to tell. After a **test** auction
  (:func:`auctions.club_health.is_test_auction`) it goes :data:`TEST_NUDGE_AFTER` after the test was
  made, unless the club or creator has run a real one. Either way it's skipped when the club (or the
  creator, with no club) has made any auction since. Only auctions made after migration 0527 qualify:
  that migration marked every existing one as asked.
* ``auction_anniversary`` (:func:`send_anniversary_emails`): :data:`ANNIVERSARY_LEAD` before a real
  auction's first anniversary, unless an auction with the same name (years, months and numbers aside,
  :func:`title_stem`) has been made since. It looks at auctions whose anniversary falls inside
  :data:`ANNIVERSARY_WINDOW`, so an old auction never sends late. Auctions from before migration 0527 do
  qualify: the anniversary of one held a year ago is the point.

Sending one marks both flags, so an auction never gets the two emails; deciding not to send marks only
its own. A flag is claimed with a conditional update before sending, so two overlapping runs can't both
send. Of a club's auctions within :data:`CLUSTER` of each other, only the earliest gets an anniversary
email. Nobody is emailed who has
unsubscribed or has no usable address; their flag is set anyway.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.contrib.sites.models import Site
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Q
from django.utils import timezone
from post_office import mail

from auctions.club_health import is_test_auction, median_gap
from auctions.models import Auction
from auctions.notifications import notify_user

logger = logging.getLogger(__name__)

#: The gap between auctions assumed when nothing says otherwise.
DEFAULT_GAP = timedelta(weeks=3)
#: The furthest ahead of the expected next auction the email goes.
MAX_LEAD = timedelta(weeks=10)
#: After a test auction, how long before asking whether they'll run a real one.
TEST_NUDGE_AFTER = timedelta(days=7)
#: Past its due time by this much (the task was down, say), an email is dropped rather than sent late.
LATE_LIMIT = timedelta(days=14)
#: Auctions made longer ago than this are never looked at.
LOOKBACK = timedelta(days=800)
ANNIVERSARY_LEAD = timedelta(weeks=10)
ANNIVERSARY_WINDOW = timedelta(days=14)
YEAR = timedelta(days=365)
#: Auctions this close together share one anniversary email, the earliest one's.
CLUSTER = timedelta(days=30)

_MONTHS = (
    "january february march april may june july august september october november december "
    "jan feb mar apr jun jul aug sep sept oct nov dec spring summer fall autumn winter"
).split()
_STEM_NOISE = re.compile(r"\b(?:" + "|".join(_MONTHS) + r")\b|\d+(?:st|nd|rd|th)?\b|[^\w\s]|_")


def title_stem(title: str) -> str:
    """An auction's name without its dates: ``"TFCB 2026 annual auction"`` -> ``"tfcb annual auction"``."""
    return " ".join(_STEM_NOISE.sub(" ", (title or "").lower()).split())


def _same_owner(auction):
    """The auctions that count as this one's club's, or its creator's when it has no club."""
    if auction.club_id:
        return Q(club_id=auction.club_id)
    return Q(created_by_id=auction.created_by_id, club__isnull=True)


def _siblings(auction):
    return Auction.objects.filter(_same_owner(auction), is_deleted=False).exclude(pk=auction.pk)


def expected_gap(auction, cache=None) -> timedelta:
    """The club's (or creator's) median gap between real auctions, or :data:`DEFAULT_GAP`. ``cache`` (a
    dict) keeps one answer per club or creator for a run."""
    key = ("club", auction.club_id) if auction.club_id else ("user", auction.created_by_id)
    if cache is not None and key in cache:
        return cache[key]
    dates = [
        other.date_start
        for other in Auction.objects.filter(_same_owner(auction), is_deleted=False).only("slug", "title", "date_start")
        if not is_test_auction(other)
    ]
    days = median_gap(dates)
    gap = timedelta(days=days) if days else DEFAULT_GAP
    if cache is not None:
        cache[key] = gap
    return gap


def next_auction_due(auction, cache=None):
    """When the ``next_auction`` email should go, or ``None`` when it shouldn't go at all."""
    if is_test_auction(auction):
        return auction.date_posted + TEST_NUDGE_AFTER
    wound_down = auction.wind_down_time
    if not wound_down:
        return None
    gap = expected_gap(auction, cache)
    return wound_down + gap - min(MAX_LEAD, gap / 2)


def should_skip(auction) -> bool:
    """Whether the club or creator has already moved on, so there's nothing to ask."""
    siblings = _siblings(auction)
    if siblings.filter(date_posted__gt=auction.date_posted).exists():
        return True
    if is_test_auction(auction):
        # Asking somebody who already runs real auctions to "run one for real" makes no sense: theirs
        # under any club count, so a club-less test followed by a club auction isn't asked.
        theirs = Auction.objects.filter(Q(created_by_id=auction.created_by_id) | _same_owner(auction), is_deleted=False)
        return any(not is_test_auction(other) for other in theirs.exclude(pk=auction.pk).only("slug", "title"))
    return False


def _deliverable(user) -> bool:
    if not user or not user.is_active or user.userdata.has_unsubscribed:
        return False
    try:
        validate_email((user.email or "").strip())
    except ValidationError:
        return False
    return True


def _claim(auction, flag: str) -> bool:
    """Set ``flag``; True only for the run that flipped it."""
    return bool(Auction.objects.filter(pk=auction.pk, **{flag: False}).update(**{flag: True}))


def _sent(auction):
    """An email went: the auction won't get the other one too."""
    Auction.objects.filter(pk=auction.pk).update(next_auction_email_sent=True, anniversary_email_sent=True)


def _send(auction, template: str, title: str, context: dict):
    domain = Site.objects.get_current().domain
    user = auction.created_by
    context = {
        "auction": auction,
        "domain": domain,
        "unsubscribe": str(user.userdata.unsubscribe_link),
        **context,
    }
    notify_user(
        user,
        category="auction_admin",
        title=title,
        body=f"Tap to copy {auction}.",
        url=f"https://{domain}/auctions/new/?copy={auction.slug}",
        send_email=lambda: mail.send(user.email, template=template, context=context),
        auction_pk=auction.pk,
    )


def next_auction_candidates(now=None):
    now = now or timezone.now()
    return (
        Auction.objects.filter(
            is_deleted=False,
            next_auction_email_sent=False,
            created_by__isnull=False,
            date_posted__gte=now - LOOKBACK,
        )
        .select_related("created_by__userdata", "club")
        .order_by("pk")
    )


def send_next_auction_emails(now=None) -> int:
    """Send every ``next_auction`` email that is due. Returns how many went."""
    now = now or timezone.now()
    sent = 0
    gaps = {}
    for auction in next_auction_candidates(now):
        try:
            due = next_auction_due(auction, gaps)
            if due is None:
                if now > auction.date_posted + LATE_LIMIT:
                    # No date to work from (an online auction with no end): never will be, so stop looking.
                    _claim(auction, "next_auction_email_sent")
                continue
            if now < due:
                continue
            if not _claim(auction, "next_auction_email_sent"):
                continue
            if now > due + LATE_LIMIT or should_skip(auction) or not _deliverable(auction.created_by):
                continue
            test = is_test_auction(auction)
            _send(
                auction,
                "next_auction",
                "Ready to run a real auction?" if test else "Ready for your next auction?",
                {"is_test": test},
            )
            _sent(auction)
            sent += 1
        except Exception:
            logger.exception("next auction email failed for auction %s", auction.pk)
    return sent


def anniversary_candidates(now=None):
    """Auctions whose first anniversary is :data:`ANNIVERSARY_LEAD` away, give or take the window."""
    now = now or timezone.now()
    latest = now - YEAR + ANNIVERSARY_LEAD
    return (
        Auction.objects.filter(
            is_deleted=False,
            anniversary_email_sent=False,
            created_by__isnull=False,
            date_start__lte=latest,
            date_start__gt=latest - ANNIVERSARY_WINDOW,
        )
        .select_related("created_by__userdata", "club")
        .order_by("date_start")
    )


def has_successor(auction) -> bool:
    """Whether an auction with the same name, dates aside, has been made since this one."""
    stem = title_stem(auction.title)
    if not stem:
        return False
    later = _siblings(auction).filter(date_posted__gt=auction.date_posted).only("title")
    return any(title_stem(other.title) == stem for other in later)


def had_one_just_before(auction) -> bool:
    """Whether the club or creator ran a real auction in the :data:`CLUSTER` before this one: then that one's
    anniversary email covers both, so a club with a busy month hears once, not once per auction."""
    earlier = (
        _siblings(auction)
        .filter(date_start__lt=auction.date_start, date_start__gte=auction.date_start - CLUSTER)
        .only("slug", "title")
    )
    return any(not is_test_auction(other) for other in earlier)


def send_anniversary_emails(now=None) -> int:
    """Send every ``auction_anniversary`` email that is due."""
    now = now or timezone.now()
    sent = 0
    for auction in anniversary_candidates(now):
        try:
            if not _claim(auction, "anniversary_email_sent"):
                continue
            if (
                is_test_auction(auction)
                or has_successor(auction)
                or had_one_just_before(auction)
                or not _deliverable(auction.created_by)
            ):
                continue
            _send(auction, "auction_anniversary", f"{auction} was almost a year ago", {})
            _sent(auction)
            sent += 1
        except Exception:
            logger.exception("anniversary email failed for auction %s", auction.pk)
    return sent
