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

# The one scope every Calendar call here needs. Checked against what Google says it actually
# granted (see exchange_code): asking for a scope and being handed a token without it is a real
# state, and the only place it can be caught before it turns into a 403 hours later.
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.app.created"

# Google rejects a syncToken once it's too old (or after we change what we ask for). When that
# happens the only fix is to forget the token and do a fresh full pull.
SYNC_TOKEN_GONE = 410

TIMEOUT = 15

# A first pull asks for a bounded window rather than everything. Without an upper bound a single
# never-ending weekly meeting expands (singleEvents=true) into an instance per week forever, and
# each one would become a club event, a club-page row and a Discord event.
PULL_WINDOW_BEFORE = datetime.timedelta(days=30)
PULL_WINDOW_AHEAD = datetime.timedelta(days=400)

# Hard stop on pagination, so a response that keeps handing back the same page token can't spin.
# At 250 events a page this is far more than a club calendar holds in the window above.
MAX_PULL_PAGES = 20


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
    # What Google *granted*, which is not the same as what we asked for. A partial grant still
    # returns a code and a refresh token, so without this check the connection is recorded as a
    # success and the first Calendar call comes back "insufficient authentication scopes" -- an
    # error about the token, surfacing on a page that has nothing to do with consent.
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
    """Best-effort lookup of which Google account authorized us, for display only.

    Skipped entirely unless userinfo.email was granted, which by default it isn't -- see the
    GOOGLE_CALENDAR_SCOPE comment in settings.py for why the calendar scope now travels alone.
    Kept working for a site that deliberately widens the scope, and a doomed request otherwise.
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
    """Get or create this club's calendar.

    Safe to call repeatedly — it only creates a calendar when the club doesn't have one yet, and
    verifies an existing one still exists (an admin may have deleted it in Google Calendar).

    Deliberately does *not* touch the calendar's sharing (ACL) rules. Doing that needs the
    ``calendar.acls`` or ``calendar`` scope, both of which grant control over every calendar the
    admin owns and put the OAuth app into Google's sensitive-scope verification track — the exact
    trade this integration is built to avoid. Admins make the calendar public themselves, in
    Google Calendar, in a few clicks; ``Club.google_calendar_is_public`` records that they have.
    """
    if club.google_calendar_id:
        # 404 only. A 403 is "you may not touch this" -- a missing scope, a rate limit, a calendar
        # that now belongs to a different Google account -- and none of those mean the admin
        # deleted it. Treating them the same used to throw away every event link on this club for
        # a temporary error, and re-push each event as a duplicate afterwards.
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
    # Nothing is thrown away until the replacement exists. The old id and the events' google_event_ids
    # are the only record of what is already in somebody's calendar, and a POST that fails after they
    # were cleared leaves the club pointing at a calendar whose events it can no longer recognize --
    # which is how a later reconnect ends up duplicating every event members subscribed to.
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
    """The start/end half of the payload, written the way Google writes it.

    A repeating event goes back anchored where its series is anchored, not at whichever occurrence
    we happen to be showing — Google generates the rest from there, and sending the next occurrence
    instead would walk the whole series forward a step on every push.

    An all-day event has to go back as ``date``, not ``dateTime``: sending a datetime would quietly
    turn the club's all-day event into a timed one the first time anyone edits it here. Google's
    all-day end date is exclusive, which is exactly what ``date_end`` holds for these.
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
    """Create or update one ClubEvent in the club's Google Calendar. Returns True on success."""
    club = event.club
    if not club.google_calendar_connected:
        return False
    if event.cancelled and not event.google_event_id:
        # Nothing to call off over there, and Google has no use for an event that arrives
        # already cancelled.
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
    — we never let a pull resurrect something we deleted, or flip a generated event's identity.
    """
    if not club.google_calendar_connected:
        return (0, 0, 0)

    calendar_id = _quote(club.google_calendar_id)
    # Deliberately *not* singleEvents: a repeating event comes back once, as itself, with its
    # rule attached. Asking Google to expand it instead turned one weekly meeting into an event
    # per week here — see auctions/recurrence.py.
    base_params = {"showDeleted": "true", "maxResults": 250}
    if club.google_calendar_sync_token:
        base_params["syncToken"] = club.google_calendar_sync_token
    else:
        # First run: a bounded window, not years of history or an endless recurrence.
        now = timezone.now()
        base_params["timeMin"] = (now - PULL_WINDOW_BEFORE).isoformat()
        base_params["timeMax"] = (now + PULL_WINDOW_AHEAD).isoformat()

    created = updated = deleted = 0
    next_sync_token = ""
    params = dict(base_params)
    for page_number in range(1, MAX_PULL_PAGES + 1):
        page = _request(club, "GET", f"/calendars/{calendar_id}/events", params=params, allow_status=(SYNC_TOKEN_GONE,))
        if page == SYNC_TOKEN_GONE:
            # Token expired. Start over from scratch on the next run rather than looping here.
            logger.info("Google sync token expired for club %s; will do a full pull next time.", club.pk)
            club.google_calendar_sync_token = ""
            club.save(update_fields=["google_calendar_sync_token"])
            return (created, updated, deleted)

        # Series before their own exceptions, so "this one occurrence moved" always has the
        # series it belongs to to attach itself to.
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
            # Give up rather than spin. Leaving the token empty means the next run starts the
            # window again, which is the right thing if this was a one-off flood.
            logger.warning("Stopped pulling club %s's calendar after %s pages.", club.pk, MAX_PULL_PAGES)
            next_sync_token = ""
            break
        # Every page of a listing has to carry the same query, or page two quietly reverts to
        # Google's defaults: deletions hidden and recurring events unexpanded.
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
    """One occurrence of a series that Google keeps its own record of — moved, or called off.

    Either way that occurrence stops being generated from the rule (an EXDATE), so the series and
    the changed occurrence can't both claim the same slot. A moved one then lives on as an
    ordinary event of its own; a cancelled one simply doesn't happen.
    """
    from auctions.models import ClubEvent

    master = ClubEvent.objects.filter(club=club, google_event_id=item["recurringEventId"]).first()
    original_start = _parse_google_datetime(item.get("originalStartTime"))
    if master and original_start and master.is_recurring:
        _exclude_occurrence(master, original_start)

    if item.get("status") == "cancelled":
        # Excluding it from the rule is the whole story, unless we'd already made a row for a
        # moved copy of this occurrence.
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
    """(anchor, rule, start, end) for one pulled item.

    A plain event is its own start and end and has no rule. A series keeps Google's start as the
    anchor the rule is measured from, and takes ``date_start``/``date_end`` from the occurrence
    that's on now or next, so the club page, the membership emails and Discord all see a date
    that means something without knowing anything about recurrence.
    """
    from auctions import recurrence

    lines = recurrence.clean_lines(item.get("recurrence"))
    if not lines:
        return (None, "", start, end)
    # Occurrences called off here (Google records those separately, as instances) would come back
    # every time the series itself is edited, so they're carried across.
    if existing and existing.recurrence:
        kept = [line for line in existing.recurrence_lines if line.upper().startswith("EXDATE") and line not in lines]
        lines = lines + kept
    length = (end - start) if (end and end > start) else datetime.timedelta(hours=2)
    occurrence = recurrence.current_or_next(start, lines, length, timezone.now())
    if not occurrence:
        # An unreadable rule: keep the event, treat it as the one-off Google says it starts as.
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
            # The auction or pickup time is still real — someone deleted its calendar entry.
            # Put it back on the next push rather than dropping it from the club page.
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
            # Generated events are owned by the auction; a Google-side edit doesn't win.
            return ""
        if existing.needs_google_sync:
            # We have an edit of our own that hasn't reached Google yet (a push that failed, or
            # one that hasn't run). Taking Google's copy here would silently throw it away.
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
        # Content came *from* Google, so don't bounce it straight back — but Discord hasn't
        # heard about it, and this is the only place that would ever tell it.
        existing.needs_google_sync = False
        existing.needs_discord_sync = True
        existing.save()
        return "updated"

    # An event we've never seen. It might still be one of ours if a push succeeded but we failed
    # to record the id — match on the uuid we stamp into extendedProperties. Anything carrying our
    # uuid is ours either way, so it never becomes a second, Google-sourced club event.
    private = (item.get("extendedProperties") or {}).get("private") or {}
    our_uuid = private.get("auctionSiteEventUuid")
    if our_uuid:
        claimed = ClubEvent.objects.filter(club=club, uuid=our_uuid).first()
        if claimed and not claimed.google_event_id:
            claimed.google_event_id = google_id
            claimed.needs_google_sync = False
            claimed.save(update_fields=["google_event_id", "needs_google_sync"])
        # Already knowing this event by a *different* id means this is a second copy of it — an
        # instance of a recurring series, or a duplicate the admin made in Google. Claiming it
        # would repoint us at the copy and orphan the original.
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
    # Stamped here rather than inside pull_events, so "last sync" means a round trip that worked
    # and not a run that gave up on an expired token half way through.
    club.google_calendar_last_sync = timezone.now()
    club.google_calendar_last_error = ""
    club.save(update_fields=["google_calendar_last_sync", "google_calendar_last_error"])
    return True


def disconnect(club, error=""):
    """Forget this club's Google connection. The calendar itself stays in their account.

    The calendar id and the events' Google ids are kept on purpose. Reconnecting the same Google
    account then picks up the same calendar and *updates* the events already in it — members who
    subscribed keep the calendar they subscribed to. Reconnecting a different account can't see
    that calendar, and ``ensure_calendar()`` notices, drops the stale ids and starts a new one.
    """
    club.google_calendar_refresh_token = ""
    club.google_calendar_access_token = ""
    club.google_calendar_token_expires = None
    club.google_calendar_account_email = ""
    club.google_calendar_sync_token = ""
    club.google_calendar_connected_on = None
    club.google_calendar_last_error = error
    club.save(
        update_fields=[
            "google_calendar_refresh_token",
            "google_calendar_access_token",
            "google_calendar_token_expires",
            "google_calendar_account_email",
            "google_calendar_sync_token",
            "google_calendar_connected_on",
            "google_calendar_last_error",
        ]
    )
    # Everything is queued to go back out, so reconnecting catches the calendar up on whatever
    # changed while it was disconnected.
    club.events.filter(is_deleted=False).update(needs_google_sync=True)


def is_calendar_public(club):
    """True when the club's calendar really is shared publicly.

    We can't read sharing through the API — that needs a scope over every calendar the admin owns
    (see ``ensure_calendar``). But a public calendar has a public iCal feed, so asking for it
    without credentials answers the question the honest way: 200 means members can subscribe.
    """
    url = club.google_calendar_ical_url_candidate
    if not url:
        return False
    try:
        resp = requests.get(url, timeout=TIMEOUT)
    except requests.RequestException as exc:
        msg = f"Could not reach Google to check the calendar's sharing: {exc}"
        raise GoogleCalendarError(msg) from exc
    return resp.status_code == 200
