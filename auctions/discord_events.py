"""Discord scheduled events for clubs.

Two things create Discord events, and they deliberately don't overlap:

* Auctions, from the ``auction_emails`` management command — long-standing behavior, gated on
  ``Club.create_events_for_auctions`` and the auction being promoted. The event's id is kept on
  the auction so ``sync_auction_events`` below can move or call it off later.
* Everything else on a club's calendar (meetings, swaps, talks — including events pulled in from
  the club's Google Calendar), from ``sync_club_events`` below, gated on
  ``Club.create_discord_events_for_club_events``.

Because generated ClubEvents (auctions and their pickup times) are skipped in ``sync_club_events``,
an auction never gets two Discord events, and a club's Discord server never fills up with an
entry for every pickup slot.

Both paths reconcile rather than fire once: an event that moved is patched, an event someone
deleted in Discord is recreated, and an event that was called off — or whose club turned the
feature off — is removed from Discord.
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.contrib.sites.models import Site
from django.utils import timezone

logger = logging.getLogger(__name__)

API_BASE = "https://discord.com/api/v10"
TIMEOUT = 10

PRIVACY_GUILD_ONLY = 2
ENTITY_TYPE_EXTERNAL = 3

# Discord has no record of the event any more; ours is stale and should be made again.
GONE_STATUSES = (404, 410)


def _bot_token():
    return getattr(settings, "DISCORD_BOT_TOKEN", "")


def _headers():
    return {"Authorization": f"Bot {_bot_token()}", "Content-Type": "application/json"}


def _send(method, path, payload=None):
    """One Discord API call. Returns (status, body); status is 0 when Discord was unreachable."""
    try:
        resp = requests.request(method, f"{API_BASE}{path}", headers=_headers(), json=payload, timeout=TIMEOUT)
    except requests.RequestException:
        logger.exception("Discord %s %s failed", method, path)
        return (0, {})
    if resp.status_code not in (200, 201, 204):
        logger.warning("Discord %s %s failed: status=%s body=%s", method, path, resp.status_code, resp.text)
        return (resp.status_code, {})
    try:
        return (resp.status_code, resp.json())
    except ValueError:
        return (resp.status_code, {})


def _event_payload(name, start_time, end_time, location, description="", *, creating=False):
    """The body Discord wants. Only a create may set the privacy level and entity type."""
    payload = {
        "name": name[:100],
        "scheduled_start_time": start_time.isoformat(),
        "scheduled_end_time": end_time.isoformat(),
        "entity_metadata": {"location": (location or "See details")[:100]},
    }
    if creating:
        payload["privacy_level"] = PRIVACY_GUILD_ONLY
        payload["entity_type"] = ENTITY_TYPE_EXTERNAL
    if description:
        payload["description"] = description[:1000]
    return payload


def create_scheduled_event(guild_id, name, start_time, end_time, location, description=""):
    """Create a Discord Guild Scheduled Event (external type).

    Returns the new event's id on success, or None. Discord requires a location string and a
    start time in the future for external events.
    """
    if not _bot_token() or not guild_id:
        return None
    status, body = _send(
        "POST",
        f"/guilds/{guild_id}/scheduled-events",
        _event_payload(name, start_time, end_time, location, description, creating=True),
    )
    if status not in (200, 201):
        return None
    return body.get("id")


def _patch_scheduled_event(guild_id, event_id, name, start_time, end_time, location, description=""):
    """Patch an event and report the status, so a caller can tell "gone" from "failed"."""
    if not _bot_token() or not guild_id or not event_id:
        return 0
    status, _body = _send(
        "PATCH",
        f"/guilds/{guild_id}/scheduled-events/{event_id}",
        _event_payload(name, start_time, end_time, location, description),
    )
    return status


def cancel_scheduled_event(guild_id, event_id):
    """Delete a Discord scheduled event. Returns True when it's gone."""
    return _delete_scheduled_event(guild_id, event_id) in (200, 204, *GONE_STATUSES)


def _delete_scheduled_event(guild_id, event_id):
    """Delete an event and report the status. 0 means we never reached Discord — worth retrying;
    404 means someone already removed it there, which is the state we wanted anyway."""
    if not _bot_token() or not guild_id or not event_id:
        return 0
    status, _body = _send("DELETE", f"/guilds/{guild_id}/scheduled-events/{event_id}")
    return status


def sync_club_events(club):
    """Bring this club's Discord scheduled events in line with its calendar. Returns how many
    events were created, changed or removed."""
    if not (club.discord_server_id and _bot_token()):
        return 0
    return _sync_member_events(club) + sync_auction_events(club)


def _sync_member_events(club):
    """Meetings, swaps and anything pulled from Google — everything but the generated events.

    Each event is attempted once per change (``needs_discord_sync``), so a permanent failure —
    the bot lacking Manage Events, say — isn't retried every run, but an edit does get another go.
    """
    from auctions.models import ClubEvent

    touched = 0
    if not club.create_discord_events_for_club_events:
        # Turned off: take back the events we made rather than leaving them stranded in Discord.
        for event in club.events.exclude(discord_event_id="").exclude(source__in=ClubEvent.AUTOMATIC_SOURCES):
            changed, _retry = _remove(club, event)
            if changed:
                touched += 1
        return touched

    pending = club.events.filter(is_deleted=False, needs_discord_sync=True).exclude(
        source__in=ClubEvent.AUTOMATIC_SOURCES
    )
    for event in pending:
        if sync_one_event(club, event):
            touched += 1
    return touched


def sync_one_event(club, event):
    """Create, move or take down one club event in Discord. Returns True when Discord changed.

    Called for each pending event by the periodic sync, and directly by the event form so an
    admin sees the result of their edit without waiting a quarter of an hour for it.
    """
    from auctions.models import ClubEvent

    if not (club.discord_server_id and _bot_token() and club.create_discord_events_for_club_events):
        return False
    if event.source in ClubEvent.AUTOMATIC_SOURCES:
        return False
    if event.is_deleted or event.cancelled or event.date_start <= timezone.now():
        # Called off, or already under way — Discord won't take a past start time, and an event
        # that isn't happening shouldn't sit in the server's list.
        changed, retry = _remove(club, event)
        event.needs_discord_sync = retry
        event.save(update_fields=["needs_discord_sync"])
        return changed
    return _push_member_event(club, event)


def _push_member_event(club, event):
    """Create or update one club event in Discord. Returns True when Discord took it."""
    location = event.location or _club_event_fallback_location(event)
    event_id = event.discord_event_id
    if event_id:
        status = _patch_scheduled_event(
            club.discord_server_id,
            event_id,
            event.title,
            event.date_start,
            event.effective_end,
            location,
            event.description,
        )
        if status == 200:
            event.needs_discord_sync = False
            event.save(update_fields=["needs_discord_sync"])
            return True
        if status not in GONE_STATUSES:
            # A refusal isn't worth repeating until something changes; a Discord we couldn't
            # reach at all is, so leave that one queued.
            event.needs_discord_sync = status == 0
            event.save(update_fields=["needs_discord_sync"])
            return False
        # Deleted in Discord — fall through and make it again.
        event_id = ""

    new_id = create_scheduled_event(
        guild_id=club.discord_server_id,
        name=event.title,
        start_time=event.date_start,
        end_time=event.effective_end,
        location=location,
        description=event.description,
    )
    event.discord_event_id = new_id or ""
    event.needs_discord_sync = False
    event.save(update_fields=["discord_event_id", "needs_discord_sync"])
    return bool(new_id)


def _remove(club, event):
    """Take one club event out of Discord and forget its id.

    Returns (changed, worth_retrying): a refusal repeated every run helps nobody, but a Discord
    we simply couldn't reach deserves another go.
    """
    if not event.discord_event_id:
        return (False, False)
    status = _delete_scheduled_event(club.discord_server_id, event.discord_event_id)
    if status not in (200, 204, *GONE_STATUSES):
        return (False, status == 0)
    event.discord_event_id = ""
    event.save(update_fields=["discord_event_id"])
    return (True, False)


def sync_auction_events(club):
    """Keep the Discord events that ``auction_emails`` made for auctions honest.

    Creation stays in that command (it waits a day after an auction is posted). This only moves,
    renames or calls off an event that already exists, which is what nothing did before: an
    auction whose date moved, or that was unpromoted or deleted, kept its original Discord event
    for ever.
    """
    from auctions.models import Auction

    auctions = Auction.objects.filter(club=club).exclude(discord_event_id="")
    if not club.create_events_for_auctions:
        # Turned off: clear out what we already made, same as for club events.
        auctions = list(auctions)
    else:
        auctions = list(auctions.filter(discord_event_needs_update=True))
    touched = 0
    for auction in auctions:
        if _sync_one_auction_event(club, auction):
            touched += 1
    return touched


def _sync_one_auction_event(club, auction):
    """Move, remake or call off one auction's Discord event. Returns True when Discord changed."""
    from auctions.club_events import auction_event_window

    gone = auction.is_deleted or not auction.promote_this_auction or not club.create_events_for_auctions
    start, end = auction_event_window(auction)
    if gone or not start:
        status = _delete_scheduled_event(club.discord_server_id, auction.discord_event_id)
        removed = status in (200, 204, *GONE_STATUSES)
        if removed:
            auction.discord_event_id = ""
        # Only an unreachable Discord is worth another go; a refusal would just repeat forever.
        auction.discord_event_needs_update = status == 0
        _store_auction_event_state(auction)
        return removed

    auction.discord_event_needs_update = False
    if start <= timezone.now():
        # Under way already; Discord starts it on its own and refuses most edits from here.
        _store_auction_event_state(auction)
        return False

    location = _auction_location(auction)
    status = _patch_scheduled_event(
        club.discord_server_id, auction.discord_event_id, auction.title, start, end, location
    )
    changed = status == 200
    if status in GONE_STATUSES:
        # Someone deleted it in Discord. auction_emails only ever makes one, so remake it here.
        new_id = create_scheduled_event(
            guild_id=club.discord_server_id,
            name=auction.title,
            start_time=start,
            end_time=end,
            location=location,
        )
        auction.discord_event_id = new_id or ""
        changed = bool(new_id)
    _store_auction_event_state(auction)
    return changed


def _store_auction_event_state(auction):
    """Write back just the two Discord columns.

    A queryset update rather than auction.save(): saving an auction re-runs the calendar
    mirroring signals, and none of that has anything to do with recording a Discord event id.
    """
    from auctions.models import Auction

    Auction.objects.filter(pk=auction.pk).update(
        discord_event_id=auction.discord_event_id,
        discord_event_needs_update=auction.discord_event_needs_update,
    )


def _auction_location(auction):
    """Discord shows this as the event's location; the auction's page is the useful thing."""
    domain = Site.objects.get_current().domain
    return f"https://{domain}/?{auction.slug}"


def _club_event_fallback_location(event):
    """Discord insists on a location for external events; fall back to the club page."""
    domain = Site.objects.get_current().domain
    return f"https://{domain}{event.get_absolute_url()}"
