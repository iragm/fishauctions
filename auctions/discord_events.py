"""Discord scheduled events for clubs.

Two things create Discord events, and they deliberately don't overlap:

* Auctions, from the ``auction_emails`` management command — long-standing behavior, gated on
  ``Club.create_events_for_auctions`` and the auction being promoted.
* Everything else on a club's calendar (meetings, swaps, talks — including events pulled in from
  the club's Google Calendar), from ``sync_club_events`` below, gated on
  ``Club.create_discord_events_for_club_events``.

Because auction-sourced ClubEvents are skipped here, an auction never gets two Discord events.
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


def _headers():
    return {"Authorization": f"Bot {settings.DISCORD_BOT_TOKEN}", "Content-Type": "application/json"}


def create_scheduled_event(guild_id, name, start_time, end_time, location, description=""):
    """Create a Discord Guild Scheduled Event (external type).

    Returns the new event's id on success, or None. Discord requires a location string and a
    start time in the future for external events.
    """
    bot_token = getattr(settings, "DISCORD_BOT_TOKEN", "")
    if not bot_token or not guild_id:
        return None
    payload = {
        "name": name[:100],
        "scheduled_start_time": start_time.isoformat(),
        "scheduled_end_time": end_time.isoformat(),
        "privacy_level": PRIVACY_GUILD_ONLY,
        "entity_type": ENTITY_TYPE_EXTERNAL,
        "entity_metadata": {"location": (location or "See details")[:100]},
    }
    if description:
        payload["description"] = description[:1000]
    try:
        resp = requests.post(
            f"{API_BASE}/guilds/{guild_id}/scheduled-events",
            headers=_headers(),
            json=payload,
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        logger.exception("Discord scheduled event creation error for guild %s", guild_id)
        return None
    if resp.status_code not in (200, 201):
        logger.warning("Discord scheduled event creation failed: status=%s body=%s", resp.status_code, resp.text)
        return None
    try:
        return resp.json().get("id")
    except ValueError:
        return None


def update_scheduled_event(guild_id, event_id, name, start_time, end_time, location, description=""):
    """Patch an existing Discord scheduled event. Returns True on success."""
    bot_token = getattr(settings, "DISCORD_BOT_TOKEN", "")
    if not bot_token or not guild_id or not event_id:
        return False
    payload = {
        "name": name[:100],
        "scheduled_start_time": start_time.isoformat(),
        "scheduled_end_time": end_time.isoformat(),
        "entity_metadata": {"location": (location or "See details")[:100]},
    }
    if description:
        payload["description"] = description[:1000]
    try:
        resp = requests.patch(
            f"{API_BASE}/guilds/{guild_id}/scheduled-events/{event_id}",
            headers=_headers(),
            json=payload,
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        logger.exception("Discord scheduled event update error for guild %s", guild_id)
        return False
    return resp.status_code == 200


def cancel_scheduled_event(guild_id, event_id):
    """Delete a Discord scheduled event. Returns True when it's gone."""
    bot_token = getattr(settings, "DISCORD_BOT_TOKEN", "")
    if not bot_token or not guild_id or not event_id:
        return False
    try:
        resp = requests.delete(
            f"{API_BASE}/guilds/{guild_id}/scheduled-events/{event_id}",
            headers=_headers(),
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        logger.exception("Discord scheduled event delete error for guild %s", guild_id)
        return False
    # 404 means someone already removed it in Discord, which is the state we wanted.
    return resp.status_code in (200, 204, 404)


def sync_club_events(club):
    """Create Discord events for this club's non-auction calendar events. Returns how many.

    Only future events are eligible — Discord rejects a start time in the past. Each event is
    attempted once (``discord_event_attempted``) so a permanent failure, like the bot lacking
    Manage Events, doesn't get retried on every run.
    """
    from auctions.models import ClubEvent

    if not (club.discord_server_id and club.create_discord_events_for_club_events):
        return 0
    if not getattr(settings, "DISCORD_BOT_TOKEN", ""):
        return 0

    now = timezone.now()
    pending = club.events.filter(
        is_deleted=False,
        cancelled=False,
        discord_event_attempted=False,
        date_start__gt=now,
    ).exclude(source=ClubEvent.SOURCE_AUCTION)

    created = 0
    for event in pending:
        event_id = create_scheduled_event(
            guild_id=club.discord_server_id,
            name=event.title,
            start_time=event.date_start,
            end_time=event.effective_end,
            location=event.location or _club_event_fallback_location(event),
            description=event.description,
        )
        event.discord_event_attempted = True
        event.discord_event_id = event_id or ""
        event.save(update_fields=["discord_event_attempted", "discord_event_id"])
        if event_id:
            created += 1
    return created


def _club_event_fallback_location(event):
    """Discord insists on a location for external events; fall back to the club page."""
    domain = Site.objects.get_current().domain
    return f"https://{domain}{event.get_absolute_url()}"
