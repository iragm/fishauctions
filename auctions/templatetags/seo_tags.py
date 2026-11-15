"""Template tags for what a search engine or a link preview reads: absolute URLs, a plain-text excerpt,
and the schema.org Event JSON-LD built in ``auctions/structured_data.py``.
"""

import html
import re

from django import template
from django.utils.html import strip_tags

from auctions import structured_data

register = template.Library()


@register.simple_tag(takes_context=True)
def absolute_uri(context, value):
    """A full URL for a path or URL: a relative ``/media/...`` gets the site's origin, ``http...`` is left alone."""
    if not value:
        return ""
    value = str(value)
    if value.startswith("http"):
        return value
    request = context.get("request")
    return request.build_absolute_uri(value) if request else value


@register.filter
def plain_excerpt(value, length=200):
    """HTML to one line of plain text, cut at ``length`` characters."""
    text = re.sub(r"\s+", " ", html.unescape(strip_tags(value or ""))).strip()
    return text if len(text) <= length else text[: length - 1].rstrip() + "…"


@register.simple_tag
def auction_summary(auction):
    """``March 05, 2026 · Club name · Online``, the one line under an auction in a link preview."""
    return structured_data.auction_summary(auction)


@register.simple_tag(takes_context=True)
def auction_json_ld(context, auction):
    """The auction's Event as a JSON-LD script, or nothing when it isn't promoted or the page is private."""
    request = context.get("request")
    if request is None or getattr(request, "is_private_page", False):
        return ""
    event = structured_data.auction_event(request, auction)
    return structured_data.script_tag(event) if event else ""


@register.simple_tag(takes_context=True)
def club_json_ld(context, club, events):
    """The club page's upcoming events as a JSON-LD script, or nothing."""
    request = context.get("request")
    if request is None or getattr(request, "is_private_page", False):
        return ""
    data = structured_data.club_events(request, club, events)
    return structured_data.script_tag(data) if data else ""
