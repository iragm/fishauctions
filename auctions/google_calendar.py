"""Two-way Google Calendar sync for clubs.

A club admin authorizes their Google account; we create a secondary "<Club> Events" calendar and
keep it in step with ClubEvent rows: ``push_event()``/``delete_event()`` out, ``pull_events()`` in
(syncToken). Only touching our own calendar allows the ``calendar.app.created`` scope, outside
Google's sensitive-scope review. All API calls go through ``_request()``, which tests mock.
"""

from __future__ import annotations

import datetime
import logging
from urllib.parse import quote, urlencode

import requests
from django.conf import settings
from django.contrib.sites.models import Site
from django.utils import timezone

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a secret
CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# Checked against what Google actually granted (exchange_code): a token without it 403s hours later.
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.app.created"

# An expired syncToken: forget it and do a full pull.
SYNC_TOKEN_GONE = 410

TIMEOUT = 15

# First pull window. Unbounded, a never-ending weekly meeting expands forever.
PULL_WINDOW_BEFORE = datetime.timedelta(days=30)
PULL_WINDOW_AHEAD = datetime.timedelta(days=400)

# Pagination hard stop against a repeating page token.
MAX_PULL_PAGES = 20

# How stale the "is it shared?" answer may get: quick enough to see the banner clear, cheap per club.
PUBLIC_CHECK_INTERVAL = datetime.timedelta(hours=1)


class GoogleCalendarError(Exception):
    """Raised for Google problems the caller should surface to the admin and log."""


def is_configured() -> bool:
    """True when the site has an OAuth app configured, so the integration can be offered."""
    return bool(
        getattr(settings, "GOOGLE_CALENDAR_CLIENT_ID", "") and getattr(settings, "GOOGLE_CALENDAR_CLIENT_SECRET", "")
    )


def authorize_url(redirect_uri, state):
    """The URL to send an admin to so they can grant access to their Google account."""
    params = {
        "client_id": settings.GOOGLE_CALENDAR_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": settings.GOOGLE_CALENDAR_SCOPE,
        "state": state,
        # offline + consent reliably returns a refresh token, even for repeat authorizations.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code, redirect_uri):
    """Swap an OAuth code for ``(refresh_token, access_token, expires_in, account_email)``."""
    data = {
        "code": code,
        "client_id": settings.GOOGLE_CALENDAR_CLIENT_ID,
        "client_secret": settings.GOOGLE_CALENDAR_CLIENT_SECRET,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    try:
        resp = requests.post(TOKEN_URL, data=data, timeout=TIMEOUT)
    except requests.RequestException as exc:
        msg = f"Could not reach Google: {exc}"
        raise GoogleCalendarError(msg) from exc
    if resp.status_code != 200:
        msg = f"Google rejected the authorization code: {_readable_error(resp)}"
        raise GoogleCalendarError(msg)
    payload = resp.json()
    refresh_token = payload.get("refresh_token", "")
    if not refresh_token:
        msg = (
            "Google didn't return a refresh token. Remove this site from "
            "https://myaccount.google.com/permissions and try connecting again."
        )
        raise GoogleCalendarError(msg)
    # A partial grant still returns tokens; without this check the connection looks successful.
    granted = set((payload.get("scope") or "").split())
    if granted and CALENDAR_SCOPE not in granted:
        msg = (
            "Google didn't grant access to your calendars. On the Google permission screen, tick "
            "the box about making and managing calendars, then press Continue."
        )
        raise GoogleCalendarError(msg)
    access_token = payload.get("access_token", "")
    return refresh_token, access_token, payload.get("expires_in", 3600), _account_email(access_token, granted)


def _account_email(access_token, granted_scopes=()):
    """Which Google account authorized us, for display, only if userinfo.email was granted (not by default;
    see settings.py).
    """
    if not access_token:
        return ""
    if granted_scopes and not any(scope.endswith("userinfo.email") for scope in granted_scopes):
        return ""
    try:
        resp = requests.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=TIMEOUT)
        if resp.status_code == 200:
            return resp.json().get("email", "")
    except requests.RequestException:
        logger.info("Could not read the Google account email; continuing without it.")
    return ""


def _readable_error(resp):
    """The readable message from a Google error body."""
    try:
        payload = resp.json()
    except ValueError:
        return (resp.text or "")[:300]
    error = payload.get("error")
    if isinstance(error, dict):
        return error.get("message") or str(error)[:300]
    description = payload.get("error_description")
    if description:
        return f"{error}: {description}" if error else description
    return str(error or payload)[:300]


def get_access_token(club):
    """A valid access token for this club, refreshing it when the cached one is stale."""
    if not club.google_calendar_refresh_token:
        msg = "This club has not connected Google Calendar."
        raise GoogleCalendarError(msg)
    expires = club.google_calendar_token_expires
    # Refresh a minute early so a token can't expire mid-request.
    if club.google_calendar_access_token and expires and expires > timezone.now() + datetime.timedelta(seconds=60):
        return club.google_calendar_access_token

    data = {
        "client_id": settings.GOOGLE_CALENDAR_CLIENT_ID,
        "client_secret": settings.GOOGLE_CALENDAR_CLIENT_SECRET,
        "refresh_token": club.google_calendar_refresh_token,
        "grant_type": "refresh_token",
    }
    try:
        resp = requests.post(TOKEN_URL, data=data, timeout=TIMEOUT)
    except requests.RequestException as exc:
        msg = f"Could not reach Google: {exc}"
        raise GoogleCalendarError(msg) from exc
    if resp.status_code != 200:
        # A revoked or expired refresh token never recovers: disconnect so the page prompts reconnection.
        detail = _readable_error(resp)
        if resp.status_code in (400, 401):
            disconnect(club, error=f"Google access was revoked ({detail}). Please reconnect.")
        msg = f"Google refused to refresh the access token: {detail}"
        raise GoogleCalendarError(msg)
    payload = resp.json()
    token = payload.get("access_token", "")
    club.google_calendar_access_token = token
    club.google_calendar_token_expires = timezone.now() + datetime.timedelta(
        seconds=int(payload.get("expires_in", 3600))
    )
    club.save(update_fields=["google_calendar_access_token", "google_calendar_token_expires"])
    return token


def _request(club, method, path, *, params=None, json=None, allow_status=()):
    """An authenticated Calendar API call; the parsed body, or {} for 204. ``allow_status`` codes are
    returned as ints instead of raising.
    """
    token = get_access_token(club)
    url = f"{CALENDAR_API_BASE}{path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        resp = requests.request(method, url, headers=headers, params=params, json=json, timeout=TIMEOUT)
    except requests.RequestException as exc:
        msg = f"Could not reach Google Calendar: {exc}"
        raise GoogleCalendarError(msg) from exc
    if resp.status_code in allow_status:
        return resp.status_code
    if resp.status_code not in (200, 201, 204):
        msg = f"Google Calendar {method} {path} failed: {_readable_error(resp)}"
        raise GoogleCalendarError(msg)
    if resp.status_code == 204 or not resp.content:
        return {}
    return resp.json()


def ensure_calendar(club):
    """Get or create this club's calendar, verifying an existing one still exists.

    Never touches sharing (ACLs): that needs a scope over all the admin's calendars. Admins share it
    themselves; ``Club.google_calendar_is_public`` records it.
    """
    if club.google_calendar_id:
        # 404 only. A 403 is a scope, rate limit or ownership problem, not a deletion; treating it as
        # one threw away every event link and duplicated events.
        existing = _request(club, "GET", f"/calendars/{_quote(club.google_calendar_id)}", allow_status=(404,))
        if existing != 404:
            return club.google_calendar_id
        logger.info("Google calendar %s for club %s no longer exists; recreating.", club.google_calendar_id, club.pk)

    created = _request(
        club,
        "POST",
        "/calendars",
        json={
            "summary": f"{club.name} events",
            "description": f"Events for {club.name}. Managed automatically — edits sync both ways.",
            "timeZone": settings.TIME_ZONE,
        },
    )
    # Old ids are kept until the replacement exists: they're the only record of what's in members'
    # calendars.
    club.google_calendar_id = created.get("id", "")
    club.google_calendar_sync_token = ""
    club.save(update_fields=["google_calendar_id", "google_calendar_sync_token"])
    club.events.filter(is_deleted=False).update(google_event_id="", needs_google_sync=True)
    return club.google_calendar_id


def _quote(calendar_id):
    return quote(calendar_id, safe="")


def _event_body(event):
    """Build the Google event payload for one ClubEvent."""
    body = {
        "summary": event.title,
        "description": event.description or "",
        "location": event.location or "",
        "status": "cancelled" if event.cancelled else "confirmed",
        # Lets pull_events() recognize our own writes and skip them.
        "extendedProperties": {"private": {"auctionSiteEventUuid": str(event.uuid)}},
    }
    body.update(_event_times(event))
    if event.is_recurring:
        body["recurrence"] = event.recurrence_lines
    if event.source == event.SOURCE_AUCTION and event.auction_id:
        body["source"] = {"title": event.title, "url": _absolute_auction_url(event)}
    return body


def _event_times(event):
    """The start/end payload, as Google writes it. A series is anchored at its series start, or each push
    walks it forward. All-day events go as ``date`` (exclusive end).
    """
    start = event.recurrence_start if event.is_recurring else event.date_start
    end = start + event.occurrence_length
    if not event.all_day:
        return {
            "start": {"dateTime": start.isoformat(), "timeZone": settings.TIME_ZONE},
            "end": {"dateTime": end.isoformat(), "timeZone": settings.TIME_ZONE},
        }
    start_day = timezone.localtime(start).date()
    end_day = timezone.localtime(end).date()
    if end_day <= start_day:
        end_day = start_day + datetime.timedelta(days=1)
    return {"start": {"date": start_day.isoformat()}, "end": {"date": end_day.isoformat()}}


def _absolute_auction_url(event):
    domain = Site.objects.get_current().domain
    return f"https://{domain}{event.auction.get_absolute_url()}"


def push_event(event):
    """Create or update one ClubEvent in the club's Google Calendar. True on success."""
    club = event.club
    if not club.google_calendar_connected:
        return False
    if event.cancelled and not event.google_event_id:
        # A cancelled event never pushed has nothing to cancel.
        event.needs_google_sync = False
        event.save(update_fields=["needs_google_sync"])
        return False
    body = _event_body(event)
    calendar_id = _quote(club.google_calendar_id)
    if event.google_event_id:
        result = _request(
            club,
            "PUT",
            f"/calendars/{calendar_id}/events/{_quote(event.google_event_id)}",
            json=body,
            # Gone on Google's side: recreate.
            allow_status=(404, 410),
        )
        if result in (404, 410):
            event.google_event_id = ""
        else:
            event.needs_google_sync = False
            event.save(update_fields=["needs_google_sync"])
            return True
    created = _request(club, "POST", f"/calendars/{calendar_id}/events", json=body)
    event.google_event_id = created.get("id", "")
    event.needs_google_sync = False
    event.save(update_fields=["google_event_id", "needs_google_sync"])
    return True


def delete_event(event):
    """Remove one event from the club's Google Calendar. Returns True when it's gone."""
    club = event.club
    if not (club.google_calendar_connected and event.google_event_id):
        return False
    _request(
        club,
        "DELETE",
        f"/calendars/{_quote(club.google_calendar_id)}/events/{_quote(event.google_event_id)}",
        # Already gone is a success as far as we're concerned.
        allow_status=(404, 410),
    )
    event.google_event_id = ""
    event.needs_google_sync = False
    event.save(update_fields=["google_event_id", "needs_google_sync"])
    return True


def push_pending(club):
    """Push every pending event. Returns ``(pushed, first_error)``; one rejected event doesn't stop the
    rest or the pull.
    """
    pushed = 0
    first_error = None
    for event in club.events.filter(is_deleted=False, needs_google_sync=True):
        try:
            if push_event(event):
                pushed += 1
        except GoogleCalendarError as exc:
            logger.warning("Could not push event %s for club %s to Google Calendar: %s", event.pk, club.pk, exc)
            if first_error is None:
                first_error = exc
    return (pushed, first_error)


def _parse_google_datetime(value):
    """A Google start/end block to an aware datetime, or None. All-day is ``date``, timed is ``dateTime``."""
    if not value:
        return None
    if value.get("dateTime"):
        parsed = datetime.datetime.fromisoformat(value["dateTime"])
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed)
        return parsed
    if value.get("date"):
        day = datetime.date.fromisoformat(value["date"])
        return timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))
    return None


def pull_events(club):
    """Pull changes into ClubEvent rows. Returns ``(created, updated, deleted)``. Our own events (by
    extendedProperties) only get content updates; a pull never resurrects or re-identifies them.
    """
    if not club.google_calendar_connected:
        return (0, 0, 0)

    calendar_id = _quote(club.google_calendar_id)
    # Not singleEvents: a series comes back once with its rule (auctions/recurrence.py).
    base_params = {"showDeleted": "true", "maxResults": 250}
    if club.google_calendar_sync_token:
        base_params["syncToken"] = club.google_calendar_sync_token
    else:
        # First run: a bounded window.
        now = timezone.now()
        base_params["timeMin"] = (now - PULL_WINDOW_BEFORE).isoformat()
        base_params["timeMax"] = (now + PULL_WINDOW_AHEAD).isoformat()

    created = updated = deleted = 0
    next_sync_token = ""
    params = dict(base_params)
    for page_number in range(1, MAX_PULL_PAGES + 1):
        page = _request(club, "GET", f"/calendars/{calendar_id}/events", params=params, allow_status=(SYNC_TOKEN_GONE,))
        if page == SYNC_TOKEN_GONE:
            # Expired token: full pull next run.
            logger.info("Google sync token expired for club %s; will do a full pull next time.", club.pk)
            club.google_calendar_sync_token = ""
            club.save(update_fields=["google_calendar_sync_token"])
            return (created, updated, deleted)

        # Series before their exceptions, so an exception finds its series.
        for item in sorted(page.get("items", []), key=lambda item: bool(item.get("recurringEventId"))):
            outcome = _apply_pulled_event(club, item)
            if outcome == "created":
                created += 1
            elif outcome == "updated":
                updated += 1
            elif outcome == "deleted":
                deleted += 1

        next_sync_token = page.get("nextSyncToken", "") or next_sync_token
        page_token = page.get("nextPageToken")
        if not page_token:
            break
        if page_number == MAX_PULL_PAGES:
            # Give up rather than spin; an empty token restarts the window next time.
            logger.warning("Stopped pulling club %s's calendar after %s pages.", club.pk, MAX_PULL_PAGES)
            next_sync_token = ""
            break
        # Every page must repeat the query, or page two reverts to defaults.
        params = dict(base_params, pageToken=page_token)

    club.google_calendar_sync_token = next_sync_token
    club.save(update_fields=["google_calendar_sync_token"])
    return (created, updated, deleted)


def _apply_pulled_event(club, item):
    """Apply one event from a Google listing. Returns what happened, for the caller's counts."""
    google_id = item.get("id", "")
    if not google_id:
        return ""
    if item.get("recurringEventId"):
        return _apply_pulled_instance(club, item, google_id)
    return _apply_event_item(club, item, google_id)


def _apply_pulled_instance(club, item, google_id):
    """One occurrence Google tracks separately, moved or cancelled: EXDATE it from the rule; a moved one
    becomes its own event.
    """
    from auctions.models import ClubEvent

    master = ClubEvent.objects.filter(club=club, google_event_id=item["recurringEventId"]).first()
    original_start = _parse_google_datetime(item.get("originalStartTime"))
    if master and original_start and master.is_recurring:
        _exclude_occurrence(master, original_start)

    if item.get("status") == "cancelled":
        # The EXDATE is enough unless we'd made a row for the moved copy.
        moved_copy = ClubEvent.objects.filter(club=club, google_event_id=google_id, is_deleted=False).first()
        if not moved_copy:
            return ""
        moved_copy.is_deleted = True
        moved_copy.save(update_fields=["is_deleted"])
        return "deleted"

    return _apply_event_item(club, item, google_id)


def _exclude_occurrence(master, moment):
    """Take one occurrence out of a series' rule."""
    from auctions import recurrence

    lines = recurrence.with_exdate(master.recurrence_lines, moment)
    text = recurrence.to_text(lines)
    if text == master.recurrence:
        return
    master.recurrence = text
    master.save(update_fields=["recurrence"])
    master.refresh_occurrence()


def _series_times(item, start, end, existing):
    """``(anchor, rule, start, end)`` for a pulled item. A series anchors at Google's start and takes
    ``date_start``/``date_end`` from the current or next occurrence.
    """
    from auctions import recurrence

    lines = recurrence.clean_lines(item.get("recurrence"))
    if not lines:
        return (None, "", start, end)
    # Keep EXDATEs we recorded; Google stores them as instances, lost when the series is edited.
    if existing and existing.recurrence:
        kept = [line for line in existing.recurrence_lines if line.upper().startswith("EXDATE") and line not in lines]
        lines = lines + kept
    length = (end - start) if (end and end > start) else datetime.timedelta(hours=2)
    occurrence = recurrence.current_or_next(start, lines, length, timezone.now())
    if not occurrence:
        # Unreadable rule: treat as a one-off.
        return (None, "", start, end)
    return (start, recurrence.to_text(lines), occurrence, occurrence + length)


def _apply_event_item(club, item, google_id):
    """Apply one plain event — or one series, taken as a whole."""
    from auctions.models import ClubEvent

    existing = ClubEvent.objects.filter(club=club, google_event_id=google_id).first()

    if item.get("status") == "cancelled":
        if not existing or existing.is_deleted:
            return ""
        if existing.is_automatic:
            # The auction or pickup is still real; re-push rather than drop it.
            existing.google_event_id = ""
            existing.needs_google_sync = True
            existing.save(update_fields=["google_event_id", "needs_google_sync"])
            return ""
        existing.is_deleted = True
        existing.save(update_fields=["is_deleted"])
        return "deleted"

    start = _parse_google_datetime(item.get("start"))
    if not start:
        return ""
    end = _parse_google_datetime(item.get("end"))
    all_day = bool((item.get("start") or {}).get("date"))
    title = item.get("summary") or "(untitled event)"
    description = item.get("description") or ""
    location = item.get("location") or ""
    anchor, rule, start, end = _series_times(item, start, end, existing)

    if existing:
        if existing.is_automatic:
            # Generated events belong to the auction; Google edits don't win.
            return ""
        if existing.needs_google_sync:
            # An unpushed local edit wins over Google's copy.
            logger.info("Keeping the unsynced local copy of event %s rather than Google's.", existing.pk)
            return ""
        changed = (
            existing.title != title
            or existing.description != description
            or existing.location != location
            or existing.date_start != start
            or existing.date_end != end
            or existing.all_day != all_day
            or existing.recurrence != rule
            or existing.recurrence_start != anchor
        )
        if not changed:
            return ""
        existing.title = title
        existing.description = description
        existing.location = location
        existing.date_start = start
        existing.date_end = end
        existing.all_day = all_day
        existing.recurrence = rule
        existing.recurrence_start = anchor
        existing.is_deleted = False
        # Came from Google, so don't push back, but Discord needs to hear.
        existing.needs_google_sync = False
        existing.needs_discord_sync = True
        existing.save()
        return "updated"

    # Unknown id: may still be ours if a push succeeded without recording the id (match our uuid).
    private = (item.get("extendedProperties") or {}).get("private") or {}
    our_uuid = private.get("auctionSiteEventUuid")
    if our_uuid:
        claimed = ClubEvent.objects.filter(club=club, uuid=our_uuid).first()
        if claimed and not claimed.google_event_id:
            claimed.google_event_id = google_id
            claimed.needs_google_sync = False
            claimed.save(update_fields=["google_event_id", "needs_google_sync"])
        # Known under a different id: this is a copy (series instance or duplicate); don't claim it.
        return ""

    ClubEvent.objects.create(
        club=club,
        title=title,
        description=description,
        location=location,
        date_start=start,
        date_end=end,
        all_day=all_day,
        recurrence=rule,
        recurrence_start=anchor,
        source=ClubEvent.SOURCE_GOOGLE,
        google_event_id=google_id,
        needs_google_sync=False,
    )
    return "created"


def sync_club(club):
    """One round trip for a club: push pending, then pull. Errors are recorded on the club, not raised."""
    if not club.google_calendar_connected:
        return False
    try:
        ensure_calendar(club)
        _pushed, push_error = push_pending(club)
        # Pull even after a push failure.
        pull_events(club)
        if push_error:
            raise push_error
    except GoogleCalendarError as exc:
        club.google_calendar_last_error = str(exc)[:1000]
        club.save(update_fields=["google_calendar_last_error"])
        logger.warning("Google Calendar sync failed for club %s: %s", club.pk, exc)
        return False
    # Stamped here, so "last sync" means a round trip that worked.
    club.google_calendar_last_sync = timezone.now()
    club.google_calendar_last_error = ""
    club.save(update_fields=["google_calendar_last_sync", "google_calendar_last_error"])
    # Last, after success is recorded. Rate-limited; never raises.
    refresh_public_flag(club)
    return True


def disconnect(club, error=""):
    """Forget this club's Google connection; the calendar stays in their account.

    Calendar and event ids are kept, so reconnecting the same account updates the same calendar.
    A different account can't see it, and ``ensure_calendar()`` starts fresh.
    """
    club.google_calendar_refresh_token = ""
    club.google_calendar_access_token = ""
    club.google_calendar_token_expires = None
    club.google_calendar_account_email = ""
    club.google_calendar_sync_token = ""
    club.google_calendar_connected_on = None
    club.google_calendar_last_error = error
    # A different account gets a new private calendar; don't advertise the old one as public.
    club.google_calendar_is_public = False
    club.google_calendar_public_checked = None
    club.save(
        update_fields=[
            "google_calendar_refresh_token",
            "google_calendar_access_token",
            "google_calendar_token_expires",
            "google_calendar_account_email",
            "google_calendar_sync_token",
            "google_calendar_connected_on",
            "google_calendar_last_error",
            "google_calendar_is_public",
            "google_calendar_public_checked",
        ]
    )
    # Queue everything so a reconnect catches up.
    club.events.filter(is_deleted=False).update(needs_google_sync=True)


def is_calendar_public(club):
    """True when the calendar is really public: its iCal feed answers 200 without credentials."""
    url = club.google_calendar_ical_url_candidate
    if not url:
        return False
    try:
        resp = requests.get(url, timeout=TIMEOUT)
    except requests.RequestException as exc:
        msg = f"Could not reach Google to check the calendar's sharing: {exc}"
        raise GoogleCalendarError(msg) from exc
    return resp.status_code == 200


def refresh_public_flag(club, *, force=False):
    """Set ``club.google_calendar_is_public`` from what Google serves. Returns True when it changed.

    Sharing is read, not an admin checkbox (which went stale both ways). A network error leaves the flag alone.
    """
    if not club.google_calendar_connected:
        return False
    if not force and club.google_calendar_public_checked:
        if timezone.now() - club.google_calendar_public_checked < PUBLIC_CHECK_INTERVAL:
            return False
    try:
        public = is_calendar_public(club)
    except GoogleCalendarError as exc:
        logger.info("Could not check calendar sharing for club %s: %s", club.pk, exc)
        return False
    changed = club.google_calendar_is_public != public
    club.google_calendar_is_public = public
    club.google_calendar_public_checked = timezone.now()
    club.save(update_fields=["google_calendar_is_public", "google_calendar_public_checked"])
    return changed
