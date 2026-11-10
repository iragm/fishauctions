"""Recording what agents ask ``/mcp/`` for, and reading it back for ``mcp_usage`` on ``/mcp/admin/``.

One :class:`~auctions.models.MCPCall` row per message that gets an answer, written after the answer
is built and never allowed to break it. The site sees an agent's calls, never its conversation, so
call outcomes are diagnostics, not task-success labels. Unknown tools and refusals are failures;
clarification, empty searches and repeated calls need interpretation in the client conversation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import Counter, defaultdict
from datetime import timedelta
from functools import lru_cache
from typing import Any

from django.utils import timezone

logger = logging.getLogger(__name__)

#: Methods not worth a row: liveness, and what every session sends before it does anything.
_NOT_RECORDED = frozenset({"ping", "notifications/initialized"})

#: The most tool calls one report reads.
MAX_CALLS = 5000

#: An activity grouping heuristic, not a host conversation identifier.
CONVERSATION_GAP = timedelta(minutes=15)

FAILURE_OUTCOMES = frozenset({"error", "unknown_tool", "bad_request"})


@lru_cache(maxsize=1)
def catalog_version() -> str:
    """Fingerprint this process's published definitions, not the client's possibly cached copy."""
    from . import protocol, tools

    catalog = {"instructions": protocol.INSTRUCTIONS, "tools": tools.tool_descriptors(None)}
    return hashlib.sha256(json.dumps(catalog, sort_keys=True).encode()).hexdigest()[:16]


def result_metadata(answer: dict[str, Any]) -> dict[str, Any]:
    """Bounded result facts for search diagnostics; no free text, contact details, URLs or images.

    Arguments already record the redacted search and filters. Retain result order and identifiers
    alongside them so a zero-result search and an irrelevant result set can be investigated.
    """
    payload = (answer.get("result") or {}).get("structuredContent") or {}
    facts = {}
    for key in (
        "found",
        "count",
        "total",
        "more",
        "showing",
        "offset",
        "next_offset",
        "next_line",
        "navigation_performed",
        "operation_completed",
    ):
        value = payload.get(key)
        if isinstance(value, (bool, int)):
            facts[key] = value
    for key in ("status", "error_code"):
        value = payload.get(key)
        if isinstance(value, str) and value.replace("_", "").isalnum() and len(value) <= 40:
            facts[key] = value
    identifiers = ("id", "lot_number", "request_id", "document", "page", "bidder_number")
    groups = {}
    for key in ("lots", "people", "pages", "results", "documents", "requests", "help", "paths", "in_the_code"):
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        # Only integer IDs and catalog page keys. Other strings may be user-authored names.
        selected = []
        for row in rows[:20]:
            if not isinstance(row, dict):
                continue
            item = {name: row[name] for name in identifiers if type(row.get(name)) is int}
            for name in ("lot_number", "bidder_number"):
                value = row.get(name)
                if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 9:
                    item[name] = value
            page = row.get("page")
            if isinstance(page, str):
                from auctions import palette_routes

                if palette_routes.get_route(page):
                    item["page"] = page
            selected.append(item)
        groups[key] = {"returned": len(rows), "first_results": selected}
    if groups:
        facts["results"] = groups
    return facts


def _connection(credential) -> str:
    token = getattr(credential, "token", None)
    if credential.kind == "oauth":
        return f"oauth-{getattr(token, 'application_id', '') or ''}"
    return f"key-{getattr(token, 'pk', '') or ''}"


def _text_of(result: dict[str, Any]) -> str:
    return " ".join(block.get("text", "") for block in result.get("content") or [] if isinstance(block, dict)).strip()


def outcome_of(method: str, answer: dict[str, Any]) -> tuple[str, str, int]:
    """``(outcome, detail, result_chars)`` for one JSON-RPC answer."""
    from auctions.models import MCPCall

    if "error" in answer:
        message = str((answer.get("error") or {}).get("message") or "")
        if method == "tools/call" and message.startswith("There is no tool"):
            return MCPCall.UNKNOWN_TOOL, message, 0
        return MCPCall.BAD_REQUEST, message, 0
    result = answer.get("result") or {}
    # Both model-visible channels count, including ChatGPT's summary-only text. Exclude image bytes.
    size = len(_text_of(result))
    if isinstance(result.get("structuredContent"), dict):
        size += len(json.dumps(result["structuredContent"], ensure_ascii=False, separators=(",", ":")))
    if method != "tools/call":
        return MCPCall.OK, "", size
    structured = result.get("structuredContent") or {}
    if result.get("isError"):
        return MCPCall.ERROR, _text_of(result), size
    if structured.get("status") == "needs_more_information":
        return MCPCall.NEEDS_INFO, str(structured.get("question") or ""), size
    if structured.get("found") is False or structured.get("count") == 0 or structured.get("total") == 0:
        return MCPCall.EMPTY, str(structured.get("summary") or ""), size
    return MCPCall.OK, "", size


def record(
    request, credential, message: Any, answer: dict[str, Any] | None, started: float, admin: bool = False
) -> None:
    """Write one row for this message and its answer. Never raises."""
    if answer is None or not isinstance(message, dict):
        return
    method = str(message.get("method") or "")
    if not method or method in _NOT_RECORDED:
        return
    try:
        from auctions.models import MCPCall

        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        tool = str(params.get("name") or "") if method == "tools/call" else ""
        arguments = params.get("arguments") if method == "tools/call" else None
        outcome, detail, size = outcome_of(method, answer)
        # Contact details out before anything is cut, so a cut can't leave half an address behind.
        from .admin import redact

        MCPCall.objects.create(
            user=credential.user,
            client=credential.label[:100],
            connection=_connection(credential)[:40],
            method=method[:40],
            tool=tool[:60],
            arguments=redact(json.dumps(arguments, ensure_ascii=False, default=str))[:600] if arguments else "",
            outcome=outcome,
            detail=redact(detail)[:300],
            result_chars=size,
            catalog_version=catalog_version(),
            result_metadata=result_metadata(answer),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            admin=admin,
        )
    except Exception:
        logger.warning("Couldn't record an MCP call", exc_info=True)


def _rate(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "0%"


def report(days: int = 7, conversations: int = 20) -> dict[str, Any]:
    """What agents asked for over ``days``, and how often it went wrong, for the weekly review.

    People are numbered rather than named, and what they or their agents wrote was stored without
    contact details; the reader is an agent that also reads strangers' text and may write to a public
    repository, so :func:`auctions.mcp.admin.mcp_usage` fences it as well. ``/mcp/admin/`` calls are
    left out: the owner's routines are in :func:`admin_report`.
    """
    from auctions.models import LLMUsage, MCPCall

    since = timezone.now() - timedelta(days=days)
    recent = MCPCall.objects.filter(createdon__gte=since, admin=False)
    # The newest MAX_CALLS, oldest first: enough for a week's review, bounded for a busy quarter.
    calls = list(
        recent.filter(method="tools/call")
        .order_by("-createdon")
        .values(
            "user_id",
            "client",
            "connection",
            "tool",
            "arguments",
            "outcome",
            "detail",
            "createdon",
            "catalog_version",
            "result_metadata",
        )[:MAX_CALLS]
    )[::-1]
    people: dict[Any, str] = {}

    def who(user_id) -> str:
        return people.setdefault(user_id, f"person {len(people) + 1}")

    by_client: dict[str, Counter] = defaultdict(Counter)
    client_people: dict[str, set] = defaultdict(set)
    for row in calls:
        by_client[row["client"] or "unknown"][row["outcome"]] += 1
        client_people[row["client"] or "unknown"].add(row["user_id"])
    clients = [
        {
            "client": client,
            "calls": sum(counts.values()),
            "people": len(client_people[client]),
            "went_wrong": _rate(sum(counts[outcome] for outcome in FAILURE_OUTCOMES), sum(counts.values())),
            "outcomes": dict(counts),
        }
        for client, counts in sorted(by_client.items(), key=lambda item: -sum(item[1].values()))
    ]
    by_tool: dict[str, Counter] = defaultdict(Counter)
    for row in calls:
        by_tool[row["tool"]][row["outcome"]] += 1
    tools = [
        {"tool": tool, "calls": sum(counts.values()), "outcomes": dict(counts)}
        for tool, counts in sorted(by_tool.items(), key=lambda item: -sum(item[1].values()))
    ]
    problems = Counter(
        (row["tool"], row["outcome"], row["detail"][:160]) for row in calls if row["outcome"] in FAILURE_OUTCOMES
    )
    # Conversations: one connection's calls with no gap longer than CONVERSATION_GAP, newest last.
    stories: list[dict[str, Any]] = []
    open_story: dict[str, dict[str, Any]] = {}
    for row in calls:
        key = f"{row['user_id']}:{row['connection']}"
        story = open_story.get(key)
        if story is None or row["createdon"] - story["_last"] > CONVERSATION_GAP:
            story = {"who": who(row["user_id"]), "client": row["client"], "started": row["createdon"], "calls": []}
            stories.append(story)
            open_story[key] = story
        story["_last"] = row["createdon"]
        step = f"{row['tool']}({row['arguments'][:120]})"
        if row["outcome"] != MCPCall.OK:
            step += f" -> {row['outcome']}: {row['detail'][:120]}"
        story["calls"].append(step)
    for story in stories:
        story.pop("_last", None)
        story["started"] = timezone.localtime(story["started"]).strftime("%a %d %b %H:%M")
    asked = LLMUsage.objects.filter(createdon__gte=since, tools_offered="ai")
    ai_requests = asked.exclude(request_id="").values("request_id").distinct().count()
    ai_page = {
        "questions": ai_requests,
        "people": asked.values("user_id").distinct().count(),
        "marked_not_helpful": list(
            asked.filter(reported=True).order_by("-createdon").values_list("query", flat=True)[:20]
        ),
        "failed": asked.filter(success=False).count(),
    }
    from auctions.models import AssistantSkillRequest

    # What the agents themselves said they couldn't do (request_feature), named but never who asked.
    wanted = list(
        AssistantSkillRequest.objects.filter(createdon__gte=since)
        .order_by("-createdon")
        .values_list("skill", flat=True)[:20]
    )
    summary = (
        f"{len(calls)} tool calls from {len({row['user_id'] for row in calls})} people in {days} days; "
        f"{sum(1 for row in calls if row['outcome'] in FAILURE_OUTCOMES)} failed. "
        f"/ai/: {ai_requests} questions, {len(ai_page['marked_not_helpful'])} marked not helpful."
    )
    return {
        "summary": summary,
        "days": days,
        "by_client": clients,
        "by_tool": tools[:40],
        "what_went_wrong": [
            {"tool": tool, "outcome": outcome, "detail": detail, "times": times}
            for (tool, outcome, detail), times in problems.most_common(30)
        ],
        "sessions_opened": recent.filter(method="initialize").count(),
        "calls_read": len(calls),
        "catalog_versions": dict(Counter(row["catalog_version"] or "unversioned" for row in calls)),
        "measurement_limits": (
            "Calls only: no host discovery searches, uncalled tools or final answers. "
            "Empty results and clarification are separate outcomes, not failures. "
            "Recent conversations are inferred activity groups separated by 15 minutes. "
            "Catalog versions describe server definitions, not the host's cached tool list."
        ),
        "recent_searches": [
            {
                "tool": row["tool"],
                "arguments": row["arguments"],
                "outcome": row["outcome"],
                "result": row["result_metadata"],
                "catalog_version": row["catalog_version"],
            }
            for row in calls
            if row["tool"].startswith(("find_", "search_", "list_"))
            or row["tool"] in {"read_source", "go_to_page", "open_lot_search"}
        ][-20:],
        "recent_conversations": stories[-conversations:],
        "ai_page": ai_page,
        "skills_asked_for": wanted,
    }


def admin_report(days: int = 7) -> dict[str, Any]:
    """What the owner's own agents read on ``/mcp/admin/``: per tool, how often, how it went, and what it
    cost in characters of answer, the biggest first, so the routines' token spend has names on it."""
    from django.db.models import Avg, Count, Max, Q, Sum

    from auctions.models import MCPCall

    since = timezone.now() - timedelta(days=days)
    calls = MCPCall.objects.filter(createdon__gte=since, admin=True, method="tools/call")
    rows = list(
        calls.values("tool")
        .annotate(
            calls=Count("pk"),
            chars=Sum("result_chars"),
            biggest=Max("result_chars"),
            average_ms=Avg("elapsed_ms"),
            went_wrong=Count("pk", filter=Q(outcome__in=FAILURE_OUTCOMES)),
        )
        .order_by("-chars")
    )
    total = sum(row["chars"] or 0 for row in rows)
    tools = [
        {
            "tool": row["tool"],
            "calls": row["calls"],
            "chars": row["chars"] or 0,
            "average_chars": round((row["chars"] or 0) / row["calls"]) if row["calls"] else 0,
            "biggest_chars": row["biggest"] or 0,
            "average_ms": round(row["average_ms"] or 0),
            "went_wrong": row["went_wrong"],
        }
        for row in rows[:40]
    ]
    return {"days": days, "calls": sum(row["calls"] for row in rows), "chars": total, "by_tool": tools}
