"""schema.org Event data (JSON-LD) for the auction page and the club page, so a search engine can show
the event itself: when, where, and who runs it.

Only auctions the club chose to promote get one (``promote_this_auction`` and not deleted); a test or
private auction says nothing to a crawler. :func:`script_tag` is the only way the data reaches a
page: names are typed by members, so ``<``, ``>`` and ``&`` are escaped and a title can't close the
``<script>`` element. The template tags that call this are in ``templatetags/seo_tags.py``.
"""

import json

from django.urls import reverse
from django.utils.safestring import mark_safe

from auctions.models import Club

_ESCAPES = {ord("<"): "\\u003C", ord(">"): "\\u003E", ord("&"): "\\u0026"}
ONLINE_MODE = "https://schema.org/OnlineEventAttendanceMode"
OFFLINE_MODE = "https://schema.org/OfflineEventAttendanceMode"


def script_tag(data):
    """``<script type="application/ld+json">`` for data (a dict or list), safe to put in HTML."""
    body = json.dumps(data, ensure_ascii=True).translate(_ESCAPES)
    return mark_safe(f'<script type="application/ld+json">{body}</script>')  # noqa: S308 - escaped above


def _absolute(request, value):
    if not value:
        return ""
    return value if str(value).startswith("http") else request.build_absolute_uri(value)


def club_url(request, club):
    return request.build_absolute_uri(reverse("club_detail", kwargs={"slug": club.slug}))


def _organizer(request, club):
    return {"@type": "Organization", "name": club.name, "url": club_url(request, club)}


def auction_summary(auction):
    """One plain line for a link preview: the date, who runs it, and online or in person."""
    when = auction.date_start.astimezone(auction.timezone).strftime("%B %d, %Y").replace(" 0", " ")
    who = auction.club.name if auction.club_id else ""
    if not who and not auction.is_online and auction.physical_locations:
        who = auction.physical_locations[0].name or ""
    return " · ".join(part for part in (when, who, "Online" if auction.is_online else "In person") if part)


def auction_event(request, auction):
    """The Event for a promoted auction, or None."""
    if not auction.promote_this_auction or auction.is_deleted:
        return None
    zone = auction.timezone
    url = request.build_absolute_uri(auction.get_absolute_url())
    event = {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": auction.title,
        "startDate": auction.date_start.astimezone(zone).isoformat(),
        "url": url,
        "eventStatus": "https://schema.org/EventScheduled",
        "eventAttendanceMode": ONLINE_MODE if auction.is_online else OFFLINE_MODE,
    }
    if auction.date_end:
        event["endDate"] = auction.date_end.astimezone(zone).isoformat()
    if auction.is_online:
        event["location"] = {"@type": "VirtualLocation", "url": url}
    else:
        place = {}
        first = next((loc for loc in auction.physical_locations if loc.name or loc.address), None)
        if first:
            place = {"name": first.name or "", "address": first.address or ""}
        elif auction.club_id and auction.club.location:
            place = {"name": auction.club.name, "address": auction.club.location}
        place = {key: value for key, value in place.items() if value}
        if place:
            event["location"] = {"@type": "Place", **place}
    club = auction.club if auction.club_id else None
    if club:
        event["organizer"] = _organizer(request, club)
        if club.icon:
            event["image"] = _absolute(request, club.icon_display_url)
    return event


def club_events(request, club, events):
    """Events the club page lists, as a list of schema.org Events; empty for a club that isn't listed."""
    if not (club.active and club.outreach_stage == Club.LISTED):
        return []
    result = []
    for item in events:
        url = request.build_absolute_uri(item.get_absolute_url())
        event = {
            "@type": "Event",
            "name": item.title,
            "startDate": item.date_start.isoformat(),
            "url": url,
            "eventStatus": "https://schema.org/EventScheduled",
            "eventAttendanceMode": OFFLINE_MODE,
            "organizer": _organizer(request, club),
        }
        if item.date_end:
            event["endDate"] = item.date_end.isoformat()
        if item.location:
            event["location"] = {"@type": "Place", "name": item.location, "address": item.location}
        else:
            event["eventAttendanceMode"] = ONLINE_MODE
            event["location"] = {"@type": "VirtualLocation", "url": url}
        result.append(event)
    return result
