"""The action registry, as MCP tools.

One catalogue, two callers: :mod:`auctions.mcp.protocol` serves it over HTTP to outside agents,
and the command palette's own model reads the same list in-process. Nothing here is written by
hand -- every tool is generated from :data:`auctions.palette_actions.ACTIONS`, so registering an
action is all it takes to expose it, and an action can never be described to one caller and not
the other.

**The schema comes out of the prose that was already there.** Every parameter in the registry is
documented in the same shape -- ``"integer, optional, default 1."``, ``"string, required. The lot
number."`` -- across all 117 of them, because that is what the old system prompt needed in order
to be readable. :func:`param_schema` reads the type and the required flag straight off that
prefix and keeps the whole sentence as the JSON Schema ``description``. So there is no second
table of types to write, and none to forget to update: the schema is derived from the one
description that has to be right anyway.

**Annotations come out of the danger tier**, which the registry has always carried:

    ``safe``      reads something                  -> ``readOnlyHint``
    ``confirm``   writes to the database           -> a write tool
    ``navigate``  resolves a URL and never acts    -> ``readOnlyHint``

That is the read/write split an MCP host needs in order to decide what it may run without
asking, and it is the same tier the palette uses to decide whether to show a countdown. One
decision, two audiences. There is deliberately no catch-all "execute" tool: a single tool
covering both reads and writes is exactly what a connector review rejects, and the registry has
never had one.

Permissions are **not** enforced here. :func:`palette_actions.run_action` calls the resolver,
which re-checks every permission against the database, exactly as it does for the palette.
:func:`tool_descriptors` filters the catalogue with ``palette_actions.actions_for`` for the same
reason the prompt did -- relevance and size, not security.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from auctions import palette_actions

from . import auth

logger = logging.getLogger(__name__)

#: How much of one tool result is worth sending back. Generous next to the palette's own
#: ``MAX_LOOKUP_RESULT_CHARS`` (which has to leave room for several rounds inside one budget):
#: an MCP host is showing this to a model with a whole context window, not squeezing it into a
#: system prompt. Still bounded, because "do not return a full database dump" is a review
#: criterion and because a runaway ``list_lots`` should not be able to fill somebody's window.
MAX_RESULT_CHARS = 20000

#: How much of a too-big result's own summary line to echo back with the refusal.
SUMMARY_CHARS = 500

#: Result keys that are ours and not the caller's. ``undo`` is the instruction for reversing the
#: action, which :func:`palette_actions.remember_undo` consumes on the way past; handing it to the
#: caller would invite them to replay it themselves, bypassing ``undo_last``'s window and stack.
_INTERNAL_RESULT_KEYS = ("undo",)

#: The words the registry uses for a parameter's type, mapped onto JSON Schema's.
_JSON_TYPES = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "array": "array",
    "object": "object",
}

#: ``"<types>, required|optional"`` -- the prefix every parameter description in the registry
#: opens with. ``<types>`` is one word, several joined by "or" ("string or boolean"), or an array
#: spelled "array of <types>".
_PARAM_PREFIX = re.compile(
    r"^(?P<types>[a-z]+(?:\s+of\s+[a-z]+(?:\s+or\s+[a-z]+)*)?(?:\s+or\s+[a-z]+)*)\s*,\s*(?P<need>required|optional)\b",
    re.IGNORECASE,
)


class UnknownTool(Exception):
    """No action by that name. Never guessed at -- see :func:`palette_actions.get_action`."""


def _types_to_schema(words: str) -> dict[str, Any]:
    """``"string or boolean"`` -> ``{"type": ["string", "boolean"]}``; ``"array of string"`` -> items."""
    words = words.strip().lower()
    if words.startswith("array of "):
        return {"type": "array", "items": _types_to_schema(words[len("array of ") :])}
    names = [_JSON_TYPES[word] for word in re.split(r"\s+or\s+", words) if word in _JSON_TYPES]
    if not names:
        # An unrecognised type word is a typo in the registry, and the audit test says so. Fall
        # back to "any" rather than emitting an invalid schema: a tool that still works with a
        # loose parameter beats a tools/list that a client refuses to parse.
        return {}
    if len(names) == 1:
        return {"type": names[0]}
    return {"type": names}


def param_schema(description: str) -> tuple[dict[str, Any], bool]:
    """One parameter's JSON Schema and whether it is required, read off its own description.

    The type prefix is *moved* into the schema rather than copied: once ``"type": "string"`` and
    ``required: [...]`` say it, repeating "string, required" in the description is the same fact
    twice in front of a model that is paying for both. Everything after the prefix stays, because
    that is the half a type cannot express -- "default 1", "ADMINS ONLY", which values a dropdown
    accepts. Fifteen of the registry's parameters are nothing but the prefix (``email``,
    ``quantity``, ``donation``); those come back with no description at all, which is honest --
    the name and the type are the whole of what there is to say.
    """
    match = _PARAM_PREFIX.match(description or "")
    if not match:
        return {"description": description}, False
    schema = _types_to_schema(match.group("types"))
    rest = (description[match.end() :]).lstrip(" ,.").strip()
    if rest:
        schema["description"] = rest[0].upper() + rest[1:]
    return schema, match.group("need").lower() == "required"


def input_schema(action: palette_actions.Action) -> dict[str, Any]:
    """The ``inputSchema`` for one action: an object, one property per documented parameter.

    ``Action.aliases`` is deliberately left out. It exists to catch a near-miss from a model that
    was working off prose, and advertising it would widen the contract to spellings the registry
    does not document. With a real schema in front of the caller there is nothing to miss.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, description in action.params.items():
        properties[name], is_required = param_schema(description)
        if is_required:
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    # No extra keys: run_action refuses a parameter the action never advertised, so saying so in
    # the schema turns a server-side refusal into something the client can catch first.
    schema["additionalProperties"] = False
    return schema


def read_only(action: palette_actions.Action) -> bool:
    """True when running this tool changes nothing.

    ``navigate`` counts as read-only, and that is not a fudge: a navigate-tier action resolves a
    URL and returns it. The thing on the far side is what the tier exists to *avoid* doing.
    """
    return action.danger != palette_actions.DANGER_CONFIRM


def idempotent(action: palette_actions.Action) -> bool:
    """Whether calling this twice with the same arguments leaves the same state as calling it once.

    Read-only tools are idempotent by definition. A write is not, unless the registry says so:
    ``add_lot`` twice is two lots, and a host that retried it on a dropped connection would sell
    the same fish twice. Actions that set a value rather than append one declare
    ``idempotent=True`` for themselves.
    """
    if action.idempotent is not None:
        return action.idempotent
    return read_only(action)


def title_for(action: palette_actions.Action) -> str:
    """A human label for the tool list. ``set_lot_winner`` -> "Set lot winner"."""
    return action.name.replace("_", " ").capitalize()


def describe(action: palette_actions.Action) -> str:
    """The tool description: what the registry says, plus the examples it carries."""
    description = action.description.strip()
    if action.examples:
        # Examples of what a person asks for, which is what the description is matched against.
        # Phrased as data ("Examples:"), never as an instruction to the model reading it.
        description += " Examples: " + "; ".join(f"“{example}”" for example in action.examples) + "."
    return description


def descriptor(action: palette_actions.Action) -> dict[str, Any]:
    """One MCP tool descriptor.

    Three things are deliberately *not* in here, and all three are the same decision: ``tools/list``
    is ~47 KB, it is paid for in full, in context, by every host on every session, and a key that
    says what the spec's own default already says is a key fifty-four times over.

    ``destructiveHint`` and ``idempotentHint`` are omitted on a read-only tool, because the spec
    defines them only when ``readOnlyHint`` is false. ``idempotentHint`` is omitted when it is
    ``false``, which is the spec's default for it. And ``annotations.title`` is gone: the spec says
    the top-level ``title`` takes precedence over it, so sending both is the same string twice, and
    a host old enough to read only the annotation falls back to ``name`` -- which for
    ``set_lot_winner`` differs from "Set lot winner" by two spaces and a capital letter.

    ``openWorldHint: false`` stays even though it is a bare boolean, because the spec's default for
    it is ``true`` and "this tool reaches out to the open internet" is the wrong thing for a host to
    assume about a tool that only ever touches this site's own database.
    """
    annotations: dict[str, Any] = {
        "readOnlyHint": read_only(action),
        # Everything here reads and writes this site's own database. Nothing reaches out.
        "openWorldHint": False,
    }
    if not read_only(action):
        annotations["destructiveHint"] = action.destructive
        if idempotent(action):
            annotations["idempotentHint"] = True
    return {
        "name": action.name,
        "title": title_for(action),
        "description": describe(action),
        "inputSchema": input_schema(action),
        "annotations": annotations,
    }


#: Which half of the site a tool belongs to, for the optional ``?tools=`` filter on the endpoint.
#: Derived from the parameters an action already declares rather than kept as a table of 51 names,
#: which would be a second list to forget to update.
AREA_GENERAL = "general"
AREA_AUCTION = "auction"
AREA_CLUB = "club"
AREA_READ = "read"

#: The handful the derivation gets wrong, because their parameters don't say what they are about.
_AREA_OVERRIDES = {
    "my_context": AREA_GENERAL,
    "auctions_near_me": AREA_GENERAL,
    "clubs_near_me": AREA_GENERAL,
    "search_help": AREA_GENERAL,
    "find_page": AREA_GENERAL,
    "go_to_page": AREA_GENERAL,
    "update_preferences": AREA_GENERAL,
    "undo_last": AREA_GENERAL,
    "renew_membership": AREA_CLUB,
    "send_membership_card": AREA_CLUB,
}


def area_of(action: palette_actions.Action) -> str:
    """Which area one tool belongs to."""
    if action.name in _AREA_OVERRIDES:
        return _AREA_OVERRIDES[action.name]
    if action.accepts("club") and not action.accepts("auction"):
        return AREA_CLUB
    if action.accepts("auction") or action.accepts("lot"):
        return AREA_AUCTION
    return AREA_GENERAL


def parse_areas(raw: str) -> set[str]:
    """``"club,read"`` -> the filter set. Unknown words are ignored rather than refused."""
    known = {AREA_GENERAL, AREA_AUCTION, AREA_CLUB, AREA_READ}
    return {word.strip().lower() for word in (raw or "").split(",")} & known


def wanted(action: palette_actions.Action, areas: set[str]) -> bool:
    """Whether one tool survives a ``?tools=`` filter. An empty filter keeps everything.

    ``general`` is always kept alongside an area, because the tools that orient a caller
    (``my_context``, ``undo_last``, ``find_page``) are the ones a narrowed list most needs.
    """
    if not areas:
        return True
    if AREA_READ in areas and not read_only(action):
        return False
    areas = areas - {AREA_READ}
    if not areas:
        return True
    return area_of(action) in (areas | {AREA_GENERAL})


def tool_descriptors(user=None, *, writes: bool = True, areas: set[str] | None = None) -> list[dict[str, Any]]:
    """The catalogue for one caller.

    ``user=None`` means every action, for the audit test. Otherwise the list is filtered by
    ``palette_actions.actions_for`` -- a bidder who runs no club and no auction is not offered
    club administration, exactly as they were not shown it in the prompt. That is a relevance and
    size filter, not the security boundary: the resolvers are.

    ``writes=False`` drops every write tool, for a credential that was only granted reads. It has
    to be applied to the *list* as well as to the call, or a read-only agent spends its turn
    picking a tool it is about to be refused.

    ``areas`` is the optional ``?tools=`` filter on the endpoint URL -- ``?tools=club``,
    ``?tools=auction,read``. The whole catalogue is a real cost: it is sent in full, in context, on
    every session a host opens, and somebody who connected this to run their club's meetings has no
    use for the lot-selling half. There is no way to ask for a subset in the protocol, so it is
    part of the address instead, which is the one thing every client lets a person type.
    """
    areas = areas or set()
    return [
        descriptor(action)
        for action in palette_actions.actions_for(user)
        if (writes or read_only(action)) and wanted(action, areas)
    ]


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    """A resolver's result with our own bookkeeping taken out of it."""
    return {key: value for key, value in result.items() if key not in _INTERNAL_RESULT_KEYS}


def _absolute(value: Any, base) -> Any:
    """Every ``url`` in a result made absolute, wherever it is nested.

    The resolvers return site-relative paths because the palette is a page on this site and a
    relative link is the right thing there. An agent is not on this site: it hands the link to a
    person, or puts it in a message, and ``/lots/all/?q=shrimp`` is not a link anybody can follow.
    Done here rather than in the resolvers so the palette keeps its relative ones.
    """
    if isinstance(value, dict):
        return {
            key: (
                base(item) if key == "url" and isinstance(item, str) and item.startswith("/") else _absolute(item, base)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_absolute(item, base) for item in value]
    return value


def _text(payload: Any) -> str:
    """Serialise a result, bounded, and say so when it does not fit.

    An over-budget result is replaced rather than cut. Slicing a JSON document at twenty thousand
    characters stops it mid-string, so a host that parses tool output got a parse error where an
    answer should have been -- and the one thing the caller needed, "ask for less and here is how",
    was the part that got cut off.
    """
    body = json.dumps(payload, indent=2, default=str)
    if len(body) <= MAX_RESULT_CHARS:
        return body
    # The summary is echoed because it is the one line that says what the answer *was* -- but it
    # is a resolver's own string and can itself be the thing that blew the budget, so it is capped
    # here rather than trusted. Whatever this function returns has to fit, unconditionally.
    summary = payload.get("summary") if isinstance(payload, dict) else None
    if isinstance(summary, str) and len(summary) > SUMMARY_CHARS:
        summary = summary[:SUMMARY_CHARS] + "…"
    return json.dumps(
        {
            "error": "That result was too big to send.",
            "summary": summary,
            "what_to_do": (
                "Ask for less of it: most list tools take limit and offset, and the search tools "
                f"take a narrower query. The ceiling is {MAX_RESULT_CHARS} characters."
            ),
        },
        indent=2,
    )


def _result(text: str, *, is_error: bool = False, structured: Any = None) -> dict[str, Any]:
    """One ``CallToolResult``: the text block every host can read, plus the parsed object.

    ``structuredContent`` is MCP 2025-06-18's answer to the thing that was wrong here: the result
    was a JSON document inside a string, so every host had to parse a string to get at it and none
    of them could be sure it was JSON at all. It is sent alongside the text rather than instead of
    it -- the spec asks for both, because a host on an older protocol version reads only the text
    and because the text is what a model actually sees.

    It is only ever an object. The spec requires ``structuredContent`` to be a JSON object, and
    every resolver returns a dict, so the guard here is for the two error paths whose whole payload
    is one sentence: those stay text-only rather than being wrapped in an invented key.

    There is deliberately **no** ``outputSchema``. Declaring one obliges every result to conform to
    it, and these results are one small envelope (``ok``/``found``/``summary``/``followups``) plus
    whatever the tool is about -- fifteen participant rows, a club's fee table, a lot's live price.
    A schema loose enough to be true of all fifty-four validates nothing, and fifty-four copies of
    it is seven kilobytes on every session for that nothing. If a tool ever grows a result worth
    validating, it can declare its own.
    """
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if isinstance(structured, dict):
        result["structuredContent"] = structured
    return result


def _needs_more_information(action: palette_actions.Action, result: dict[str, Any]) -> dict[str, Any]:
    """ "Which bob?" is not a failure, and sending it as one is what made it look like a broken tool.

    ``isError`` is for a tool that tried and could not. A disambiguation is a tool that has *not
    tried yet* and is one parameter short -- an entirely ordinary turn, which hosts render in red
    and some stop on when it arrives as an error.

    MCP's own answer to this is elicitation, and it is genuinely not available here: elicitation is
    a **server-to-client request** raised in the middle of a tool call, so it needs the call to
    stay open across a round trip. This server answers one POST with one JSON body and holds no
    session (see :mod:`auctions.mcp.transport`), so there is nowhere for that request to go.
    Supporting it means an SSE stream and per-call state, which is a transport decision and not a
    tools one.

    What is sent instead says three things a model cannot misread: nothing was changed, here is the
    question with its candidates, and here is the parameter to put the answer in.
    """
    options = [option for option in result.get("options") or [] if isinstance(option, dict)]
    payload: dict[str, Any] = {
        "status": "needs_more_information",
        "nothing_was_changed": True,
        "question": str(result["more_info_needed"]),
        "what_to_do": (
            f"Ask the user this question, then call {action.name} again with their answer. "
            "Do not guess, and do not report this as done."
        ),
    }
    if options:
        payload["choices"] = [
            {"answer": option.get("value") or option.get("label"), "label": option.get("label")} for option in options
        ]
    body = _text(payload)
    # Round-tripped for the same reason ``call_tool`` does it: whatever the text says is what the
    # structure says, even in the (unlikely, for a question) case where the payload was too big.
    return _result(body, structured=json.loads(body))


def call_tool(request, name: str, arguments: Any, *, writes: bool = True) -> dict[str, Any]:
    """Run one tool for the user on ``request`` and return an MCP ``CallToolResult``.

    Every path through here goes to :func:`palette_actions.run_action`, which is the same single
    entry point the palette's execute endpoint uses: it re-checks the permissions and re-validates
    the parameters against the database, whatever the caller believed a moment ago.

    The three shapes a resolver can return map onto MCP like this:

    ``{"error": …}``            -> ``isError``. The message is already written for a person and
                                   says what to do instead, which is what makes it recoverable.
    ``{"more_info_needed": …}`` -> a **successful** result that says the tool did not act and names
                                   what it needs. See :func:`_needs_more_information`.
    ``{"ok": True, …}``         -> the result as JSON, summary included.
    """
    if not isinstance(arguments, dict):
        arguments = {}
    action = palette_actions.get_action(name)
    if action is None:
        raise UnknownTool(name)
    if not writes and not read_only(action):
        return _result(
            f"“{action.name}” changes data and this credential is read-only. Only read-only tools "
            "are available to it. To change that, disconnect and reconnect this assistant from "
            "/account/api-keys/ — a credential's read/write ceiling is fixed when it is issued.",
            is_error=True,
        )
    credential = getattr(request, "mcp_credential", None)
    if credential is not None and not read_only(action) and not auth.within_write_budget(credential):
        # See ``auth.within_write_budget``: this is the ceiling on how far an instruction hidden in
        # somebody else's lot description can get before it runs out of room.
        return _result(
            f"This connection has changed {credential.write_budget} things in the last hour, which "
            "is its limit. Reads still work. If this wasn't you, disconnect it at "
            "/account/api-keys/.",
            is_error=True,
        )
    # Resolvers read this to work out which auction the caller means from the page they are
    # looking at. An agent is not looking at a page, so the answer is "nothing", set explicitly
    # rather than left to whatever else may have touched this request.
    request.palette_page = {}
    result = palette_actions.run_action(request, action.name, arguments)
    if "error" in result:
        return _result(str(result["error"]), is_error=True)
    if "more_info_needed" in result:
        return _needs_more_information(action, result)
    # Same call the palette's execute endpoint makes, so "undo that" works across both surfaces:
    # the stack is per user, and a lot added by an agent is a lot the same person can undo.
    palette_actions.remember_undo(request.user, action.name, result)
    body = _text(_absolute(_payload(result), request.build_absolute_uri))
    # The structure is parsed back out of the text rather than handed over alongside it, for two
    # reasons. It guarantees the two are the same answer -- when ``_text`` refuses an over-budget
    # payload and replaces it with an explanation, the structure is that explanation and not the
    # twenty thousand characters the refusal exists to withhold. And it guarantees the structure is
    # JSON-safe: ``_text`` serialises Decimals and datetimes with ``default=str``, and a Decimal
    # left in ``structuredContent`` would blow up when the transport serialises the whole response.
    return _result(body, structured=json.loads(body))
