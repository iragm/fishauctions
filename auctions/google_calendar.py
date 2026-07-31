"""Two-way Google Calendar sync for clubs.

A club admin authorizes this site against their own Google account (see the GoogleCalendar*View
classes in views.py). We then create a *secondary* calendar in that account — "<Club> Events" —
and keep it in step with the club's ClubEvent rows:

    site  -> Google   push_event() / delete_event(), driven by needs_google_sync
    Google -> site    pull_events(), an incremental sync using Google's syncToken

Because we only ever touch the calendar we created, the default OAuth scope is
``calendar.app.created`` rather than full calendar access. That keeps the site out of Google's
sensitive-scope verification track while still doing everything the integration needs.

All Google API access goes through _request(); tests mock that single entry point.
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

# Google rejects a syncToken once it's too old (or after we change what we ask for). When that
# happens the only fix is to forget the token and do a fresh full pull.
SYNC_TOKEN_GONE = 410

TIMEOUT = 15


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
        # offline + consent is the only combination that reliably returns a refresh token,
        # including for an admin who has authorized this site before.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code, redirect_uri):
    """Swap an OAuth authorization code for tokens.

    Returns (refresh_token, access_token, expires_in, account_email).
    """
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
    access_token = payload.get("access_token", "")
    return refresh_token, access_token, payload.get("expires_in", 3600), _account_email(access_token)


def _account_email(access_token):
    """Best-effort lookup of which Google account authorized us, for display only."""
    if not access_token:
        return ""
    try:
        resp = requests.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=TIMEOUT)
        if resp.status_code == 200:
            return resp.json().get("email", "")
    except requests.RequestException:
        logger.info("Could not read the Google account email; continuing without it.")
    return ""


def _readable_error(resp):
    """Pull the human-readable message out of a Google error body, for last_error / messages."""
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
        # A revoked or expired refresh token can never recover on its own — clear the connection
        # so the settings page prompts the admin to reconnect instead of failing silently forever.
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
    """Make an authenticated Calendar API call. Returns the parsed body (or {} for 204s).

    ``allow_status`` lists extra status codes the caller wants to handle itself; those come back
    as the integer status instead of raising.
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
    """Get or create this club's calendar, applying the current public/private setting.

    Safe to call repeatedly — it only creates a calendar when the club doesn't have one yet, and
    verifies an existing one still exists (an admin may have deleted it in Google Calendar).
    """
    if club.google_calendar_id:
        existing = _request(club, "GET", f"/calendars/{_quote(club.google_calendar_id)}", allow_status=(404, 403))
        if existing not in (404, 403):
            _apply_sharing(club)
            return club.google_calendar_id
        # The calendar is gone on Google's side. Drop the stale id (and sync token) and make a
        # new one, then let the events re-push.
        logger.info("Google calendar %s for club %s no longer exists; recreating.", club.google_calendar_id, club.pk)
        club.google_calendar_id = ""
        club.google_calendar_sync_token = ""
        club.events.filter(is_deleted=False).update(google_event_id="", needs_google_sync=True)

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
    club.google_calendar_id = created.get("id", "")
    club.save(update_fields=["google_calendar_id", "google_calendar_sync_token"])
    _apply_sharing(club)
    return club.google_calendar_id


def _apply_sharing(club):
    """Make the calendar world-readable, or take that back, per google_calendar_is_public."""
    path = f"/calendars/{_quote(club.google_calendar_id)}/acl"
    if club.google_calendar_is_public:
        _request(
            club,
            "POST",
            path,
            json={"role": "reader", "scope": {"type": "default"}},
            # 409 means the public rule is already there, which is exactly what we wanted.
            allow_status=(409,),
        )
    else:
        _request(club, "DELETE", f"{path}/default", allow_status=(404,))


def _quote(calendar_id):
    return quote(calendar_id, safe="")


def _event_body(event):
    """Build the Google event payload for one ClubEvent."""
    body = {
        "summary": event.title,
        "description": event.description or "",
        "location": event.location or "",
        "start": {"dateTime": event.date_start.isoformat(), "timeZone": settings.TIME_ZONE},
        "end": {"dateTime": event.effective_end.isoformat(), "timeZone": settings.TIME_ZONE},
        "status": "cancelled" if event.cancelled else "confirmed",
        # Lets pull_events() recognize our own writes and skip them.
        "extendedProperties": {"private": {"auctionSiteEventUuid": str(event.uuid)}},
    }
    if event.source == event.SOURCE_AUCTION and event.auction_id:
        body["source"] = {"title": event.title, "url": _absolute_auction_url(event)}
    return body


def _absolute_auction_url(event):
    domain = Site.objects.get_current().domain
    return f"https://{domain}{event.auction.get_absolute_url()}"


def push_event(event):
    """Create or update one ClubEvent in the club's Google Calendar. Returns True on success."""
    club = event.club
    if not club.google_calendar_connected:
        return False
    body = _event_body(event)
    calendar_id = _quote(club.google_calendar_id)
    if event.google_event_id:
        result = _request(
            club,
            "PUT",
            f"/calendars/{calendar_id}/events/{_quote(event.google_event_id)}",
            json=body,
            # If the event vanished on Google's side, fall through and recreate it.
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
    """Push every event that's waiting to go to Google.

    Returns (pushed, first_error). One event Google won't accept — a title it dislikes, a
    date it rejects — must not stop the rest of the club's events from syncing, or stop the
    pull that runs after this, so failures are collected rather than raised. The caller
    surfaces the first one on the club's settings page.
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
    """Parse a Google start/end block into an aware datetime, or None.

    All-day events come back as {"date": "2026-08-01"}; timed ones as {"dateTime": "..."}.
    """
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
    """Pull changes from Google into ClubEvent rows. Returns (created, updated, deleted).

    Uses Google's syncToken so each run only fetches what changed. Events that originated on
    this site are recognized by their extendedProperties and only have their *content* updated
    — we never let a pull resurrect something we deleted, or flip an auction event's identity.
    """
    from auctions.models import ClubEvent

    if not club.google_calendar_connected:
        return (0, 0, 0)

    calendar_id = _quote(club.google_calendar_id)
    params = {"showDeleted": "true", "maxResults": 250, "singleEvents": "true"}
    if club.google_calendar_sync_token:
        params["syncToken"] = club.google_calendar_sync_token
    else:
        # First run: don't drag in years of history, just what's current and upcoming.
        params["timeMin"] = (timezone.now() - datetime.timedelta(days=30)).isoformat()

    created = updated = deleted = 0
    next_sync_token = ""
    while True:
        page = _request(club, "GET", f"/calendars/{calendar_id}/events", params=params, allow_status=(SYNC_TOKEN_GONE,))
        if page == SYNC_TOKEN_GONE:
            # Token expired. Start over from scratch on the next run rather than looping here.
            logger.info("Google sync token expired for club %s; will do a full pull next time.", club.pk)
            club.google_calendar_sync_token = ""
            club.save(update_fields=["google_calendar_sync_token"])
            return (created, updated, deleted)

        for item in page.get("items", []):
            google_id = item.get("id", "")
            if not google_id:
                continue
            existing = ClubEvent.objects.filter(club=club, google_event_id=google_id).first()
            if item.get("status") == "cancelled":
                if existing and not existing.is_deleted:
                    if existing.source == ClubEvent.SOURCE_AUCTION:
                        # The auction is still real — someone deleted its calendar entry. Put it
                        # back on the next push rather than dropping it from the club page.
                        existing.google_event_id = ""
                        existing.needs_google_sync = True
                        existing.save(update_fields=["google_event_id", "needs_google_sync"])
                    else:
                        existing.is_deleted = True
                        existing.save(update_fields=["is_deleted"])
                        deleted += 1
                continue

            start = _parse_google_datetime(item.get("start"))
            if not start:
                continue
            end = _parse_google_datetime(item.get("end"))
            title = item.get("summary") or "(untitled event)"
            description = item.get("description") or ""
            location = item.get("location") or ""

            if existing:
                if existing.source == ClubEvent.SOURCE_AUCTION:
                    # Auction events are owned by the auction; a Google-side edit doesn't win.
                    continue
                changed = (
                    existing.title != title
                    or existing.description != description
                    or existing.location != location
                    or existing.date_start != start
                    or existing.date_end != end
                )
                if changed:
                    existing.title = title
                    existing.description = description
                    existing.location = location
                    existing.date_start = start
                    existing.date_end = end
                    existing.is_deleted = False
                    # Content came *from* Google, so don't bounce it straight back.
                    existing.needs_google_sync = False
                    existing.save()
                    updated += 1
                continue

            # An event we've never seen. It might still be one of ours if a push succeeded but
            # we failed to record the id — match on the uuid we stamp into extendedProperties.
            private = (item.get("extendedProperties") or {}).get("private") or {}
            our_uuid = private.get("auctionSiteEventUuid")
            if our_uuid:
                claimed = ClubEvent.objects.filter(club=club, uuid=our_uuid).first()
                if claimed:
                    claimed.google_event_id = google_id
                    claimed.needs_google_sync = False
                    claimed.save(update_fields=["google_event_id", "needs_google_sync"])
                    continue

            ClubEvent.objects.create(
                club=club,
                title=title,
                description=description,
                location=location,
                date_start=start,
                date_end=end,
                source=ClubEvent.SOURCE_GOOGLE,
                google_event_id=google_id,
                needs_google_sync=False,
            )
            created += 1

        next_sync_token = page.get("nextSyncToken", "") or next_sync_token
        page_token = page.get("nextPageToken")
        if not page_token:
            break
        params = {"pageToken": page_token}

    club.google_calendar_sync_token = next_sync_token
    club.google_calendar_last_sync = timezone.now()
    club.save(update_fields=["google_calendar_sync_token", "google_calendar_last_sync"])
    return (created, updated, deleted)


def sync_club(club):
    """One full round trip for a club: push what's pending, then pull what changed.

    Errors are recorded on the club (and surfaced on the settings page) rather than raised, so a
    single broken connection can't stop the periodic task from servicing every other club.
    """
    if not club.google_calendar_connected:
        return False
    try:
        ensure_calendar(club)
        _pushed, push_error = push_pending(club)
        # Pull regardless of a push failure, so a single rejected event can't cut the club off
        # from changes made in Google Calendar.
        pull_events(club)
        if push_error:
            raise push_error
    except GoogleCalendarError as exc:
        club.google_calendar_last_error = str(exc)[:1000]
        club.save(update_fields=["google_calendar_last_error"])
        logger.warning("Google Calendar sync failed for club %s: %s", club.pk, exc)
        return False
    if club.google_calendar_last_error:
        club.google_calendar_last_error = ""
        club.save(update_fields=["google_calendar_last_error"])
    return True


def disconnect(club, error=""):
    """Forget this club's Google connection. The calendar itself stays in their account."""
    club.google_calendar_refresh_token = ""
    club.google_calendar_access_token = ""
    club.google_calendar_token_expires = None
    club.google_calendar_id = ""
    club.google_calendar_account_email = ""
    club.google_calendar_sync_token = ""
    club.google_calendar_connected_on = None
    club.google_calendar_last_error = error
    club.save(
        update_fields=[
            "google_calendar_refresh_token",
            "google_calendar_access_token",
            "google_calendar_token_expires",
            "google_calendar_id",
            "google_calendar_account_email",
            "google_calendar_sync_token",
            "google_calendar_connected_on",
            "google_calendar_last_error",
        ]
    )
    # Events stay on the club page; they just aren't linked to a Google event any more.
    club.events.exclude(google_event_id="").update(google_event_id="", needs_google_sync=True)
