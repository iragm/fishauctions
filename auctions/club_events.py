"""Keeps a club's event list, its Google Calendar, and its Discord events in step.

The club page renders ``ClubEvent`` rows, so anything that should appear there has to become one
first. ``sync_auction_events`` mirrors promoted auctions into events; ``sync_all`` is what the
periodic task calls to service every club.
"""

from __future__ import annotations

import datetime
import logging

from django.db.models import Q
from django.utils import timezone

from auctions import discord_events, google_calendar

logger = logging.getLogger(__name__)

# How long an in-person auction is assumed to run when it has no end date. Matches what the
# Discord auction events have always used.
DEFAULT_AUCTION_LENGTH = datetime.timedelta(hours=2)


def auction_event_window(auction):
    """(start, end) for an auction's calendar entry, or (None, None) when it can't be placed.

    Online auctions span from the start of bidding to the end of it. In-person auctions have no
    meaningful ``date_end`` (bidding is live in the room), so they get a fixed-length block.
    """
    start = auction.date_start
    if not start:
        return (None, None)
    if auction.is_online and auction.date_end and auction.date_end > start:
        return (start, auction.date_end)
    return (start, start + DEFAULT_AUCTION_LENGTH)


def sync_one_auction_event(auction):
    """Create, update, or retire the single ClubEvent mirroring one auction.

    Called from a post_save signal so the club's calendar tracks the auction as soon as it's
    edited. Returns the event, or None when the auction doesn't belong on a calendar.
    """
    from auctions.models import ClubEvent

    club = auction.club
    event = ClubEvent.objects.filter(auction=auction).first()

    belongs = bool(club and club.add_auctions_to_calendar and auction.promote_this_auction and not auction.is_deleted)
    start, end = auction_event_window(auction) if belongs else (None, None)
    if not belongs or not start:
        if event and not event.is_deleted:
            # DB-only: this runs from the auction post_save signal, inside its transaction.
            # purge_retired() removes the Google/Discord copies on the next sync.
            retire_event(event, remote=False)
        return None

    description = _auction_description(auction)
    location = _auction_location(auction)
    if event is None:
        # get_or_create rather than create: the unique index on `auction` is the real guard, and
        # this keeps a concurrent sync from raising instead of just no-opping.
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

    changed = (
        event.title != auction.title
        or event.date_start != start
        or event.date_end != end
        or event.description != description
        or event.location != location
        or event.is_deleted
        or event.club_id != club.pk
    )
    if changed:
        event.club = club
        event.source = ClubEvent.SOURCE_AUCTION
        event.title = auction.title
        event.description = description
        event.location = location
        event.date_start = start
        event.date_end = end
        event.is_deleted = False
        event.needs_google_sync = True
        event.save()
    return event


def sync_auction_events(club):
    """Reconcile every one of this club's auctions with its calendar.

    The per-auction signal keeps things current day to day; this is the periodic backstop that
    catches auctions saved before the club turned the feature on, and events left behind by an
    auction that has since been unpromoted or deleted. Returns how many events changed.
    """
    from auctions.models import Auction, ClubEvent

    touched = 0
    if club.add_auctions_to_calendar:
        auctions = Auction.objects.filter(club=club, is_deleted=False, promote_this_auction=True)
        for auction in auctions.select_related("club"):
            if sync_one_auction_event(auction):
                touched += 1

    # Events whose auction is gone, unpromoted, or no longer wanted on the calendar.
    stale = ClubEvent.objects.filter(club=club, source=ClubEvent.SOURCE_AUCTION, is_deleted=False)
    if club.add_auctions_to_calendar:
        stale = stale.filter(
            Q(auction__isnull=True) | Q(auction__is_deleted=True) | Q(auction__promote_this_auction=False)
        )
    for event in stale:
        retire_event(event)
        touched += 1
    return touched


def retire_event(event, *, remote=True):
    """Soft-delete an event and take it off Google Calendar and Discord.

    Pass ``remote=False`` from anywhere that runs inside a transaction (the auction signal) so
    the soft-delete stays a plain DB write. ``purge_retired(club)`` does the remote half later —
    it picks up any soft-deleted event that still carries a remote id.
    """
    event.is_deleted = True
    event.save(update_fields=["is_deleted"])
    if remote:
        _remove_remote(event)


def _remove_remote(event):
    """Delete an event's Google and Discord counterparts. Never raises."""
    if event.google_event_id and event.club.google_calendar_connected:
        try:
            google_calendar.delete_event(event)
        except google_calendar.GoogleCalendarError:
            logger.warning("Could not remove event %s from Google Calendar", event.pk)
    if event.discord_event_id and event.club.discord_server_id:
        if discord_events.cancel_scheduled_event(event.club.discord_server_id, event.discord_event_id):
            event.discord_event_id = ""
            event.save(update_fields=["discord_event_id"])


def purge_retired(club):
    """Clean up after events that were soft-deleted without their remote copies being removed."""
    from auctions.models import ClubEvent

    stragglers = ClubEvent.objects.filter(club=club, is_deleted=True).exclude(google_event_id="", discord_event_id="")
    for event in stragglers:
        _remove_remote(event)


def _auction_description(auction):
    """A short plain-text blurb for the calendar entry."""
    parts = []
    if auction.is_online:
        parts.append("Online auction with in-person pickup.")
    else:
        parts.append("In-person auction.")
    if auction.lot_submission_end_date:
        parts.append(f"Lot submission closes {auction.lot_submission_end_date:%b %-d, %Y at %-I:%M %p}.")
    return " ".join(parts)


def _auction_location(auction):
    """The auction's pickup address. Only used when there's a single physical location — with
    several, no one address is the right one to put on the calendar entry."""
    locations = auction.physical_location_qs
    if locations.count() != 1:
        return ""
    address = locations.first().address
    return address[:500] if address else ""


def sync_club(club):
    """Bring one club fully up to date. Safe to call often; every step is idempotent."""
    sync_auction_events(club)
    purge_retired(club)
    if club.google_calendar_connected:
        google_calendar.sync_club(club)
    if club.discord_server_id:
        discord_events.sync_club_events(club)


def sync_all():
    """Service every club that has something to sync. Returns how many clubs were touched."""
    from auctions.models import Club

    # Skip clubs with nothing to do. The token column is encrypted, so it can only be tested for
    # NULL — an empty-string token slips through and sync_club() no-ops on it, which is fine.
    clubs = Club.objects.filter(
        Q(google_calendar_refresh_token__isnull=False)
        | Q(discord_server_id__isnull=False)
        | Q(auctions__is_deleted=False, auctions__promote_this_auction=True)
        | Q(events__is_deleted=False)
    ).distinct()
    count = 0
    for club in clubs:
        try:
            sync_club(club)
        except Exception:
            # One club's broken integration must never stop the rest of the run.
            logger.exception("Club event sync failed for club %s", club.pk)
            continue
        count += 1
    return count


def upcoming_events(club, *, limit=None, include_past=False, past_limit=5):
    """Events for the club page: everything upcoming, plus a little recent history.

    Returns (upcoming, past). ``past`` is newest-first so the most recent event is on top.
    """
    from auctions.models import ClubEvent

    now = timezone.now()
    base = ClubEvent.objects.filter(club=club, is_deleted=False).select_related("auction")
    upcoming = base.filter(Q(date_end__gte=now) | Q(date_end__isnull=True, date_start__gte=now)).order_by("date_start")
    if limit:
        upcoming = upcoming[:limit]
    past = []
    if include_past:
        past = base.filter(Q(date_end__lt=now) | Q(date_end__isnull=True, date_start__lt=now)).order_by("-date_start")[
            :past_limit
        ]
    return (upcoming, past)
