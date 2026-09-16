"""Addressable reads: the read-only tools' answers, reachable by URI.

A tool is chosen by the model; a resource is attached by the person, which saves the tool-selection
turn and the arguments guessed from a sentence. It also makes ``?tools=read`` usable, since
:data:`TEMPLATES` is a fraction of the tool schemas' size.

Every read is a tool call wearing a URI: a template names a registered read-only action and how to
fill its parameters, and the read goes through :func:`auctions.mcp.tools.call_tool` with the
caller's request, so the resolver's own permission check runs. There is no second path to the data.

Nothing concrete is listed: ``resources/list`` returns the widgets, the ``me://`` reads and public
documents, which say nothing about anybody; enumeration stays in the tools, behind permissions.
"""

from __future__ import annotations

import json
from typing import Any, NamedTuple
from urllib.parse import unquote

#: What a data resource is served as. The body is the tool's own text block.
DATA_MIME_TYPE = "application/json"


class Template(NamedTuple):
    """One addressable read. ``uri`` is an RFC 6570 level-1 template, all MCP allows.

    ``action`` is the read-only action answering it, ``fields`` maps each ``{placeholder}`` to its
    parameter, and ``extra`` is anything the URI doesn't carry.
    """

    uri: str
    name: str
    title: str
    description: str
    action: str
    fields: tuple[str, ...]
    extra: dict[str, Any] = {}


#: The catalogue. Every ``action`` must be read-only; ``test_mcp_resources`` enforces it.
TEMPLATES: tuple[Template, ...] = (
    Template(
        "auction://{auction}",
        "auction",
        "An auction",
        "One auction's dates, fees, whether it is taking lots, and the club's rules in full. "
        "The auction's slug goes in the URI, e.g. auction://spring-auction-2027.",
        "describe_auction",
        ("auction",),
    ),
    Template(
        "auction://{auction}/lots",
        "auction-lots",
        "The lots in an auction",
        "Every lot in one auction: number, name, whether it has sold and for how much. Up to 100.",
        "list_lots",
        ("auction",),
        {"limit": 100},
    ),
    Template(
        "auction://{auction}/people",
        "auction-people",
        "The people in an auction",
        "Everyone in one auction, with their bidder numbers. Auction admins only — for anybody "
        "else this reads as a refusal, exactly as the tool does.",
        "list_people",
        ("auction",),
        {"limit": 100},
    ),
    Template(
        "auction://{auction}/history",
        "auction-history",
        "An auction's change log",
        "Who did what in one auction and when, newest first: check-ins, bidder numbers, sales, "
        "invoices, rules. Auction admins only — for anybody else this reads as a refusal, "
        "exactly as the tool does.",
        "recent_changes",
        ("auction",),
        # 50: a history line is long, and 100 would approach ``tools.MAX_RESULT_CHARS``.
        {"limit": 50},
    ),
    Template(
        "lot://{auction}/{lot}",
        "lot",
        "One lot",
        "One lot by its number within an auction, e.g. lot://spring-auction-2027/14 — its name, "
        "its price, its pictures and where it is collected.",
        "describe_lot",
        ("auction", "lot"),
    ),
    Template(
        "invoice://{auction}/{person}",
        "invoice",
        "One person's invoice",
        "What one person owes an auction or is owed by it, itemised, with the extra lines on it. "
        "The auction's slug and their bidder number go in the URI, e.g. invoice://spring-2027/14. "
        "Auction admins only for anybody but the caller — for anyone else it reads as a refusal, "
        "exactly as the tool does.",
        "find_invoice",
        ("auction", "person"),
    ),
    Template(
        "club://{club}",
        "club",
        "A club",
        "One club: what it is, where it meets, how many members it has and what it charges. "
        "The club's slug goes in the URI.",
        "describe_club",
        ("club",),
    ),
    Template(
        "club://{club}/events",
        "club-events",
        "A club's calendar",
        "What one club has coming up: meetings, auctions and pickup times.",
        "list_club_events",
        ("club",),
    ),
    Template(
        "club://{club}/history",
        "club-history",
        "A club's change log",
        "Who did what to one club and when, newest first: renewals and dues, members added and "
        "edited, settings, points, announcements. Club staff only — for anybody else this reads "
        "as a refusal, exactly as the tool does.",
        "club_history",
        ("club",),
        # 50: a history line is long, and 100 would approach ``tools.MAX_RESULT_CHARS``.
        {"limit": 50},
    ),
)

#: Fixed resources: no placeholders, the same URI for everybody, and about the caller.
FIXED: tuple[Template, ...] = (
    Template(
        "me://context",
        "my-context",
        "Which auctions and clubs I am in",
        "The first thing to read: the auctions and clubs this user belongs to, which one is "
        "running, and what they were last looking at.",
        "my_context",
        (),
    ),
    Template(
        "me://activity",
        "my-activity",
        "What I have bought and sold",
        "This user's own lots, bids and invoice in whichever auction is current.",
        "my_activity",
        (),
    ),
)

#: Concrete resources that are the same for everybody and hold nobody's data.
PUBLIC: tuple[Template, ...] = (
    Template(
        "help://faq",
        "faq",
        "This site's FAQ",
        "Every question and answer this site has written down, including the ones kept off the "
        "public FAQ page for assistants to answer out of. This site does not work the same way as "
        "other auction sites, so this is where the real answers to how-does-this-work questions "
        "are. Ask search_help for the rest if there are more than this carries.",
        "search_help",
        (),
        {"source": "faq", "limit": 25},
    ),
)

ALL: tuple[Template, ...] = TEMPLATES + FIXED + PUBLIC


def _descriptor(template: Template, *, as_template: bool) -> dict[str, Any]:
    from . import icons

    key = "uriTemplate" if as_template else "uri"
    return {
        key: template.uri,
        "name": template.name,
        "title": template.title,
        "description": template.description,
        "mimeType": DATA_MIME_TYPE,
        "icons": icons.for_uri(template.uri),
    }


def template_descriptors() -> list[dict[str, Any]]:
    """The ``resources/templates/list`` answer."""
    return [_descriptor(template, as_template=True) for template in TEMPLATES]


def fixed_descriptors() -> list[dict[str, Any]]:
    """The concrete data resources for ``resources/list``: :data:`FIXED` (about the caller) and
    :data:`PUBLIC` (about nobody).
    """
    return [_descriptor(template, as_template=False) for template in FIXED + PUBLIC]


def _scheme_and_parts(uri: str) -> tuple[str, list[str]]:
    """``"lot://spring/14"`` -> ``("lot", ["spring", "14"])``; empty scheme when it isn't one."""
    scheme, separator, rest = uri.partition("://")
    if not separator:
        return "", []
    return scheme, [unquote(part) for part in rest.split("/") if part != ""]


def _shape(template: Template) -> tuple[str, list[str]]:
    """The template's own scheme and path parts, with the placeholders left in."""
    scheme, separator, rest = template.uri.partition("://")
    return scheme, [part for part in rest.split("/") if part != ""]


def match(uri: str) -> tuple[Template, dict[str, str]] | None:
    """Which template a URI is, and its parameters, or ``None``.

    Matched on scheme and path shape rather than a regex, because the values are slugs and lot numbers
    people type (``BOB-1``). Each part becomes one parameter to a registered action.
    """
    scheme, parts = _scheme_and_parts(uri.strip())
    if not scheme:
        return None
    for template in ALL:
        wanted_scheme, wanted_parts = _shape(template)
        if scheme != wanted_scheme or len(parts) != len(wanted_parts):
            continue
        arguments: dict[str, str] = {}
        literal_mismatch = False
        for value, pattern in zip(parts, wanted_parts, strict=True):
            if pattern.startswith("{") and pattern.endswith("}"):
                arguments[pattern[1:-1]] = value
            elif pattern != value:
                literal_mismatch = True
                break
        if literal_mismatch:
            continue
        if set(arguments) != set(template.fields):
            continue
        return template, arguments
    return None


def read(request, uri: str) -> dict[str, Any] | None:
    """One data resource, or ``None`` for a URI this server doesn't publish.

    The answer is the tool's own text block verbatim, so a resource and a tool call can never drift.
    """
    from . import tools

    matched = match(uri)
    if matched is None:
        return None
    template, arguments = matched
    payload = dict(template.extra)
    payload.update(arguments)
    result = tools.call_tool(request, template.action, payload, writes=False)
    blocks = [block for block in result.get("content", []) if block.get("type") == "text"]
    text = blocks[0]["text"] if blocks else ""
    if result.get("isError") or not text:
        # A refused read is served as JSON too, so the tool's one-sentence error is wrapped rather
        # than passed through. The sentence is kept, since it says what to do instead.
        text = json.dumps({"error": text or "That answered with nothing."})
    return {
        "uri": uri,
        "name": template.name,
        "title": template.title,
        "mimeType": DATA_MIME_TYPE,
        "text": text,
    }


#: How many ``resource_link`` blocks one result may carry. Twelve covers a person's clubs and
#: running auctions (the shape of ``my_context``) without enumerating lots.
MAX_LINKS = 12


def _link(template: Template, uri: str) -> dict[str, Any]:
    """One ``resource_link`` content block (MCP 2025-06-18)."""
    return {
        "type": "resource_link",
        "uri": uri,
        "name": template.name,
        "title": template.title,
        "mimeType": DATA_MIME_TYPE,
    }


def _uris(about: dict[str, Any]) -> list[str]:
    """The URIs one ``_about`` block names, most specific first.

    ``_about`` is written by the resolver holding the object (``palette_actions.KEY_ABOUT``), so the
    slugs are real; nothing is sniffed out of the answer, where ``auction`` may be a title.
    """
    found: list[str] = []
    auction = about.get("auction")
    lot = about.get("lot")
    person = about.get("person")
    if auction and lot:
        found.append(f"lot://{auction}/{lot}")
    if auction and person:
        # A bidder number, which ``find_invoice`` resolves. A name with a slash builds a URI
        # ``match`` rejects, and :func:`links_for` drops those silently.
        found.append(f"invoice://{auction}/{person}")
    if auction:
        found.append(f"auction://{auction}")
    if about.get("club"):
        found.append(f"club://{about['club']}")
    for slug in about.get("auctions") or ():
        found.append(f"auction://{slug}")
    for slug in about.get("clubs") or ():
        found.append(f"club://{slug}")
    return found


def _children(uri: str) -> list[str]:
    """The sub-resources of a subject URI: an auction's lots and people, a club's events.

    Offered only in place of a dropped self-link (see :func:`links_for`).
    """
    return [
        child.uri.replace("{auction}", uri.removeprefix("auction://")).replace("{club}", uri.removeprefix("club://"))
        for child in TEMPLATES
        if child.uri.startswith(uri.split("://")[0] + "://") and child.uri.count("/") > uri.count("/")
    ]


def links_for(action: str, about: Any) -> list[dict[str, Any]]:
    """The ``resource_link`` blocks to hang off one tool result.

    A host that supports resources can fetch the whole auction after a write that named one; one that
    doesn't ignores the unknown block. The tool's own answer is never linked: a URI answered by this
    action is dropped and replaced by what sits underneath it (:func:`_children`).
    """
    if not isinstance(about, dict) or not about:
        return []
    links: list[dict[str, Any]] = []
    seen: set[str] = set()
    queue = list(_uris(about))
    while queue and len(links) < MAX_LINKS:
        uri = queue.pop(0)
        if uri in seen:
            continue
        seen.add(uri)
        matched = match(uri)
        # Unmatched means this server doesn't publish it; a decoration must never fail a call.
        if matched is None:
            continue
        if matched[0].action == action:
            queue.extend(_children(uri))
            continue
        links.append(_link(matched[0], uri))
    return links
