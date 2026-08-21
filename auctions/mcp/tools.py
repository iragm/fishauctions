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

logger = logging.getLogger(__name__)

#: How much of one tool result is worth sending back. Generous next to the palette's own
#: ``MAX_LOOKUP_RESULT_CHARS`` (which has to leave room for several rounds inside one budget):
#: an MCP host is showing this to a model with a whole context window, not squeezing it into a
#: system prompt. Still bounded, because "do not return a full database dump" is a review
#: criterion and because a runaway ``list_lots`` should not be able to fill somebody's window.
MAX_RESULT_CHARS = 20000

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
    """One MCP tool descriptor."""
    title = title_for(action)
    return {
        "name": action.name,
        "title": title,
        "description": describe(action),
        "inputSchema": input_schema(action),
        "annotations": {
            "title": title,
            "readOnlyHint": read_only(action),
            "destructiveHint": action.destructive,
            "idempotentHint": idempotent(action),
            # Everything here reads and writes this site's own database. Nothing reaches out.
            "openWorldHint": False,
        },
    }


def tool_descriptors(user=None, *, writes: bool = True) -> list[dict[str, Any]]:
    """The catalogue for one caller.

    ``user=None`` means every action, for the audit test. Otherwise the list is filtered by
    ``palette_actions.actions_for`` -- a bidder who runs no club and no auction is not offered
    club administration, exactly as they were not shown it in the prompt. That is a relevance and
    size filter, not the security boundary: the resolvers are.

    ``writes=False`` drops every write tool, for a credential that was only granted reads. It has
    to be applied to the *list* as well as to the call, or a read-only agent spends its turn
    picking a tool it is about to be refused.
    """
    return [descriptor(action) for action in palette_actions.actions_for(user) if writes or read_only(action)]


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    """A resolver's result with our own bookkeeping taken out of it."""
    return {key: value for key, value in result.items() if key not in _INTERNAL_RESULT_KEYS}


def _text(payload: Any) -> str:
    """Serialise a result, bounded, and say so when it had to be cut."""
    body = json.dumps(payload, indent=2, default=str)
    if len(body) <= MAX_RESULT_CHARS:
        return body
    return body[:MAX_RESULT_CHARS] + f"\n… truncated at {MAX_RESULT_CHARS} characters. Narrow the query."


def _result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(request, name: str, arguments: Any, *, writes: bool = True) -> dict[str, Any]:
    """Run one tool for the user on ``request`` and return an MCP ``CallToolResult``.

    Every path through here goes to :func:`palette_actions.run_action`, which is the same single
    entry point the palette's execute endpoint uses: it re-checks the permissions and re-validates
    the parameters against the database, whatever the caller believed a moment ago.

    The three shapes a resolver can return map onto MCP like this:

    ``{"error": …}``            -> ``isError``. The message is already written for a person and
                                   says what to do instead, which is what makes it recoverable.
    ``{"more_info_needed": …}`` -> ``isError`` too, carrying the question and the candidates. The
                                   caller asks its user and calls again. (Elicitation is the
                                   spec-native way to do this without a round trip; it is not here
                                   yet because host support for it is still arriving.)
    ``{"ok": True, …}``         -> the result as JSON, summary included.
    """
    if not isinstance(arguments, dict):
        arguments = {}
    action = palette_actions.get_action(name)
    if action is None:
        raise UnknownTool(name)
    if not writes and not read_only(action):
        return _result(
            f"“{action.name}” changes data and this credential is read-only. Only read-only tools are available to it.",
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
        options = [option.get("label", "") for option in result.get("options") or [] if isinstance(option, dict)]
        text = str(result["more_info_needed"])
        if options:
            text += "\nCandidates: " + "; ".join(filter(None, options))
        return _result(text, is_error=True)
    # Same call the palette's execute endpoint makes, so "undo that" works across both surfaces:
    # the stack is per user, and a lot added by an agent is a lot the same person can undo.
    palette_actions.remember_undo(request.user, action.name, result)
    return _result(_text(_payload(result)))
