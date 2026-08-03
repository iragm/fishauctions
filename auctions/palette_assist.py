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

**It streams.** A round trip to the model is slow and several of them are slower, so
``assist_stream`` yields ``{"kind": "progress"}`` events as it goes -- "Searching for “bob”…",
"Opening the treasurer report…" -- and the client renders them as they arrive. Every one of those
lines describes something the server has just decided or is about to do; none of them is a timer
animating a guess. ``assist()`` is the same loop with the progress swallowed, for the plain JSON
endpoint.

**It doesn't dead-end.** When the loop runs out of rounds, or the model says it can't help, the
answer is not "I couldn't work out how to do that". :func:`_give_up` tries ordinary search results
first, then its own best guess at a page from the route catalog, and only errors when the query
matched nothing anywhere on the site. Each of those outcomes is recorded under its own
``LLMUsage.response_kind`` so the analytics page can list the queries that defeated it -- which is
the most direct feature backlog this thing has.

Response kinds returned to the client:
``progress`` (streaming only) | ``results`` | ``navigate`` | ``countdown`` | ``clarify`` |
``done`` | ``error``
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from django.core.cache import cache

from . import command_palette, palette_actions, palette_routes
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
#: Streamed to the client while the loop is still working. Never a final answer.
KIND_PROGRESS = "progress"

# What ``LLMUsage.response_kind`` records when things go wrong. These used to all be "error", which
# made the analytics page unable to tell "the model refused" from "OpenAI was down" -- and the
# first of those is a feature request while the second is an outage.
FAIL_GAVE_UP = "gave_up"  # the loop ran out of rounds without reaching an answer
FAIL_MODEL_ERROR = "model_error"  # the model said it couldn't do this
FAIL_PROVIDER = "provider_error"  # we couldn't reach the provider at all
FAIL_INVALID = "invalid_shape"  # the model replied with something off-contract
FAIL_THROTTLED = "throttled"  # the user hit the spend ceiling
#: Recorded when we couldn't act but ordinary search had something worth showing.
KIND_FALLBACK = "fallback"

#: Every failure kind, for the analytics page's "queries we couldn't answer" list.
FAILURE_KINDS = (FAIL_GAVE_UP, FAIL_MODEL_ERROR, FAIL_INVALID, KIND_FALLBACK)


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
- If the user does not say which auction, leave 'auction' out — it defaults to whatever they are
  looking at right now, and then to their most recent auction.
- When the user refers to something from earlier in the conversation ("print that label", "add
  another one"), use the details in the recent exchanges below.
- Do not make up bidder numbers, lot numbers or prices. Look them up or ask.
- **Never reply with an error just because nothing fits.** Every page on this site is listed below,
  so if you can't work out a specific action, take your best guess at what the user was trying to
  reach and send them there with go_to_page. Landing on roughly the right page is useful; telling
  them you don't understand is not. Only use the error shape when the request is genuinely not
  something this site does at all.

Available actions:
{actions}

Pages you can open with go_to_page (this is every page on the site — the 'page' parameter must be
one of these keys):
{pages}

About this user:
{context}
"""


def build_system_prompt(user, page: dict[str, Any] | None = None) -> str:
    """Generate the system prompt from the registry so it can never drift from the server.

    Every action's name, description, parameter list and danger level comes straight out of
    ``palette_actions.ACTIONS``, and the page list out of ``palette_routes.ROUTE_LIST``, so
    registering an action or a route is all it takes to teach the model about it.

    The page catalog costs roughly a thousand prompt tokens per call, which buys two things worth
    more than that: the model can see every destination without a round trip to look one up (a
    whole model call of latency, on a feature where latency is the main complaint), and it can
    always answer *something* rather than giving up.
    """
    actions = json.dumps(palette_actions.registry_for_prompt(), indent=None)
    context = json.dumps(palette_actions.user_context(user, page), indent=None, default=str)
    pages = palette_routes.catalog_for_prompt(user)
    return SYSTEM_PROMPT.format(actions=actions, pages=pages, context=context)


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


# --- narrating what's happening ----------------------------------------------
#
# The whole point of streaming: an assist call can take the better part of twenty seconds, and a
# motionless "Working out what you mean…" for that long reads as a hang. Every line below describes
# something the server has actually just done or is about to do -- none of it is a timer pretending
# to be progress.

#: Opening line, chosen from the shape of the query so the very first frame already says something.
_OPENERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("add ", "create ", "new lot", "list a", "sell "), "Adding that…"),
    (("sold", "sell to", "winner", "goes to", "hammer"), "Recording that sale…"),
    (("undo", "unsell", "mistake", "wrong bidder"), "Undoing that…"),
    (("check in", "check-in", "checkin", "arriv", "sign in"), "Checking them in…"),
    (("print", "label", "sticker"), "Finding the right labels…"),
    (("renew", "membership", "dues", "subscri"), "Looking at memberships…"),
    (("take me", "go to", "open ", "show me", "where is", "where do i"), "Finding that page…"),
    (("invoice", "owe", "pay", "bill", "receipt"), "Looking up invoices…"),
    (("who ", "find ", "look up", "search"), "Searching…"),
)


def opening_line(query: str) -> str:
    """A first progress line derived from what the user actually typed.

    Costs nothing and is on screen before the model has said a word, which is most of the
    difference between "it's working" and "it's stuck".
    """
    lowered = f" {query.lower()} "
    for needles, line in _OPENERS:
        if any(needle in lowered for needle in needles):
            return line
    return "Working out what you mean…"


#: What each lookup is really doing, phrased for the person waiting.
_LOOKUP_NARRATION = {
    "find_person": "Searching for {target}…",
    "find_lot": "Looking for lot {target}…",
    "find_page": "Looking for the right page…",
    "my_context": "Checking which auction you're in…",
}


def narrate_lookup(action, params: dict[str, Any]) -> str:
    """Describe a lookup round: "Searching for bob…"."""
    template = _LOOKUP_NARRATION.get(action.name, "Looking that up…")
    target = ""
    for key in ("name", "query", "lot", "page"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            target = value.strip()[:60]
            break
    if "{target}" in template:
        return template.format(target=f"“{target}”") if target else "Searching…"
    return template


def narrate_action(action, params: dict[str, Any]) -> str:
    """Describe the action round: what we're about to do, before we do it."""
    if action.name == "go_to_page":
        route = palette_routes.get_route(str(params.get("page") or ""))
        if route:
            return f"Opening {route.label.lower()}…"
        return "Finding that page…"
    if action.confirm_template:
        return f"{action.confirm_template}…"
    return "Nearly there…"


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


#: Words that say how the user wants something rather than what they want. Stripped before the
#: fallback search, because "can you take me to where I pay my dues" only has one useful word in it.
_FILLER = frozenset(
    """a about all am an and any are as at be been being but by can could did do does for from get
    give go going had has have how i id if in into is it its just let like make may me my need of on
    once one only or our out over please put see should show so some take tell that the their them
    then there these they this to too took up us use want was way we were what when where whether
    which who whom why will with would you your""".split()
)


def _keywords(query: str) -> str:
    """The content words of a query, for a second search pass when the literal one found nothing."""
    words = [word for word in re.findall(r"[A-Za-z0-9']+", query.lower()) if word not in _FILLER]
    return " ".join(words[:6])


def _search_fallback(request, query: str, note: str) -> dict[str, Any] | None:
    """Ordinary search results, if there are any worth showing. Otherwise ``None``.

    This is what "I couldn't work out how to do that" becomes. A dead end is the one answer that
    is never useful; a list of things that match some of the words nearly always beats it, and the
    user can see for themselves whether we understood.
    """
    for attempt in (query, _keywords(query)):
        if not attempt:
            continue
        groups = command_palette.search(request, attempt)
        if any(group.get("items") for group in groups):
            return {"kind": KIND_RESULTS, "groups": groups, "note": note}
    return None


def _best_guess_page(request, query: str) -> dict[str, Any] | None:
    """Our own guess at the page the user wanted, from the route catalog.

    Runs after the model has failed, so it is deliberately not clever: one confident match gets
    offered as a navigation, several get offered as a choice, nothing gets nothing.
    """
    matches = palette_routes.match_routes(query, request.user, limit=3)
    if not matches:
        return None
    if len(matches) == 1:
        result = palette_routes.resolve_route(request, matches[0], {})
        if "error" not in result and result.get("url"):
            return {
                "kind": KIND_NAVIGATE,
                "url": result["url"],
                "message": f"I wasn't sure, so I've taken you to {matches[0].label.lower()}.",
                "action": "go_to_page",
            }
        return None
    return {
        "kind": KIND_CLARIFY,
        "message": "I'm not sure what you meant. Did you want one of these?",
        "options": [route.label for route in matches],
    }


def _give_up(request, query: str, message: str) -> dict[str, Any]:
    """The end of the line, in the order that helps most.

    Search results first (they show what we understood), then our own best-guess page, then --
    only if the query matched nothing anywhere on the site -- an actual error.
    """
    fallback = _search_fallback(request, query, "I wasn't sure what you meant. Here's what I found:")
    if fallback:
        return fallback
    guess = _best_guess_page(request, query)
    if guess:
        return guess
    return {"kind": KIND_ERROR, "message": message}


def _progress(text: str) -> dict[str, Any]:
    return {"kind": KIND_PROGRESS, "message": text}


def assist_stream(request, query: str, context: Any = None, path: str = ""):
    """Answer one palette command, yielding progress as it goes.

    Yields zero or more ``{"kind": "progress"}`` dicts and then exactly one final response. The
    progress messages are what the server is doing at that moment -- a lookup it just decided to
    run, an action it's about to perform -- so the client can narrate honestly instead of animating
    a guess.

    :func:`assist` wraps this for callers that only want the answer. Never raises: every failure
    path degrades to ordinary search results or a friendly error.
    """
    user = request.user
    query = (query or "").strip()[:MAX_QUERY_LENGTH]
    # Worked out once, here, and hung on the request so every resolver downstream sees the same
    # thing without re-parsing the path.
    request.palette_page = palette_routes.page_context_from_path(user, path) if path else {}

    if not query:
        yield {"kind": KIND_RESULTS, "groups": command_palette.search(request, "")}
        return

    # With no provider configured the palette must behave exactly as it did before this feature.
    if not assist_enabled():
        yield {"kind": KIND_RESULTS, "groups": command_palette.search(request, query)}
        return

    groups = obvious_match(request, query)
    if groups is not None:
        yield {"kind": KIND_RESULTS, "groups": groups}
        return

    yield _progress(opening_line(query))

    entries = sanitize_context(context)
    provider = get_provider()
    system = build_system_prompt(user, request.palette_page)
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
            record_usage(user, None, query, FAIL_THROTTLED, success=False)
            log_assist(user, query, KIND_ERROR)
            yield {"kind": KIND_ERROR, "message": over_budget}
            return
        try:
            result = provider.complete_json(system, messages)
        except LLMError as error:
            logger.warning("Assist provider error: %s", error)
            record_usage(user, None, query, FAIL_PROVIDER, success=False)
            log_assist(user, query, KIND_ERROR)
            # The provider being down says nothing about the query, so search is still worth a go.
            yield _give_up(request, query, "I couldn't reach the assistant just now.")
            return

        reply = parse_reply(result.data)
        kind = reply["kind"]

        if kind == "invalid":
            record_usage(user, result, query, FAIL_INVALID, success=False)
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
            yield _progress(narrate_lookup(action, reply["params"]))
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
            yield {"kind": KIND_CLARIFY, "message": reply["message"], "options": reply["options"]}
            return

        if kind == "error":
            # The model has told us it can't do this. It is still better to show the user something
            # than to show them a refusal, so this goes through the same ladder as running out.
            record_usage(user, result, query, FAIL_MODEL_ERROR, success=False)
            log_assist(user, query, KIND_ERROR)
            yield _give_up(request, query, reply["message"])
            return

        # kind == "action"
        action = reply["action"]
        params = reply["params"]
        summary = reply["summary"]
        yield _progress(narrate_action(action, params))

        if action.danger == palette_actions.DANGER_CONFIRM:
            # Do NOT execute. The client counts down and then calls the execute endpoint, which
            # runs the resolver (and therefore every permission check) from scratch.
            record_usage(user, result, query, KIND_COUNTDOWN, action.name)
            log_assist(user, query, KIND_COUNTDOWN)
            yield _countdown_response(action, params, summary)
            return

        action_result = palette_actions.run_action(request, action.name, params)
        response = _result_to_response(action, params, action_result, summary)
        if response["kind"] == KIND_ERROR:
            # The action was understood but couldn't run (wrong auction, no permission, closed for
            # submissions). Keep the real reason -- it's specific and useful -- but offer the
            # search results underneath it rather than ending on a wall.
            record_usage(user, result, query, KIND_ERROR, action.name, success=False)
            log_assist(user, query, KIND_ERROR)
            yield response
            return
        record_usage(user, result, query, response["kind"], action.name)
        log_assist(user, query, response["kind"])
        yield response
        return

    record_usage(user, None, query, FAIL_GAVE_UP, success=False)
    log_assist(user, query, KIND_ERROR)
    yield _give_up(request, query, "I couldn't work out how to do that.")


def assist(request, query: str, context: Any = None, path: str = "") -> dict[str, Any]:
    """Answer one palette command and return just the answer.

    The non-streaming half of :func:`assist_stream`, for the plain JSON endpoint and for callers
    that have nowhere to put progress messages.
    """
    response: dict[str, Any] = {"kind": KIND_ERROR, "message": "I couldn't work out how to do that."}
    for event in assist_stream(request, query, context, path):
        if event.get("kind") != KIND_PROGRESS:
            response = event
    return response


def execute(request, name: str, params: Any, path: str = "") -> dict[str, Any]:
    """Run a confirm-tier action after the client's countdown finished.

    The server is the gate, not the countdown: this re-runs the resolver, which re-checks
    permissions and re-validates every parameter. A client that skips, shortens or fakes the
    countdown gains nothing.

    The page is resolved again here too. It has to be: the resolver reads it to default an auction,
    and this call does not trust anything the assist call worked out a moment ago.
    """
    request.palette_page = palette_routes.page_context_from_path(request.user, path) if path else {}
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
