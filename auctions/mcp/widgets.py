"""Interactive views this server publishes as MCP-app widgets.

An MCP host that supports the apps surface renders a tool's answer as a *page* rather than as
JSON: it reads a ``ui://`` resource from us, mounts it in a sandboxed iframe, and pipes the tool
result into it. Four things on this site are worth looking at rather than reading out --

    ``ui://auction.fish/lot``      one lot, with its photo, its price and where to collect it
    ``ui://auction.fish/rules``    an auction's dates and the club's own rules text
    ``ui://auction.fish/invoice``  what somebody owes or is owed, itemised
    ``ui://auction.fish/card``     a membership card, with the barcode a door scanner reads

-- and each is attached to the tool that already answers that question, so nothing new has to be
called to get one. A host without the apps surface ignores ``_meta`` and shows the same JSON it
always did; that is the whole reason the widget renders from the tool's own ``structuredContent``
rather than from a payload only a widget can read. There is one answer, and the picture is a way
of looking at it.

The card is the one of the four that has to be *looked at* rather than read: a membership number
read out is a number, and a membership number drawn as a barcode is the thing the door scanner
takes. Its Renew button opens the club's payment page through the host rather than taking money --
PayPal's and Square's own scripts could not run inside the iframe if we wanted them to, and a chat
window is the wrong place to be entering a card number.

**One document, four renderers.** ``auctions/templates/auctions/mcp/widget.html`` is the only HTML
here; ``view`` is baked in per resource and the script switches on it. Four documents would be four
copies of the palette, the theming and the runtime -- the drift that
``auctions/templates/auctions/embeds/base.html`` exists to prevent for the club embeds, which is
also where this file's CSS class names come from. A club embed and a widget are the same problem
twice: HTML that has to look right somewhere that is not our page.

**The runtime is vendored, not fetched.** The iframe's CSP blocks every external script, so
``@modelcontextprotocol/ext-apps`` is inlined into the document -- see ``vendor/README.md``. It is
an ES module ending in ``export{…}``, which an inline ``<script type="module">`` cannot do, so
:func:`_bundle` rewrites that one statement into a ``globalThis`` assignment. That rewrite is the
only edit made to somebody else's bytes, and ``test_mcp_widgets`` fails if it stops matching.

**Nothing here is a capability.** Every widget is a way of *showing* an answer a registered action
already produced. Nothing here initiates a write, and every one of the four draws a thing that has
already happened, which is what keeps "a host may render this" from meaning "a host may run this".

**There is deliberately no selling console, and that is a reversal.** One was built: the lot queue
with three fields under it, validating against
:class:`auctions.views.DynamicSetLotWinner`'s own checks and recording the sale through
``set_lot_winner``. It worked, and it was the wrong thing to put in a chat window. Selling is the
busiest, most time-critical job on this site and it already has a full-screen page with a keyboard
flow, voice input and a lot queue that advances itself; a second half-sized copy of it inside an
iframe, with a debounce between the operator and every check, is not a better version of that page.
It is a *confusing* one -- two places to do the same job, one of which is quietly worse, and no way
for somebody mid-auction to tell which one they are looking at. The tools stay
(``set_lot_winner``, ``no_sale``, ``undo_sale``, ``lot_queue``), because saying "lot 14, bidder
seven, twelve dollars" out loud is a real thing to want. Drawing a form for it is not.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from django.conf import settings
from django.template.loader import render_to_string

#: The mime type that tells a host "render this, don't print it". ``RESOURCE_MIME_TYPE`` in
#: ``@modelcontextprotocol/ext-apps``; a resource served as plain ``text/html`` is shown as source.
RESOURCE_MIME_TYPE = "text/html;profile=mcp-app"

#: The flat ``_meta`` key naming a tool's widget (``RESOURCE_URI_META_KEY``). The same value also
#: goes in the nested ``ui`` object: ``registerAppTool`` writes both, hosts read one or the other,
#: and one extra short string beats guessing which.
RESOURCE_URI_META_KEY = "ui/resourceUri"

#: The capability a client declares when it can render these (``EXTENSION_ID``). We publish the
#: resources unconditionally rather than reading it -- this server holds no session, so there is
#: nowhere to remember the answer, and a host that cannot render one simply ignores the ``_meta``.
UI_EXTENSION_ID = "io.modelcontextprotocol/ui"

#: Where the widget script lives, so the vendored bundle can be dropped in ahead of it.
_BUNDLE_PLACEHOLDER = "/*__EXT_APPS_BUNDLE__*/"

#: The trailing ``export{a as b,c as d};`` of the vendored ES module.
_EXPORT_STATEMENT = re.compile(r"export\s*\{([^}]*)\}\s*;?\s*$")


def _widget(view: str, title: str, description: str, tools: tuple[str, ...]) -> dict[str, Any]:
    return {"view": view, "title": title, "description": description, "tools": tools}


#: The catalogue. ``tools`` is which registered actions render as this widget -- several may, and
#: the invoice is the case that matters: an admin arrives at it by settling somebody's invoice and
#: a bidder by asking what they owe, and it is the same page either time.
WIDGETS: dict[str, dict[str, Any]] = {
    "ui://auction.fish/lot": _widget(
        "lot",
        "Lot",
        "One lot: its photo, what it is, what it is going for, and where it is collected.",
        ("describe_lot",),
    ),
    "ui://auction.fish/rules": _widget(
        "rules",
        "Auction rules",
        "An auction's dates, whether it is taking lots, and the club's own rules in full.",
        ("describe_auction",),
    ),
    "ui://auction.fish/invoice": _widget(
        "invoice",
        "Invoice",
        "What one person owes the club or is owed by it, itemised.",
        ("my_activity", "find_invoice", "set_invoice_status", "add_invoice_adjustment"),
    ),
    "ui://auction.fish/card": _widget(
        "card",
        "Membership card",
        "The signed-in member's own club card: the membership number, its barcode, when it runs "
        "out, and a way to renew when it needs it.",
        # Deliberately not ``send_membership_card``. That one now sends somebody *else's* card as
        # well as the caller's, and drawing the recipient's card in the caller's chat window is the
        # wrong receipt for it -- "sent Jane's card to jane@example.com" is the whole answer, and a
        # picture of Jane's barcode next to it is a membership number nobody asked to be shown.
        ("my_membership", "renew_membership"),
    ),
}

#: Tool name -> the ``ui://`` resource it renders as. Built from :data:`WIDGETS` so the two cannot
#: disagree about which tool owns which view.
TOOL_WIDGETS: dict[str, str] = {tool: uri for uri, widget in WIDGETS.items() for tool in widget["tools"]}


def _resource_domains() -> list[str]:
    """The origins a widget may load an image from. Everything else is blocked by the host's CSP.

    Two things need bytes from off the page: a lot's photographs, from this site's own media or
    from the Cloudflare Images delivery host once one has been migrated, and a membership card's
    barcode, which is this site's own SVG endpoint. Declared rather than inlined as ``data:`` URLs
    because a lot may carry six photos and a tool result is capped at twenty thousand characters;
    a base64 photograph is not something to spend that on.
    """
    domains = []
    site = getattr(settings, "SITE_DOMAIN", "")
    if site:
        domains.append(f"https://{site}")
    custom = getattr(settings, "CLOUDFLARE_IMAGES_DOMAIN", "")
    domains.append(f"https://{custom}" if custom else "https://imagedelivery.net")
    return domains


@lru_cache(maxsize=1)
def _bundle() -> str:
    """The vendored ext-apps runtime, with its ``export{…}`` rewritten to a global assignment.

    An inline ``<script type="module">`` may not export, so the module's last statement has to
    become ``globalThis.ExtApps = {App: eI, …}``. Cached because it is a third of a megabyte and
    the answer never changes for the life of the process.
    """
    from pathlib import Path

    source = (Path(__file__).parent / "vendor" / "ext_apps.js").read_text()
    match = _EXPORT_STATEMENT.search(source)
    if not match:
        # A newer bundle that no longer ends this way. Better a widget that fails loudly in the
        # tests than one that renders a blank rectangle in somebody's chat with nothing in any
        # console we can see.
        msg = "The vendored ext-apps bundle no longer ends in an export statement; see vendor/README.md."
        raise RuntimeError(msg)
    pairs = []
    for pair in match.group(1).split(","):
        local, _, exported = (part.strip() for part in pair.partition(" as "))
        pairs.append(f"{exported or local}:{local}")
    return source[: match.start()] + "globalThis.ExtApps={" + ",".join(pairs) + "};"


def resource_descriptors() -> list[dict[str, Any]]:
    """The ``resources/list`` answer: one entry per widget.

    ``prefersBorder`` is set explicitly on every one of them because the note in the schema says
    host defaults vary, and a card drawn inside a card is the commonest way one of these looks
    wrong. Ours draw their own spacing and no chrome, so the host's border is the one to keep.

    Deliberately **no** ``icons``, though everything else that appears in a list here has them (see
    :mod:`auctions.mcp.icons`). A widget is not browsed, it is rendered: what a person sees of one
    is the lot with its photograph on it, and a thumbnail beside its name in a resource list is a
    picture of nothing. Deriving one would also collapse -- three of the four are reads about an
    auction, so four widgets would carry two distinct icons between them.
    """
    return [
        {
            "uri": uri,
            "name": widget["view"],
            "title": widget["title"],
            "description": widget["description"],
            "mimeType": RESOURCE_MIME_TYPE,
            "_meta": {
                "ui": {
                    "prefersBorder": True,
                    "csp": {
                        # No connectDomains at all: a widget never talks to this site directly.
                        # Since the selling console came out it does not talk to it *at all* --
                        # nothing in the document calls ``callServerTool`` any more, so the only
                        # bytes any of these fetch are the lot photo and the barcode named below.
                        "connectDomains": [],
                        "resourceDomains": _resource_domains(),
                    },
                }
            },
        }
        for uri, widget in WIDGETS.items()
    ]


def read_resource(uri: str) -> dict[str, Any] | None:
    """One widget document, or ``None`` for a URI we do not publish."""
    widget = WIDGETS.get(uri)
    if not widget:
        return None
    html = render_to_string(
        "auctions/mcp/widget.html",
        {"view": widget["view"], "widget_title": widget["title"]},
    )
    # ``str.replace`` and not ``re.sub``: the minified bundle is full of ``$&`` and ``\\1``, which a
    # regex replacement would expand into whatever happened to match. Python's plain replace is
    # literal on both sides, which is the property being relied on here.
    html = html.replace(_BUNDLE_PLACEHOLDER, _bundle())
    return {"uri": uri, "name": widget["view"], "title": widget["title"], "mimeType": RESOURCE_MIME_TYPE, "text": html}


def tool_meta(name: str) -> dict[str, Any] | None:
    """The ``_meta`` a tool descriptor carries so a host knows which widget draws its answer.

    Both spellings, flat and nested, for the same reason ``registerAppTool`` writes both: hosts
    read one or the other and the cost of sending both is one short string on four of fifty-odd
    tools.
    """
    uri = TOOL_WIDGETS.get(name)
    if not uri:
        return None
    return {RESOURCE_URI_META_KEY: uri, "ui": {"resourceUri": uri}}
