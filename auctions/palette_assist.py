"""Natural-language orchestration for the command palette.

``assist(request, query, context)``:

1. A short query with an obvious search match never reaches the model.
2. Otherwise a bounded tool-calling loop (:data:`MAX_ROUNDS`, :data:`TOTAL_BUDGET_SECONDS`) over the
   same catalogue ``/mcp/`` serves (:func:`auctions.mcp.tools.tool_descriptors`).

**Every turn ends in something the user can touch**: a link, a countdown card, a question with
clickable options, or one or two sentences with the things they name linked underneath
(:func:`about_groups`). A bare paragraph is not one of those, which is why the model is called with
``tool_choice="required"``, and an answer is a *read's own summary*: the model picks which read holds
it (:func:`answers_on_its_own`) and the resolver supplies the sentence. It once told somebody "I've
updated the email on your account" about a write that never ran.

The provider enforces tool schemas; ``run_action`` and the resolvers enforce everything else.

  ``safe``     -> executed here, returned as ``done``
  ``confirm``  -> returned as ``countdown``, **not executed**; the execute endpoint re-runs it
  ``navigate`` -> returned as a URL

``assist_stream`` yields ``progress`` events describing real server steps; ``assist()`` drops them.
Failure never dead-ends (:func:`_give_up`), and :func:`humanize` strips slugs and route keys from
anything a user reads.

Response kinds: ``progress`` (streaming) | ``results`` | ``navigate`` | ``countdown`` | ``clarify`` |
``answer`` | ``done`` | ``error``
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from typing import Any

from django.core.cache import cache
from django.db.models import F, Q
from django.urls import reverse

from . import command_palette, llm, palette_actions, palette_routes
from .llm import LLMError, assist_enabled, get_provider
from .models import CommandPalettePage, CommandPaletteSearch, LLMUsage

logger = logging.getLogger(__name__)

# Agent loop bounds. Every round resends the ~3k-token prompt. Measured: usable answers took one
# round, occasionally two; none needed three.
MAX_ROUNDS = 2
#: The ceiling once a lookup has run: it has earned the rounds needed to say what it found. Raised
#: from 3 when the registry cut halved the prompt -- "who won lot 12" spent two rounds looking and
#: then ran out before it could say what it had found.
MAX_ROUNDS_AFTER_LOOKUP = 4
#: How many times the model is worth telling that it already has what it just asked for again.
MAX_REPEAT_NUDGES = 1
TOTAL_BUDGET_SECONDS = 20.0

# Recent exchanges kept for context ("print that label" -> the lot we just added).
MAX_CONTEXT_ENTRIES = 5

# How much of a lookup's result is fed back to the model. The largest (``describe_auction`` with
# long rules) fits with a few hundred characters to spare; :func:`lookup_payload` logs when that runs
# out. Raised from 5000 when every read gained a ``summary`` — the sentence the user is shown, which
# the model also sees so it can tell whether that read answered the question.
MAX_LOOKUP_RESULT_CHARS = 5600

#: The palette's own three tools, absent from ``/mcp/`` where hosts ask, answer and fail for themselves.
ASK_THE_USER = "ask_the_user"
CANNOT_DO_THIS = "cannot_do_this"
#: Reads that exist to feed another tool rather than to answer: they turn a name into a bidder
#: number or a lot number. Every other read, having produced a summary, has answered the question,
#: and :func:`answers_on_its_own` stops the loop there.
STEP_LOOKUPS = frozenset({"find_person", "find_lot", "find_page", "my_context"})

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


def navigate_only(user) -> bool:
    """Whether this user's palette opens pages and never writes.

    Theirs to choose on the preferences page, and ``ASSISTANT_NAVIGATE_ONLY`` turns it on for
    everybody — the kill switch for a write that misfires in the middle of somebody's auction.
    """
    from django.conf import settings

    if getattr(settings, "ASSISTANT_NAVIGATE_ONLY", False):
        return True
    userdata = getattr(user, "userdata", None)
    return bool(userdata and userdata.palette_navigate_only)


def tools_for(user, query: str = "") -> list[dict[str, Any]]:
    """Every tool this user's palette may call: the shared catalogue, plus the three above.

    ``Action.mcp_only`` actions are dropped: ``read_source`` and ``club_api`` return pages of text,
    and the rest are writes. ``go_to_page`` still reaches every one of their pages.

    A *query* asks for the tiered list. The writes come last and are dropped for a question, so the
    shorter list is a byte-exact **prefix** of the longer one and both share one cached prompt —
    tiering by suffix rather than by set. No query means the whole catalogue.
    """
    from .mcp import tools as mcp_tools

    shared = [
        tool
        for tool in mcp_tools.tool_descriptors(user)
        if not getattr(palette_actions.get_action(tool["name"]), "mcp_only", False)
    ]
    reads = [tool for tool in shared if not _is_a_write(tool)]
    if navigate_only(user):
        # Reads still answer a question; nothing here can change anything.
        return [*reads, *PALETTE_TOOLS]
    if query and asks_for_something_removed(query) and not asks_a_question(query):
        # They asked for something this box no longer does. The page still does it, so leave the
        # tools that reach a page and the ones that aim it at the right lot or person — and nothing
        # that could write. Told to refund a lot with no way to, it reached for no_sale.
        reachable = [tool for tool in reads if tool["name"] in STEP_LOOKUPS or _is_navigation(tool)]
        return [*reachable, *PALETTE_TOOLS]
    if not query or wants_the_writes(query):
        writes = [tool for tool in shared if _is_a_write(tool)]
        return [*reads, *PALETTE_TOOLS, *writes]
    return [*reads, *PALETTE_TOOLS]


def _is_a_write(tool: dict[str, Any]) -> bool:
    action = palette_actions.get_action(tool["name"])
    return bool(action and action.danger == palette_actions.DANGER_CONFIRM)


def _is_navigation(tool: dict[str, Any]) -> bool:
    action = palette_actions.get_action(tool["name"])
    return bool(action and action.danger == palette_actions.DANGER_NAVIGATE)


#: Question words that, with nothing from :func:`_write_vocabulary` in the query, mean nobody is
#: asking for anything to be changed.
_QUESTION_WORDS = frozenset("what when where which who whom whose why how".split())

_write_vocabulary_cache: frozenset[str] | None = None
_removed_vocabulary_cache: frozenset[str] | None = None


#: Words this whole site is about, so a query containing one has said nothing about wanting a write.
#: Without these, "when does the fall auction start?" kept every write tool, because two of them have
#: the word auction in their name.
_TOO_GENERAL = frozenset("auction auctions lot lots club clubs member members person people user users".split())


def _write_vocabulary() -> frozenset[str]:
    """The words the registry's own writes are named and confirmed by.

    Built from the registry, so a new write brings its own trigger words and nothing here is kept in
    step by hand. Names and confirm lines only: the *examples* are full spoken sentences, and their
    incidental nouns ("the fall auction", "blue shrimp", "bob") matched everything.
    """
    global _write_vocabulary_cache
    if _write_vocabulary_cache is None:
        words: set[str] = set()
        for action in palette_actions.ACTIONS.values():
            if action.mcp_only or action.danger != palette_actions.DANGER_CONFIRM:
                continue
            text = f"{action.name.replace('_', ' ')} {action.confirm_template}"
            words.update(normalize_query(text).split())
        _write_vocabulary_cache = frozenset(words - _FILLER - _TOO_GENERAL)
    return _write_vocabulary_cache


def _removed_vocabulary() -> frozenset[str]:
    """Words that name a capability the palette gave up: "refund", "award points", "place a bid".

    Built from :data:`palette_actions.MCP_ONLY_SKILLS`, minus every word the surviving writes are
    named by, so an overlap like "add" or "undo" never fires. Nothing is listed by hand and nothing
    is listed in the prompt — the model is never told what it can't do, which is an endless list.
    """
    global _removed_vocabulary_cache
    if _removed_vocabulary_cache is None:
        words: set[str] = set()
        for name in palette_actions.MCP_ONLY_SKILLS:
            action = palette_actions.get_action(name)
            if action is None or action.lookup:
                continue
            words.update(normalize_query(f"{action.name.replace('_', ' ')} {action.confirm_template}").split())
        _removed_vocabulary_cache = frozenset(words - _FILLER - _TOO_GENERAL - _write_vocabulary())
    return _removed_vocabulary_cache


def asks_for_a_write(query: str) -> bool:
    """Whether the query actually names something the palette can do: "add", "check in", "renew"."""
    return bool(set(normalize_query(query).split()) & _write_vocabulary())


#: Verbs both halves of the registry are described by, so hitting one says nothing about which half
#: was meant. "change my email" names a capability that left and a verb that stayed.
_SHARED_VERBS = frozenset("add set change update make take give send put remove delete create edit new".split())


def asks_for_something_removed(query: str) -> bool:
    """Whether the query names a capability that is on ``/mcp/`` and not here, and nothing that is.

    "refund lot 14" became a countdown for ``no_sale`` and "give bob 10 points for the corydoras"
    became a $10 charge on his invoice: told to do something it no longer has a tool for, the model
    reaches for the nearest one it does have. Taking the writes away for that turn leaves it the page.

    A word from a skill that stayed calls it off — unless that word is only a shared verb, which is
    how "change my email" was reading as a skill the palette still has.
    """
    words = set(normalize_query(query).split())
    if not words & _removed_vocabulary():
        return False
    return not (words & _write_vocabulary() - _SHARED_VERBS)


def wants_the_writes(query: str) -> bool:
    """Whether this query is worth sending the write tools with. Generous on purpose: a question with
    no write word in it loses them, and everything else keeps them.
    """
    words = set(normalize_query(query).split())
    if not words:
        return True
    if asks_for_a_write(query):
        return True
    return not (words & _QUESTION_WORDS or query.strip().endswith("?"))


# A query this short that already has a good match is answered by search alone.
SHORT_QUERY_WORDS = 4

# How long the client counts down before a confirm-tier action runs.
COUNTDOWN_MS = 5000

# Countdown once an admin has let this exact action, on this subject, run in the last ten minutes.
# Shortened, never skipped; cancelling spends the trust (:func:`forget_trust`).
TRUSTED_COUNTDOWN_MS = 1500
TRUST_WINDOW_SECONDS = 600

MAX_QUERY_LENGTH = 600

# Throttling: 1/second is the anti-bot floor, the window caps are the spend ceiling.
COOLDOWN_SECONDS = 1
COOLDOWN_MESSAGE = "One at a time — try that again in a second."
WINDOW_SECONDS = 300
#: Commands per window. Counted per *request*, because that is what a person does; counting model
#: calls meant one unlucky four-round query spent an eighth of somebody's afternoon allowance.
WINDOW_MAX_REQUESTS = 20
#: And a backstop on the rounds those requests are allowed to cost between them.
WINDOW_MAX_CALLS = 60
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

# ``LLMUsage.response_kind`` for failures, split so the analytics page can tell a refusal from an
# outage.
FAIL_GAVE_UP = "gave_up"  # the loop ran out of rounds without reaching an answer
FAIL_MODEL_ERROR = "model_error"  # the model said it couldn't do this
FAIL_PROVIDER = "provider_error"  # we couldn't reach the provider at all
FAIL_INVALID = "invalid_shape"  # the model replied with something off-contract
FAIL_THROTTLED = "throttled"  # the user hit the spend ceiling
FAIL_BUSY = "provider_busy"  # the whole site is over the provider's limit, not an outage
#: Recorded when we couldn't act but ordinary search had something worth showing.
KIND_FALLBACK = "fallback"

#: Every failure kind, for the analytics page's "queries we couldn't answer" list.
FAILURE_KINDS = (FAIL_GAVE_UP, FAIL_MODEL_ERROR, FAIL_INVALID, KIND_FALLBACK)

#: ``LLMUsage.destination`` prefix for an answer from one lookup. See :func:`preloadable_lookup`.
LOOKUP_DESTINATION_PREFIX = "lookup:"

#: Preload a lookup after this many unanimous answers from it; one disagreement sends the phrase
#: back to the model.
PRELOAD_MIN_COUNT = 5
PRELOAD_CACHE_SECONDS = 3600


# --- who gets this at all -----------------------------------------------------


def assist_enabled_for(user) -> bool:
    """True when this user should be offered natural-language/voice commands.

    Needs a configured model and ``UserData.use_llm_search``. That preference is an admin lever (not on
    the preferences page). Everything user-facing asks this, not ``assist_enabled()``.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    # A missing reverse one-to-one raises an AttributeError, which getattr turns into None.
    userdata = getattr(user, "userdata", None)
    if not userdata or not userdata.use_llm_search:
        return False
    return assist_enabled()


# --- throttling --------------------------------------------------------------


def check_cooldown(user) -> str | None:
    """Roughly one request per second per user; ``cache.add`` is atomic. Returns a message when throttled."""
    key = f"palette_assist_cooldown_{user.pk}"
    if cache.add(key, 1, timeout=COOLDOWN_SECONDS):
        return None
    return COOLDOWN_MESSAGE


def _bump(key: str) -> int:
    """One more in this window, returning the running count."""
    cache.add(key, 0, timeout=WINDOW_SECONDS)
    try:
        return cache.incr(key)
    except ValueError:
        # The key expired between add and incr; treat this as the first of a new window.
        cache.set(key, 1, timeout=WINDOW_SECONDS)
        return 1


def check_request_budget(user) -> str | None:
    """Enforce the cap on commands. One per thing the user typed. Returns a message when over."""
    if _bump(f"palette_assist_requests_{user.pk}") > WINDOW_MAX_REQUESTS:
        return WINDOW_MESSAGE
    return None


def check_call_budget(user) -> str | None:
    """Enforce the backstop on model calls, which one request may cost several of."""
    if _bump(f"palette_assist_calls_{user.pk}") > WINDOW_MAX_CALLS:
        return WINDOW_MESSAGE
    return None


# --- how busy the whole site is ----------------------------------------------
#
# The per-user limits below stop one person running away with it. They do nothing about ten people
# each within their own limit, and the ceiling that binds first is the provider's: at roughly 8.6k
# tokens a call against a 200k-tokens-per-minute account, the whole site gets about 23 calls a minute.
# Past that every user gets "I couldn't reach the assistant just now" at once.
#
# So: everyone gets slower before anyone gets refused, and the waiting is on screen.


#: Tokens a minute this site will spend before it starts making people wait. Under the provider's own
#: limit on purpose -- the point is to never reach theirs.
def _tokens_per_minute() -> int:
    from django.conf import settings

    return int(getattr(settings, "LLM_TOKENS_PER_MINUTE", 0) or 150_000)


#: Load at which the waiting starts. Below this nobody notices anything.
BUSY_THRESHOLD = 0.6
#: The longest anybody is made to wait before their command is given up on. Two model calls' worth of
#: patience; past it, ordinary search is a better answer than a spinner.
MAX_WAIT_SECONDS = 8.0
#: Consecutive provider failures before the model is left alone for a while. An outage answers every
#: caller with a ten second timeout otherwise.
BREAKER_FAILURES = 4
BREAKER_COOLDOWN_SECONDS = 60

_TOKENS_KEY = "palette_tokens_spent_"
_BREAKER_KEY = "palette_provider_failures"


def _minute_key(now: float | None = None) -> str:
    return f"{_TOKENS_KEY}{int((now or time.time()) // 60)}"


def spend_tokens(count: int) -> None:
    """Record what a call cost, for the minute it landed in. Best-effort; a lost count only under-counts."""
    if count <= 0:
        return
    key = _minute_key()
    try:
        cache.add(key, 0, timeout=120)
        cache.incr(key, count)
    except (ValueError, Exception):  # noqa: B014 - the key can expire between add and incr
        logger.debug("Could not record palette token spend")


def site_load() -> float:
    """Tokens spent this minute as a fraction of what this site allows itself. 0 when idle."""
    try:
        spent = cache.get(_minute_key()) or 0
    except Exception:
        return 0.0
    return float(spent) / float(_tokens_per_minute())


def wait_for_the_queue(load: float) -> float:
    """How long to hold this request, in seconds, at that load.

    Nothing until :data:`BUSY_THRESHOLD`, then a ramp: the busier the site the longer everybody waits,
    which is the whole mechanism. Capped, because past :data:`MAX_WAIT_SECONDS` search is the better
    answer and the person can see it now rather than the right answer in a minute.
    """
    if load < BUSY_THRESHOLD:
        return 0.0
    over = (load - BUSY_THRESHOLD) / (1.0 - BUSY_THRESHOLD)
    return min(MAX_WAIT_SECONDS, round(over * MAX_WAIT_SECONDS, 1))


def provider_is_resting() -> bool:
    """Whether consecutive failures have taken the model out of service for a moment."""
    try:
        return int(cache.get(_BREAKER_KEY) or 0) >= BREAKER_FAILURES
    except Exception:
        return False


def note_provider_failure() -> None:
    try:
        cache.add(_BREAKER_KEY, 0, timeout=BREAKER_COOLDOWN_SECONDS)
        cache.incr(_BREAKER_KEY)
    except Exception:
        logger.debug("Could not record a provider failure")


def note_provider_success() -> None:
    try:
        cache.delete(_BREAKER_KEY)
    except Exception:
        logger.debug("Could not clear the provider failure count")


# --- input sanitising --------------------------------------------------------


def sanitize_context(raw: Any) -> list[dict[str, Any]]:
    """Validate and truncate the client-supplied (sessionStorage) recent-exchange list."""
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
    """Lowercase, depunctuated, single-spaced. Shared with ``mine_palette_shortcuts`` so phrases match."""
    return " ".join(re.findall(r"[a-z0-9']+", (query or "").lower()))


def shortcut_match(request, query: str) -> list[dict[str, Any]] | None:
    """Answer from a curated shortcut when the normalized query matches exactly. Returns groups or ``None``.

    Exact on purpose: local fuzzy scoring agreed with the model on well under half of real commands.
    """
    normalized = normalize_query(query)
    if not normalized:
        return None
    for page in CommandPalettePage.objects.filter(is_active=True):
        if normalized not in {normalize_query(phrase) for phrase in command_palette._page_phrases(page)}:
            continue
        # resolve_page returns nothing for a page this user can't open; fall through to the model.
        items = command_palette.resolve_page(page, request.user)
        if items:
            CommandPalettePage.objects.filter(pk=page.pk).update(hits=F("hits") + 1)
            return [{"label": "Go to", "items": items}]
    return None


#: Ways of saying "go there". Only the verb is stripped: "my" and "the" are left on the front of the
#: destination, because "my invoices" finds one page and "invoices" finds two and picks neither.
_NAVIGATION_OPENERS = (
    "take me to",
    "where do i find",
    "where do i see",
    "where do i go for",
    "where are",
    "where is",
    "jump to",
    "show me",
    "go to",
    "open",
)

#: How far ahead of the runner-up a route has to score to be followed without asking the model.
NAVIGATION_CONFIDENCE = 2.0


def navigation_shortcut(request, query: str):
    """A navigation the route catalog is sure about, answered without a model call.

    "take me to my invoices" is the plainest thing anybody types into this box, and it was costing a
    round trip and sometimes landing on a read instead: ``my_activity`` and ``go_to_page`` compete for
    the same phrasing, and the model has to pick a key out of four hundred. Here the sentence names
    its own destination, so the matcher does it — free, instant, and it cannot pick a write.

    Only when one route is clearly ahead of the next; anything closer is left to the model.
    """
    lowered = query.lower().strip()
    for opener in _NAVIGATION_OPENERS:
        if lowered.startswith(opener + " "):
            target = query.strip()[len(opener) :].strip(" ?.")
            break
    else:
        return None
    if not target:
        return None
    scored = palette_routes.match_routes_with_scores(target, request.user, limit=2)
    if not scored:
        return None
    (route, best), runner_up = scored[0], (scored[1][1] if len(scored) > 1 else 0.0)
    if best - runner_up < NAVIGATION_CONFIDENCE:
        return None
    result = palette_routes.resolve_route(request, route, {})
    if "error" in result or not result.get("url"):
        return None
    return {
        "kind": KIND_NAVIGATE,
        "url": result["url"],
        "message": result.get("summary", "") or f"Taking you to {route.label.lower()}.",
        "action": "go_to_page",
        "data": _carry_over(result),
        "route": route.key,
    }


def preloadable_lookup(query: str) -> str | None:
    """The one parameterless lookup this exact phrase has always been answered from, if any.

    Run before the first model call, saving a round. Only parameterless lookups qualify, which is the
    safety argument: they resolve from the caller's own context, so the next person gets their own
    answer. Only the choice of lookup is cached, never its result.
    """
    phrase = normalize_query(query)
    if not phrase:
        return None
    # Hashed: memcached rejects keys with spaces.
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


#: How many times a phrase must be asked the same way before it is worth writing down: low enough for
#: the long tail, high enough that one person experimenting doesn't make shortcuts for everybody.
MINE_MIN_COUNT = 5


def mine_shortcuts(min_count: int = MINE_MIN_COUNT):
    """Phrases the assistant has always answered with the same destination, and the ones it hasn't.

    Returns ``(candidates, rejected)``. **The model's own repeated answers are the ground truth**,
    which is what makes this safe: nothing here scores or guesses. Unanimity is required, not a
    majority — one disagreement and the phrase is left alone, because a query that resolves two ways
    is one where context matters.

    ``lookup:<name>`` rows are dropped: a lookup has no URL to point a shortcut at and its answer
    differs per user. Those are already handled by :func:`preloadable_lookup`, and
    :func:`mine_preloaded_lookups` reports them so the whole picture is on screen.
    """
    from collections import defaultdict

    destinations = defaultdict(set)
    counts = defaultdict(int)
    rows = LLMUsage.objects.filter(success=True).exclude(destination="").exclude(query="")
    for query, destination in rows.values_list("query", "destination"):
        phrase = normalize_query(query)
        if not phrase or destination.startswith(LOOKUP_DESTINATION_PREFIX):
            continue
        destinations[phrase].add(destination)
        counts[phrase] += 1
    candidates = {}
    rejected = {}
    for phrase, routes in destinations.items():
        if counts[phrase] < min_count:
            continue
        if len(routes) == 1:
            candidates[phrase] = (next(iter(routes)), counts[phrase])
        else:
            rejected[phrase] = routes
    return candidates, rejected


def mine_preloaded_lookups(min_count: int = MINE_MIN_COUNT):
    """Phrases the assistant keeps answering out of one lookup, and how often.

    Nothing to create — :func:`preloadable_lookup` already acts on these — but "why is this phrase not
    in the shortcut list" has an answer and it should be on screen.
    """
    from collections import defaultdict

    counts = defaultdict(int)
    names = defaultdict(set)
    rows = (
        LLMUsage.objects.filter(success=True, destination__startswith=LOOKUP_DESTINATION_PREFIX)
        .exclude(query="")
        .values_list("query", "destination")
    )
    for query, destination in rows:
        phrase = normalize_query(query)
        if not phrase:
            continue
        counts[phrase] += 1
        names[phrase].add(destination[len(LOOKUP_DESTINATION_PREFIX) :])
    return {
        phrase: (sorted(names[phrase]), count)
        for phrase, count in counts.items()
        if count >= min_count and len(names[phrase]) == 1
    }


def phrases_with_a_shortcut() -> set[str]:
    """Every phrase already covered by a shortcut, normalized the same way as the queries."""
    phrases = set()
    for page in CommandPalettePage.objects.all():
        for phrase in command_palette._page_phrases(page):
            phrases.add(normalize_query(phrase))
    return phrases


def shortcut_proposals(min_count: int = MINE_MIN_COUNT) -> list[dict[str, Any]]:
    """Shortcuts worth creating, for the analytics page's approval queue.

    The mining has always been there; nothing ran it, so nothing was ever mined. Every one of these
    accepted is a query that stops costing a model call and stops being able to come back wrong.
    """
    candidates, _ = mine_shortcuts(min_count)
    existing = phrases_with_a_shortcut()
    proposals = []
    for phrase, (route_key, count) in sorted(candidates.items(), key=lambda item: -item[1][1]):
        if phrase in existing:
            continue
        route = palette_routes.get_route(route_key)
        proposals.append(
            {"phrase": phrase, "route": route_key, "label": route.label if route else route_key, "count": count}
        )
    return proposals


def _answered_from(lookups_run: set[tuple[str, str]]) -> str:
    """The ``destination`` to record: the single parameterless lookup behind an answer, or ""."""
    if len(lookups_run) != 1:
        return ""
    name, params = next(iter(lookups_run))
    if params not in ("{}", ""):
        return ""
    return f"{LOOKUP_DESTINATION_PREFIX}{name}"


def _preload_messages(request, query: str, messages: list[dict[str, Any]]) -> str:
    """Run the preloadable lookup and add its result before the first round. Best-effort."""
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


def asks_a_question(query: str) -> bool:
    """Whether the user is asking rather than naming something. A question is never an obvious match."""
    words = normalize_query(query).split()
    return bool(query.strip().endswith("?") or (words and words[0] in _QUESTION_WORDS))


def obvious_match(request, query: str) -> list[dict[str, Any]] | None:
    """Ordinary search groups when a short, non-command query already has a clear match.

    The match has to be in a result's own title. It used to be enough for the search to return *any*
    page at all, so "who won lot 12" was answered with the Won lots page and never reached the model
    — four words and a weak page hit were all it took to swallow a question.
    """
    if _looks_like_a_command(query) or len(query.split()) > SHORT_QUERY_WORDS:
        return None
    if asks_a_question(query):
        return None
    lowered = query.lower().strip()
    if not lowered:
        return None
    groups = command_palette.search(request, query)
    for group in groups:
        for item in group["items"]:
            title = (item.get("title") or "").lower()
            if lowered == title or lowered in title:
                return groups
    return None


# --- prompt building ---------------------------------------------------------


SYSTEM_PROMPT = """You turn one thing a person typed or said into one tool call on a fish auction site.

Always call a tool. Never write a reply of your own: they read the tool's result, not your words.

- A question — when, how much, how many, is it, what are the rules — is always a read, never a page:
  call describe_auction / describe_lot / describe_person / describe_club. What it returns is the
  answer they see, so pick the one that holds it and you are done.
- find_person and find_lot turn a name into a number for the tool you call next.
- "take me to", "show me", "open", "where is", "where do I" — that is go_to_page with one of the
  keys below, not a read. Send them there.
- Doing something: call that tool. They get a countdown and a cancel button, so guess confidently
  rather than asking.
- ask_the_user only when you truly cannot tell what they meant.
- cannot_do_this only when this site does not do it at all. Not knowing the page is not that: guess
  the closest one and go there.

Leave 'auction' out unless they named one. Never invent a number or a price. Never put their whole
sentence in a field. When they say "that lot" or "another one", look in the earlier exchanges.
With an action you may add one short sentence for the countdown card: "Add blue shrimp for Bob".

Facts about this person are the first message below.

go_to_page keys (every page on the site):
{pages}
"""


def build_system_prompt(user, page: dict[str, Any] | None = None, app_destinations=()) -> str:
    """The system prompt: the instructions and the page catalog (``palette_routes.ROUTE_LIST``).

    Skills are tool definitions (:func:`tools_for`), not prompt text. The catalog (~1k tokens) is
    filtered for relevance, not security, and saves a lookup round. ``app_destinations`` adds the app's
    native screens.

    **Nothing user-specific goes in here.** The catalog and the app's screens depend only on which of
    three permissions the user holds, so the whole message is byte-identical for everyone in a tier
    and they share one cached prompt prefix at the provider. A per-user prefix went cold between
    sessions and was paid for again every time; the user's own facts ride in the first user message
    instead (:func:`context_message`). ``page`` is unused, kept for positional callers.
    """
    pages = palette_routes.catalog_for_prompt(user)
    if app_destinations:
        pages += "\nIn the app, where this user is right now (native screens, same 'page' parameter):\n"
        pages += "\n".join(f"  {name}: {description}" for name, description in app_destinations)
    return SYSTEM_PROMPT.format(pages=pages)


def context_message(user, page: dict[str, Any] | None = None) -> dict[str, Any]:
    """The user's own facts, as the conversation's first message. See :func:`build_system_prompt`."""
    # ``strip_internal``: the resource URIs ``my_context`` carries are dead weight in a prompt.
    facts = json.dumps(
        palette_actions.strip_internal(palette_actions.user_context(user, page)), indent=None, default=str
    )
    return {"role": "user", "content": "About this user:\n" + facts}


def build_messages(user, query: str, context: list[dict[str, Any]], page: dict[str, Any] | None = None):
    """The conversation: this user's facts, recent exchanges, then the query. Tool turns are built by
    :mod:`auctions.llm`.
    """
    messages: list[dict[str, Any]] = [context_message(user, page)]
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
#: Cap on an answer. It is a resolver's own summary now, so this guards against a long one, not
#: against a model in full flow; the 300 that replaced 1200 was sized for the latter.
MAX_ANSWER_CHARS = 700

#: Openings that mean the model is about to ask rather than tell. A sentence starting with one of
#: these and ending in a question mark is the clarifying question it should have asked with.
_ASKING_OPENERS = (
    "do you",
    "would you",
    "did you",
    "should i",
    "shall i",
    "which ",
    "what ",
    "who ",
    "where ",
    "when ",
    "are you",
    "is that",
    "can you tell",
    "tell me",
    "let me know",
)


def _sentences(message: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", message.strip()) if part.strip()]


def _is_really_a_question(message: str) -> str:
    """The question inside an answer that is actually asking something, or ``""``.

    ``ask_the_user`` exists and a weak model ignores it, writing the question out as prose instead.
    Prose can't be clicked, so a voice user dead-ends on it. This finds it and sends it back through
    the clarify card.
    """
    for sentence in _sentences(message):
        if not sentence.endswith("?"):
            continue
        lowered = sentence.lower().lstrip("-•* ")
        if lowered.startswith(_ASKING_OPENERS):
            return sentence
    return ""


def echoes_the_query(params: dict[str, Any], query: str) -> bool:
    """Whether a call just put the user's whole sentence into one of its own text fields.

    "add lots to my next club auction" came back as add_person with the name "add lots to my next
    club auction". The countdown card catches it, but only after asking somebody to read their own
    words back; a call that has understood nothing is better spent on another round.
    """
    asked = normalize_query(query)
    if not asked or len(asked.split()) < 3:
        return False
    return any(isinstance(value, str) and normalize_query(value) == asked for value in params.values())


def read_reply(result, user=None, query: str = "") -> dict[str, Any]:
    """Read one model reply into a dict with a ``kind``: lookup, action, the palette's three tools, or
    an answer. Shape is already enforced by the provider. ``user`` is unused, kept for positional
    callers. ``query`` is what the person typed, for :func:`echoes_the_query`.
    """
    calls = getattr(result, "tool_calls", None) or []
    if calls:
        # One call at a time: several at once would mean guessing an order for writes.
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
            return {
                "kind": "error",
                "message": (reason or "That isn't something this site does.")[:MAX_QUESTION_CHARS],
            }
        action = palette_actions.get_action(call.name)
        if action is None or action.mcp_only:
            # Only reachable behind an LLM_BASE_URL that doesn't enforce the tool list.
            return {"kind": "invalid", "reason": f"unknown tool {call.name!r}"}
        if action.lookup:
            return {"kind": "lookup", "action": action, "params": params}
        if echoes_the_query(params, query):
            return {
                "kind": "invalid",
                "reason": f"{action.name} was called with the whole query as a field",
                "retry": True,
            }
        # The model's own sentence, if any, is the countdown summary; else ``default_summary``.
        return {
            "kind": "action",
            "action": action,
            "params": params,
            "summary": (getattr(result, "text", "") or "").strip()[:MAX_SUMMARY_CHARS],
        }

    # Prose is off-contract: the model has no tool that takes words, and ``tool_choice="required"``
    # means it should not be able to send any. Only an endpoint that doesn't enforce the tool list
    # gets here. A question it wrote out is still worth rescuing as a card; anything else earns a round.
    text = (getattr(result, "text", "") or "").strip()
    if not text:
        return {"kind": "invalid", "reason": "empty reply"}
    question = _is_really_a_question(text)
    if question:
        return {"kind": "clarify", "message": question[:MAX_QUESTION_CHARS], "options": []}
    return {"kind": "invalid", "reason": "wrote a reply of its own instead of calling a tool", "retry": True}


# --- keeping identifiers out of what the user reads --------------------------
#
# The prompt forbids echoing slugs, but a prompt isn't a guarantee: everything goes through humanize.

#: A possible slug or URL name. Loose is safe: a candidate is replaced only if it resolves.
_IDENTIFIER = re.compile(r"\b[a-z0-9]+(?:[-_][a-z0-9]+)+\b")

#: Cap on candidates looked up per message.
_MAX_IDENTIFIERS = 20


def _names_for_slugs(slugs: set[str], user=None) -> dict[str, str]:
    """Map each auction or club slug to its title, scoped to *user*.

    Unscoped, this was an oracle: an echoed guessed slug came back as the auction's real title. With no
    user nothing resolves.
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
    """Replace slugs and route keys in a user-facing string with their names; leave anything else alone.
    *user* scopes the slug lookup (:func:`_names_for_slugs`).
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
        # One pass with a function: a title must never be read as a regex template, or re-replaced.
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


_variant_cache: str | None = None


def variant() -> str:
    """A fingerprint of the assistant as it stands: its prompt, the skills it offers, and the model.

    Recorded on every row so the analytics page can compare a week of one assistant with a week of the
    next. It changes by itself when any of the three does, which is the only version number nobody
    forgets to bump.
    """
    global _variant_cache
    if _variant_cache is None:
        from django.conf import settings

        skills = sorted(name for name, action in palette_actions.ACTIONS.items() if not action.mcp_only)
        material = "|".join(
            [
                SYSTEM_PROMPT,
                *skills,
                str(getattr(settings, "LLM_MODEL", "")),
                str(getattr(settings, "LLM_REASONING_EFFORT", "")),
            ]
        )
        _variant_cache = hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]
    return _variant_cache


def record_usage(
    user,
    result,
    query: str,
    response_kind: str,
    action_name: str = "",
    success: bool = True,
    destination: str = "",
    request_id: str = "",
    started: float | None = None,
) -> int | None:
    """Write one :class:`LLMUsage` row and return its id (sent back on cancel). Never breaks the request."""
    try:
        return LLMUsage.objects.create(
            destination=(destination or "")[:100],
            request_id=(request_id or "")[:32],
            elapsed_ms=int((time.monotonic() - started) * 1000) if started else 0,
            variant=variant(),
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

    Scoped to the caller's rows. A cancel is the only record of a confident wrong match. It also spends
    the trust window for that action.
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
    """Record that the user reported a command didn't work. Scoped to the caller's rows; only sets a flag
    the analytics page sorts on.
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


def note_missing_skill(request, query: str, reason: str) -> None:
    """Record a refusal as a skill request, on the model's behalf.

    ``cannot_do_this`` is the honest signal that somebody wanted something this site doesn't do, and it
    was only ever written down when the model *also* chose to call ``request_a_skill`` — which it
    rarely did, so the queue stayed empty while the refusals piled up. Keyed on the query, so the same
    phrase from the same person updates one row. Best-effort: never breaks the reply.
    """
    query = (query or "").strip()
    if not query or not getattr(request.user, "is_authenticated", False):
        return
    try:
        palette_actions.run_action(
            request,
            "request_a_skill",
            {
                "skill": query[:100],
                "reason": f"Asked in the command palette. The assistant refused: {reason}"[:2000],
            },
        )
    except Exception:
        logger.exception("Could not record a refused palette query as a skill request")


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
# Each line describes something the server is really doing; none is a timer.

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
    """A first progress line from the query's shape, on screen before the model answers."""
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
    """Describe the action about to run, naming its object so a wrong auction can be caught in time."""
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
            # Carried forward so "take me to the fall auction" then "add a lot" means that auction.
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


# --- links to whatever the answer is about -----------------------------------
#
# Every resolver already says which objects it touched, in ``palette_actions.KEY_ABOUT``: it is what
# ``/mcp/`` turns into ``resource_link`` blocks. The palette used to strip it and throw it away, so
# "the next TFCB auction is on the 19th" arrived with no way to open that auction. These turn the
# same block into rows the user can click.

#: Rows offered under an answer. Enough to name what was talked about, not a second search result.
MAX_ANSWER_LINKS = 6


def _about_blocks(result: Any) -> list[dict[str, Any]]:
    """The ``_about`` block a resolver's result carries, if any."""
    if not isinstance(result, dict):
        return []
    about = result.get(palette_actions.KEY_ABOUT)
    return [about] if isinstance(about, dict) and about else []


def _merge_about(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """One ``_about`` from several, later blocks winning the single-valued keys.

    A lookup run late in the conversation is the one the answer is about: ``describe_auction`` after
    ``my_context`` means the answer is about that auction, not about all fifteen of them.
    """
    merged: dict[str, Any] = {}
    many: dict[str, list[str]] = {"auctions": [], "clubs": []}
    for block in blocks:
        for key, value in block.items():
            if key in many:
                many[key].extend(slug for slug in value or () if slug not in many[key])
            elif value:
                merged[key] = value
    for key, slugs in many.items():
        if slugs:
            merged[key] = slugs
    return merged


def links_for_about(user, about: dict[str, Any]) -> list[dict[str, Any]]:
    """Palette items for the objects an ``_about`` block names, most specific first.

    Scoped to what this user can already see, so an echoed slug can't become an oracle — the same
    rule :func:`_names_for_slugs` follows.
    """
    from .models import Club, Lot

    if not about:
        return []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(item: dict[str, Any] | None) -> None:
        if item and item["url"] and item["url"] not in seen and len(items) < MAX_ANSWER_LINKS:
            seen.add(item["url"])
            items.append(item)

    visible = command_palette._visible_auctions(user)
    auction_slug = about.get("auction")
    if auction_slug and about.get("lot"):
        # ``lot_number_display`` is one of three columns depending on the auction's numbering, so all
        # three are matched rather than guessing which one this auction uses.
        number = str(about["lot"])
        matches = Q(custom_lot_number=number)
        if number.isdigit():
            matches |= Q(lot_number_int=int(number)) | Q(pk=int(number))
        lot = (
            Lot.objects.exclude(is_deleted=True)
            .filter(auction__in=command_palette._joined_auctions(user), auction__slug=auction_slug)
            .filter(matches)
            .select_related("auction")
            .first()
        )
        if lot:
            add(command_palette._item("lot", lot.lot_name, lot.get_absolute_url(), "bi-tag", lot.auction.title, lot.pk))
    slugs = [slug for slug in [auction_slug, *(about.get("auctions") or ())] if slug]
    if slugs:
        found = {auction.slug: auction for auction in visible.filter(slug__in=slugs).select_related("club")}
        for slug in slugs:
            auction = found.get(slug)
            if auction:
                add(
                    command_palette._item(
                        "auction",
                        auction.title,
                        auction.get_absolute_url(),
                        "bi-hammer",
                        auction.club.name if auction.club else "",
                        auction.pk,
                    )
                )
    club_slugs = [slug for slug in [about.get("club"), *(about.get("clubs") or ())] if slug]
    if club_slugs:
        for club in Club.objects.filter(slug__in=club_slugs, active=True):
            add(
                command_palette._item(
                    "club",
                    club.name,
                    reverse("club_detail", kwargs={"slug": club.slug}),
                    "bi-people",
                    club.abbreviation or "",
                    club.pk,
                )
            )
    return items


def about_groups(user, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The one group of links to show under an answer, or nothing."""
    items = links_for_about(user, _merge_about(blocks))
    return [{"label": "About", "items": items}] if items else []


#: Values a resolver may hand forward. Must stay a subset of what ``sanitize_context`` accepts.
_CARRY_OVER_KEYS = ("lot_id", "lot_name", "bidder_number", "auction", "club")


def _carry_over(result: dict[str, Any]) -> dict[str, Any]:
    """The few values worth remembering for the next command ("print *that* label", "his email is…",
    "another in the same auction").

    Scalars only: ``describe_auction`` calls its whole payload ``auction``, and carrying that forward
    put a dict where every other result puts a slug.
    """
    data = {}
    for key in _CARRY_OVER_KEYS:
        value = result.get(key)
        if value is not None and isinstance(value, (str, int, float)) and not isinstance(value, bool):
            data[key] = value
    return data


def _trust_key(user, action, context: str) -> str:
    """Cache key for "this user has already approved this action, on this thing, recently"."""
    subject = re.sub(r"[^a-z0-9]+", "-", (context or "").lower())[:60]
    return f"palette_trust_{getattr(user, 'pk', 0)}_{action.name}_{subject}"


def remember_trust(request, action, params: dict[str, Any]) -> None:
    """Record a completed confirm-tier action so the next identical one counts down less. Success only."""
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
    """The card shown before a database change. ``context`` comes from the server, not the model's summary."""
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


#: Words about how, not what; stripped before the fallback search.
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
    """Ordinary search results worth showing instead of a dead end, or ``None``."""
    for attempt in (query, _keywords(query)):
        if not attempt:
            continue
        groups = command_palette.search(request, attempt)
        if any(group.get("items") for group in groups):
            return {"kind": KIND_RESULTS, "groups": groups, "note": note}
    return None


def _best_guess_page(request, query: str) -> dict[str, Any] | None:
    """A deliberately simple guess at the page from the route catalog, after the model has failed."""
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
    """The end of the line: search results, then a best-guess page, then an error.

    Every outcome carries ``usage_id`` for the "that didn't work" button.
    """
    fallback = _search_fallback(request, query, "I wasn't sure what you meant. Here's what I found:")
    if fallback:
        return {**fallback, "usage_id": usage_id}
    guess = _best_guess_page(request, query)
    if guess:
        return {**humanize_response(guess, request.user), "usage_id": usage_id}
    return {"kind": KIND_ERROR, "message": humanize(message, request.user), "usage_id": usage_id}


def lookup_payload(name: str, result: Any) -> str:
    """One lookup's result as the model's next message. If it must cut, it says so and the model is told
    to send the user to the page. ``test_palette_assist`` asserts describe_* payloads fit.
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


def answers_on_its_own(action, query: str, result: Any) -> bool:
    """Whether this read's own summary is the answer, so the loop stops without another model call.

    Measured, not assumed: given a ``describe_auction`` result that plainly answered the question, the
    model called ``describe_auction`` again rather than any tool meaning "show that" — it has the
    answer and no way to say so. The server can see the same thing without asking.

    Not for a query that wants something done ("add a lot", "check in bob"), where a read is a step on
    the way, and not for the reads that exist to feed another tool (:data:`STEP_LOOKUPS`).
    """
    if action.name in STEP_LOOKUPS or asks_for_a_write(query):
        return False
    return bool(isinstance(result, dict) and result.get("summary"))


def _rounds_allowed(lookups_run: set) -> int:
    """Model calls this request may still make: one more once a lookup has fetched something real."""
    return MAX_ROUNDS_AFTER_LOOKUP if lookups_run else MAX_ROUNDS


def _progress(text: str) -> dict[str, Any]:
    return {"kind": KIND_PROGRESS, "message": text}


def assist_stream(request, query: str, context: Any = None, path: str = ""):
    """Answer one palette command, yielding ``progress`` dicts then exactly one final response. Never raises."""
    user = request.user
    query = (query or "").strip()[:MAX_QUERY_LENGTH]
    # Parsed once and hung on the request for every resolver.
    request.palette_page = palette_routes.page_context_from_path(user, path) if path else {}

    if not query:
        yield {"kind": KIND_RESULTS, "groups": command_palette.search(request, "")}
        return

    # A curated shortcut answers first, even with assist disabled.
    groups = shortcut_match(request, query)
    if groups is not None:
        yield {"kind": KIND_RESULTS, "groups": groups}
        return

    # "take me to my invoices" names its own destination; no model call needed.
    going = navigation_shortcut(request, query)
    if going is not None:
        log_assist(user, query, KIND_NAVIGATE)
        yield humanize_response(going, user)
        return

    if not assist_enabled_for(user):
        yield {"kind": KIND_RESULTS, "groups": command_palette.search(request, query)}
        return

    groups = obvious_match(request, query)
    if groups is not None:
        yield {"kind": KIND_RESULTS, "groups": groups}
        return

    over_budget = check_request_budget(user)
    if over_budget:
        yield {"kind": KIND_ERROR, "message": over_budget}
        return

    yield _progress(opening_line(query))

    # One id for every round of this one thing they typed, and one clock for what they waited.
    request_id = uuid.uuid4().hex
    started = time.monotonic()

    def record(result, kind, action_name="", success=True, destination=""):
        return record_usage(
            user, result, query, kind, action_name, success, destination, request_id=request_id, started=started
        )

    entries = sanitize_context(context)
    provider = get_provider()
    system = build_system_prompt(user, request.palette_page, command_palette.app_destinations_for_prompt(request))
    # Built once per request: two queries, unchanged mid-loop.
    tools = tools_for(user, query)
    messages = build_messages(user, query, entries, request.palette_page)
    # Every object a lookup touched, so the answer can be clicked. See :func:`about_groups`.
    abouts: list[dict[str, Any]] = []
    # The last read that said something a person could read.
    found: dict[str, Any] = {}
    nudges = 0
    lookups_run: set[tuple[str, str]] = set()
    # Recorded as if the model asked, so it won't ask again and the extra round is earned.
    preloaded = _preload_messages(request, query, messages)
    if preloaded:
        lookups_run.add((preloaded, json.dumps({}, sort_keys=True)))

    round_number = 0
    while round_number < _rounds_allowed(lookups_run):
        if time.monotonic() - started > TOTAL_BUDGET_SECONDS:
            logger.info("Assist budget exhausted after %s rounds", round_number)
            break
        round_number += 1
        # Per model call: a spend ceiling on tokens.
        over_budget = check_call_budget(user)
        if over_budget:
            record(None, FAIL_THROTTLED, success=False)
            log_assist(user, query, KIND_ERROR)
            yield {"kind": KIND_ERROR, "message": over_budget}
            return
        if provider_is_resting():
            # The model has failed several times running; a spinner and a timeout help nobody.
            record(None, FAIL_PROVIDER, success=False)
            log_assist(user, query, KIND_ERROR)
            yield _give_up(request, query, "The assistant is having a moment. Here's what I found:", None)
            return
        held = wait_for_the_queue(site_load())
        if held:
            yield _progress(f"Busy right now — waiting {held:.0f} second{'s' if held >= 1.5 else ''}…")
            time.sleep(held)
        try:
            # "required": a bare paragraph is the one reply this box cannot render.
            result = provider.complete(system, messages, tools, tool_choice="required")
        except llm.RateLimited as limited:
            # Not an outage: the provider will take this in a moment. Wait its own number, retry once.
            pause = min(MAX_WAIT_SECONDS, max(float(limited.retry_after or 0), 1.0))
            logger.info("Assist waiting %ss for the provider's rate limit", pause)
            yield _progress("Busy right now — still working…")
            time.sleep(pause)
            try:
                result = provider.complete(system, messages, tools, tool_choice="required")
            except LLMError as error:
                logger.warning("Assist still rate limited: %s", error)
                note_provider_failure()
                usage_id = record(None, FAIL_BUSY, success=False)
                log_assist(user, query, KIND_ERROR)
                yield _give_up(request, query, "Everyone's using this at once. Here's what I found:", usage_id)
                return
        except LLMError as error:
            logger.warning("Assist provider error: %s", error)
            note_provider_failure()
            usage_id = record(None, FAIL_PROVIDER, success=False)
            log_assist(user, query, KIND_ERROR)
            yield _give_up(request, query, "I couldn't reach the assistant just now.", usage_id)
            return
        note_provider_success()
        spend_tokens(result.total_tokens)

        reply = read_reply(result, user, query)
        kind = reply["kind"]

        if kind == "invalid":
            record(result, FAIL_INVALID, success=False)
            logger.info("Assist got an unusable reply: %s", reply.get("reason"))
            # "Please wait a moment while I look up the auctions list" used to end the request right
            # there, having looked nothing up. It has tools and rounds left, so say so once.
            if reply.get("retry") and nudges < MAX_REPEAT_NUDGES and round_number < _rounds_allowed(lookups_run):
                nudges += 1
                nudge = (
                    "That reply was not shown to the person, because it was not something they can "
                    "read, click or undo. You have the tools in front of you: use one of them "
                    "properly now, in this reply. Never put their whole sentence into a field — "
                    "work out which words are the name, the number and the amount."
                )
                calls = getattr(result, "tool_calls", None) or []
                if calls:
                    messages.append(llm.tool_call_message([calls[0]]))
                    messages.append(llm.tool_result_message(calls[0], nudge))
                else:
                    messages.append({"role": "user", "content": nudge})
                continue
            # Otherwise: a non-enforcing LLM_BASE_URL or an empty reply, and asking again won't fix
            # either. Fall back to search.
            break

        if kind == "lookup":
            action = reply["action"]
            # A repeated identical lookup: the answer hasn't changed.
            signature = (action.name, json.dumps(reply["params"], sort_keys=True, default=str))
            if signature in lookups_run:
                record(result, "lookup", action.name)
                if nudges >= MAX_REPEAT_NUDGES:
                    logger.info("Assist repeated the %s lookup; stopping", action.name)
                    break
                # Usually the model forgot it already has the result. Nudge once, without re-running
                # the lookup; the round budget still caps the request.
                nudges += 1
                logger.info("Assist repeated the %s lookup; nudging it to answer", action.name)
                # Every tool call needs a result turn, so the nudge is the result.
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
            record(result, "lookup", action.name)
            lookup_result = palette_actions.run_action(request, action.name, reply["params"])
            # Before ``strip_internal``: this is the only place the objects it touched are named.
            abouts.extend(_about_blocks(lookup_result))
            if isinstance(lookup_result, dict) and lookup_result.get("summary"):
                found = {"summary": str(lookup_result["summary"]), "action": action.name, "result": lookup_result}
            if answers_on_its_own(action, query, lookup_result):
                record(result, KIND_ANSWER, action.name, destination=_answered_from(lookups_run))
                log_assist(user, query, KIND_ANSWER)
                # What the answer is about, linked. Ordinary search results only when it is about
                # nothing, which is the case a keyword search was always a poor answer to.
                groups = about_groups(user, abouts)
                if not groups:
                    related = _search_fallback(request, query, "")
                    groups = related["groups"] if related else []
                yield humanize_response(
                    {
                        "kind": KIND_ANSWER,
                        "message": found["summary"][:MAX_ANSWER_CHARS],
                        "groups": groups,
                        # "when does the fall auction start" then "sign me up" means that auction.
                        "data": _carry_over({**_merge_about(abouts), **_carry_over(lookup_result)}),
                    },
                    user,
                )
                return
            messages.append(llm.tool_call_message([result.tool_calls[0]]))
            messages.append(llm.tool_result_message(result.tool_calls[0], lookup_payload(action.name, lookup_result)))
            continue

        if kind == "clarify":
            record(result, KIND_CLARIFY)
            log_assist(user, query, KIND_CLARIFY)
            response = {"kind": KIND_CLARIFY, "message": reply["message"], "options": reply["options"]}
            if not reply["options"]:
                # A question with nothing to click dead-ends voice users; offer whatever it is about,
                # and ordinary search results when it is about nothing yet.
                groups = about_groups(user, abouts)
                if not groups:
                    fallback = _search_fallback(request, query, "")
                    groups = fallback["groups"] if fallback else []
                if groups:
                    response["groups"] = groups
            yield humanize_response(response, user)
            return

        if kind == "error":
            usage_id = record(result, FAIL_MODEL_ERROR, success=False)
            log_assist(user, query, KIND_ERROR)
            # The model saying the site can't do this is the one honest record of a missing skill,
            # and it used to be kept only when the model also chose to call request_a_skill.
            note_missing_skill(request, query, reply["message"])
            yield _give_up(request, query, reply["message"], usage_id)
            return

        # kind == "action"
        action = reply["action"]
        params = reply["params"]
        summary = reply["summary"]
        yield _progress(narrate_action(request, action, params))

        if action.danger == palette_actions.DANGER_CONFIRM and action.asks_first:
            # Do NOT execute: the execute endpoint re-runs the resolver after the countdown.
            # ``asks_first=False`` runs the write here instead, still through ``run_action``.
            usage_id = record(result, KIND_COUNTDOWN, action.name)
            log_assist(user, query, KIND_COUNTDOWN)
            yield humanize_response(_countdown_response(request, action, params, summary, usage_id), user)
            return

        action_result = palette_actions.run_action(request, action.name, params)
        response = _result_to_response(action, params, action_result, summary)
        if response["kind"] != KIND_ERROR:
            # Without this, "undo that" silently undid the previous action instead.
            palette_actions.remember_undo(request.user, action.name, action_result)
        if response["kind"] == KIND_ERROR:
            # Keep the specific reason, with search results underneath.
            usage_id = record(result, KIND_ERROR, action.name, success=False)
            log_assist(user, query, KIND_ERROR)
            yield {**humanize_response(response, user), "usage_id": usage_id}
            return
        record(result, response["kind"], action.name, destination=response.get("route", ""))
        log_assist(user, query, response["kind"])
        yield humanize_response(response, user)
        return

    usage_id = record(None, FAIL_GAVE_UP, success=False)
    log_assist(user, query, KIND_ERROR)
    yield _give_up(request, query, "I couldn't work out how to do that.", usage_id)


def assist(request, query: str, context: Any = None, path: str = "") -> dict[str, Any]:
    """Answer one palette command and return just the answer (non-streaming :func:`assist_stream`)."""
    response: dict[str, Any] = {"kind": KIND_ERROR, "message": "I couldn't work out how to do that."}
    for event in assist_stream(request, query, context, path):
        if event.get("kind") != KIND_PROGRESS:
            response = event
    return response


def execute(request, name: str, params: Any, path: str = "") -> dict[str, Any]:
    """Run a confirm-tier action after the countdown. The server is the gate: this re-resolves the page,
    re-checks permissions and re-validates everything.
    """
    # A countdown started before assist was turned off must not still run.
    if not assist_enabled_for(request.user):
        return {"kind": KIND_ERROR, "message": "I don't know how to do that."}
    if navigate_only(request.user):
        # Same for a card that was on screen when the switch went the other way.
        return {"kind": KIND_ERROR, "message": "I can only take you to the right page. That one's done there."}
    request.palette_page = palette_routes.page_context_from_path(request.user, path) if path else {}
    action = palette_actions.get_action(name)
    if action is None:
        return {"kind": KIND_ERROR, "message": "I don't know how to do that."}
    if action.danger != palette_actions.DANGER_CONFIRM:
        # Safe actions already ran during assist; navigate actions are the client's job.
        return {"kind": KIND_ERROR, "message": "That isn't something to confirm."}
    # ``asks_first=False`` isn't refused here: only a stale page reaches this, and the write is allowed.
    if not isinstance(params, dict):
        return {"kind": KIND_ERROR, "message": "Those instructions didn't make sense."}
    result = palette_actions.run_action(request, action.name, params)
    response = _result_to_response(action, params, result, "")
    if response["kind"] != KIND_ERROR:
        # The next identical card gets a shorter countdown.
        remember_trust(request, action, params)
        # Only a completed action is offered to undo.
        palette_actions.remember_undo(request.user, action.name, result)
    return humanize_response(response, request.user)
