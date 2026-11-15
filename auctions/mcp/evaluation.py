"""Export a labeled MCP prompt set and score recorded client runs without executing any tools.

Run each prompt in a fresh ChatGPT or Claude conversation against a disposable account. Repeat
with competing connectors enabled and after longer conversations. Capture canonical tool names
in order; label ``completed`` and ``navigation_worked`` from the observed outcome, not a tool's
success flag. Server logs cannot supply no-call failures or the assistant's final response.

``manage.py mcp_eval`` exports cases; ``--results runs.jsonl`` scores one JSON object per run:
``{"case_id":"source_debug", "client":"ChatGPT", "variant":"before", "tools":["read_source"], "completed":true}``.
Use variant to identify model, catalog revision and connector configuration, so before/after runs
do not get mixed. This measures selection separately from human-labeled completion, and never calls a paid model
or performs the writes described in the prompts. Use staging for those writes.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

CASES_PATH = Path(__file__).with_name("eval_cases.json")


def cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text())


def score(runs: list[dict]) -> dict:
    """Score every repetition; an absent completion label is unknown, not a success."""
    expected = {case["id"]: case for case in cases()}
    groups = defaultdict(list)
    for run in runs:
        if not isinstance(run, dict) or run.get("case_id") not in expected:
            msg = "Every run needs a known case_id."
            raise ValueError(msg)
        called = run.get("tools")
        if not isinstance(called, list) or not all(isinstance(tool, str) for tool in called):
            msg = "tools must be an ordered list of canonical tool names (an empty list records no call)."
            raise ValueError(msg)
        for flag in ("completed", "navigation_worked"):
            if flag in run and type(run[flag]) is not bool:
                msg = f"{flag} must be true or false when supplied."
                raise ValueError(msg)
        case = expected[run["case_id"]]
        required = case["expected_tools"]
        relevant = [
            name for name in called if name in required or name not in case.get("allowed_preflight", ["my_context"])
        ]
        first_correct = bool(relevant and relevant[0] in required) if required else not relevant
        group = (str(run.get("client") or "unknown"), str(run.get("variant") or "unspecified"))
        groups[group].append(
            {
                "case_id": case["id"],
                "first_tool_correct": first_correct,
                "expected_tools_called": all(name in called for name in required),
                "forbidden_tool_called": bool(set(called) & set(case.get("forbidden_tools", []))),
                "calls": len(called),
                "within_call_budget": len(called) <= case["max_calls"],
                "completed": run.get("completed"),
                "navigation_worked": run.get("navigation_worked"),
            }
        )
    clients = []
    for (client, variant), results in groups.items():
        labeled = [row for row in results if row["completed"] is not None]
        clients.append(
            {
                "client": client,
                "variant": variant,
                "runs": len(results),
                "cases_covered": len({row["case_id"] for row in results}),
                "first_tool_correct": sum(row["first_tool_correct"] for row in results),
                "within_call_budget": sum(row["within_call_budget"] for row in results),
                "navigation_labels": sum(row["navigation_worked"] is not None for row in results),
                "navigation_worked": sum(row["navigation_worked"] is True for row in results),
                "forbidden_tool_calls": sum(row["forbidden_tool_called"] for row in results),
                "completion_labels": len(labeled),
                "completed": sum(row["completed"] for row in labeled),
                "runs_detail": results,
            }
        )
    return {"cases_available": len(expected), "clients": clients}
