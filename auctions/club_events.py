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

# Pickups are a "be there at this time" slot rather than a window, so they get a short block.
PICKUP_LENGTH = datetime.timedelta(minutes=15)


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

    # A title or description a club admin typed is theirs and stays: the meeting details that
    # belong on the calendar entry change every month, and the auction's own title and "In-person
    # auction." don't. Everything else here is still owned by the auction, so an auction that
    # moves still moves its event. Clearing the flag on the form brings the generated value back
    # on the next save.
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
            touched += sync_pickup_events(auction)

    # Events whose auction is gone, unpromoted, or no longer wanted on the calendar.
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
            # An event that comes back — a pickup time re-added, an auction re-promoted — has to
            # be able to reach Discord again, so re-arm it rather than leaving it "already tried".
            event.needs_discord_sync = True
            event.save(update_fields=["discord_event_id", "needs_discord_sync"])
    # An auction's own Discord event is made by auction_emails and tracked on the auction, so it
    # needs taking down here too — otherwise the auction disappears everywhere but Discord.
    auction = event.auction if event.auction_id else None
    if auction and auction.discord_event_id:
        if discord_events.cancel_scheduled_event(club.discord_server_id, auction.discord_event_id):
            from auctions.models import Auction

            auction.discord_event_id = ""
            auction.discord_event_needs_update = False
            # A queryset update, not auction.save(): this also runs from ClubEvent's pre_delete,
            # where saving the auction would re-enter the mirroring signal mid-cascade.
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
    """A short plain-text blurb for the calendar entry.

    Deliberately just what kind of auction it is. The lot submission deadline used to be here and
    was removed: it is a seller's deadline, not the event, and it landed in every member's Google
    Calendar and Discord next to a date that had nothing to do with when to turn up.
    """
    if auction.is_online:
        return "Online auction with in-person pickup."
    return "In-person auction."


def auction_display_location(auction):
    """The one address worth advertising for an auction, or "" when there isn't one.

    Public wrapper over the rule the calendar entry already uses, so the website embeds show the
    same place the club's Google Calendar does instead of inventing a second answer.
    """
    return _auction_location(auction)


def _auction_location(auction):
    """Where the auction itself happens.

    Online auctions have no location — the bidding happens on the website, and the addresses
    people actually need belong on the pickup events instead. In-person auctions use their single
    physical location; with several, no one address is the right one to advertise.
    """
    if auction.is_online:
        return ""
    # Count distinct *addresses*, not locations: switching an auction to in-person auto-creates a
    # default location with no address, so counting rows would blank out the real one sitting
    # next to it.
    addresses = {
        location.address.strip() for location in auction.physical_location_qs if (location.address or "").strip()
    }
    if len(addresses) != 1:
        return ""
    return addresses.pop()[:500]


def pickup_slots(auction):
    """Yield (location, slot, start) for each pickup time an online auction advertises.

    Only online auctions get pickup events: for an in-person auction the "pickup" is the auction
    itself, which already has its own event. Mail-only locations have no time or place to show.

    An auction with several pickup locations gets none at all — only the auction's own event.
    A member goes to exactly one of those locations, so putting all of them on the club calendar
    (and from there into everyone's Google Calendar and Discord) buries the auction itself under
    a pile of appointments that don't apply to them. The auction page is where you pick your
    location; that's where the full list belongs.
    """
    if not auction.is_online:
        return
    # Count locations that would actually produce an event, not rows: a half-filled location with
    # no pickup time yet shouldn't suppress the one real location sitting next to it.
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
    """Create, update, or retire the calendar events for an online auction's pickup times.

    Each pickup time ``pickup_slots`` yields becomes its own short event at that location's
    address, so members can see exactly when and where to collect their lots. Multi-location
    auctions yield nothing, and any events they picked up before are retired here along with
    cleared pickup times. Returns how many events changed.
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
        # Same rule as the auction event above: hand-typed wording survives, everything else
        # tracks the pickup time. "Pickup — swap table open too" is a real thing a club says.
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

    # Pickup times that have been cleared, or that belong to an auction no longer on the calendar.
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
    """(title, description) as this site would write them for a generated event, or ("", "").

    The counterpart to ``title_is_custom`` / ``description_is_custom``: those columns say "don't
    overwrite this", and this says what the overwrite *would* have been. The edit form needs it
    twice — to show an admin what they are replacing, and to put it back when they press reset.

    Recomputed rather than stored. It is two attribute reads and a string join, and a stored copy
    would be one more thing that can fall out of step with the auction it came from.
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
    """Move each repeating event on to the occurrence that's on now, or the next one.

    One row stands for a whole series (see auctions/recurrence.py), and its ``date_start`` is what
    every other part of the site reads. Nothing else would ever move it along, so a weekly meeting
    would sit on the club page showing last Tuesday for ever. Returns how many moved.
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

    # Skip clubs with nothing to do. The token column is encrypted, so it can only be tested for
    # NULL — an empty-string token slips through and sync_club() no-ops on it, which is fine.
    # Everything else is tested for content: `discord_server_id__isnull=False` looked like a
    # filter but matched every club that had ever been saved with the field left blank, because
    # a blank CharField stores "" rather than NULL.
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
            # One club's broken integration must never stop the rest of the run.
            logger.exception("Club event sync failed for club %s", club.pk)
            continue
        count += 1
    return count


def next_member_facing_event(club):
    """The club's next event worth advertising in a membership email, or None.

    Pickup events are left out on purpose: they're logistics for people who already won lots, not
    something to invite a new or renewing member to. Cancelled events are skipped too. An event
    that's under way still counts — "our next event" shouldn't skip past today's meeting.
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


def upcoming_events(club, *, limit=None, include_past=False, past_limit=5, exclude_pickups=False):
    """Events for the club page: everything upcoming, plus a little recent history.

    Returns (upcoming, past). ``past`` is newest-first so the most recent event is on top.

    ``exclude_pickups`` drops pickup events for the same reason ``next_member_facing_event``
    does — they're logistics for people who already won lots, not something to advertise. It
    filters in the query rather than after the slice, so ``limit`` still counts real rows.
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
