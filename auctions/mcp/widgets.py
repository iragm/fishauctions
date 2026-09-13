"""Interactive views this server publishes as MCP-app widgets.

A host with the apps surface renders a tool's answer as a page: it reads a ``ui://`` resource,
mounts it in a sandboxed iframe, and pipes the tool's own ``structuredContent`` into it -- four
widgets (lot, rules, invoice, card), each attached to the tool that already answers that question.
A host without the apps surface ignores ``_meta`` and shows the same JSON as always.

One template (``auctions/templates/auctions/mcp/widget.html``) renders all four; ``view`` selects
which. The ext-apps runtime is vendored and inlined (see ``vendor/README.md``); :func:`_bundle`
rewrites its trailing ``export{...}`` into a ``globalThis`` assignment because an inline
``<script type="module">`` cannot export -- ``test_mcp_widgets`` guards that rewrite.

No widget initiates a write; each draws a thing that already happened. There is deliberately no
selling console here -- one was tried and was a confusing second copy of the full-screen lot-queue
page; the tools (``set_lot_winner``, ``no_sale``, ``undo_sale``, ``lot_queue``) stay without a form.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from django.conf import settings
from django.template.loader import render_to_string

#: Tells a host "render this, don't print it"; plain ``text/html`` would be shown as source.
RESOURCE_MIME_TYPE = "text/html;profile=mcp-app"

#: Flat ``_meta`` key naming a tool's widget; also written into the nested ``ui`` object.
RESOURCE_URI_META_KEY = "ui/resourceUri"

#: Capability a client declares when it can render these. Published unconditionally -- no session
#: to remember the answer in, and a host that can't render one just ignores ``_meta``.
UI_EXTENSION_ID = "io.modelcontextprotocol/ui"

#: Marks where the vendored bundle is dropped into the widget document.
_BUNDLE_PLACEHOLDER = "/*__EXT_APPS_BUNDLE__*/"

#: The trailing ``export{a as b,c as d};`` of the vendored ES module.
_EXPORT_STATEMENT = re.compile(r"export\s*\{([^}]*)\}\s*;?\s*$")


def _widget(view: str, title: str, description: str, tools: tuple[str, ...]) -> dict[str, Any]:
    return {"view": view, "title": title, "description": description, "tools": tools}


#: The catalogue. ``tools`` lists which registered actions render as this widget.
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
        # Not send_membership_card: that can send someone else's card too, wrong to draw here.
        ("my_membership", "renew_membership"),
    ),
}

#: Tool name -> the ``ui://`` resource it renders as. Derived from :data:`WIDGETS`.
TOOL_WIDGETS: dict[str, str] = {tool: uri for uri, widget in WIDGETS.items() for tool in widget["tools"]}


def _resource_domains() -> list[str]:
    """Origins a widget may load an image from; everything else is blocked by the host's CSP."""
    domains = []
    site = getattr(settings, "SITE_DOMAIN", "")
    if site:
        domains.append(f"https://{site}")
    custom = getattr(settings, "CLOUDFLARE_IMAGES_DOMAIN", "")
    domains.append(f"https://{custom}" if custom else "https://imagedelivery.net")
    return domains


@lru_cache(maxsize=1)
def _bundle() -> str:
    """Vendored ext-apps runtime, with its trailing ``export{...}`` rewritten to a global assignment."""
    from pathlib import Path

    source = (Path(__file__).parent / "vendor" / "ext_apps.js").read_text()
    match = _EXPORT_STATEMENT.search(source)
    if not match:
        msg = "The vendored ext-apps bundle no longer ends in an export statement; see vendor/README.md."
        raise RuntimeError(msg)
    pairs = []
    for pair in match.group(1).split(","):
        local, _, exported = (part.strip() for part in pair.partition(" as "))
        pairs.append(f"{exported or local}:{local}")
    return source[: match.start()] + "globalThis.ExtApps={" + ",".join(pairs) + "};"


def resource_descriptors() -> list[dict[str, Any]]:
    """The ``resources/list`` answer: one entry per widget. No ``icons``: a widget is rendered, not
    browsed, so a thumbnail beside its name would show nothing useful."""
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
                        "connectDomains": [],  # a widget never calls a tool
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
    # str.replace, not re.sub: the minified bundle has literal $& and \1 that regex would expand.
    html = html.replace(_BUNDLE_PLACEHOLDER, _bundle())
    return {"uri": uri, "name": widget["view"], "title": widget["title"], "mimeType": RESOURCE_MIME_TYPE, "text": html}


def tool_meta(name: str) -> dict[str, Any] | None:
    """``_meta`` naming which widget draws a tool's answer, in both the flat and nested spellings."""
    uri = TOOL_WIDGETS.get(name)
    if not uri:
        return None
    return {RESOURCE_URI_META_KEY: uri, "ui": {"resourceUri": uri}}
