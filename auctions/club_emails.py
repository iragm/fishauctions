"""Three emails to clubs that already use the site, each once per club ever.

* ``monthly_invite`` (:func:`monthly_invite_due`): a club ran a big auction here, one with "annual" in
  its title or a gross over :data:`BIG_GROSS`, and it ended at least :data:`AFTER_AUCTION` ago (and no
  more than :data:`LOOKBACK`, so last year's annual auction counts). It goes to that auction's creator
  and asks whether the club's monthly meeting auctions could run here too. Three buttons, each to
  :class:`auctions.views.ClubCadenceAnswerView`, which records the club's answer in
  ``Club.auction_cadence``: "I'm in" (then on to copying the auction), "my club doesn't have monthly
  auctions" and "not interested in software for them". Any answer stops this email; so does a club
  that already runs auctions about monthly here (:func:`runs_monthly`), which gets the next one instead.
* ``club_tools`` (:func:`club_tools_due`): a club that made :data:`MONTHLY_AUCTIONS` real auctions within
  :data:`MONTHLY_WINDOW`. It goes to the creator of the club's latest auction and to that auction's
  admins, and lists the club management features (:data:`FEATURES`), each with its help guide and the
  club's own setup page. A club that answered "no monthly auctions" or "not interested" still gets it
  once it really does make that many: the answer stops only the question.
* ``thanks`` (:func:`thanks_due`): a club that has finished :data:`THANKS_AFTER` real auctions here, one
  within :data:`THANKS_ACTIVE`. A thank you to its latest auction's creator that asks them to tell
  another club about the site. No link and no referral code: it's a nudge, not a program.

The claim is a ``ClubEmail`` row per club and kind, inserted before sending. Each person it goes to also
gets a ``FeatureEmail`` row of the same kind, so ``feature_email_results`` reports them beside the
feature emails, and the opt-out watch (:func:`auctions.feature_emails.check_reactions`) counts them.
Links carry ``?src=email_<kind>``. Nobody gets one who opted out of emails about features, unsubscribed,
bounced or has no usable address; a person who already had that kind for another club, or any feature
email in the last :data:`RECENT`, is skipped. A club nobody could be sent to today isn't claimed.
Nothing goes before ``CLUB_EMAILS_START`` (blank means no wait), while feature emails are paused, or
past :data:`PER_RUN` clubs in a day. Only club staff get one: someone who has left the club (or holds no
club permission) is dropped from the recipients, and a club nobody is left to send to isn't claimed.
``club_tools`` goes only to people who can edit the club. A "monthly" answer is only recorded from someone
with the club's ``permission_view``.
"""

from __future__ import annotations

import datetime
import logging
import re

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.contrib.sites.models import Site
from django.core import signing
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone
from post_office import mail

from auctions.club_health import is_test_auction
from auctions.feature_emails import _eligible, add_src, check_reactions, paused

logger = logging.getLogger(__name__)

BIG_GROSS = 3000
AFTER_AUCTION = datetime.timedelta(weeks=3)
LOOKBACK = datetime.timedelta(days=400)
MONTHLY_AUCTIONS = 3
MONTHLY_WINDOW = datetime.timedelta(days=92)
THANKS_AFTER = 10
#: A club thanked must still be using the site: a finished auction this recent.
THANKS_ACTIVE = datetime.timedelta(days=180)
#: Clubs per day, across every kind: one email per club is a few people at most.
PER_RUN = 25
#: Someone who had any feature email this recently waits a day or two: never two in one morning.
RECENT = datetime.timedelta(days=7)
ANNUAL = re.compile(r"\bannual\b", re.IGNORECASE)
TOKEN_SALT = "club-cadence"
TOKEN_MAX_AGE = 60 * 60 * 24 * 365

ANSWERS = {
    "monthly": "I'm in, create my monthly auction now",
    "no_monthly": "My club doesn't have monthly auctions",
    "not_interested": "Not interested in using software for my monthly auctions",
}

#: ``(name, what it does, help guide slug, the club's setup page)``, in the order the email lists them.
FEATURES = (
    (
        "Membership and dues",
        "Members renew and pay dues online, with reminders sent for you.",
        "club-membership",
        "club_membership_settings",
    ),
    ("Club emails", "Welcome, renewal and expiration emails go out on their own.", "club-email", "club_email_settings"),
    (
        "Discord",
        "New auctions and events post to your server, and member roles stay in sync.",
        "club-events",
        "club_discord_config",
    ),
    (
        "Google Calendar",
        "Your auctions and meetings appear on a shared calendar.",
        "club-events",
        "club_google_calendar_config",
    ),
    (
        "Your club's website",
        "Your next auction and events show on your own site without anyone updating it.",
        "club-events",
        "club_website_integration",
    ),
    (
        "Breeder award program",
        "Lots bred by the seller count toward awards automatically.",
        "breeder-award-programs",
        "club_bap_settings",
    ),
    (
        "Donation tracking",
        "Ask vendors for donations and keep track of who said yes.",
        "club-donations",
        "club_donation_settings",
    ),
)


def start_date() -> datetime.date | None:
    """``CLUB_EMAILS_START``, or None for no wait. A value that isn't a date turns the emails off."""
    text = (getattr(settings, "CLUB_EMAILS_START", "") or "").strip()
    if not text:
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        logger.error("CLUB_EMAILS_START %r isn't a date; club emails are off", text)
        return datetime.date.max


def answer_token(club, user, auction=None) -> str:
    """Signs who answers for which club, and the auction "I'm in" copies."""
    return signing.dumps(
        {"club": club.pk, "user": user.pk, "auction": auction.pk if auction else None}, salt=TOKEN_SALT
    )


def read_token(token: str) -> dict | None:
    try:
        return signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
    except signing.BadSignature:
        return None


def _ended_before(moment):
    """Auctions that had wound down by ``moment``: their end, or their start when there's no end."""
    return Q(date_end__lte=moment) | Q(date_end__isnull=True, date_start__lte=moment)


def _real(auctions):
    return [auction for auction in auctions if not is_test_auction(auction)]


def runs_monthly(club, now) -> bool:
    """:data:`MONTHLY_AUCTIONS` real auctions made within the last :data:`MONTHLY_WINDOW`."""
    from auctions.models import Auction

    recent = Auction.objects.filter(club=club, is_deleted=False, date_posted__gte=now - MONTHLY_WINDOW)
    return len(_real(recent)) >= MONTHLY_AUCTIONS


def is_big(auction) -> bool:
    return bool(ANNUAL.search(auction.title or "")) or auction.gross > BIG_GROSS


def monthly_invite_due(now=None):
    """``(club, auction)`` for each club owed the monthly-auction email, its latest big auction."""
    from auctions.models import Auction, ClubEmail

    now = now or timezone.now()
    asked = set(ClubEmail.objects.filter(kind="monthly_invite").values_list("club_id", flat=True))
    auctions = (
        Auction.objects.filter(
            _ended_before(now - AFTER_AUCTION),
            is_deleted=False,
            club__isnull=False,
            club__auction_cadence="",
            created_by__isnull=False,
            date_start__gte=now - LOOKBACK,
        )
        .exclude(club_id__in=asked)
        .select_related("club", "created_by__userdata")
        .order_by("-date_start")
    )
    seen = set()
    for auction in auctions:
        if auction.club_id in seen or is_test_auction(auction) or not is_big(auction):
            continue
        seen.add(auction.club_id)
        if not runs_monthly(auction.club, now):
            yield auction.club, auction


def club_tools_due(now=None):
    """``(club, latest auction)`` for each club that runs auctions about monthly and hasn't had the list."""
    from auctions.models import Auction, ClubEmail

    now = now or timezone.now()
    told = set(ClubEmail.objects.filter(kind="club_tools").values_list("club_id", flat=True))
    recent = (
        Auction.objects.filter(is_deleted=False, club__isnull=False, date_posted__gte=now - MONTHLY_WINDOW)
        .exclude(club_id__in=told)
        .select_related("club", "created_by__userdata")
        .order_by("-date_posted")
    )
    by_club: dict[int, list] = {}
    for auction in _real(recent):
        by_club.setdefault(auction.club_id, []).append(auction)
    for auctions in by_club.values():
        if len(auctions) >= MONTHLY_AUCTIONS:
            yield auctions[0].club, auctions[0]


def club_tools_recipients(auction) -> list:
    """The auction's creator, then its admins, once each, keeping only those who can edit the club."""
    from auctions.models import AuctionTOS
    from auctions.views.base import check_club_permission

    people = [auction.created_by] if auction.created_by else []
    admins = AuctionTOS.objects.filter(auction=auction, is_admin=True, user__isnull=False).select_related(
        "user__userdata"
    )
    for tos in admins:
        if tos.user not in people:
            people.append(tos.user)
    return [person for person in people if check_club_permission(person, auction.club, "permission_edit_club")]


def _url(domain, path, kind):
    return add_src(f"https://{domain}{path}", f"email_{kind}")


def monthly_invite_context(club, auction, user, domain) -> dict:
    token = answer_token(club, user, auction)
    page = reverse("club_cadence_answer", kwargs={"slug": club.slug, "token": token})
    buttons = [
        {"label": label, "url": _url(domain, f"{page}?answer={answer}", "monthly_invite")}
        for answer, label in ANSWERS.items()
    ]
    return {
        "subject": f"Monthly auctions for {club.name}",
        "club": club,
        "auction": auction,
        "buttons": buttons,
    }


def club_tools_context(club, auction, user, domain) -> dict:
    features = [
        {
            "name": name,
            "does": does,
            "help_url": _url(domain, reverse("help_guide", kwargs={"slug": guide}), "club_tools"),
            "setup_url": _url(domain, reverse(page, kwargs={"slug": club.slug}), "club_tools"),
        }
        for name, does, guide, page in FEATURES
    ]
    return {"subject": f"Club tools for {club.name}", "club": club, "auction": auction, "features": features}


def thanks_due(now=None):
    """``(club, latest auction)`` for each club that has finished :data:`THANKS_AFTER` real auctions here,
    one of them within :data:`THANKS_ACTIVE`, and hasn't been thanked."""
    from auctions.models import Auction, ClubEmail

    now = now or timezone.now()
    thanked = ClubEmail.objects.filter(kind="thanks").values_list("club_id", flat=True)
    finished = Auction.objects.filter(
        _ended_before(now), is_deleted=False, club__isnull=False, created_by__isnull=False
    )
    busy = (
        finished.filter(club_id__in=finished.filter(date_start__gte=now - THANKS_ACTIVE).values("club_id"))
        .exclude(club_id__in=thanked)
        .values("club_id")
        .annotate(made=Count("pk"))
        .filter(made__gte=THANKS_AFTER)
        .values_list("club_id", flat=True)
    )
    for club_id in list(busy):
        auctions = _real(
            finished.filter(club_id=club_id).select_related("club", "created_by__userdata").order_by("-date_start")
        )
        if len(auctions) >= THANKS_AFTER and auctions[0].date_start >= now - THANKS_ACTIVE:
            yield auctions[0].club, auctions[0]


def thanks_context(club, auction, user, domain) -> dict:
    return {"subject": f"Thanks, {club.name}", "club": club, "auction": auction}


KINDS = {
    "monthly_invite": (monthly_invite_due, lambda auction: [auction.created_by], monthly_invite_context),
    "club_tools": (club_tools_due, club_tools_recipients, club_tools_context),
    "thanks": (thanks_due, lambda auction: [auction.created_by], thanks_context),
}


def _send_to_club(kind, club, auction, people, build, now) -> int | None:
    """Claim the club, then send to each eligible person. Returns how many went, or None when nobody
    could get it today (the club is left unclaimed, to try again tomorrow)."""
    from django.db.models import Q

    from auctions.models import ClubEmail, ClubMember, FeatureEmail
    from auctions.views.base import CLUB_PERMISSION_FIELDS

    any_permission = Q()
    for field in CLUB_PERMISSION_FIELDS:
        any_permission |= Q(**{field: True})
    staff = set(
        ClubMember.objects.filter(club=club, user__in=people, is_deleted=False)
        .filter(any_permission)
        .values_list("user_id", flat=True)
    )
    people = [person for person in people if person.pk in staff]
    if not people:
        return None
    already = set(FeatureEmail.objects.filter(kind=kind, user__in=people).values_list("user_id", flat=True))
    recently = set(
        FeatureEmail.objects.filter(user__in=people, sent_on__gte=now - RECENT).values_list("user_id", flat=True)
    )
    people = [person for person in people if person.pk not in already and _eligible(person, now, recently)]
    if not people:
        return None
    try:
        with transaction.atomic():
            claim = ClubEmail.objects.create(club=club, kind=kind)
    except IntegrityError:
        return None
    domain = Site.objects.get_current().domain
    sent = 0
    for person in people:
        try:
            with transaction.atomic():
                row = FeatureEmail.objects.create(user=person, kind=kind, about=club.slug[:100])
        except IntegrityError:
            continue
        context = {
            **build(club, auction, person, domain),
            "user": person,
            "domain": domain,
            "unsubscribe": str(person.userdata.unsubscribe_link),
        }
        try:
            mail.send(person.email, template=f"club_email_{kind}", context=context)
        except SoftTimeLimitExceeded:
            row.delete()
            if not sent:
                claim.delete()
            raise
        except Exception:
            logger.exception("club email %s to user %s failed", kind, person.pk)
            row.delete()
            continue
        sent += 1
    if not sent:
        claim.delete()
        return None
    return sent


def send_club_emails(now=None) -> int:
    """Send every club email that's due, up to :data:`PER_RUN` clubs. Returns how many emails went."""
    now = now or timezone.now()
    start = start_date()
    if (start and timezone.localdate(now) < start) or paused() or check_reactions(now):
        return 0
    clubs = sent = 0
    for kind, (due, recipients, build) in KINDS.items():
        for club, auction in due(now):
            if clubs >= PER_RUN:
                return sent
            went = _send_to_club(kind, club, auction, recipients(auction), build, now)
            if went is not None:
                clubs += 1
                sent += went
    return sent


def record_answer(club, user, answer: str) -> bool:
    """Store the club's answer to the monthly-auction email. False for an answer that isn't one."""
    from auctions.models import ClubHistory
    from auctions.views.base import check_club_permission

    if answer not in ANSWERS or not check_club_permission(user, club, "permission_view"):
        return False
    if club.auction_cadence != answer:
        club.auction_cadence = answer
        club.save(update_fields=["auction_cadence"])
        ClubHistory.objects.create(
            club=club,
            user=user,
            action=f"Answered the monthly-auction email: {ANSWERS[answer]}",
            applies_to="SETTINGS",
        )
    return True
