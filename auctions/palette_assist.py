"""Natural-language orchestration for the command palette.

``assist(request, query, context)`` is the whole feature. It decides whether a query even needs a
language model, and if it does, runs a short, bounded conversation with one:

1. **Obvious matches never reach the model.** The ordinary palette search runs first. A short query
   that already produces a Go-To shortcut or a close title match comes straight back as
   ``{"kind": "results"}`` -- no LLM call, no cost, no latency. Typing "invoice" behaves exactly as
   it always has.
2. **Everything else gets a bounded agent loop** (at most :data:`MAX_ROUNDS` rounds inside
   :data:`TOTAL_BUDGET_SECONDS`), with the model **calling tools**. The tools are the same
   catalogue ``/mcp/`` serves to Claude -- :func:`auctions.mcp.tools.tool_descriptors`, generated
   from :data:`auctions.palette_actions.ACTIONS` -- so the palette can never be taught a skill the
   MCP endpoint doesn't have, or shown one the server won't accept. The model may call read-only
   lookups (``find_person``, ``my_context``) to resolve "bob" into a bidder number, then finishes
   with an action, a navigation, a clarifying question, an error, or plain text.

**The model is untrusted input**, but the shape of what it says is no longer this module's problem.
The provider enforces the tool schemas: the name is one of the ones we sent and the arguments fit
the JSON Schema we generated. This file used to carry ~200 lines whose entire job was reading
replies that JSON mode had no way to prevent -- a page key in the "action" slot, an auction title
where a lookup name goes, a call written as ``{"go_to_page": {...}}`` -- and those are gone with the
thing that caused them. What remains is the part that was never about shape: ``run_action`` refuses
a parameter the action never advertised, and the resolver re-validates everything against the
database and the user's permissions.

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

**It never shows the user an identifier.** The model works in slugs and route keys because that is
what the lookups give it, and it will happily repeat them back. :func:`humanize` runs over every
user-facing string on the way out and turns them back into titles and labels.

Response kinds returned to the client:
``progress`` (streaming only) | ``results`` | ``navigate`` | ``countdown`` | ``clarify`` |
``answer`` | ``done`` | ``error``
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any

from django.core.cache import cache
from django.db.models import F

from . import command_palette, llm, palette_actions, palette_routes
from .llm import LLMError, assist_enabled, get_provider
from .models import CommandPalettePage, CommandPaletteSearch, LLMUsage

logger = logging.getLogger(__name__)

# Agent loop bounds. A palette command is a one-liner; anything needing more than a few rounds is
# a sign the model is lost, and the user is waiting.
#
# Every round is a fresh call carrying the whole ~3k-token system prompt, so rounds are what this
# feature costs -- two rounds is twice the price of one.
#
# Two, from measuring it rather than from a hunch: over a spread of realistic commands, eleven of
# the thirteen that produced a usable answer did it in one round and the other two took two (a
# lookup, then the action it enabled). Nothing needed a third. A third round only helps the query
# that replies off-contract *and* then needs a lookup, which is rare enough not to be worth adding
# 50% to the worst-case cost of every command that goes wrong.
MAX_ROUNDS = 2
#: The ceiling once a lookup has run. See :func:`_rounds_allowed`: a request that has fetched real
#: data has earned the round it needs to say what it found, and ending the loop on the lookup itself
#: wastes everything already spent on it.
MAX_ROUNDS_AFTER_LOOKUP = 3
#: How many times the model is worth telling that it already has what it just asked for again.
MAX_REPEAT_NUDGES = 1
TOTAL_BUDGET_SECONDS = 20.0

# Recent exchanges kept for context ("print that label" -> the lot we just added).
MAX_CONTEXT_ENTRIES = 5

# How much of a lookup's result is fed back to the model.
#
# This was 2000, and a describe_* lookup is bigger than that, so the cut landed in the middle of the
# JSON and took the end of it away. ``describe_auction`` lists the auction's fees *after* its dates
# and its rules text, so "what's the bidder fee split" got a reply built from an auction whose fee
# settings had been trimmed off before the model ever saw them -- and answered with an invented one.
# The describe_* lookups have been slimmed to fit under this (the chart blob they used to carry was
# most of the old overflow), so the raise is headroom rather than the fix.
#
# 5000 leaves the largest lookup (``describe_auction``, with a full-length rules block) about 700
# characters of headroom, so a couple of new settings can be added before anyone has to think about
# it again -- and :func:`lookup_payload` says so loudly if that day comes.
MAX_LOOKUP_RESULT_CHARS = 5000

#: Two things the palette needs the model to be able to do that no auction skill covers: ask the
#: person a question, and say it can't help. Over ``/mcp/`` neither exists -- a host does its own
#: asking, and a tool that fails says so through ``isError`` -- so they live here rather than in
#: the shared catalogue, and they are the only tools the palette adds to it.
#:
#: They are tools rather than a "just write some JSON instead" instruction because that is the
#: whole point of the rewrite: one enforced shape, with the arguments schema-checked by the
#: provider, instead of a second contract this file has to police.
ASK_THE_USER = "ask_the_user"
CANNOT_DO_THIS = "cannot_do_this"

PALETTE_TOOLS: list[dict[str, Any]] = [
    {
        "name": ASK_THE_USER,
        "title": "Ask the user",
        "description": (
            "Ask the person a short question when you genuinely cannot tell what they meant. Any "
            "question offering a choice between things must put each choice in 'options', written "
            "so it can be clicked as a reply on its own. Only offer choices between things you "
            "have actually looked up."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "One short question."},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The choices, each one clickable on its own.",
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
        "annotations": {"title": "Ask the user", "readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": CANNOT_DO_THIS,
        "title": "Cannot do this",
        "description": (
            "Say that the request is impossible or is not something this site does. Not for "
            "'I am not sure which page' — go_to_page reaches every page on the site."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "Why it can't be done."}},
            "required": ["reason"],
            "additionalProperties": False,
        },
        "annotations": {"title": "Cannot do this", "readOnlyHint": True, "destructiveHint": False},
    },
]


def tools_for(user) -> list[dict[str, Any]]:
    """Every tool this user's palette may call: the shared catalogue, plus the two above.

    The catalogue is the same object ``/mcp/`` hands to Claude. That is the point of the whole
    arrangement -- there is one description of what this site can do, one dispatcher, and one set
    of permission checks, and the palette is simply another client of it.

    ``Action.mcp_only`` is the one subtraction, and it names rather than breaks that rule: the two
    surfaces differ in who reads the answer, not in what may be done or by whom. ``read_source``
    hands back a page of Python, which is the right answer for an agent with a context window of
    its own and the wrong thing to render in a one-line box on somebody's phone at this site's own
    expense. Nothing else has ever qualified, and a second one should have to argue for itself.
    """
    from .mcp import tools as mcp_tools

    shared = [
        tool
        for tool in mcp_tools.tool_descriptors(user)
        if not getattr(palette_actions.get_action(tool["name"]), "mcp_only", False)
    ]
    return [*shared, *PALETTE_TOOLS]


# A query this short that already has a good match is answered by search alone.
SHORT_QUERY_WORDS = 4

# How long the client counts down before a confirm-tier action runs.
COUNTDOWN_MS = 5000

# The countdown, once you have already done this exact thing and let it run.
#
# Five seconds is right the first time somebody adds a lot: it is the only chance to catch a
# misheard name before it is written down. It is wrong the fortieth time, at a drop-off table, with
# a queue -- that is three minutes of an evening spent watching progress bars for an action the
# person has now approved thirty-nine times, and it is how a feature gets abandoned mid-event.
#
# So the window is earned, per action and per auction, by letting one run finish: same action, same
# subject, ten minutes, administrators only. It is shortened, never skipped -- Cancel is still on
# screen and still works, and the server still re-runs every permission check. Cancelling once
# spends the trust immediately (see :func:`forget_trust`), because a cancel is the user saying we
# got it wrong, and the next card is exactly where they need the time back.
TRUSTED_COUNTDOWN_MS = 1500
TRUST_WINDOW_SECONDS = 600

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
KIND_ANSWER = "answer"
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

#: What ``LLMUsage.destination`` holds when a query was answered out of one lookup rather than by
#: going somewhere. See :func:`preloadable_lookup`.
LOOKUP_DESTINATION_PREFIX = "lookup:"

#: How many times a phrase must have been answered by the same lookup before it is preloaded, and
#: how long that verdict is cached. The same unanimity rule ``mine_palette_shortcuts`` uses: one
#: disagreement and the phrase goes back to the model, because a query answered two different ways
#: is one where context matters.
PRELOAD_MIN_COUNT = 5
PRELOAD_CACHE_SECONDS = 3600


# --- who gets this at all -----------------------------------------------------


def assist_enabled_for(user) -> bool:
    """True when *this user* should be offered natural-language/voice commands.

    Two independent gates, both required:

    * the install has a model configured at all (:func:`auctions.llm.assist_enabled`), and
    * the user has opted in (``UserData.use_llm_search``).

    The preference is on by default and is deliberately not on the preferences page -- it is a
    lever for the site's admins, unchecked in the Django admin to take the palette away from one
    user who abused it. With either gate shut the palette must behave exactly as it did before
    this feature existed: no mic, no assist calls, Enter falls through to ordinary search.
    Everything user-facing asks *this* function rather than ``assist_enabled()`` so the answer
    can't differ between the template and the endpoints.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    # Reverse one-to-one raises RelatedObjectDoesNotExist (an AttributeError) when the row is
    # missing, which getattr turns back into the default rather than a 500 on every page.
    userdata = getattr(user, "userdata", None)
    if not userdata or not userdata.use_llm_search:
        return False
    return assist_enabled()


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


def normalize_query(query: str) -> str:
    """The form a query is stored and matched under: lowercase, depunctuated, single-spaced.

    Shared by the shortcut short-circuit and ``manage.py mine_palette_shortcuts`` so the phrase the
    miner writes down is exactly the phrase the palette will later match.
    """
    return " ".join(re.findall(r"[a-z0-9']+", (query or "").lower()))


def shortcut_match(request, query: str) -> list[dict[str, Any]] | None:
    """Answer from a curated shortcut, without a model call, when one matches the query exactly.

    This is the only path that costs nothing at all, and the one the shortcut miner exists to feed.
    Deliberately an **exact** match on the normalized phrase rather than the substring matching the
    ordinary palette search uses, and with no length limit: an exact hit on a curated phrase is not
    a guess, so it is safe at any length, where a fuzzy one at seven words is not. Measured on real
    commands, scoring routes locally and taking the best agreed with the model on well under half
    of the queries it fired on -- which is why nothing here scores anything.

    Returns search-result groups (so the answer looks like any other), or ``None`` to carry on to
    the model.
    """
    normalized = normalize_query(query)
    if not normalized:
        return None
    for page in CommandPalettePage.objects.filter(is_active=True):
        if normalized not in {normalize_query(phrase) for phrase in command_palette._page_phrases(page)}:
            continue
        # resolve_page re-checks permissions and returns nothing for a page this user can't open,
        # in which case we fall through to the model rather than answering with an empty list.
        items = command_palette.resolve_page(page, request.user)
        if items:
            CommandPalettePage.objects.filter(pk=page.pk).update(hits=F("hits") + 1)
            return [{"label": "Go to", "items": items}]
    return None


def preloadable_lookup(query: str) -> str | None:
    """The lookup this exact phrase has always been answered from, if there is one.

    ``mine_palette_shortcuts`` turns repeated *navigations* into zero-token shortcuts, because a
    navigation's whole answer is a URL. A repeated *lookup* -- "when does this auction end", asked by
    forty people -- has no such answer to write down: what comes back depends on who is asking. So it
    paid full price every time, and at two rounds (pick the lookup, then say what it found) it is the
    most expensive shape of query this feature has.

    What generalises across users is not the answer, it is the *choice of lookup*. So this looks for
    a phrase that has been answered from one parameterless lookup at least
    :data:`PRELOAD_MIN_COUNT` times and never from any other, and the loop then runs that lookup
    *before* the first model call -- one round instead of two, for the same answer.

    Restricted to lookups that were called with **no parameters**, and that is the whole safety
    argument: such a lookup resolves entirely from the caller's own context and permissions
    (``my_activity``, ``auction_numbers``, ``lot_queue``), so running it for the next person who
    types the same words tells them about *their* auction and can't leak anybody else's. A phrase
    that resolved with an argument ("how many lots in the spring auction") is not preloaded, because
    the argument is the part that varies.

    Nothing is cached about the *result*, only about which lookup to run. Cached for an hour per
    phrase; a wrong verdict costs one unnecessary read-only query, never a wrong answer, because the
    model still sees the result and still decides what to say.
    """
    phrase = normalize_query(query)
    if not phrase:
        return None
    # Hashed, not embedded: a normalized phrase still contains spaces, and a cache key with a space
    # in it is a hard error on memcached and a warning on every other backend.
    key = "palette_preload_" + hashlib.sha256(phrase.encode("utf-8")).hexdigest()[:32]
    cached = cache.get(key)
    if cached is not None:
        return cached or None
    destinations = set(
        LLMUsage.objects.filter(query__iexact=query, success=True)
        .exclude(destination="")
        .values_list("destination", flat=True)[: PRELOAD_MIN_COUNT * 4]
    )
    verdict = ""
    if len(destinations) == 1:
        only = next(iter(destinations))
        if only.startswith(LOOKUP_DESTINATION_PREFIX):
            name = only[len(LOOKUP_DESTINATION_PREFIX) :]
            hits = LLMUsage.objects.filter(query__iexact=query, success=True, destination=only).count()
            action = palette_actions.get_action(name)
            if hits >= PRELOAD_MIN_COUNT and action is not None and action.lookup:
                verdict = name
    cache.set(key, verdict, timeout=PRELOAD_CACHE_SECONDS)
    return verdict or None


def _answered_from(lookups_run: set[tuple[str, str]]) -> str:
    """The ``destination`` to record for an answer: the one parameterless lookup behind it, or "".

    Empty for everything else, which is the point -- an unrecorded query can never be mined into a
    preload, so the restriction enforces itself rather than needing to be re-checked later.
    """
    if len(lookups_run) != 1:
        return ""
    name, params = next(iter(lookups_run))
    if params not in ("{}", ""):
        return ""
    return f"{LOOKUP_DESTINATION_PREFIX}{name}"


def _preload_messages(request, query: str, messages: list[dict[str, Any]]) -> str:
    """Run the lookup this phrase always needs, and hand its result over before the first round.

    Returns the lookup's name when one ran, so the loop can record that this request already has it
    (and never asks for it twice). Best-effort: anything that goes wrong here just means the model
    picks the lookup itself, exactly as it did before.
    """
    name = preloadable_lookup(query)
    if not name:
        return ""
    try:
        result = palette_actions.run_action(request, name, {})
    except Exception:
        logger.exception("Preloading the %s lookup failed", name)
        return ""
    if not isinstance(result, dict) or "error" in result:
        return ""
    messages.append(
        {
            "role": "user",
            "content": (
                f"This question is usually answered from {name}, so I have already run it. "
                "Do not call it again.\n" + lookup_payload(name, result)
            ),
        }
    )
    return name


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

You have tools. Call one of them, or reply in plain words.

**Call a read-only tool** (find_person, find_lot, my_context, describe_*) to look something up
before deciding — to turn a name into a bidder number, or to check which auction they're in. You'll
be given the result and can then act. You may do this a few times.

**Call an action tool** to do the thing. The user gets a 5 second countdown with a cancel button
before anything is written, so a confident, sensible guess is better than a question.

**Call go_to_page** to take them somewhere. Its 'page' parameter takes one of the destination keys
listed below, which is every page this site has.

**Call ask_the_user** when you genuinely can't tell what they meant.

**Call cannot_do_this** only when the request is not something this site does at all.

**Reply in plain words** to answer a question — but only from a tool result above, or from the
facts under "About this user". Never from memory, and never a guess: if it isn't in one of those
two places, look it up first. Two or three sentences at most; they are reading this in a small box.
Answer the question that was asked, and lead with the fact rather than with "Yes" or "No": write
"The Fall Auction is in person, not online", never "Yes. The Fall Auction is in person". A yes that
contradicts the sentence after it is worse than no answer at all.

Rules:
- If the user does not say which auction, leave 'auction' out — it defaults to whatever they are
  looking at right now, and then to their most recent auction.
- When the user refers to something from earlier in the conversation ("print that label", "add
  another one"), use the details in the recent exchanges below.
- Do not make up bidder numbers, lot numbers or prices. Look them up or ask.
- **Never show the user a slug, a database id, a route key or a URL.** Those are for you. The user
  gets titles and names: "the Spring Auction 2026", not "s-auction-july-2026"; "the lot list", not
  "auction_lot_list". Never repeat a tool result back to them raw.
- A question about how something works ("what are the rules", "how do I earn points", "when does
  submission close") wants an answer, not a page. Call the matching describe_* tool and then answer
  in words. Send them to a page only when they asked to go somewhere, or when the answer is
  genuinely not in anything you can look up.
- **Never say you can't help just because nothing fits.** Every page on this site is listed below,
  so if you can't work out a specific action, take your best guess at what the user was trying to
  reach and send them there with go_to_page. Landing on roughly the right page is useful; telling
  them you don't understand is not.
- When you call an action, you may also write one short sentence saying what will happen. It is
  shown to the user on the countdown card, so write it for them: "Add a lot of blue shrimp to the
  Spring Auction for Bob (bidder 14)".

Pages you can open with go_to_page (this is every page on the site — the 'page' parameter must be
one of these keys):
{pages}

About this user:
{context}
"""


def build_system_prompt(user, page: dict[str, Any] | None = None, app_destinations=()) -> str:
    """Generate the system prompt from the registry so it can never drift from the server.

    The skills are no longer in here at all: they are tool definitions now (:func:`tools_for`),
    generated from ``palette_actions.ACTIONS`` by the same code that serves ``/mcp/``. What is
    left is the page catalog, out of ``palette_routes.ROUTE_LIST``, and the user's own context.

    The catalog is filtered to what this user could plausibly reach, exactly as the tool list is
    and for the same reason. That is a prompt-size and relevance filter and nothing more: the
    resolvers re-check every permission.

    The page catalog costs roughly a thousand prompt tokens per call, which buys two things worth
    more than that: the model can see every destination without a round trip to look one up (a
    whole model call of latency, on a feature where latency is the main complaint), and it can
    always answer *something* rather than giving up.

    ``app_destinations`` (from :func:`~auctions.command_palette.app_destinations_for_prompt`) adds
    the handful of native screens that exist only inside the app -- lot scanning, Tap to Pay. They
    aren't pages and so can't be in the catalog, but they are places a user asks to be taken to, and
    the catalog holds a plausible wrong answer for each.
    """
    # ``strip_internal`` because ``user_context`` also answers the ``my_context`` tool, where it
    # carries the URIs a host can address things by. That is worth bytes there and is dead weight
    # here, in a system prompt rebuilt on every keystroke.
    context = json.dumps(
        palette_actions.strip_internal(palette_actions.user_context(user, page)), indent=None, default=str
    )
    pages = palette_routes.catalog_for_prompt(user)
    if app_destinations:
        pages += "\nIn the app, where this user is right now (native screens, same 'page' parameter):\n"
        pages += "\n".join(f"  {name}: {description}" for name, description in app_destinations)
    return SYSTEM_PROMPT.format(pages=pages, context=context)


def build_messages(query: str, context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The user turn: the recent exchanges, then what they just asked for.

    The list grows as the loop runs, and the turns it grows by are not all ``{role, content}``
    strings any more: an assistant turn may carry ``tool_calls`` and a tool turn carries a
    ``tool_call_id``. Both are built by :mod:`auctions.llm` so nothing here has to know the
    provider's spelling for them.
    """
    messages: list[dict[str, Any]] = []
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


#: How much of a model-written summary is worth keeping on the countdown card.
MAX_SUMMARY_CHARS = 300
#: Cap on one clarifying question and each of its options.
MAX_QUESTION_CHARS = 400
MAX_OPTION_CHARS = 120
MAX_OPTIONS = 6
#: Cap on a plain-text answer. They are reading it in a small box.
MAX_ANSWER_CHARS = 1200


def read_reply(result, user=None) -> dict[str, Any]:
    """Read one model reply into a normalized dict with a ``kind``.

    Short, now, and that is the whole story of this rewrite. The provider was handed real JSON
    Schema and enforced it, so the name is one of ours and the arguments fit -- there is no page
    key in the wrong slot to rescue, no auction title where a verb should be, no call written as
    its own key. Everything this used to do about *shape* is done before the bytes arrive.

    What is left is the mapping the palette actually needs: which of the tools the model picked is
    a read-only lookup (run it and come back), which is a thing to do, and which two are the
    palette's own (:data:`ASK_THE_USER`, :data:`CANNOT_DO_THIS`). Plain text with no call is an
    answer.

    ``user`` is unused and kept for the callers that pass it positionally; the scoping it used to
    do belonged to a repair path that no longer exists.
    """
    calls = getattr(result, "tool_calls", None) or []
    if calls:
        # One at a time. The loop is a short conversation with a person waiting on it, and a model
        # that asks for three things at once is asking us to guess an order to do them in --
        # which, for anything at confirm tier, is a guess about what gets written to the database.
        call = calls[0]
        params = call.arguments if isinstance(call.arguments, dict) else {}
        if call.name == ASK_THE_USER:
            question = str(params.get("question") or "").strip()
            if not question:
                return {"kind": "invalid", "reason": "asked nothing"}
            options = []
            raw = params.get("options")
            if isinstance(raw, list):
                options = [str(o).strip()[:MAX_OPTION_CHARS] for o in raw[:MAX_OPTIONS] if str(o).strip()]
            return {"kind": "clarify", "message": question[:MAX_QUESTION_CHARS], "options": options}
        if call.name == CANNOT_DO_THIS:
            reason = str(params.get("reason") or "").strip()
            return {"kind": "error", "message": (reason or "That isn't something this site does.")[:MAX_QUESTION_CHARS]}
        action = palette_actions.get_action(call.name)
        if action is None or action.mcp_only:
            # Only reachable behind an LLM_BASE_URL whose server doesn't enforce the tool list --
            # which is also the only way an ``mcp_only`` action could be named here, since
            # ``tools_for`` never sends one. Same answer either way: this palette has no such tool.
            return {"kind": "invalid", "reason": f"unknown tool {call.name!r}"}
        if action.lookup:
            return {"kind": "lookup", "action": action, "params": params}
        # A model may write a sentence alongside a call. When it does, it is the summary the
        # countdown card wants; when it doesn't, ``default_summary`` builds one from the
        # parameters, which is the case that used to read "Add someone to the auction."
        return {
            "kind": "action",
            "action": action,
            "params": params,
            "summary": (getattr(result, "text", "") or "").strip()[:MAX_SUMMARY_CHARS],
        }

    text = (getattr(result, "text", "") or "").strip()
    if text:
        return {"kind": "answer", "message": text[:MAX_ANSWER_CHARS]}
    return {"kind": "invalid", "reason": "empty reply"}


# --- keeping identifiers out of what the user reads --------------------------
#
# The model works in slugs and route keys because that is what the lookups hand it, and left to
# itself it repeats them back: "5 page(s) matching “open auction s-auction-july-2026 lots”" is a
# real answer this feature gave. The prompt now forbids it, but a prompt is a request, not a
# guarantee, so every user-facing string goes through :func:`humanize` on the way out.

#: Anything that could be a slug or a URL name: lowercase words joined by hyphens or underscores.
#: Matching loosely here is safe because nothing is replaced on the strength of its shape -- every
#: candidate has to turn out to be a real slug or a real route key, so ordinary hyphenated English
#: ("check-in", "sign-up", "e-mail") matches the pattern, resolves to nothing, and is left alone.
_IDENTIFIER = re.compile(r"\b[a-z0-9]+(?:[-_][a-z0-9]+)+\b")

#: Cap on how many candidates we'll look up for one message. A message is a couple of sentences;
#: anything past this is not prose and is not worth a query.
_MAX_IDENTIFIERS = 20


def _names_for_slugs(slugs: set[str], user=None) -> dict[str, str]:
    """Map each slug that belongs to an auction or club to its title. Two queries, not two per word.

    Scoped to *user*. Unscoped, this was a slug-to-title oracle: several of the messages it runs
    over echo a hint the caller typed, so posting ``{"auction": "a-guessed-slug"}`` to the execute
    endpoint came back as "I couldn't find an auction called “The Real Title Of It”" -- confirming
    the auction exists, and handing over its *current* title, for an auction the caller has no
    relationship with. (Slugs are frozen at creation, so a renamed auction's title is not
    recoverable from its slug.) Soft-deleted auctions and inactive clubs answered too.

    Nothing legitimate is lost: the slugs this is meant to tidy away come out of lookups, and every
    lookup is already scoped to the same user. Without a user, nothing resolves -- a message that
    still contains a slug is a cosmetic problem, and this function's failure mode must not be worse
    than the thing it exists to fix.
    """
    from . import command_palette
    from .models import Club

    names = {}
    if user is None or not slugs:
        return names
    clubs = Club.objects.filter(slug__in=slugs, active=True)
    if not getattr(user, "is_superuser", False):
        clubs = clubs.filter(id__in=[club.id for club in command_palette._admin_clubs(user)])
    for slug, name in clubs.values_list("slug", "name"):
        names[slug] = name
    # Auctions win a collision: the palette talks about auctions far more than clubs.
    visible = command_palette._visible_auctions(user).filter(slug__in=slugs)
    for slug, title in visible.values_list("slug", "title"):
        names[slug] = title
    return names


def humanize(text: str, user=None) -> str:
    """Replace identifiers in a user-facing string with the names they stand for.

    A slug becomes its auction or club title, a route key becomes the destination's label. A token
    that is neither is left exactly as it was: this runs over ordinary prose, and mangling a
    sentence would be worse than the identifier it was trying to remove.

    *user* scopes the slug lookup -- see :func:`_names_for_slugs`. Route keys need no scoping: a
    label like "all lots in an auction" is a static string from the catalog and names no object.
    """
    if not text or not isinstance(text, str):
        return text or ""
    try:
        candidates = set(_IDENTIFIER.findall(text))
        if not candidates or len(candidates) > _MAX_IDENTIFIERS:
            return text
        names = _names_for_slugs({candidate for candidate in candidates if "-" in candidate}, user)
        for candidate in candidates - set(names):
            route = palette_routes.get_route(candidate)
            if route:
                names[candidate] = route.label.lower()
        if not names:
            return text
        # One pass, with the replacement chosen by a function: a title is arbitrary user text and
        # must never be read as a regex template, and substituting one at a time would let an
        # earlier replacement's words be rewritten by a later candidate.
        return _IDENTIFIER.sub(lambda match: names.get(match.group(0), match.group(0)), text)
    except Exception:  # pragma: no cover - never let tidying break the answer
        logger.exception("Could not humanize a palette message")
        return text


#: Response keys that hold something a person is going to read.
_USER_FACING_KEYS = ("message", "summary", "note")


def humanize_response(response: dict[str, Any], user=None) -> dict[str, Any]:
    """Run every user-facing string in a response through :func:`humanize`. Mutates and returns it."""
    for key in _USER_FACING_KEYS:
        if isinstance(response.get(key), str):
            response[key] = humanize(response[key], user)
    options = response.get("options")
    if isinstance(options, list):
        response["options"] = [humanize(option, user) if isinstance(option, str) else option for option in options]
    return response


# --- usage logging -----------------------------------------------------------


def record_usage(
    user,
    result,
    query: str,
    response_kind: str,
    action_name: str = "",
    success: bool = True,
    destination: str = "",
) -> int | None:
    """Write one :class:`LLMUsage` row and return its id. Never allowed to break the request.

    The id matters for confirm-tier actions: it is what the client sends back if the user cancels
    the countdown, which is the only evidence we ever get that a command was understood as the
    wrong thing (see :func:`mark_cancelled`).
    """
    try:
        return LLMUsage.objects.create(
            destination=(destination or "")[:100],
            user=user,
            model=(result.model if result else "")[:100],
            prompt_tokens=result.prompt_tokens if result else 0,
            cached_prompt_tokens=result.cached_prompt_tokens if result else 0,
            completion_tokens=result.completion_tokens if result else 0,
            total_tokens=result.total_tokens if result else 0,
            query=(query or "")[:600],
            response_kind=response_kind[:30],
            action=(action_name or "")[:50],
            success=success,
        ).pk
    except Exception:
        logger.exception("Could not record LLM usage")
        return None


def mark_cancelled(user, usage_id: Any, *, request=None, action_name: str = "", params: Any = None) -> bool:
    """Record that the user cancelled this command's countdown. Returns whether a row was updated.

    Scoped to the caller's own rows, so one user can't mark up another's history. Cancelling is the
    single most useful signal this feature produces: the model was confident, the server was happy,
    the countdown ran -- and the person watching it said no. That is a bad match, and unlike an
    error nothing else records it.

    It also spends the shortened-countdown trust window for that action, when the client sent enough
    to identify it. Whatever the countdown was shortened on the strength of, this is the user
    disagreeing with it, and the next card is the one that needs the full five seconds back.
    """
    if request is not None and action_name:
        action = palette_actions.get_action(action_name)
        if action is not None and isinstance(params, dict):
            try:
                forget_trust(request, action, params)
            except Exception:
                logger.exception("Could not clear the palette trust window")
    try:
        usage_id = int(usage_id)
    except (TypeError, ValueError):
        return False
    try:
        return bool(LLMUsage.objects.filter(pk=usage_id, user=user).update(cancelled=True))
    except Exception:
        logger.exception("Could not record a palette cancellation")
        return False


def mark_reported(user, usage_id: Any) -> bool:
    """Record that the user told us a command didn't work. Returns whether a row was updated.

    ``FAIL_GAVE_UP`` and its siblings leave the user at a wall, and the exact query that produced
    the wall is already stored -- ``mine_palette_shortcuts`` mines the successes into free
    shortcuts and nothing has ever mined the failures into anything a person could read.

    Scoped to the caller's own rows, like :func:`mark_cancelled`, so nobody can flag anybody else's
    history. Nothing is emailed and nothing is escalated: this sets a flag the analytics page sorts
    on, which is the honest version of "tell the site owner" for a one-tap button.
    """
    try:
        usage_id = int(usage_id)
    except (TypeError, ValueError):
        return False
    try:
        return bool(LLMUsage.objects.filter(pk=usage_id, user=user).update(reported=True))
    except Exception:
        logger.exception("Could not record a palette failure report")
        return False


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
    (("what ", "how ", "when does", "when is", "why ", "rules", "points", "am i allowed"), "Looking that up…"),
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
    "describe_auction": "Reading the auction's details…",
    "describe_club": "Reading the club's details…",
    "describe_lot": "Reading up on lot {target}…",
    "describe_person": "Looking up {target}…",
}


def narrate_lookup(action, params: dict[str, Any]) -> str:
    """Describe a lookup round: "Searching for bob…"."""
    template = _LOOKUP_NARRATION.get(action.name, "Looking that up…")
    target = ""
    for key in ("name", "query", "lot", "page", "person", "auction", "club"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            target = value.strip()[:60]
            break
    if "{target}" in template:
        return template.format(target=f"“{target}”") if target else "Searching…"
    return template


def narrate_action(request, action, params: dict[str, Any]) -> str:
    """Describe the action round: what we're about to do, and what to, before we do it.

    Naming the object is the point. "Adding a lot…" and "Adding a lot to the May 2025 auction…" cost
    the same to show, and only one of them lets the user notice we picked the wrong auction while
    there is still time to stop it.
    """
    context = palette_actions.action_context(request, action, params)
    if action.name == "go_to_page":
        route = palette_routes.get_route(str(params.get("page") or ""))
        if route:
            where = f" for {context}" if context else ""
            return f"Opening {route.label.lower()}{where}…"
        return "Finding that page…"
    if action.confirm_template:
        where = f" in {context}" if context else ""
        return f"{action.confirm_template}{where}…"
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
            # Navigations carry their subject forward too. "Take me to the fall auction" followed by
            # "add a lot" has to mean the fall auction, and before this the second sentence lost the
            # first one's answer entirely -- only ``done`` responses were remembered.
            "data": _carry_over(result),
            # Not sent to the client; recorded, so the miner can see where this query landed.
            "route": result.get("route", ""),
        }
    return {
        "kind": KIND_DONE,
        "message": result.get("summary") or summary or "Done.",
        "followups": result.get("followups", []),
        "action": action.name,
        # Carried into the next command's context, so "print that label" knows which lot.
        "data": _carry_over(result),
    }


#: Values a resolver may hand forward. Must stay a subset of what ``sanitize_context`` will accept
#: back from the client, or the next command silently loses them.
_CARRY_OVER_KEYS = ("lot_id", "lot_name", "bidder_number", "auction", "club")


def _carry_over(result: dict[str, Any]) -> dict[str, Any]:
    """The few values worth remembering for the next command ("print *that* label").

    ``bidder_number`` is here so the exchange that follows adding somebody -- "his email is
    bob@example.com" -- knows who "he" is without another lookup.

    ``auction`` and ``club`` are here for the same reason one sentence further on. With only the lot
    and the bidder, "now make it twenty dollars" worked and "and add another for the same bidder in
    the other auction" did not -- the second sentence of a conversation lost the subject of the
    first, and defaulted back to whatever auction the user last touched, which is the one thing it
    definitely did not mean.
    """
    data = {}
    for key in _CARRY_OVER_KEYS:
        if result.get(key) is not None:
            data[key] = result[key]
    return data


def _trust_key(user, action, context: str) -> str:
    """Cache key for "this user has already approved this action, on this thing, recently"."""
    subject = re.sub(r"[^a-z0-9]+", "-", (context or "").lower())[:60]
    return f"palette_trust_{getattr(user, 'pk', 0)}_{action.name}_{subject}"


def remember_trust(request, action, params: dict[str, Any]) -> None:
    """Record that a confirm-tier action ran to completion, so the next identical one counts down less.

    Called only from :func:`execute`, and only on success: an action that errored was not approved
    by anybody, it merely failed, and treating that as consent would shorten the countdown on the
    one card most likely to need reading.
    """
    if not palette_actions.administers_anything(request.user):
        return
    context = palette_actions.action_context(request, action, params)
    cache.set(_trust_key(request.user, action, context), 1, timeout=TRUST_WINDOW_SECONDS)


def forget_trust(request, action, params: dict[str, Any]) -> None:
    """Spend the trust window. Called when the user cancels: a cancel is "you got that wrong"."""
    context = palette_actions.action_context(request, action, params)
    cache.delete(_trust_key(request.user, action, context))


def _countdown_ms(request, action, params: dict[str, Any], context: str) -> int:
    """How long to count down before this particular write. See :data:`TRUSTED_COUNTDOWN_MS`."""
    if not palette_actions.administers_anything(request.user):
        return COUNTDOWN_MS
    if cache.get(_trust_key(request.user, action, context)):
        return TRUSTED_COUNTDOWN_MS
    return COUNTDOWN_MS


def _countdown_response(request, action, params: dict[str, Any], summary: str, usage_id=None) -> dict[str, Any]:
    """The card shown in the seconds before a database change.

    ``context`` is worked out by the server rather than taken from the model's summary: the summary
    is the model telling us what it thinks it's doing, and this is the auction the resolver will
    actually write to. When they disagree, the user should be looking at the second one.
    """
    context = palette_actions.action_context(request, action, params)
    return {
        "kind": KIND_COUNTDOWN,
        "action": action.name,
        "params": params,
        "summary": summary or palette_actions.default_summary(action, params),
        "context": context,
        "delay_ms": _countdown_ms(request, action, params, context),
        # Sent back if the user hits Cancel, so a bad match can be traced to the query that caused it.
        "usage_id": usage_id,
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


def _give_up(request, query: str, message: str, usage_id=None) -> dict[str, Any]:
    """The end of the line, in the order that helps most.

    Search results first (they show what we understood), then our own best-guess page, then --
    only if the query matched nothing anywhere on the site -- an actual error.

    Every one of those outcomes carries ``usage_id``, which is what puts a "that didn't work" button
    under it. This is the feature's worst moment and the only one where the person in front of it
    knows something the logs don't: whether search results they didn't ask for were any use. Without
    a way to say so, a failure is a wall for them and a row in a table nobody sorts for us.
    """
    fallback = _search_fallback(request, query, "I wasn't sure what you meant. Here's what I found:")
    if fallback:
        return {**fallback, "usage_id": usage_id}
    guess = _best_guess_page(request, query)
    if guess:
        return {**humanize_response(guess, request.user), "usage_id": usage_id}
    return {"kind": KIND_ERROR, "message": humanize(message, request.user), "usage_id": usage_id}


def lookup_payload(name: str, result: Any) -> str:
    """One lookup's result, as the message the model reads next.

    Truncation used to be a bare slice, which is the worst possible way to lose data here: the cut
    landed mid-JSON, the model was handed a broken object, and nothing anywhere said so. That is how
    "what's the split" came to be answered with a fee percentage nobody had ever configured -- the
    settings were past the cut. So when it does have to cut, it says it cut, in the message itself
    and in the log, and the model is told to send the user to the page rather than fill in the gap.

    ``auctions/test_palette_assist.py`` asserts the describe_* payloads fit without truncation. This
    is the backstop for the day a new field pushes one of them over anyway.
    """
    body = json.dumps(palette_actions.strip_internal(result), default=str)
    if len(body) <= MAX_LOOKUP_RESULT_CHARS:
        return f"Result of {name}: {body}"
    logger.warning("Lookup %s returned %s chars and was truncated to %s", name, len(body), MAX_LOOKUP_RESULT_CHARS)
    return (
        f"Result of {name} (TRUNCATED — this is not the whole result, and the end of it is missing): "
        f"{body[:MAX_LOOKUP_RESULT_CHARS]}\n"
        "Do not fill in anything the truncated result does not show. If the user asked about "
        "something that isn't in it, say you can't see it here and send them to the relevant page."
    )


def _rounds_allowed(lookups_run: set) -> int:
    """How many model calls this request may still make.

    :data:`MAX_ROUNDS` normally, and :data:`MAX_ROUNDS_AFTER_LOOKUP` once a lookup has actually run.
    A flat cap of two spent whole requests like this: an off-contract first reply, a correction that
    produced a perfectly good lookup, and then the loop ended -- having paid for two model calls and
    a database read to find the answer, and then thrown the answer away without saying it. The extra
    round is only ever granted to a request that has already fetched something real to answer with,
    which is the case where a further round is worth what it costs.
    """
    return MAX_ROUNDS_AFTER_LOOKUP if lookups_run else MAX_ROUNDS


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

    # A curated shortcut answers before anything else, including the enabled check: it is the same
    # answer the model gave the last N times this exact phrase was typed, for none of the cost.
    groups = shortcut_match(request, query)
    if groups is not None:
        yield {"kind": KIND_RESULTS, "groups": groups}
        return

    # No provider configured, or this user hasn't opted in: the palette must behave exactly as it
    # did before this feature existed.
    if not assist_enabled_for(user):
        yield {"kind": KIND_RESULTS, "groups": command_palette.search(request, query)}
        return

    groups = obvious_match(request, query)
    if groups is not None:
        yield {"kind": KIND_RESULTS, "groups": groups}
        return

    yield _progress(opening_line(query))

    entries = sanitize_context(context)
    provider = get_provider()
    system = build_system_prompt(user, request.palette_page, command_palette.app_destinations_for_prompt(request))
    # The same catalogue ``/mcp/`` serves, plus the palette's own two. Built once per request: it
    # costs two queries (see ``palette_actions.actions_for``) and does not change mid-loop.
    tools = tools_for(user)
    messages = build_messages(query, entries)
    started = time.monotonic()
    nudges = 0
    lookups_run: set[tuple[str, str]] = set()
    # A phrase this site keeps answering out of one lookup gets that lookup run up front, so the
    # request costs one round instead of two. Recorded in ``lookups_run`` as though the model had
    # asked for it, which is what stops it asking again and what earns the extra round if it does.
    preloaded = _preload_messages(request, query, messages)
    if preloaded:
        lookups_run.add((preloaded, json.dumps({}, sort_keys=True)))

    round_number = 0
    while round_number < _rounds_allowed(lookups_run):
        if time.monotonic() - started > TOTAL_BUDGET_SECONDS:
            logger.info("Assist budget exhausted after %s rounds", round_number)
            break
        round_number += 1
        # Counted per model call, not per request: one request can take several rounds, and the
        # cap is a spend ceiling on tokens rather than a limit on how often the box is used.
        over_budget = check_call_budget(user)
        if over_budget:
            record_usage(user, None, query, FAIL_THROTTLED, success=False)
            log_assist(user, query, KIND_ERROR)
            yield {"kind": KIND_ERROR, "message": over_budget}
            return
        try:
            result = provider.complete(system, messages, tools)
        except LLMError as error:
            logger.warning("Assist provider error: %s", error)
            usage_id = record_usage(user, None, query, FAIL_PROVIDER, success=False)
            log_assist(user, query, KIND_ERROR)
            # The provider being down says nothing about the query, so search is still worth a go.
            yield _give_up(request, query, "I couldn't reach the assistant just now.", usage_id)
            return

        reply = read_reply(result, user)
        kind = reply["kind"]

        if kind == "invalid":
            # Rare now, and no longer worth a correction round: the provider enforced the tool
            # schemas, so getting here means the endpoint behind ``LLM_BASE_URL`` doesn't, or the
            # model said nothing at all. Neither is fixed by asking again -- the whole reason the
            # old retry existed was replies that were the right idea in the wrong shape, and those
            # can't happen any more. Fall through to the search ladder, which is faster and more
            # use to the person waiting.
            record_usage(user, result, query, FAIL_INVALID, success=False)
            logger.info("Assist got an unusable reply: %s", reply.get("reason"))
            break

        if kind == "lookup":
            action = reply["action"]
            # The same lookup, with the same parameters, asked for twice. The answer will not have
            # changed, and in practice the model then asks a third and fourth time and runs the
            # request out of rounds -- four full calls to learn one thing that wasn't there.
            signature = (action.name, json.dumps(reply["params"], sort_keys=True, default=str))
            if signature in lookups_run:
                record_usage(user, result, query, "lookup", action.name)
                if nudges >= MAX_REPEAT_NUDGES:
                    logger.info("Assist repeated the %s lookup; stopping", action.name)
                    break
                # Asking for the same thing twice usually means the model has lost track of the fact
                # that it already has it, not that it needs it again. Stopping here threw away a
                # result already fetched and already in the conversation -- "what's the split" came
                # back as "I'm not sure what you meant" while the fee settings sat two messages up.
                # Say so instead, once, and let it answer. The lookup is *not* re-run: this costs a
                # model call and no database work, and the round budget still caps the whole request.
                nudges += 1
                logger.info("Assist repeated the %s lookup; nudging it to answer", action.name)
                # The call still has to be answered -- a tool call with no matching result turn is
                # rejected by the API rather than ignored -- so the nudge *is* the result.
                messages.append(llm.tool_call_message([result.tool_calls[0]]))
                messages.append(
                    llm.tool_result_message(
                        result.tool_calls[0],
                        f"You already called {action.name} with those parameters and its result is "
                        "earlier in this conversation. Do not call it again. Answer the user now "
                        "using that result, or choose an action.",
                    )
                )
                continue
            lookups_run.add(signature)
            yield _progress(narrate_lookup(action, reply["params"]))
            record_usage(user, result, query, "lookup", action.name)
            lookup_result = palette_actions.run_action(request, action.name, reply["params"])
            messages.append(llm.tool_call_message([result.tool_calls[0]]))
            messages.append(llm.tool_result_message(result.tool_calls[0], lookup_payload(action.name, lookup_result)))
            continue

        if kind == "answer":
            # An answer built from exactly one parameterless lookup is a routing decision worth
            # remembering: the next person to type this phrase can have that lookup run for them up
            # front (see :func:`preloadable_lookup`). Anything more complicated than that -- two
            # lookups, or one with an argument -- is left unrecorded, so it never becomes a preload.
            record_usage(user, result, query, KIND_ANSWER, destination=_answered_from(lookups_run))
            log_assist(user, query, KIND_ANSWER)
            # Search results under the answer: the answer came out of a lookup, and the things it
            # is about are usually one click away in ordinary search.
            related = _search_fallback(request, query, "")
            yield humanize_response(
                {
                    "kind": KIND_ANSWER,
                    "message": reply["message"],
                    "groups": related["groups"] if related else [],
                },
                user,
            )
            return

        if kind == "clarify":
            record_usage(user, result, query, KIND_CLARIFY)
            log_assist(user, query, KIND_CLARIFY)
            response = {"kind": KIND_CLARIFY, "message": reply["message"], "options": reply["options"]}
            if not reply["options"]:
                # A question with nothing to click is a dead end for anyone using this by voice, and
                # a nuisance for everyone else. If the model didn't offer choices, offer the search
                # results instead so there is always a way forward from the question.
                fallback = _search_fallback(request, query, "")
                if fallback:
                    response["groups"] = fallback["groups"]
            yield humanize_response(response, user)
            return

        if kind == "error":
            # The model has told us it can't do this. It is still better to show the user something
            # than to show them a refusal, so this goes through the same ladder as running out.
            usage_id = record_usage(user, result, query, FAIL_MODEL_ERROR, success=False)
            log_assist(user, query, KIND_ERROR)
            yield _give_up(request, query, reply["message"], usage_id)
            return

        # kind == "action"
        action = reply["action"]
        params = reply["params"]
        summary = reply["summary"]
        yield _progress(narrate_action(request, action, params))

        if action.danger == palette_actions.DANGER_CONFIRM and action.asks_first:
            # Do NOT execute. The client counts down and then calls the execute endpoint, which
            # runs the resolver (and therefore every permission check) from scratch.
            #
            # ``asks_first=False`` skips the card and runs the write here instead. It is still a
            # write and still goes through ``run_action``, which is the gate; what it loses is the
            # countdown, on the handful of actions that are non-destructive, idempotent and undone
            # by a tool the assistant already has. See ``palette_actions.Action.asks_first``.
            usage_id = record_usage(user, result, query, KIND_COUNTDOWN, action.name)
            log_assist(user, query, KIND_COUNTDOWN)
            yield humanize_response(_countdown_response(request, action, params, summary, usage_id), user)
            return

        action_result = palette_actions.run_action(request, action.name, params)
        response = _result_to_response(action, params, action_result, summary)
        if response["kind"] != KIND_ERROR:
            # The write that skipped the countdown still has to reach the undo stack. Without this
            # "check bob in" then "undo that" did not fail -- it silently undid whatever the person
            # had done *before* that, which is worse than refusing. A no-op unless the resolver
            # said how to reverse itself, so it costs nothing on a safe action. The execute
            # endpoint does the same for every write that does count down.
            palette_actions.remember_undo(request.user, action.name, action_result)
        if response["kind"] == KIND_ERROR:
            # The action was understood but couldn't run (wrong auction, no permission, closed for
            # submissions). Keep the real reason -- it's specific and useful -- but offer the
            # search results underneath it rather than ending on a wall.
            usage_id = record_usage(user, result, query, KIND_ERROR, action.name, success=False)
            log_assist(user, query, KIND_ERROR)
            yield {**humanize_response(response, user), "usage_id": usage_id}
            return
        record_usage(user, result, query, response["kind"], action.name, destination=response.get("route", ""))
        log_assist(user, query, response["kind"])
        yield humanize_response(response, user)
        return

    usage_id = record_usage(user, None, query, FAIL_GAVE_UP, success=False)
    log_assist(user, query, KIND_ERROR)
    yield _give_up(request, query, "I couldn't work out how to do that.", usage_id)


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
    # Re-checked here and not just in ``assist_stream``: a countdown started before the preference
    # was turned off (or before the key was pulled) must not still be able to run the action.
    if not assist_enabled_for(request.user):
        return {"kind": KIND_ERROR, "message": "I don't know how to do that."}
    request.palette_page = palette_routes.page_context_from_path(request.user, path) if path else {}
    action = palette_actions.get_action(name)
    if action is None:
        return {"kind": KIND_ERROR, "message": "I don't know how to do that."}
    if action.danger != palette_actions.DANGER_CONFIRM:
        # Safe actions already ran during assist; navigate actions are the client's job.
        return {"kind": KIND_ERROR, "message": "That isn't something to confirm."}
    # ``asks_first=False`` is deliberately *not* refused here. Nothing offers a countdown for one
    # any more, so this is only ever reached by a page that was open when the flag changed -- and
    # refusing a write the server is perfectly willing to do, because it would rather not have been
    # asked twice, is a worse answer than doing it. Everything below re-checks it from scratch.
    if not isinstance(params, dict):
        return {"kind": KIND_ERROR, "message": "Those instructions didn't make sense."}
    result = palette_actions.run_action(request, action.name, params)
    response = _result_to_response(action, params, result, "")
    if response["kind"] != KIND_ERROR:
        # Approved and completed. The next identical card gets a shorter countdown for a while.
        remember_trust(request, action, params)
        # ...and, when the resolver said how to reverse itself, "undo that" now has something to
        # reach for. Only from here: an action that never ran is not something to offer to undo.
        palette_actions.remember_undo(request.user, action.name, result)
    return humanize_response(response, request.user)
