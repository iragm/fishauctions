"""Addressable reads: the same answers the read-only tools give, reachable by URI.

A tool is something the *model* chooses. A resource is something the **person** attaches, the way
they attach a file, and the difference is where the decision is made. "Here is my auction, now
write the announcement" costs no tool-selection turn, no arguments guessed from a sentence, and no
round trip to correct them -- the host fetches the URI and the content is simply in the context.

That is also the token argument, and it is worth being precise about it because it is easy to
overstate. Attaching a resource does not shrink ``tools/list``: a host still lists the tools. What
it saves is the *turn* -- the model deciding which tool, filling in the auction slug, and being
told it guessed the wrong auction -- and it makes ``?tools=read`` a genuinely usable narrowing for
an integration that only ever reads, because the reads it wants are addressable without the
catalogue. :data:`TEMPLATES` is under two kilobytes against the tool schemas' forty-seven.

**Every read is a tool call wearing a URI, and that is the whole security design.** A template
names a registered read-only action and how to fill its parameters out of the URI; the read goes
through :func:`auctions.mcp.tools.call_tool` with the caller's own request, so the resolver runs
its own permission check, the same one it runs for a model. There is no second path to the data
and no second place a permission could be forgotten. It is the same property that makes the
``ui://`` widgets safe, for the same reason.

**Nothing concrete is ever listed.** ``resources/list`` returns the widget documents and the two
``me://`` reads, which are the same URI for every caller and so say nothing about anybody;
``resources/templates/list`` returns patterns. A list of ``auction://spring-2027`` would be a list
of which auctions exist, handed to anyone who asked -- so the enumeration stays in the tools,
where it is behind a permission check that knows whose auctions they are.
"""

from __future__ import annotations

import json
from typing import Any, NamedTuple
from urllib.parse import unquote

#: What a data resource is served as. Not ``application/json``: the body is the tool's own text
#: block, which is JSON, but a host that renders resources as documents should show it as text
#: rather than offering to parse it into something.
DATA_MIME_TYPE = "application/json"


class Template(NamedTuple):
    """One addressable read. ``uri`` is an RFC 6570 level-1 template, which is all MCP allows.

    ``action`` is the registered read-only action that answers it, and ``fields`` maps each
    ``{placeholder}`` in the template onto the parameter name that action expects. ``extra`` is
    anything the action needs that the URI does not carry -- a page size, a status filter.
    """

    uri: str
    name: str
    title: str
    description: str
    action: str
    fields: tuple[str, ...]
    extra: dict[str, Any] = {}


#: The catalogue. Every ``action`` here must be read-only; ``test_mcp_resources`` fails the build
#: if one stops being, because a URI a host may fetch on a person's behalf must never be a write.
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
)

#: Fixed resources -- no placeholders, and the same URI for everybody. Both are about the caller,
#: which is what makes them safe to list: the URI says "me", so knowing it exists tells nobody
#: anything about anybody.
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

ALL: tuple[Template, ...] = TEMPLATES + FIXED


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
    """The concrete data resources for ``resources/list``, alongside the widget documents."""
    return [_descriptor(template, as_template=False) for template in FIXED]


def _scheme_and_parts(uri: str) -> tuple[str, list[str]]:
    """``"lot://spring/14"`` -> ``("lot", ["spring", "14"])``. Empty scheme when it isn't one."""
    scheme, separator, rest = uri.partition("://")
    if not separator:
        return "", []
    return scheme, [unquote(part) for part in rest.split("/") if part != ""]


def _shape(template: Template) -> tuple[str, list[str]]:
    """The template's own scheme and path parts, with the placeholders left in."""
    scheme, separator, rest = template.uri.partition("://")
    return scheme, [part for part in rest.split("/") if part != ""]


def match(uri: str) -> tuple[Template, dict[str, str]] | None:
    """Which template a concrete URI is, and the parameters it carries. ``None`` for no match.

    Matched on the scheme and the *shape* of the path rather than by a regex over the whole
    thing, because the values are slugs and lot numbers people type -- ``BOB-1`` is an ordinary lot
    number in a seller-dash auction, and a pattern tight enough to be safe would refuse it. Nothing
    is interpolated anywhere: each part becomes one parameter to a registered action, which
    resolves it the same way it resolves the same parameter from a model.
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
    """One data resource, or ``None`` for a URI this server does not publish.

    The answer is the tool's own text block verbatim -- not a second rendering of it. A resource
    and a tool call that return different things for the same question is the drift this whole
    layer is written to avoid, and it is also what would make the permission checks diverge.
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
        # A refused read is still served as ``application/json``, so it has to *be* JSON. A tool
        # error's text block is one sentence written for a person -- correct there, and a lie about
        # the mime type here -- so it is wrapped rather than passed through. The sentence is kept
        # word for word: it already says what to do instead, which is what makes it recoverable.
        text = json.dumps({"error": text or "That answered with nothing."})
    return {
        "uri": uri,
        "name": template.name,
        "title": template.title,
        "mimeType": DATA_MIME_TYPE,
        "text": text,
    }


#: How many ``resource_link`` blocks one tool result may carry. A link is about 150 bytes with its
#: name and description, and the point of them is to save a turn rather than to enumerate an
#: auction: twelve is every club a person belongs to and every auction running at once, which is
#: the shape of ``my_context``, and it is nowhere near a hundred lots.
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

    ``_about`` is written by the resolver that is holding the object (see
    ``palette_actions.KEY_ABOUT``), so the slugs here are real slugs. Nothing is sniffed out of the
    answer itself, because the answer cannot be: ``auction`` is a slug in some results and a title
    in others, and a URI built from a title is a link that does not resolve.
    """
    found: list[str] = []
    auction = about.get("auction")
    lot = about.get("lot")
    person = about.get("person")
    if auction and lot:
        found.append(f"lot://{auction}/{lot}")
    if auction and person:
        # A bidder number, which is what ``find_invoice`` resolves. A name with a slash in it builds
        # a URI ``match`` will not accept, and :func:`links_for` drops those silently -- which is the
        # right outcome: a decoration must never fail a call that otherwise worked.
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
    """The sub-resources of one subject URI: an auction's lots and people, a club's events.

    Offered only in place of a **dropped self-link** (see :func:`links_for`), which is what makes
    them precise rather than noise: ``describe_auction`` has just answered ``auction://spring``, so
    what is left to point at is what is underneath it. ``list_lots`` has not, so it gets the
    auction itself and no siblings it did not ask about.
    """
    return [
        child.uri.replace("{auction}", uri.removeprefix("auction://")).replace("{club}", uri.removeprefix("club://"))
        for child in TEMPLATES
        if child.uri.startswith(uri.split("://")[0] + "://") and child.uri.count("/") > uri.count("/")
    ]


def links_for(action: str, about: Any) -> list[dict[str, Any]]:
    """The ``resource_link`` blocks to hang off one tool result.

    A link says "there is more about this, at this address, and you can fetch it without asking me
    again". It is the cheap half of this module: a host that supports resources can pull the whole
    auction after a write that named one, and a host that has never heard of ``resource_link``
    ignores an unknown content block, which is what the spec requires of it.

    The tool's **own** answer is never linked. ``describe_lot`` returning a link to
    ``lot://spring/14`` is a pointer at the document it just sent, which costs bytes and says
    nothing -- so a URI whose template is answered by *this* action is dropped, and what goes in its
    place is what sits underneath it (:func:`_children`). That is why ``describe_auction`` offers
    the auction's lots and its people while ``list_lots`` offers only the auction: one of them has
    already answered the top-level thing and the other has not.
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
        # Unmatched means this server does not publish it -- a slug with a slash in it, or a
        # scheme that has been retired. Silently skipped: a decoration must never be able to fail
        # a call that otherwise worked.
        if matched is None:
            continue
        if matched[0].action == action:
            queue.extend(_children(uri))
            continue
        links.append(_link(matched[0], uri))
    return links
