"""Keeps a club's event list, its Google Calendar, and its Discord events in step.

The club page renders ``ClubEvent`` rows. ``sync_auction_events`` mirrors promoted auctions into
events; ``sync_all`` is what the periodic task calls to service every club.
"""

from __future__ import annotations

import datetime
import logging

from django.db.models import F, Q
from django.utils import timezone

from auctions import discord_events, google_calendar

logger = logging.getLogger(__name__)

# How long an in-person auction is assumed to run when it has no end date.
DEFAULT_AUCTION_LENGTH = datetime.timedelta(hours=2)

# Pickups are a "be there at this time" slot rather than a window, so they get a short block.
PICKUP_LENGTH = datetime.timedelta(minutes=15)


def auction_event_window(auction):
    """(start, end) for an auction's calendar entry, or (None, None) when it can't be placed.

    Online auctions span the bidding window; in-person auctions have no meaningful ``date_end``, so
    they get a fixed-length block.
    """
    start = auction.date_start
    if not start:
        return (None, None)
    if auction.is_online and auction.date_end and auction.date_end > start:
        return (start, auction.date_end)
    return (start, start + DEFAULT_AUCTION_LENGTH)


def sync_one_auction_event(auction):
    """Create, update or retire the single ClubEvent mirroring one auction, from a post_save signal.

    Returns the event, or None when it doesn't belong on a calendar.
    """
    from auctions.models import ClubEvent

    club = auction.club
    event = ClubEvent.objects.filter(auction=auction).first()

    belongs = bool(club and club.add_auctions_to_calendar and auction.promote_this_auction and not auction.is_deleted)
    start, end = auction_event_window(auction) if belongs else (None, None)
    if not belongs or not start:
        if event and not event.is_deleted:
            # DB-only: this runs inside the auction's save transaction, and purge_retired() removes
            # the remote copies later.
            retire_event(event, remote=False)
        return None

    description = _auction_description(auction)
    location = _auction_location(auction)
    if event is None:
        # get_or_create: the unique index on `auction` guards against a concurrent sync.
        event, _created = ClubEvent.objects.get_or_create(
            auction=auction,
            defaults={
                "club": club,
                "source": ClubEvent.SOURCE_AUCTION,
                "title": auction.title,
                "description": description,
                "location": location,
                "date_start": start,
                "date_end": end,
            },
        )
        return event

    # Hand-typed title and description survive; everything else tracks the auction.
    keep_title = event.title_is_custom
    keep_description = event.description_is_custom
    changed = (
        (not keep_title and event.title != auction.title)
        or event.date_start != start
        or event.date_end != end
        or (not keep_description and event.description != description)
        or event.location != location
        or event.is_deleted
        or event.club_id != club.pk
    )
    if changed:
        event.club = club
        event.source = ClubEvent.SOURCE_AUCTION
        if not keep_title:
            event.title = auction.title
        if not keep_description:
            event.description = description
        event.location = location
        event.date_start = start
        event.date_end = end
        event.is_deleted = False
        event.needs_google_sync = True
        event.save()
    return event


def sync_auction_events(club):
    """Reconcile every one of this club's auctions with its calendar; returns how many changed.

    The periodic backstop for the per-auction signal, catching auctions saved before the feature was on
    and events left by an unpromoted or deleted auction.
    """
    from auctions.models import Auction, ClubEvent

    touched = 0
    if club.add_auctions_to_calendar:
        auctions = Auction.objects.filter(club=club, is_deleted=False, promote_this_auction=True)
        for auction in auctions.select_related("club"):
            if sync_one_auction_event(auction):
                touched += 1
            touched += sync_pickup_events(auction)

    stale = ClubEvent.objects.filter(club=club, source=ClubEvent.SOURCE_AUCTION, is_deleted=False)
    stale_pickups = ClubEvent.objects.filter(club=club, source=ClubEvent.SOURCE_PICKUP, is_deleted=False)
    if club.add_auctions_to_calendar:
        gone = Q(auction__isnull=True) | Q(auction__is_deleted=True) | Q(auction__promote_this_auction=False)
        stale = stale.filter(gone)
        stale_pickups = stale_pickups.filter(
            Q(pickup_location__isnull=True)
            | Q(pickup_location__auction__isnull=True)
            | Q(pickup_location__auction__is_deleted=True)
            | Q(pickup_location__auction__promote_this_auction=False)
        )
    for event in list(stale) + list(stale_pickups):
        retire_event(event)
        touched += 1
    return touched


def retire_event(event, *, remote=True):
    """Soft-delete an event and take it off Google Calendar and Discord.

    ``remote=False`` from inside a transaction (the auction signal); ``purge_retired(club)`` does the
    remote half later.
    """
    event.is_deleted = True
    event.save(update_fields=["is_deleted"])
    if remote:
        _remove_remote(event)


def _remove_remote(event):
    """Delete an event's Google and Discord counterparts. Never raises."""
    club = event.club
    if event.google_event_id and club.google_calendar_connected:
        try:
            google_calendar.delete_event(event)
        except google_calendar.GoogleCalendarError:
            logger.warning("Could not remove event %s from Google Calendar", event.pk)
    if not club.discord_server_id:
        return
    if event.discord_event_id:
        if discord_events.cancel_scheduled_event(club.discord_server_id, event.discord_event_id):
            event.discord_event_id = ""
            # Re-arm rather than leave "already tried", so a re-added time can reach Discord again.
            event.needs_discord_sync = True
            event.save(update_fields=["discord_event_id", "needs_discord_sync"])
    # An auction's own Discord event (made by auction_emails) needs taking down here too.
    auction = event.auction if event.auction_id else None
    if auction and auction.discord_event_id:
        if discord_events.cancel_scheduled_event(club.discord_server_id, auction.discord_event_id):
            from auctions.models import Auction

            auction.discord_event_id = ""
            auction.discord_event_needs_update = False
            # A queryset update: this also runs from ClubEvent's pre_delete, where saving the
            # auction would re-enter the mirroring signal mid-cascade.
            Auction.objects.filter(pk=auction.pk).update(discord_event_id="", discord_event_needs_update=False)


def purge_retired(club):
    """Clean up after events that were soft-deleted without their remote copies being removed."""
    from auctions.models import ClubEvent

    stragglers = ClubEvent.objects.filter(club=club, is_deleted=True).filter(
        Q(google_event_id__gt="") | Q(discord_event_id__gt="") | Q(auction__discord_event_id__gt="")
    )
    for event in stragglers.select_related("club", "auction"):
        _remove_remote(event)


def _auction_description(auction):
    """A short plain-text blurb for the calendar entry: just what kind of auction it is."""
    if auction.is_online:
        return "Online auction with in-person pickup."
    return "In-person auction."


def auction_display_location(auction):
    """The one address worth advertising for an auction, or "" -- the same rule the calendar entry uses,
    so embeds agree with the club's Google Calendar.
    """
    return _auction_location(auction)


def _auction_location(auction):
    """Where the auction itself happens. Online auctions have none (bidding is on the website)."""
    if auction.is_online:
        return ""
    # Count distinct addresses, not locations: switching to in-person auto-creates a default
    # location with no address.
    addresses = {
        location.address.strip() for location in auction.physical_location_qs if (location.address or "").strip()
    }
    if len(addresses) != 1:
        return ""
    return addresses.pop()[:500]


def pickup_slots(auction):
    """Yield (location, slot, start) for each pickup time an online auction advertises.

    Online auctions only, and none at all when there are several pickup locations: a member goes to one,
    and the auction page is where they pick it.
    """
    if not auction.is_online:
        return
    # Count locations that would produce an event: a half-filled one shouldn't suppress a real one.
    locations = [
        location
        for location in auction.location_qs.filter(pickup_by_mail=False)
        if location.pickup_time or location.second_pickup_time
    ]
    if len(locations) != 1:
        return
    location = locations[0]
    for slot, start in ((1, location.pickup_time), (2, location.second_pickup_time)):
        if start:
            yield (location, slot, start)


def sync_pickup_events(auction):
    """Create, update or retire an online auction's pickup events; returns how many changed.

    Multi-location auctions yield nothing from ``pickup_slots``, and earlier events are retired here.
    """
    from auctions.models import ClubEvent

    club = auction.club
    touched = 0
    wanted = {}
    if club and club.add_auctions_to_calendar and auction.promote_this_auction and not auction.is_deleted:
        wanted = {(location.pk, slot): (location, start) for location, slot, start in pickup_slots(auction)}

    existing = {
        (event.pickup_location_id, event.pickup_slot): event
        for event in ClubEvent.objects.filter(
            pickup_location__auction=auction, source=ClubEvent.SOURCE_PICKUP
        ).select_related("pickup_location")
    }

    for key, (location, start) in wanted.items():
        title = _pickup_title(auction, location)
        description = _pickup_description(auction, location)
        address = (location.address or "")[:500]
        event = existing.get(key)
        if event is None:
            ClubEvent.objects.get_or_create(
                pickup_location=location,
                pickup_slot=key[1],
                defaults={
                    "club": club,
                    "source": ClubEvent.SOURCE_PICKUP,
                    "title": title,
                    "description": description,
                    "location": address,
                    "date_start": start,
                    "date_end": start + PICKUP_LENGTH,
                },
            )
            touched += 1
            continue
        # Same rule as the auction event above: hand-typed wording survives.
        keep_title = event.title_is_custom
        keep_description = event.description_is_custom
        changed = (
            (not keep_title and event.title != title)
            or (not keep_description and event.description != description)
            or event.location != address
            or event.date_start != start
            or event.date_end != start + PICKUP_LENGTH
            or event.is_deleted
            or event.club_id != club.pk
        )
        if changed:
            event.club = club
            if not keep_title:
                event.title = title
            if not keep_description:
                event.description = description
            event.location = address
            event.date_start = start
            event.date_end = start + PICKUP_LENGTH
            event.is_deleted = False
            event.needs_google_sync = True
            event.save()
            touched += 1

    # Pickup times that were cleared, or belong to an auction no longer on the calendar.
    for key, event in existing.items():
        if key not in wanted and not event.is_deleted:
            retire_event(event, remote=False)
            touched += 1
    return touched


def _pickup_title(auction, location):
    """A title that stands on its own in someone's calendar, away from this site."""
    title = f"{auction.title} pickup"
    if location.name:
        title = f"{title} — {location.name}"
    return title[:255]


def _pickup_description(auction, location):
    parts = ["Pick up the lots you won."]
    if location.description:
        parts.append(location.description)
    if location.users_must_coordinate_pickup:
        parts.append("Coordinate the exact time with the seller.")
    return " ".join(parts)


def generated_wording(event):
    """(title, description) as this site would generate them, or ("", "") -- what title_is_custom and
    description_is_custom would overwrite. Recomputed rather than stored.
    """
    from auctions.models import ClubEvent

    if event.source == ClubEvent.SOURCE_AUCTION and event.auction:
        return event.auction.title, _auction_description(event.auction)
    if event.source == ClubEvent.SOURCE_PICKUP and event.pickup_location:
        auction = event.pickup_location.auction
        if auction:
            return _pickup_title(auction, event.pickup_location), _pickup_description(auction, event.pickup_location)
    return "", ""


def refresh_recurring_events(club):
    """Move each repeating event on to the occurrence that's on now, or the next one; returns how many.

    One row stands for a series (auctions/recurrence.py) and nothing else moves ``date_start``.
    """
    from auctions.models import ClubEvent

    touched = 0
    for event in ClubEvent.objects.filter(club=club, is_deleted=False).exclude(recurrence=""):
        if event.refresh_occurrence():
            touched += 1
    return touched


def sync_club(club):
    """Bring one club fully up to date. Safe to call often; every step is idempotent."""
    sync_auction_events(club)
    refresh_recurring_events(club)
    purge_retired(club)
    if club.google_calendar_connected:
        google_calendar.sync_club(club)
    if club.discord_server_id:
        discord_events.sync_club_events(club)


def sync_all():
    """Service every club that has something to sync. Returns how many clubs were touched."""
    from auctions.models import Club

    # discord_server_id__gt="" not __isnull=False: a blank CharField stores "" rather than NULL.
    clubs = Club.objects.filter(
        Q(google_calendar_refresh_token__isnull=False)
        | Q(discord_server_id__gt="")
        | Q(auctions__is_deleted=False, auctions__promote_this_auction=True)
        | Q(events__is_deleted=False),
        active=True,
    ).distinct()
    count = 0
    for club in clubs:
        try:
            sync_club(club)
        except Exception:
            logger.exception("Club event sync failed for club %s", club.pk)
            continue
        count += 1
    return count


def next_member_facing_event(club):
    """The club's next event worth advertising in a membership email, or None.

    Pickup events and cancelled events are excluded. An event under way still counts.
    """
    from auctions.models import ClubEvent

    now = timezone.now()
    return (
        ClubEvent.objects.filter(club=club, is_deleted=False, cancelled=False)
        .exclude(source=ClubEvent.SOURCE_PICKUP)
        .filter(Q(date_end__gte=now) | Q(date_end__isnull=True, date_start__gte=now))
        .select_related("auction")
        .order_by("date_start")
        .first()
    )


def record_website_view(club):
    """Count one impression of a club's events embed and stamp when it happened.

    Counted on the club, not a row, so an empty embed counts too. The club page isn't counted. ``F()``
    avoids read-modify-write races.
    """
    from auctions.models import Club

    Club.objects.filter(pk=club.pk).update(
        events_website_views=F("events_website_views") + 1,
        events_website_last_view=timezone.now(),
    )


def past_events(club, *, limit=5, exclude_pickups=False):
    """The club's most recent events, newest first, for the "what we've been up to" embed."""
    from auctions.models import ClubEvent

    now = timezone.now()
    rows = ClubEvent.objects.filter(club=club, is_deleted=False).select_related("auction")
    if exclude_pickups:
        rows = rows.exclude(source=ClubEvent.SOURCE_PICKUP)
    rows = rows.filter(Q(date_end__lt=now) | Q(date_end__isnull=True, date_start__lt=now)).order_by("-date_start")
    if limit:
        rows = rows[:limit]
    return rows


def upcoming_events(club, *, limit=None, include_past=False, past_limit=5, exclude_pickups=False):
    """Events for the club page: everything upcoming plus a little recent history, as (upcoming, past).

    ``exclude_pickups`` drops pickup events for the same reason ``next_member_facing_event`` does.
    """
    from auctions.models import ClubEvent

    now = timezone.now()
    base = ClubEvent.objects.filter(club=club, is_deleted=False).select_related("auction")
    if exclude_pickups:
        base = base.exclude(source=ClubEvent.SOURCE_PICKUP)
    upcoming = base.filter(Q(date_end__gte=now) | Q(date_end__isnull=True, date_start__gte=now)).order_by("date_start")
    if limit:
        upcoming = upcoming[:limit]
    past = []
    if include_past:
        past = base.filter(Q(date_end__lt=now) | Q(date_end__isnull=True, date_start__lt=now)).order_by("-date_start")[
            :past_limit
        ]
    return (upcoming, past)
