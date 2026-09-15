"""Natural-language orchestration for the command palette.

``assist(request, query, context)``:

1. A short query with an obvious search match never reaches the model.
2. Otherwise a bounded tool-calling loop (:data:`MAX_ROUNDS`, :data:`TOTAL_BUDGET_SECONDS`) over the
   same catalogue ``/mcp/`` serves (:func:`auctions.mcp.tools.tool_descriptors`).

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
from typing import Any

from django.core.cache import cache
from django.db.models import F

from . import command_palette, llm, palette_actions, palette_routes
from .llm import LLMError, assist_enabled, get_provider
from .models import CommandPalettePage, CommandPaletteSearch, LLMUsage

logger = logging.getLogger(__name__)

# Agent loop bounds. Every round resends the ~3k-token prompt. Measured: usable answers took one
# round, occasionally two; none needed three.
MAX_ROUNDS = 2
#: The ceiling once a lookup has run: it has earned the round needed to say what it found.
MAX_ROUNDS_AFTER_LOOKUP = 3
#: How many times the model is worth telling that it already has what it just asked for again.
MAX_REPEAT_NUDGES = 1
TOTAL_BUDGET_SECONDS = 20.0

# Recent exchanges kept for context ("print that label" -> the lot we just added).
MAX_CONTEXT_ENTRIES = 5

# How much of a lookup's result is fed back to the model. The largest (``describe_auction`` with
# long rules) fits with ~700 characters to spare; :func:`lookup_payload` logs when that runs out.
MAX_LOOKUP_RESULT_CHARS = 5000

#: The palette's own two tools, absent from ``/mcp/`` where hosts ask and fail for themselves.
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

    ``Action.mcp_only`` actions are dropped: ``read_source`` and ``club_api`` return pages of text,
    and the rest are writes. ``go_to_page`` still reaches every one of their pages.
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

# Countdown once an admin has let this exact action, on this subject, run in the last ten minutes.
# Shortened, never skipped; cancelling spends the trust (:func:`forget_trust`).
TRUSTED_COUNTDOWN_MS = 1500
TRUST_WINDOW_SECONDS = 600

MAX_QUERY_LENGTH = 600

# Throttling: 1/second is the anti-bot floor, the window cap is the spend ceiling.
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

# ``LLMUsage.response_kind`` for failures, split so the analytics page can tell a refusal from an
# outage.
FAIL_GAVE_UP = "gave_up"  # the loop ran out of rounds without reaching an answer
FAIL_MODEL_ERROR = "model_error"  # the model said it couldn't do this
FAIL_PROVIDER = "provider_error"  # we couldn't reach the provider at all
FAIL_INVALID = "invalid_shape"  # the model replied with something off-contract
FAIL_THROTTLED = "throttled"  # the user hit the spend ceiling
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


def obvious_match(request, query: str) -> list[dict[str, Any]] | None:
    """Ordinary search groups when a short, non-command query already has a clear match."""
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
    """The system prompt: the page catalog (``palette_routes.ROUTE_LIST``) and the user's context.

    Skills are tool definitions (:func:`tools_for`), not prompt text. The catalog (~1k tokens) is
    filtered for relevance, not security, and saves a lookup round. ``app_destinations`` adds the app's
    native screens.
    """
    # ``strip_internal``: the resource URIs ``my_context`` carries are dead weight in a prompt.
    context = json.dumps(
        palette_actions.strip_internal(palette_actions.user_context(user, page)), indent=None, default=str
    )
    pages = palette_routes.catalog_for_prompt(user)
    if app_destinations:
        pages += "\nIn the app, where this user is right now (native screens, same 'page' parameter):\n"
        pages += "\n".join(f"  {name}: {description}" for name, description in app_destinations)
    return SYSTEM_PROMPT.format(pages=pages, context=context)


def build_messages(query: str, context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The user turn: recent exchanges, then the query. Tool turns are built by :mod:`auctions.llm`."""
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
    """Read one model reply into a dict with a ``kind``: lookup, action, the palette's two tools, or an
    answer. Shape is already enforced by the provider. ``user`` is unused, kept for positional callers.
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
            return {"kind": "error", "message": (reason or "That isn't something this site does.")[:MAX_QUESTION_CHARS]}
        action = palette_actions.get_action(call.name)
        if action is None or action.mcp_only:
            # Only reachable behind an LLM_BASE_URL that doesn't enforce the tool list.
            return {"kind": "invalid", "reason": f"unknown tool {call.name!r}"}
        if action.lookup:
            return {"kind": "lookup", "action": action, "params": params}
        # The model's own sentence, if any, is the countdown summary; else ``default_summary``.
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


def record_usage(
    user,
    result,
    query: str,
    response_kind: str,
    action_name: str = "",
    success: bool = True,
    destination: str = "",
) -> int | None:
    """Write one :class:`LLMUsage` row and return its id (sent back on cancel). Never breaks the request."""
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


#: Values a resolver may hand forward. Must stay a subset of what ``sanitize_context`` accepts.
_CARRY_OVER_KEYS = ("lot_id", "lot_name", "bidder_number", "auction", "club")


def _carry_over(result: dict[str, Any]) -> dict[str, Any]:
    """The few values worth remembering for the next command ("print *that* label", "his email is…",
    "another in the same auction").
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
    # Built once per request: two queries, unchanged mid-loop.
    tools = tools_for(user)
    messages = build_messages(query, entries)
    started = time.monotonic()
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
            yield _give_up(request, query, "I couldn't reach the assistant just now.", usage_id)
            return

        reply = read_reply(result, user)
        kind = reply["kind"]

        if kind == "invalid":
            # Schemas are enforced, so this means a non-enforcing LLM_BASE_URL or an empty reply.
            # Asking again won't fix either; fall back to search.
            record_usage(user, result, query, FAIL_INVALID, success=False)
            logger.info("Assist got an unusable reply: %s", reply.get("reason"))
            break

        if kind == "lookup":
            action = reply["action"]
            # A repeated identical lookup: the answer hasn't changed.
            signature = (action.name, json.dumps(reply["params"], sort_keys=True, default=str))
            if signature in lookups_run:
                record_usage(user, result, query, "lookup", action.name)
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
            record_usage(user, result, query, "lookup", action.name)
            lookup_result = palette_actions.run_action(request, action.name, reply["params"])
            messages.append(llm.tool_call_message([result.tool_calls[0]]))
            messages.append(llm.tool_result_message(result.tool_calls[0], lookup_payload(action.name, lookup_result)))
            continue

        if kind == "answer":
            # Only a single parameterless lookup is recorded as a destination (preloadable).
            record_usage(user, result, query, KIND_ANSWER, destination=_answered_from(lookups_run))
            log_assist(user, query, KIND_ANSWER)
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
                # A question with nothing to click dead-ends voice users; offer search results.
                fallback = _search_fallback(request, query, "")
                if fallback:
                    response["groups"] = fallback["groups"]
            yield humanize_response(response, user)
            return

        if kind == "error":
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
            # Do NOT execute: the execute endpoint re-runs the resolver after the countdown.
            # ``asks_first=False`` runs the write here instead, still through ``run_action``.
            usage_id = record_usage(user, result, query, KIND_COUNTDOWN, action.name)
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
