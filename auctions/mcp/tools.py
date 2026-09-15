"""The action registry, as MCP tools.

Generated from :data:`auctions.palette_actions.ACTIONS`, which the palette's own model reads too,
so both callers always see the same catalogue.

Each parameter description opens with ``"<type>, required|optional."``; :func:`param_schema` reads
the JSON Schema off that prefix, so there is no second type table.

Annotations come from the danger tier: ``safe`` and ``navigate`` are read-only, ``confirm`` writes.
There is deliberately no catch-all "execute" tool.

Permissions are not enforced here; the resolvers re-check them in ``run_action``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from auctions import palette_actions

from . import auth, icons, resources, widgets

logger = logging.getLogger(__name__)

#: Bounded, so a runaway ``list_lots`` can't fill a host's context.
MAX_RESULT_CHARS = 20000

#: How much of a too-big result's own summary line to echo back with the refusal.
SUMMARY_CHARS = 500

#: Result keys stripped at any depth before a result leaves. ``undo`` is consumed by
#: ``remember_undo``; handing it out would let a caller bypass ``undo_last``. ``lot_id`` is a
#: primary key: a lot's public name is ``lot_number_display`` (see ``_lot_echo``), and resolvers
#: still accept ``lot_id`` as an alias.
_INTERNAL_RESULT_KEYS = ("undo", "lot_id", *palette_actions.INTERNAL_RESULT_KEYS)

#: The words the registry uses for a parameter's type, mapped onto JSON Schema's.
_JSON_TYPES = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "array": "array",
    "object": "object",
}

#: The ``"<types>, required|optional"`` prefix every registry parameter description opens with.
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
        # An unknown type word is a registry typo (the audit test catches it). Loose beats invalid.
        return {}
    if len(names) == 1:
        return {"type": names[0]}
    return {"type": names}


def param_schema(description: str) -> tuple[dict[str, Any], bool]:
    """One parameter's JSON Schema and whether it is required, read off its description.

    The prefix moves into the schema rather than being repeated in the description. A description
    that is only the prefix comes back with none.
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
    """The ``inputSchema`` for one action.

    ``Action.aliases`` is left out: aliases catch near-misses from prose, and a schema has none.
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
    # run_action refuses unadvertised parameters, so say so up front.
    schema["additionalProperties"] = False
    return schema


def read_only(action: palette_actions.Action) -> bool:
    """True when running this tool changes nothing. ``navigate`` only resolves a URL."""
    return action.danger != palette_actions.DANGER_CONFIRM


def idempotent(action: palette_actions.Action) -> bool:
    """Whether a repeat call leaves the same state. Writes aren't, unless the action says so."""
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
        # Phrased as data, never as an instruction to the model.
        description += " Examples: " + "; ".join(f"“{example}”" for example in action.examples) + "."
    return description


def descriptor(action: palette_actions.Action) -> dict[str, Any]:
    """One MCP tool descriptor.

    ``tools/list`` is paid for in context every session, so keys that restate the spec's default are
    omitted: ``destructiveHint``/``idempotentHint`` on read-only tools, ``idempotentHint: false``, and
    ``annotations.title``. ``openWorldHint`` is always sent because its default is ``true``.
    """
    annotations: dict[str, Any] = {
        "readOnlyHint": read_only(action),
        "openWorldHint": action.open_world,
    }
    if not read_only(action):
        annotations["destructiveHint"] = action.destructive
        if idempotent(action):
            annotations["idempotentHint"] = True
    built = {
        "name": action.name,
        "title": title_for(action),
        "description": describe(action),
        "inputSchema": input_schema(action),
        "annotations": annotations,
        # A URL, not inline SVG; see auctions.mcp.icons.
        "icons": icons.for_action(action),
    }
    # Tools with a widget advertise it; the widget draws from the same structuredContent the model reads.
    ui = widgets.tool_meta(action.name)
    if ui:
        built["_meta"] = ui
    return built


#: Which half of the site a tool belongs to, for ``?tools=``. Derived from the action's parameters.
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
    # BAP is a club's, though these three also take an ``auction``.
    "points_queue": AREA_CLUB,
    "review_points": AREA_CLUB,
    "my_points": AREA_CLUB,
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
    """Whether one tool survives a ``?tools=`` filter. ``general`` is always kept alongside an area."""
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

    ``user=None`` means every action (for the audit test). ``actions_for`` filters for relevance, not
    security. ``writes=False`` drops write tools from the list as well as the call. ``areas`` is the
    ``?tools=`` filter, since the protocol has no way to ask for a subset.
    """
    areas = areas or set()
    return [
        descriptor(action)
        for action in palette_actions.actions_for(user)
        if (writes or read_only(action)) and wanted(action, areas)
    ]


def _payload(result: Any) -> Any:
    """A resolver's result with our own bookkeeping taken out of it, however deeply it is nested."""
    if isinstance(result, dict):
        return {key: _payload(value) for key, value in result.items() if key not in _INTERNAL_RESULT_KEYS}
    if isinstance(result, list):
        return [_payload(item) for item in result]
    return result


#: Keys holding a link. A suffix rule, because matching only ``url`` left ``renew_url`` relative.
def _is_url_key(key: Any) -> bool:
    return isinstance(key, str) and (key == "url" or key.endswith("_url"))


def _absolute(value: Any, base) -> Any:
    """Every link in a result made absolute. Resolvers return relative paths for the palette."""
    if isinstance(value, dict):
        return {
            key: (
                base(item)
                if _is_url_key(key) and isinstance(item, str) and item.startswith("/")
                else _absolute(item, base)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_absolute(item, base) for item in value]
    return value


def _text(payload: Any) -> str:
    """Serialise a result, replacing (never slicing) one over budget, so it stays valid JSON."""
    body = json.dumps(payload, indent=2, default=str)
    if len(body) <= MAX_RESULT_CHARS:
        return body
    # The summary is a resolver's string and may itself be the oversized part.
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


def _result(
    text: str, *, is_error: bool = False, structured: Any = None, links: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """One ``CallToolResult``: a text block plus ``structuredContent`` (object only).

    No ``outputSchema``: one loose enough to fit every result validates nothing.
    """
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if links:
        result["content"].extend(links)
    if isinstance(structured, dict):
        result["structuredContent"] = structured
    return result


def _needs_more_information(action: palette_actions.Action, result: dict[str, Any]) -> dict[str, Any]:
    """A disambiguation is a successful result, not ``isError``: the tool hasn't tried yet.

    Elicitation would be MCP's answer, but it needs a session this transport doesn't hold.
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
    return _result(body, structured=json.loads(body))


def call_tool(request, name: str, arguments: Any, *, writes: bool = True) -> dict[str, Any]:
    """Run one tool for the user on ``request`` and return an MCP ``CallToolResult``.

    Everything goes through :func:`palette_actions.run_action`. ``{"error"}`` becomes ``isError``;
    ``{"more_info_needed"}`` is a successful result (:func:`_needs_more_information`).
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
            "/ai/ — a credential's read/write ceiling is fixed when it is issued.",
            is_error=True,
        )
    credential = getattr(request, "mcp_credential", None)
    if credential is not None and not read_only(action) and not auth.within_write_budget(credential):
        # Caps how far an instruction hidden in someone's lot description can get.
        return _result(
            f"This connection has changed {credential.write_budget} things in the last hour, which "
            "is its limit. Reads still work. If this wasn't you, disconnect it at "
            "/ai/.",
            is_error=True,
        )
    # Agents have no page to infer an auction from.
    request.palette_page = {}
    result = palette_actions.run_action(request, action.name, arguments)
    if "error" in result:
        return _result(str(result["error"]), is_error=True)
    if "more_info_needed" in result:
        return _needs_more_information(action, result)
    # Same undo stack as the palette.
    palette_actions.remember_undo(request.user, action.name, result)
    links = resources.links_for(action.name, result.get(palette_actions.KEY_ABOUT))
    body = _text(_absolute(_payload(result), request.build_absolute_uri))
    # Parsed back from the text so both say the same thing and the structure is JSON-safe (Decimals).
    return _result(body, structured=json.loads(body), links=links)
