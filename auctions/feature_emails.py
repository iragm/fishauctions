"""Telling people about a part of the site they aren't using, once per feature, a few times a year at most.

Each :class:`Kind` is one email: who it's for, the one sentence and button, and where the button goes
(with ``?src=email_<kind>``, so the click lands in ``PageView.source``). Two families:

* **Moments** follow something that just happened: an auction ended with no card checkout, a club with
  members here takes no dues, a club auction with no Discord or Calendar, seller-bred lots with no awards
  program, 50+ lots typed in by hand, a club with events and no website embed. They go within
  :data:`MOMENT_WINDOW` of the moment, to the person who can act on it (the creator, or club staff with
  the right permission).
* **Re-engagement** goes to people who have gone quiet: last seen between :data:`QUIET_AFTER` and
  :data:`QUIET_LIMIT` ago, by the latest of their last sign-in or activity, auction joined and bid. The
  speaker directory (club staff), the library and the command palette (everyone), and "does your club run
  an auction?" (members of a club with people here but no auctions). Those quiet kinds go once per quiet
  spell: not again while the person stays quiet after one of them.
* **Announcements**: once ``APP_STORE_URL`` is set, the iPhone app, to everyone who joined an in-person
  auction in the past year, quiet or not. Its button goes straight to the App Store, so its clicks
  aren't counted.

Rules, all here: nothing before ``FEATURE_EMAILS_START``; each kind once per person ever
(``FeatureEmail``'s unique constraint, claimed before sending); one per person per :data:`SPACING`;
nobody unsubscribed, opted out of ``email_me_about_new_features``, bounced, inactive or without a usable
address. A day's sends are capped by :func:`daily_cap`, which ramps from :data:`RAMP_START` a day. If
more than :data:`MAX_OPT_OUT_RATE` of the last :data:`WATCH_WINDOW`'s recipients opted out, or
:data:`MAX_BOUNCE_RATE` bounced, a ``FeatureEmailPause`` row stops everything until somebody deletes it.
No holdout group: there aren't enough people for one to say anything. A run stops itself after
:data:`RUN_SECONDS`, well inside the task's soft time limit; the next day carries on.
"""

from __future__ import annotations

import datetime
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sites.models import Site
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, OuterRef, Q, Subquery
from django.urls import reverse
from django.utils import timezone
from post_office import mail

from auctions.next_auction import _deliverable

logger = logging.getLogger(__name__)

MOMENT_WINDOW = datetime.timedelta(days=14)
QUIET_AFTER = datetime.timedelta(days=21)
QUIET_LIMIT = datetime.timedelta(days=730)
SPACING = datetime.timedelta(weeks=8)
RAMP_START = 200
RAMP_MOST = 5000
WATCH_WINDOW = datetime.timedelta(days=14)
#: Below this many recent sends, the rates below mean nothing.
WATCH_MIN_SENT = 50
MAX_OPT_OUT_RATE = 0.02
MAX_BOUNCE_RATE = 0.03
HAND_ENTERED_LOTS = 50
SELLER_BRED_LOTS = 3
DUES_MIN_MEMBERS = 10
AWARE_CLUB_MEMBERS = 5
#: How long one run may spend before leaving the rest for tomorrow (the task's soft limit is 300s).
RUN_SECONDS = 200
STAFF = Q(permission_admin=True) | Q(permission_edit_club=True) | Q(permission_manage_auctions=True)


@dataclass(frozen=True)
class Kind:
    key: str
    subject: str
    sentence: str
    button: str
    #: ``(user, about, path)`` for everyone this email is due to right now; ``about`` names the auction or
    #: club it concerns (for the report), ``path`` is where the button goes.
    recipients: Callable[[datetime.datetime, dict], Iterable[tuple[User, str, str]]]
    moment: bool = True
    #: False when the button leaves the site, so no ``PageView`` can count the click.
    tracked: bool = True
    #: Re-engagement kinds: one per quiet spell (see :func:`send_feature_emails`).
    quiet: bool = False


def add_src(url: str, src: str) -> str:
    """``url`` with ``src=<src>`` in its query, before any ``#fragment``."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    query = f"{parts.query}&src={src}" if parts.query else f"src={src}"
    return urlunsplit(parts._replace(query=query))


def start_date() -> datetime.date | None:
    text = (getattr(settings, "FEATURE_EMAILS_START", "") or "").strip()
    try:
        return datetime.date.fromisoformat(text) if text else None
    except ValueError:
        logger.error("FEATURE_EMAILS_START %r isn't a date; feature emails are off", text)
        return None


def daily_cap(today: datetime.date) -> int:
    """How many may go today: :data:`RAMP_START` for the first two weeks, then doubling weekly."""
    start = start_date()
    if start is None or today < start:
        return 0
    days = (today - start).days
    if days < 14:
        return RAMP_START
    return min(RAMP_MOST, RAMP_START * 2 ** ((days - 14) // 7 + 1))


def _ended(now):
    """Auctions that wound down inside :data:`MOMENT_WINDOW`, with a creator."""
    from auctions.models import Auction

    return Auction.objects.filter(
        is_deleted=False,
        created_by__isnull=False,
        date_end__lte=now,
        date_end__gt=now - MOMENT_WINDOW,
    ).select_related("club", "created_by__userdata")


def _staff(club, extra=Q()):
    """Club staff with an account who hold ``extra`` too (club admins always do)."""
    return [
        member.user
        for member in club.members.filter(user__isnull=False)
        .filter(Q(permission_admin=True) | (STAFF & extra))
        .select_related("user__userdata")
    ]


def _can_take_money(auction) -> bool:
    if not auction.club_id:
        return True
    return (
        auction.club.members.filter(user=auction.created_by)
        .filter(Q(permission_admin=True) | Q(permission_money=True))
        .exists()
    )


def takes_cards(auction) -> bool:
    """Whether buyers can already pay this auction's invoices online: its own switches, or for a club
    auction the club's PayPal or Square (a club auction's own switches are always off)."""
    if auction.paypal_payments_enabled or auction.enable_square_payments:
        return True
    return bool(auction.club_id and auction.club.effective_square_seller)


def _card_checkout(now, run):
    from auctions.models import Invoice

    for auction in _ended(now):
        if takes_cards(auction) or not _can_take_money(auction):
            continue
        if not Invoice.objects.filter(auction=auction).exists():
            continue
        if auction.club_id:
            path = reverse("club_edit", kwargs={"slug": auction.club.slug})
        else:
            path = reverse("edit_auction", kwargs={"slug": auction.slug})
        yield auction.created_by, auction.slug, path


def _dues(now, run):
    from auctions.models import Club

    clubs = (
        Club.objects.filter(active=True, membership_system="none")
        .annotate(here=Count("members", filter=Q(members__user__isnull=False)))
        .filter(here__gte=DUES_MIN_MEMBERS)
    )
    for club in clubs:
        path = reverse("club_membership_settings", kwargs={"slug": club.slug})
        for user in _staff(club, Q(permission_money=True) & Q(permission_add_edit=True)):
            yield user, club.slug, path


def _discord_calendar(now, run):
    seen = set()
    no_discord = Q(club__discord_server_id__isnull=True) | Q(club__discord_server_id="")
    for auction in _ended(now).filter(no_discord, club__isnull=False, club__google_calendar_id=""):
        if auction.club_id in seen:
            continue
        seen.add(auction.club_id)
        path = reverse("club_edit", kwargs={"slug": auction.club.slug})
        for user in _staff(auction.club, Q(permission_edit_club=True)):
            yield user, auction.club.slug, path


def _breeder_awards(now, run):
    from auctions.models import Lot

    seen = set()
    for auction in _ended(now).filter(club__isnull=False, club__enable_breeder_award_program=False):
        if auction.club_id in seen:
            continue
        bred = Lot.objects.filter(auction=auction, is_deleted=False, i_bred_this_fish=True).count()
        if bred < SELLER_BRED_LOTS:
            continue
        seen.add(auction.club_id)
        path = reverse("club_bap_settings", kwargs={"slug": auction.club.slug})
        for user in _staff(auction.club, Q(permission_admin=True)):
            yield user, auction.club.slug, path


def _ai_lot_entry(now, run):
    from auctions.models import Lot

    for auction in _ended(now):
        typed = Lot.objects.filter(auction=auction, is_deleted=False, added_by=auction.created_by).count()
        if typed >= HAND_ENTERED_LOTS:
            yield auction.created_by, auction.slug, reverse("ai")


def _website_embed(now, run):
    from auctions.models import Club, PageView

    stale = now - datetime.timedelta(days=Club.EVENTS_EMBED_ACTIVE_DAYS)
    clubs = (
        Club.objects.filter(active=True, events__date_start__gt=now - MOMENT_WINDOW)
        .filter(Q(events_website_last_view__isnull=True) | Q(events_website_last_view__lt=stale))
        .distinct()
    )
    for club in clubs:
        # The auction embed records no view of its own; a visit through one of its links shows it's there.
        if PageView.objects.filter(source="embed", auction__club=club, date_start__gte=stale).exists():
            continue
        path = reverse("club_website_integration", kwargs={"slug": club.slug})
        for user in _staff(club, Q(permission_edit_club=True)):
            yield user, club.slug, path


def quiet_people(now, run):
    """Accounts last seen between :data:`QUIET_AFTER` and :data:`QUIET_LIMIT` ago, worked out once per run
    (``run`` is that run's scratch dict)."""
    if "quiet" not in run:
        run["quiet"] = list(_quiet_people(now))
    return run["quiet"]


def _quiet_people(now):
    from auctions.models import AuctionTOS, Bid

    recent = now - QUIET_AFTER
    oldest = now - QUIET_LIMIT
    # One subquery each rather than Max over two joins at once, which multiplies a person's rows.
    joined = AuctionTOS.objects.filter(user=OuterRef("pk")).order_by("-createdon").values("createdon")[:1]
    bid = Bid.objects.filter(user=OuterRef("pk")).order_by("-bid_time").values("bid_time")[:1]
    people = User.objects.filter(is_active=True, userdata__last_activity__gte=oldest).annotate(
        last_joined=Subquery(joined), last_bid=Subquery(bid)
    )
    for user in people.select_related("userdata"):
        seen = max(when for when in (user.userdata.last_activity, user.last_joined, user.last_bid) if when)
        if oldest <= seen <= recent:
            user.last_seen = seen
            yield user


def _speakers(now, run):
    from auctions.models import ClubMember

    staff = set(ClubMember.objects.filter(STAFF, user__isnull=False).values_list("user_id", flat=True))
    for user in quiet_people(now, run):
        if user.pk in staff:
            yield user, "", reverse("speaker_list")


def _library(now, run):
    # Every account has the library (documents.search.can_use_library). The button searches it.
    for user in quiet_people(now, run):
        yield user, "", reverse("library_search")


def _palette(now, run):
    for user in quiet_people(now, run):
        yield user, "", "/"


def _aware_clubs(now, run):
    """Members here of a club that has never made an auction."""
    from auctions.club_health import ClubHealth

    quiet = {user.pk: user for user in quiet_people(now, run)}
    for health in ClubHealth.objects.filter(stage="aware", real_auctions=0, test_auctions=0).select_related("club"):
        club = health.club
        members = [
            quiet[pk] for pk in club.members.filter(user__isnull=False).values_list("user_id", flat=True) if pk in quiet
        ]
        members.sort(key=lambda user: user.userdata.last_activity, reverse=True)
        for user in members[:AWARE_CLUB_MEMBERS]:
            yield user, club.slug, reverse("help_guide", kwargs={"slug": "run-an-in-person-auction"})


def _iphone_app(now, run):
    url = getattr(settings, "APP_STORE_URL", "")
    if not url:
        return
    users = User.objects.filter(
        is_active=True,
        auctiontos__auction__is_online=False,
        auctiontos__auction__date_start__gt=now - datetime.timedelta(days=365),
    ).distinct()
    for user in users.select_related("userdata"):
        yield user, "", url


KINDS = (
    Kind(
        "card_checkout",
        "Let buyers pay by card",
        "Buyers at your next auction can pay their invoice by card from their phone.",
        "Turn on card payments",
        _card_checkout,
    ),
    Kind(
        "dues",
        "Collect dues here",
        "The site can collect dues and send renewal reminders for the members you already have here.",
        "Set up dues",
        _dues,
    ),
    Kind(
        "discord_calendar",
        "Post auctions to Discord and Google Calendar",
        "New auctions can post themselves to your Discord and Google Calendar.",
        "Connect them",
        _discord_calendar,
    ),
    Kind(
        "breeder_awards",
        "Count seller-bred lots automatically",
        "Lots bred by the seller in your last auction can count toward a breeder award program automatically.",
        "Set up an awards program",
        _breeder_awards,
    ),
    Kind(
        "ai_lot_entry",
        "Skip typing the lot sheet",
        "Next time, ChatGPT or Claude can enter the lot sheet for you.",
        "See how",
        _ai_lot_entry,
    ),
    Kind(
        "website_embed",
        "Your next auction on your club's website",
        "Your club website can show your next auction without anyone updating it.",
        "Get the snippet",
        _website_embed,
    ),
    Kind(
        "iphone_app",
        "The iPhone app is out",
        "The iPhone app is out.",
        "Get it",
        _iphone_app,
        moment=False,
        tracked=False,
    ),
    Kind(
        "who_runs_your_auction",
        "Does your club run an auction?",
        "Your club can run its auction night here for free: forward this to whoever organizes it.",
        "See how it works",
        _aware_clubs,
        moment=False,
        quiet=True,
    ),
    Kind(
        "speakers",
        "Need a speaker for your club?",
        "The speaker directory lists people who give talks to aquarium clubs, with their topics.",
        "Find a speaker",
        _speakers,
        moment=False,
        quiet=True,
    ),
    Kind(
        "library",
        "Old club articles, searchable",
        "Club newsletters and breeding reports are searchable in the library.",
        "Search the library",
        _library,
        moment=False,
        quiet=True,
    ),
    Kind(
        "palette",
        "Just type what you want",
        "Press Ctrl+K (or tap the search box) anywhere on the site and type what you want, in plain words.",
        "Try it",
        _palette,
        moment=False,
        quiet=True,
    ),
)
BY_KEY = {kind.key: kind for kind in KINDS}
QUIET_KEYS = [kind.key for kind in KINDS if kind.quiet]


def paused() -> bool:
    from auctions.models import FeatureEmailPause

    return FeatureEmailPause.objects.exists()


def check_reactions(now=None) -> str:
    """Pause everything if recent recipients opted out or bounced too often. Returns the reason, or ""."""
    from auctions.models import FeatureEmail, FeatureEmailPause

    now = now or timezone.now()
    recent = FeatureEmail.objects.filter(sent_on__gte=now - WATCH_WINDOW).select_related("user__userdata")
    sent = opted_out = bounced = 0
    for row in recent:
        sent += 1
        data = row.user.userdata
        opted_out += bool(data.has_unsubscribed or not data.email_me_about_new_features)
        bounced += bool(data.email_bounced_on and data.email_bounced_on >= row.sent_on)
    if sent < WATCH_MIN_SENT:
        return ""
    reason = ""
    if opted_out / sent > MAX_OPT_OUT_RATE:
        reason = f"{opted_out} of {sent} recipients in {WATCH_WINDOW.days} days opted out"
    elif bounced / sent > MAX_BOUNCE_RATE:
        reason = f"{bounced} of {sent} recipients in {WATCH_WINDOW.days} days bounced"
    if reason and not paused():
        FeatureEmailPause.objects.create(reason=reason)
        logger.error("Feature emails paused: %s. Delete the FeatureEmailPause row to resume.", reason)
    return reason


def _eligible(user, now, recently: set[int]) -> bool:
    data = user.userdata
    if not data.email_me_about_new_features or data.email_bounced_on:
        return False
    return user.pk not in recently and _deliverable(user)


def _send(user, kind: Kind, path: str):
    """Always an email, never a push: the ramp, the bounce check and the opt-out are all about email."""
    domain = Site.objects.get_current().domain
    url = path if path.startswith("http") else add_src(f"https://{domain}{path}", f"email_{kind.key}")
    context = {
        "subject": kind.subject,
        "sentence": kind.sentence,
        "button": kind.button,
        "url": url,
        "user": user,
        "domain": domain,
        "unsubscribe": str(user.userdata.unsubscribe_link),
    }
    mail.send(user.email, template="feature_email", context=context)


def send_feature_emails(now=None) -> int:
    """Send today's share of every feature email that's due. Returns how many went."""
    from auctions.models import FeatureEmail

    now = now or timezone.now()
    cap = daily_cap(timezone.localdate(now))
    if not cap or paused() or check_reactions(now):
        return 0
    today = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    cap -= FeatureEmail.objects.filter(sent_on__gte=today).count()
    recently = set(FeatureEmail.objects.filter(sent_on__gte=now - SPACING).values_list("user_id", flat=True))
    had = set(FeatureEmail.objects.values_list("user_id", "kind"))
    last_quiet = dict(FeatureEmail.objects.filter(kind__in=QUIET_KEYS).values_list("user_id").annotate(Max("sent_on")))
    sent = 0
    run: dict = {}
    began = time.monotonic()
    for kind in sorted(KINDS, key=lambda kind: not kind.moment):
        for user, about, path in kind.recipients(now, run):
            if sent >= cap or time.monotonic() - began > RUN_SECONDS:
                return sent
            if (user.pk, kind.key) in had or not _eligible(user, now, recently):
                continue
            # A quiet kind once per quiet spell: skip if one went since this person last showed up.
            last_seen = getattr(user, "last_seen", None)
            if kind.quiet and last_seen and last_quiet.get(user.pk) and last_quiet[user.pk] >= last_seen:
                continue
            try:
                with transaction.atomic():
                    row = FeatureEmail.objects.create(user=user, kind=kind.key, about=about[:100])
            except IntegrityError:
                continue
            had.add((user.pk, kind.key))
            try:
                _send(user, kind, path)
            except SoftTimeLimitExceeded:
                row.delete()
                raise
            except Exception:
                # Not sent, so not counted, and free to go another day.
                row.delete()
                logger.exception("feature email %s failed for user %s", kind.key, user.pk)
                continue
            recently.add(user.pk)
            if kind.quiet:
                last_quiet[user.pk] = now
            sent += 1
    return sent
