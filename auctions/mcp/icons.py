"""Icons for the tools, the prompts, the resources and the server itself.

MCP 2025-11-25 lets a ``Tool``, a ``Prompt``, a ``Resource``, a ``ResourceTemplate`` and the
server's own ``Implementation`` each carry ``icons``. A host draws them beside the name in a
connector list, in a tool picker, and on the card it shows while a tool runs -- so this is the
difference between fifty-four identical grey rows and a list somebody can scan.

**They are URLs on this site, not ``data:`` URIs, and that is the whole design decision.**
``tools/list`` is around 47 KB and every host pays for it in context once a session; the same five
icons inlined would be about 400 bytes each, times fifty-four, for decoration -- while a URL is
sixty. The icons are static files behind the same nginx that serves the site's CSS, they are
fetched by the host rather than by the model, and a host that cannot fetch one draws what it drew
before. Nothing here can fail a call.

**Five icons, derived, not a table of fifty-four.** Same rule as
:func:`auctions.mcp.tools.area_of`: what a tool is about is already written down in the registry,
in its danger tier and in the parameters it takes, so asking those is cheaper than keeping a second
list in step with the first. A read is a magnifier whatever it reads; a write on an auction is a
tag; a write on a club is people.

There is deliberately no light/dark pair, though the schema has a ``theme`` field for one. The
stroke is ``#2fa4e7``, this site's link accent, which is legible on white and on near-black alike --
and a pair would double what is sent to solve a problem one well-chosen colour does not have.
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
    """The host these URLs are served from.

    ``SITE_DOMAIN`` first and the ``Site`` row only as a fallback, which is the same order
    :func:`auctions.mcp.widgets._resource_domains` uses and matters for the same reason: an origin
    this module names has to be one the site really answers on, and the two disagree on a
    deployment whose ``Site`` row was never updated. A widget's CSP is built from ``SITE_DOMAIN``,
    so an icon served from anywhere else would be an origin the sandbox blocks.
    """
    return getattr(settings, "SITE_DOMAIN", "") or Site.objects.get_current().domain


def absolute(path: str) -> str:
    """A static file's absolute URL.

    Absolute because the reader is a host somewhere else, and because there is no request here to
    build one from: ``tools/list`` is answered out of the registry, and the same descriptors are
    handed to the command palette's own model in-process.
    """
    try:
        url = static(path)
    except ValueError:
        # A hashed-manifest storage raises for a file that was not in the last ``collectstatic``.
        # An icon must never be able to fail a ``tools/list``, so the un-hashed path is what a
        # missing entry degrades to: at worst the host draws nothing, which is what it drew before.
        logger.warning("No static entry for %s; sending the plain path", path)
        url = f"{settings.STATIC_URL}{path}"
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"https://{domain()}{url}"


def _icon(name: str) -> dict[str, Any]:
    # No ``sizes``. These are SVGs, so every size is the right size, and ``["any"]`` is twenty-five
    # characters saying so once per tool -- the same arithmetic that keeps ``annotations.title``
    # and a defaulted ``idempotentHint`` out of :func:`auctions.mcp.tools.descriptor`. ``mimeType``
    # stays, because it lets a host that cannot draw SVG skip the fetch instead of making it.
    #
    # Not memoised on purpose: the fallback goes through ``Site.objects.get_current``, which keeps
    # its own per-process cache and clears it when the row is saved -- a cache of our own would
    # silently outlive that.
    return {"src": absolute(f"mcp/{name}.svg"), "mimeType": SVG}


def icons(name: str) -> list[dict[str, Any]]:
    """The ``icons`` array for one of :data:`READ` and friends."""
    return [_icon(name)]


def for_action(action: palette_actions.Action) -> list[dict[str, Any]]:
    """Which icon one tool gets.

    Read off the registry rather than chosen per tool. A navigate-tier action does not act at all,
    which is the most useful thing to be able to see at a glance; after that the split people
    actually care about is read against write, and then which half of the site a write is in.
    """
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
    """Which icon one prompt gets, off the arguments it already declares.

    Exactly :func:`auctions.mcp.tools.area_of`'s rule, applied to the other primitive: a recipe
    that takes a club is about a club, one that takes an auction is about an auction.
    """
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
    """The site's own mark, for the connector list a person picks this out of.

    The favicon set rather than one of the five: this is the entry somebody looks for by sight
    among every other connector they have added, and what they are looking for is the site.
    """
    return [
        # ``sizes`` here and nowhere else: these two are raster and differ only in size, which is
        # the one case where a host has a choice to make and needs to be told how to make it.
        {"src": absolute("favicon-32x32.png"), "mimeType": PNG, "sizes": ["32x32"]},
        {"src": absolute("android-chrome-192x192.png"), "mimeType": PNG, "sizes": ["192x192"]},
    ]
