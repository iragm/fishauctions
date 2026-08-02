"""Natural-language orchestration for the command palette.

``assist(request, query, context)`` is the whole feature. It decides whether a query even needs a
language model, and if it does, runs a short, bounded conversation with one:

1. **Obvious matches never reach the model.** The ordinary palette search runs first. A short query
   that already produces a Go-To shortcut or a close title match comes straight back as
   ``{"kind": "results"}`` -- no LLM call, no cost, no latency. Typing "invoice" behaves exactly as
   it always has.
2. **Everything else gets a bounded agent loop** (at most :data:`MAX_ROUNDS` rounds inside
   :data:`TOTAL_BUDGET_SECONDS`). The system prompt is generated from the action registry, so it can
   never advertise something the server won't accept. The model may call read-only lookups
   (``find_person``, ``my_context``) to resolve "bob" into a bidder number, then must finish with an
   action, a navigation, a clarifying question, or an error.

**The model is untrusted input.** Its replies are schema-checked here, the action name must be in
the registry, parameters are checked against that action's own schema, and the resolver re-validates
everything again against the database and the user's permissions. A reply that doesn't fit the
contract is discarded, never guessed at.

Danger levels decide what comes back:

  ``safe``     -> executed here, returned as ``done``
  ``confirm``  -> returned as ``countdown``; **not executed**. The client shows a 5s spinner with
                  Cancel / Go now and then calls the execute endpoint, which runs the resolver from
                  scratch. The countdown is UX; the server is the gate.
  ``navigate`` -> returned as ``navigate`` with a URL. We never perform the thing on the far side.

Response kinds returned to the client:
``results`` | ``navigate`` | ``countdown`` | ``clarify`` | ``done`` | ``error``
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from django.core.cache import cache

from . import command_palette, palette_actions
from .llm import LLMError, assist_enabled, get_provider
from .models import CommandPaletteSearch, LLMUsage

logger = logging.getLogger(__name__)

# Agent loop bounds. A palette command is a one-liner; anything needing more than a few rounds is
# a sign the model is lost, and the user is waiting.
MAX_ROUNDS = 4
TOTAL_BUDGET_SECONDS = 20.0

# Recent exchanges kept for context ("print that label" -> the lot we just added).
MAX_CONTEXT_ENTRIES = 5

# A query this short that already has a good match is answered by search alone.
SHORT_QUERY_WORDS = 4

# How long the client counts down before a confirm-tier action runs.
COUNTDOWN_MS = 5000

MAX_QUERY_LENGTH = 600

# Throttling. The 1/second cooldown is the anti-bot floor -- a real user, typing behind a 300ms
# debounce and reading the result, never sees it. The window cap is the spend ceiling: even a bot
# politely pacing itself at 1/sec can only burn 30 model calls per 5 minutes.
COOLDOWN_SECONDS = 1
COOLDOWN_MESSAGE = "One at a time — try that again in a second."
WINDOW_SECONDS = 300
WINDOW_MAX_CALLS = 30
WINDOW_MESSAGE = "You've used a lot of commands just now. Give it a few minutes and try again."

KIND_RESULTS = "results"
KIND_NAVIGATE = "navigate"
KIND_COUNTDOWN = "countdown"
KIND_CLARIFY = "clarify"
KIND_DONE = "done"
KIND_ERROR = "error"


# --- throttling --------------------------------------------------------------


def check_cooldown(user) -> str | None:
    """Enforce roughly one request per second per user. Returns a message when throttled.

    ``cache.add`` is atomic and only succeeds when the key is absent, which makes this a
    single round trip with no read-modify-write race.
    """
    key = f"palette_assist_cooldown_{user.pk}"
    if cache.add(key, 1, timeout=COOLDOWN_SECONDS):
        return None
    return COOLDOWN_MESSAGE


def check_call_budget(user) -> str | None:
    """Enforce the sustained cap on model calls. Returns a message when the user is over it."""
    key = f"palette_assist_calls_{user.pk}"
    cache.add(key, 0, timeout=WINDOW_SECONDS)
    try:
        used = cache.incr(key)
    except ValueError:
        # The key expired between add and incr; treat this as the first call of a new window.
        cache.set(key, 1, timeout=WINDOW_SECONDS)
        used = 1
    if used > WINDOW_MAX_CALLS:
        return WINDOW_MESSAGE
    return None


# --- input sanitising --------------------------------------------------------


def sanitize_context(raw: Any) -> list[dict[str, Any]]:
    """Validate and truncate the client-supplied recent-exchange list.

    The client keeps this in sessionStorage, so it is user-controlled and gets the same treatment
    as anything else from the browser: at most :data:`MAX_CONTEXT_ENTRIES` entries, strings capped,
    and only a small allow-list of scalar carry-over values (the lot we just made, the auction we
    were in) survives.
    """
    entries: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return entries
    for item in raw[-MAX_CONTEXT_ENTRIES:]:
        if not isinstance(item, dict):
            continue
        entry: dict[str, Any] = {
            "query": str(item.get("query") or "")[:300],
            "result": str(item.get("result") or "")[:300],
        }
        action = item.get("action")
        if isinstance(action, str):
            entry["action"] = action[:50]
        data = item.get("data")
        if isinstance(data, dict):
            safe = {}
            for key in ("lot_id", "lot_name", "auction", "bidder_number", "club"):
                value = data.get(key)
                if isinstance(value, bool) or not isinstance(value, (str, int)):
                    continue
                safe[key] = str(value)[:100]
            if safe:
                entry["data"] = safe
        if entry["query"] or entry["result"]:
            entries.append(entry)
    return entries


# --- the obvious-match heuristic ---------------------------------------------


def _looks_like_a_command(query: str) -> bool:
    """Cheap check for phrasing that wants doing rather than finding."""
    lowered = query.lower()
    verbs = (
        "add ",
        "create ",
        "make ",
        "sell ",
        "sold ",
        "check in",
        "check-in",
        "print ",
        "renew",
        "set ",
        "record ",
        "take me",
        "show me",
        "go to",
    )
    return any(lowered.startswith(verb) or f" {verb}" in lowered for verb in verbs)


def obvious_match(request, query: str) -> list[dict[str, Any]] | None:
    """Return ordinary search groups when the query clearly doesn't need a model.

    Short queries that already produce a Go-To shortcut, or that hit a result whose title is
    essentially what was typed, are answered by the existing palette search. Anything phrased as
    a command ("add a lot of...") goes to the model even when short.
    """
    # Cheap disqualifiers first, so a long or command-shaped query doesn't pay for a search it
    # was never going to be answered by.
    if _looks_like_a_command(query) or len(query.split()) > SHORT_QUERY_WORDS:
        return None
    groups = command_palette.search(request, query)
    if not groups:
        return None
    lowered = query.lower().strip()
    for group in groups:
        if group["label"] == "Go to" and group["items"]:
            return groups
        for item in group["items"]:
            title = (item.get("title") or "").lower()
            if lowered and (lowered == title or lowered in title):
                return groups
    return None


# --- prompt building ---------------------------------------------------------


SYSTEM_PROMPT = """You turn what a user typed or said into one action on an online fish-auction site.

You always reply with a single JSON object, and it must be exactly one of these shapes:

{{"lookup": "<name>", "params": {{...}}}}
    Call a read-only lookup and see the result before deciding. Use this to turn a name into a
    bidder number, or to check what auction the user is in. You may do this a few times.

{{"action": "<name>", "params": {{...}}, "summary": "<one short sentence saying what will happen>"}}
    Do the thing. The summary is shown to the user, so write it for them: "Add a lot of blue
    shrimp to the Spring Auction for Bob (bidder 14)".

{{"clarify": "<question>", "options": ["<choice>", "<choice>"]}}
    Ask when you genuinely can't tell what they meant. Keep it to one short question. Options are
    optional but helpful when there is a small set of possibilities.

{{"error": "<why this can't be done>"}}
    Use when the request is impossible or isn't something this site does.

Rules:
- Only use the action and lookup names listed below. Never invent one, and never invent a parameter.
- Only send parameters that are listed for that action.
- Prefer doing the obvious thing over asking. The user gets a 5 second countdown with a cancel
  button before anything is written, so a confident, sensible guess is better than a question.
- If the user does not say which auction, leave 'auction' out — it defaults to their most recent.
- When the user refers to something from earlier in the conversation ("print that label", "add
  another one"), use the details in the recent exchanges below.
- Do not make up bidder numbers, lot numbers or prices. Look them up or ask.

Available actions:
{actions}

About this user:
{context}
"""


def build_system_prompt(user) -> str:
    """Generate the system prompt from the registry so it can never drift from the server.

    Every action's name, description, parameter list and danger level comes straight out of
    ``palette_actions.ACTIONS``, so registering an action is all it takes to teach the model
    about it.
    """
    actions = json.dumps(palette_actions.registry_for_prompt(), indent=None)
    context = json.dumps(palette_actions.user_context(user), indent=None, default=str)
    return SYSTEM_PROMPT.format(actions=actions, context=context)


def build_messages(query: str, context: list[dict[str, Any]]) -> list[dict[str, str]]:
    """The user turn: the recent exchanges, then what they just asked for."""
    messages: list[dict[str, str]] = []
    if context:
        messages.append(
            {
                "role": "user",
                "content": "Recent exchanges, oldest first:\n" + json.dumps(context, default=str),
            }
        )
    messages.append({"role": "user", "content": query})
    return messages


# --- validating what the model said ------------------------------------------


def parse_reply(data: Any) -> dict[str, Any]:
    """Schema-check one model reply. Returns a normalized dict with a ``kind``.

    Anything that doesn't match the contract exactly comes back as ``{"kind": "invalid"}`` so the
    loop can tell the model it got the shape wrong rather than acting on a guess.
    """
    if not isinstance(data, dict):
        return {"kind": "invalid", "reason": "not an object"}

    params = data.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return {"kind": "invalid", "reason": "params must be an object"}

    if isinstance(data.get("lookup"), str):
        action = palette_actions.get_action(data["lookup"])
        if action is None or not action.lookup:
            return {"kind": "invalid", "reason": f"unknown lookup {data['lookup']!r}"}
        return {"kind": "lookup", "action": action, "params": params}

    if isinstance(data.get("action"), str):
        action = palette_actions.get_action(data["action"])
        if action is None:
            return {"kind": "invalid", "reason": f"unknown action {data['action']!r}"}
        summary = data.get("summary")
        return {
            "kind": "action",
            "action": action,
            "params": params,
            "summary": str(summary)[:300] if isinstance(summary, str) else "",
        }

    clarify = data.get("clarify")
    if clarify is None and isinstance(data.get("message"), str):
        clarify = data["message"]
    if isinstance(clarify, str) and clarify.strip():
        options = data.get("options")
        clean_options = []
        if isinstance(options, list):
            for option in options[:6]:
                if isinstance(option, str) and option.strip():
                    clean_options.append(option.strip()[:120])
                elif isinstance(option, dict) and isinstance(option.get("label"), str):
                    clean_options.append(option["label"].strip()[:120])
        return {"kind": "clarify", "message": clarify.strip()[:400], "options": clean_options}

    if isinstance(data.get("error"), str) and data["error"].strip():
        return {"kind": "error", "message": data["error"].strip()[:400]}

    return {"kind": "invalid", "reason": "no recognized key"}


# --- usage logging -----------------------------------------------------------


def record_usage(user, result, query: str, response_kind: str, action_name: str = "", success: bool = True) -> None:
    """Write one :class:`LLMUsage` row. Never allowed to break the request."""
    try:
        LLMUsage.objects.create(
            user=user,
            model=(result.model if result else "")[:100],
            prompt_tokens=result.prompt_tokens if result else 0,
            completion_tokens=result.completion_tokens if result else 0,
            total_tokens=result.total_tokens if result else 0,
            query=(query or "")[:600],
            response_kind=response_kind[:30],
            action=(action_name or "")[:50],
            success=success,
        )
    except Exception:
        logger.exception("Could not record LLM usage")


def log_assist(user, query: str, kind: str) -> None:
    """Record the query in the normal palette search log so analytics keeps working."""
    try:
        command_palette.log_search(
            user,
            search=query,
            result=CommandPaletteSearch.RESULT_CLICKED if kind != KIND_ERROR else CommandPaletteSearch.RESULT_BOUNCE,
            result_type="assist",
        )
    except Exception:
        logger.exception("Could not log assist search")


# --- results -----------------------------------------------------------------


def _result_to_response(action, params: dict[str, Any], result: dict[str, Any], summary: str) -> dict[str, Any]:
    """Turn a resolver's return value into a client response."""
    if "error" in result:
        return {"kind": KIND_ERROR, "message": result["error"]}
    if "more_info_needed" in result:
        return {
            "kind": KIND_CLARIFY,
            "message": result["more_info_needed"],
            "options": [o.get("label", "") for o in result.get("options", []) if isinstance(o, dict)],
        }
    if action.danger == palette_actions.DANGER_NAVIGATE and result.get("url"):
        return {
            "kind": KIND_NAVIGATE,
            "url": result["url"],
            "message": result.get("summary", ""),
            "action": action.name,
        }
    return {
        "kind": KIND_DONE,
        "message": result.get("summary") or summary or "Done.",
        "followups": result.get("followups", []),
        "action": action.name,
        # Carried into the next command's context, so "print that label" knows which lot.
        "data": _carry_over(result),
    }


def _carry_over(result: dict[str, Any]) -> dict[str, Any]:
    """The few values worth remembering for the next command ("print *that* label")."""
    data = {}
    for key in ("lot_id", "lot_name"):
        if result.get(key) is not None:
            data[key] = result[key]
    return data


def _countdown_response(action, params: dict[str, Any], summary: str) -> dict[str, Any]:
    return {
        "kind": KIND_COUNTDOWN,
        "action": action.name,
        "params": params,
        "summary": summary or f"{action.confirm_template or action.name}.",
        "delay_ms": COUNTDOWN_MS,
    }


# --- the loop ----------------------------------------------------------------


def assist(request, query: str, context: Any = None) -> dict[str, Any]:
    """Answer one palette command. See the module docstring for the contract.

    Never raises: every failure path degrades to ordinary search results or a friendly error.
    """
    user = request.user
    query = (query or "").strip()[:MAX_QUERY_LENGTH]
    if not query:
        return {"kind": KIND_RESULTS, "groups": command_palette.search(request, "")}

    # With no provider configured the palette must behave exactly as it did before this feature.
    if not assist_enabled():
        return {"kind": KIND_RESULTS, "groups": command_palette.search(request, query)}

    groups = obvious_match(request, query)
    if groups is not None:
        return {"kind": KIND_RESULTS, "groups": groups}

    entries = sanitize_context(context)
    provider = get_provider()
    system = build_system_prompt(user)
    messages = build_messages(query, entries)
    started = time.monotonic()

    for round_number in range(MAX_ROUNDS):
        if time.monotonic() - started > TOTAL_BUDGET_SECONDS:
            logger.info("Assist budget exhausted after %s rounds", round_number)
            break
        # Counted per model call, not per request: one request can take several rounds, and the
        # cap is a spend ceiling on tokens rather than a limit on how often the box is used.
        over_budget = check_call_budget(user)
        if over_budget:
            log_assist(user, query, KIND_ERROR)
            return {"kind": KIND_ERROR, "message": over_budget}
        try:
            result = provider.complete_json(system, messages)
        except LLMError as error:
            logger.warning("Assist provider error: %s", error)
            record_usage(user, None, query, KIND_ERROR, success=False)
            response = {"kind": KIND_ERROR, "message": "I couldn't reach the assistant just now."}
            log_assist(user, query, KIND_ERROR)
            return response

        reply = parse_reply(result.data)
        kind = reply["kind"]

        if kind == "invalid":
            record_usage(user, result, query, KIND_ERROR, success=False)
            logger.info("Assist got an invalid reply: %s", reply.get("reason"))
            messages.append({"role": "assistant", "content": json.dumps(result.data)[:1000]})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "That was not one of the allowed JSON shapes "
                        f"({reply.get('reason')}). Reply again with a single valid object."
                    ),
                }
            )
            continue

        if kind == "lookup":
            action = reply["action"]
            record_usage(user, result, query, "lookup", action.name)
            lookup_result = palette_actions.run_action(request, action.name, reply["params"])
            messages.append({"role": "assistant", "content": json.dumps(result.data)[:1000]})
            messages.append(
                {
                    "role": "user",
                    "content": f"Result of {action.name}: {json.dumps(lookup_result, default=str)[:2000]}",
                }
            )
            continue

        if kind == "clarify":
            record_usage(user, result, query, KIND_CLARIFY)
            log_assist(user, query, KIND_CLARIFY)
            return {"kind": KIND_CLARIFY, "message": reply["message"], "options": reply["options"]}

        if kind == "error":
            record_usage(user, result, query, KIND_ERROR, success=False)
            log_assist(user, query, KIND_ERROR)
            return {"kind": KIND_ERROR, "message": reply["message"]}

        # kind == "action"
        action = reply["action"]
        params = reply["params"]
        summary = reply["summary"]

        if action.danger == palette_actions.DANGER_CONFIRM:
            # Do NOT execute. The client counts down and then calls the execute endpoint, which
            # runs the resolver (and therefore every permission check) from scratch.
            record_usage(user, result, query, KIND_COUNTDOWN, action.name)
            log_assist(user, query, KIND_COUNTDOWN)
            return _countdown_response(action, params, summary)

        action_result = palette_actions.run_action(request, action.name, params)
        response = _result_to_response(action, params, action_result, summary)
        record_usage(user, result, query, response["kind"], action.name, success=response["kind"] != KIND_ERROR)
        log_assist(user, query, response["kind"])
        return response

    record_usage(user, None, query, KIND_ERROR, success=False)
    log_assist(user, query, KIND_ERROR)
    return {"kind": KIND_ERROR, "message": "I couldn't work out how to do that."}


def execute(request, name: str, params: Any) -> dict[str, Any]:
    """Run a confirm-tier action after the client's countdown finished.

    The server is the gate, not the countdown: this re-runs the resolver, which re-checks
    permissions and re-validates every parameter. A client that skips, shortens or fakes the
    countdown gains nothing.
    """
    action = palette_actions.get_action(name)
    if action is None:
        return {"kind": KIND_ERROR, "message": "I don't know how to do that."}
    if action.danger != palette_actions.DANGER_CONFIRM:
        # Safe actions already ran during assist; navigate actions are the client's job.
        return {"kind": KIND_ERROR, "message": "That isn't something to confirm."}
    if not isinstance(params, dict):
        return {"kind": KIND_ERROR, "message": "Those instructions didn't make sense."}
    result = palette_actions.run_action(request, action.name, params)
    return _result_to_response(action, params, result, "")
