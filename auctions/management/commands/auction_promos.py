"""Tell nearby users about a promoted auction, once each: by email, or by push for app users who chose push.

This replaced the weekly promo email. An auction is promoted inside a window:

- in person: from a week before it starts until it starts;
- online: from a day after bidding opens (so there are lots to look at) until bidding ends;

and never sooner than a day after it was created, which gives its organizer time to fix it. Clubs'
other events (meetings, swaps) are never promoted.

A user hears about it when they ticked the preference for that kind of auction and one of its pickup
locations is inside their distance. Emails only go to people who haven't been on the site in six
days (anybody who has already knows what's on near them) but have been in the last 400 (anybody
older is gone). Everything goes out in the 10 o'clock hour of the user's own time zone, unless the
window closes before their next 10 AM.

Hourly, and every open window is looked at again each time, so somebody who moves into range, signs
up or goes quiet halfway through still hears about it. The ``AuctionCampaign`` row (kind ``promo``) is
the sent log: it is written before anything is sent, so a failure never sends twice, and its link
records the click and the join the way the join reminder's does.
"""

import datetime
import logging
import zoneinfo

from django.conf import settings
from django.contrib.sites.models import Site
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.db.models import F, Q
from django.utils import timezone
from post_office import mail

from auctions.filters import get_recommended_lots
from auctions.models import (
    Auction,
    AuctionCampaign,
    AuctionTOS,
    PickupLocation,
    PushNotificationSent,
    UserData,
    distance_to,
)
from auctions.templatetags.distance_filters import distance_display

logger = logging.getLogger(__name__)

#: A new auction's time to be corrected before anybody is told about it.
SETTLE = datetime.timedelta(hours=24)
#: How far ahead of an in-person auction people are told.
IN_PERSON_LEAD = datetime.timedelta(days=7)
#: How long an online auction's bidding runs before people are told, so there is something to bid on.
ONLINE_DELAY = datetime.timedelta(hours=24)
#: Email only: somebody on the site this recently already knows what's running.
ACTIVE_WITHIN = datetime.timedelta(days=6)
#: Email only: somebody not seen for this long has left.
GONE_AFTER = datetime.timedelta(days=400)
#: The local hour messages go out in.
SEND_HOUR = 10
#: Recommended lots in the email.
LOTS_IN_EMAIL = 6

LOCK_KEY = "auction_promos:running"
#: Past the Celery hard limit, so a killed run can't hold the lock for ever.
LOCK_SECONDS = 15 * 60


def promotion_window(auction):
    """``(opens, closes)`` for telling people about this auction; ``closes`` is None for an online
    auction with no end date. None when it has no start date."""
    if not auction.date_start or not auction.date_posted:
        return None
    settled = auction.date_posted + SETTLE
    if auction.is_online:
        return max(settled, auction.date_start + ONLINE_DELAY), auction.date_end
    return max(settled, auction.date_start - IN_PERSON_LEAD), auction.date_start


def auctions_to_promote(now):
    """Promoted auctions whose window is open now."""
    in_person = Q(is_online=False, date_start__gt=now, date_start__lte=now + IN_PERSON_LEAD)
    online = Q(is_online=True, date_start__lte=now - ONLINE_DELAY) & (Q(date_end__isnull=True) | Q(date_end__gt=now))
    return Auction.objects.filter(
        in_person | online,
        is_deleted=False,
        promote_this_auction=True,
        date_posted__lte=now - SETTLE,
    )


def user_timezone(userdata):
    try:
        return zoneinfo.ZoneInfo(userdata.timezone or settings.TIME_ZONE)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return zoneinfo.ZoneInfo(settings.TIME_ZONE)


def is_send_time(userdata, now, closes):
    """True in the user's 10 o'clock hour, or at any hour when the window closes before their next 10 AM."""
    local = now.astimezone(user_timezone(userdata))
    if local.hour == SEND_HOUR:
        return True
    next_send = local.replace(hour=SEND_HOUR, minute=0, second=0, microsecond=0)
    if next_send <= local:
        next_send += datetime.timedelta(days=1)
    return closes is not None and closes <= next_send


def recipients(auction):
    """``(userdata, distance, nearest pickup location)`` for everybody near enough who wants this kind of
    auction and hasn't already been told about it or joined it."""
    if auction.is_online:
        wants, radius = "email_me_about_new_auctions", "email_me_about_new_auctions_distance"
    else:
        wants, radius = "email_me_about_new_in_person_auctions", "email_me_about_new_in_person_auctions_distance"
    told = AuctionCampaign.objects.filter(auction=auction, kind=AuctionCampaign.KIND_PROMO, user__isnull=False)
    # Pushed by promo_push_notifications before this job replaced it.
    pushed = PushNotificationSent.objects.filter(category="promo", auction=auction)
    joined = AuctionTOS.objects.filter(auction=auction, user__isnull=False)
    candidates = (
        UserData.objects.filter(**{wants: True}, has_unsubscribed=False, user__is_active=True)
        .exclude(latitude__isnull=True)
        .exclude(longitude__isnull=True)
        .exclude(latitude=0, longitude=0)
        .exclude(user__in=told.values("user"))
        .exclude(user__in=pushed.values("user"))
        .exclude(user__in=joined.values("user"))
        .select_related("user")
    )
    if auction.created_by_id:
        candidates = candidates.exclude(user_id=auction.created_by_id)
    locations = (
        PickupLocation.objects.filter(auction=auction, latitude__isnull=False, longitude__isnull=False)
        .exclude(latitude=0, longitude=0)
        .exclude(pickup_by_mail=True)
    )
    nearest = {}
    for location in locations:
        in_range = candidates.annotate(distance=distance_to(location.latitude, location.longitude)).filter(
            Q(distance__lte=F(radius)) | Q(**{f"{radius}__isnull": True})
        )
        for userdata in in_range:
            if userdata.pk not in nearest or userdata.distance < nearest[userdata.pk][1]:
                nearest[userdata.pk] = (userdata, userdata.distance, location)
    return list(nearest.values())


def is_quiet(userdata, now):
    """Email only: not on the site in the last six days, but not gone either."""
    last = userdata.last_activity
    return last is not None and now - GONE_AFTER < last <= now - ACTIVE_WITHIN


def when_text(auction, userdata):
    """ "starts Saturday, May 3 at 10:00 AM" / "bidding ends ...", in the user's own time."""
    if auction.is_online:
        moment, verb = auction.date_end, "bidding ends"
    else:
        moment, verb = auction.date_start, "starts"
    if not moment:
        return ""
    local = moment.astimezone(user_timezone(userdata))
    return f"{verb} {local.strftime('%A, %B')} {local.day} at {local.strftime('%I:%M %p').lstrip('0')}"


class Command(BaseCommand):
    help = "Tell nearby users about promoted auctions, once each, by email or push"

    def handle(self, *args, **options):
        if not cache.add(LOCK_KEY, 1, timeout=LOCK_SECONDS):
            logger.info("auction_promos is already running; skipping this tick.")
            return
        try:
            emails, pushes = self.promote_all(timezone.now())
        finally:
            cache.delete(LOCK_KEY)
        logger.info("auction_promos: %s email(s), %s push(es)", emails, pushes)
        self.stdout.write(f"auction_promos: {emails} email(s), {pushes} push(es)")

    def promote_all(self, now):
        domain = Site.objects.get_current().domain
        emails = pushes = 0
        for auction in auctions_to_promote(now):
            window = promotion_window(auction)
            if window is None or now < window[0]:
                continue
            closes = window[1]
            for userdata, distance, location in recipients(auction):
                try:
                    sent = self.promote(auction, userdata, distance, location, now, closes, domain)
                except Exception:
                    logger.exception("auction_promos: failed for auction %s, user %s", auction.pk, userdata.user_id)
                    continue
                if sent == AuctionCampaign.SOURCE_PROMO_EMAIL:
                    emails += 1
                elif sent == AuctionCampaign.SOURCE_PROMO_PUSH:
                    pushes += 1
        return emails, pushes

    def promote(self, auction, userdata, distance, location, now, closes, domain):
        """Tell one user about one auction if it's time; the source it went by, or None."""
        user = userdata.user
        push = userdata.user_prefers_push()
        if not push and (not user.email or not is_quiet(userdata, now)):
            return None
        if not is_send_time(userdata, now, closes):
            return None
        source = AuctionCampaign.SOURCE_PROMO_PUSH if push else AuctionCampaign.SOURCE_PROMO_EMAIL
        try:
            # The claim: written first, so a send that fails is never repeated.
            campaign = AuctionCampaign.objects.create(
                kind=AuctionCampaign.KIND_PROMO,
                auction=auction,
                user=user,
                email=user.email or "",
                source=source,
                email_sent=not push,
            )
        except ValidationError:
            return None
        auction_url = f"https://{domain}{auction.get_absolute_url()}?src={campaign.uuid}"
        kind = "online auction" if auction.is_online else "in-person auction"
        distance_text = distance_display(distance, user)
        if push:
            from auctions.tasks import send_push_to_user

            # The name and distance go in the body, where they aren't cut off.
            body = f"{auction.title} — {kind}"
            if distance_text:
                body += f", {distance_text} away"
            send_push_to_user.delay(
                user.pk,
                title="Bidding is open" if auction.is_online else "Auction coming up",
                body=body,
                url=auction_url,
                category="promo",
                auction_pk=auction.pk,
            )
            return source
        lots = list(get_recommended_lots(user=user, auction=auction.slug, qty=LOTS_IN_EMAIL))
        mail.send(
            user.email,
            template="auction_promo_email",
            context={
                "name": user.first_name,
                "domain": domain,
                "auction": auction,
                "auction_url": auction_url,
                "kind": kind,
                "distance": distance_text,
                "location": location,
                "multiple_locations": auction.number_of_locations > 1,
                "when": when_text(auction, userdata),
                "lots": lots,
                "uuid": campaign.uuid,
                "unsubscribe": userdata.unsubscribe_link,
            },
        )
        return source
