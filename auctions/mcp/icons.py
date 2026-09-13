"""Icons for the tools, the prompts, the resources and the server itself.

Icons are URLs on this site, not ``data:`` URIs, so a host fetches them instead of paying for them
inline on every ``tools/list``. Five icons are derived from each item's danger tier and area rather
than kept as a table of fifty-four; no icon fetch failure can fail a call.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.contrib.sites.models import Site
from django.templatetags.static import static

from auctions import palette_actions

logger = logging.getLogger(__name__)

SVG = "image/svg+xml"
PNG = "image/png"

#: The five, by the name of their file in ``auctions/static/mcp/``.
READ = "read"
GO = "go"
AUCTION = "auction"
CLUB = "club"
EDIT = "edit"


def domain() -> str:
    """Host these URLs are served from: ``SITE_DOMAIN`` first, the ``Site`` row as fallback."""
    return getattr(settings, "SITE_DOMAIN", "") or Site.objects.get_current().domain


def absolute(path: str) -> str:
    """A static file's absolute URL; there is no request here to build a relative one from."""
    try:
        url = static(path)
    except ValueError:
        # Missing hashed-manifest entry: fall back to the plain path rather than fail the call.
        logger.warning("No static entry for %s; sending the plain path", path)
        url = f"{settings.STATIC_URL}{path}"
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"https://{domain()}{url}"


def _icon(name: str) -> dict[str, Any]:
    # No ``sizes``: SVGs, so every size is the right size. Not memoised: relies on Site's own cache.
    return {"src": absolute(f"mcp/{name}.svg"), "mimeType": SVG}


def icons(name: str) -> list[dict[str, Any]]:
    """The ``icons`` array for one of :data:`READ` and friends."""
    return [_icon(name)]


def for_action(action: palette_actions.Action) -> list[dict[str, Any]]:
    """Which icon one tool gets, read off its danger tier and area."""
    from . import tools

    if action.danger == palette_actions.DANGER_NAVIGATE:
        return icons(GO)
    if tools.read_only(action):
        return icons(READ)
    area = tools.area_of(action)
    if area == tools.AREA_CLUB:
        return icons(CLUB)
    if area == tools.AREA_AUCTION:
        return icons(AUCTION)
    return icons(EDIT)


def for_prompt(prompt) -> list[dict[str, Any]]:
    """Which icon one prompt gets, off the arguments it declares."""
    names = {argument.name for argument in prompt.arguments}
    if "club" in names and "auction" not in names:
        return icons(CLUB)
    if "auction" in names:
        return icons(AUCTION)
    return icons(EDIT)


def for_uri(uri: str) -> list[dict[str, Any]]:
    """Which icon one resource or resource template gets, from its scheme."""
    if uri.startswith("club://"):
        return icons(CLUB)
    if uri.startswith("auction://") or uri.startswith("lot://") or uri.startswith("invoice://"):
        return icons(AUCTION)
    return icons(READ)


def server() -> list[dict[str, Any]]:
    """The site's favicon set, for the connector list a person picks this server out of."""
    return [
        {"src": absolute("favicon-32x32.png"), "mimeType": PNG, "sizes": ["32x32"]},
        {"src": absolute("android-chrome-192x192.png"), "mimeType": PNG, "sizes": ["192x192"]},
    ]
