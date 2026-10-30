"""Recording what agents ask ``/mcp/`` for, and reading it back for ``mcp_usage`` on ``/mcp/admin/``.

One :class:`~auctions.models.MCPCall` row per message that gets an answer, written after the answer
is built and never allowed to break it. The site sees an agent's calls, never its conversation, so
"did they get a good answer" is read off the calls: a tool name that doesn't exist, a refused call, a
question asked back, a find that found nothing, or the same tool called again and again.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any

from django.utils import timezone

logger = logging.getLogger(__name__)

#: Methods not worth a row: liveness, and what every session sends before it does anything.
_NOT_RECORDED = frozenset({"ping", "notifications/initialized"})

#: The most tool calls one report reads.
MAX_CALLS = 5000

#: Calls by one connection this close together are one conversation.
CONVERSATION_GAP = timedelta(minutes=15)


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
    # The text blocks, not a second serialisation of everything: a scan's pictures are megabytes.
    size = len(_text_of(result))
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


def record(request, credential, message: Any, answer: dict[str, Any] | None, started: float) -> None:
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
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception:
        logger.warning("Couldn't record an MCP call", exc_info=True)


def _rate(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "0%"


def report(days: int = 7, conversations: int = 20) -> dict[str, Any]:
    """What agents asked for over ``days``, and how often it went wrong, for the weekly review.

    People are numbered rather than named, and what they or their agents wrote was stored without
    contact details; the reader is an agent that also reads strangers' text and may write to a public
    repository, so :func:`auctions.mcp.admin.mcp_usage` fences it as well. ``/mcp/admin/`` is never recorded, so the owner's routines aren't here.
    """
    from auctions.models import LLMUsage, MCPCall

    since = timezone.now() - timedelta(days=days)
    recent = MCPCall.objects.filter(createdon__gte=since)
    # The newest MAX_CALLS, oldest first: enough for a week's review, bounded for a busy quarter.
    calls = list(
        recent.filter(method="tools/call")
        .order_by("-createdon")
        .values("user_id", "client", "connection", "tool", "arguments", "outcome", "detail", "createdon")[:MAX_CALLS]
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
            "went_wrong": _rate(sum(counts.values()) - counts["ok"], sum(counts.values())),
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
        (row["tool"], row["outcome"], row["detail"][:160]) for row in calls if row["outcome"] != MCPCall.OK
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

    # What the agents themselves said they couldn't do (request_a_skill), named but never who asked.
    wanted = list(
        AssistantSkillRequest.objects.filter(createdon__gte=since)
        .order_by("-createdon")
        .values_list("skill", flat=True)[:20]
    )
    summary = (
        f"{len(calls)} tool calls from {len({row['user_id'] for row in calls})} people in {days} days; "
        f"{sum(1 for row in calls if row['outcome'] != MCPCall.OK)} went wrong. "
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
        "recent_conversations": stories[-conversations:],
        "ai_page": ai_page,
        "skills_asked_for": wanted,
    }
