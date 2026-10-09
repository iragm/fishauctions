"""Crashes the mobile app reports about itself: storing them, grouping them into bugs, and reading them back.

Neither store gives an agent its crash reports (Apple has no API for them; Play's needs a service
account), so the app sends its own to ``POST /api/mobile/crashes/``. A row is one crash; its
:func:`fingerprint` names the bug, so the same fault on a hundred phones, or in two app versions whose
line numbers moved, is one group. :func:`groups` is what ``list_app_crashes`` on ``/mcp/admin/`` returns
and :func:`recent_count` what ``site_health`` counts.

What a phone sends is untrusted: anybody can post here. The reader fences every field a phone wrote.
"""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from typing import Any

from django.db.models import Count, Max, Min
from django.utils import timezone

MESSAGE_CHARS = 2000
STACK_CHARS = 20000
#: Frames that name the bug. More, and an inlined helper changing splits one bug in two.
FINGERPRINT_FRAMES = 5

_DART_FRAME = re.compile(r"^#\d+\s+(?P<where>.+?)\s+\((?P<file>[^)]*?)(?::\d+)*\)\s*$")
_ADDRESS = re.compile(r"0x[0-9a-fA-F]+|\b[0-9a-fA-F]{8,}\b")
_NUMBER = re.compile(r"\d+")


def _frames(stack: str) -> list[str]:
    """The stack's frames with what changes between builds (line numbers, addresses, offsets) taken out."""
    frames = []
    for raw in stack.splitlines():
        line = raw.strip()
        if not line or line.startswith(("<asynchronous suspension>", "...")):
            continue
        dart = _DART_FRAME.match(line)
        if dart:
            frames.append(" ".join(_ADDRESS.sub("", f"{dart['where']} ({dart['file']})").split()))
            continue
        line = _ADDRESS.sub("", line)
        line = re.sub(r":\d+\)", ")", line)  # Java "(File.kt:42)"
        line = _NUMBER.sub("", line) if not line.startswith("at ") else line
        line = " ".join(line.split())
        if line:
            frames.append(line)
    return frames


def error_type(message: str) -> str:
    """``StateError`` from ``StateError: Bad state: no element``; the first line, numbers out, otherwise."""
    first = (message or "").strip().splitlines()[0] if (message or "").strip() else ""
    head = first.split(":", 1)[0].strip()
    if head and len(head) <= 80 and " " not in head:
        return head
    return _NUMBER.sub("#", first)[:120]


def fingerprint(kind: str, platform: str, message: str, stack: str) -> str:
    """One bug's id. Dart errors are the same code on both platforms, so platform only splits native ones."""
    where = "" if kind == "dart" else platform
    parts = [kind, where, error_type(message), *_frames(stack)[:FINGERPRINT_FRAMES]]
    return hashlib.sha1("\n".join(parts).encode("utf-8", "replace"), usedforsecurity=False).hexdigest()


def record(user, report: dict[str, Any]):
    """Store one validated report from the app."""
    from auctions.models import AppCrash

    message = (report.get("message") or "")[:MESSAGE_CHARS]
    stack = (report.get("stack") or "")[:STACK_CHARS]
    return AppCrash.objects.create(
        fingerprint=fingerprint(report["kind"], report["platform"], message, stack),
        kind=report["kind"],
        platform=report["platform"],
        fatal=report.get("fatal", True),
        app_version=report.get("app_version", "")[:40],
        os_version=report.get("os_version", "")[:100],
        device=report.get("device", "")[:100],
        message=message,
        stack=stack,
        user=user if getattr(user, "is_authenticated", False) else None,
        occurred_at=report.get("occurred_at"),
    )


def recent_count(hours: int = 24) -> dict[str, int]:
    """Crashes received in the last ``hours`` and how many distinct bugs they are."""
    from auctions.models import AppCrash

    rows = AppCrash.objects.filter(createdon__gte=timezone.now() - timedelta(hours=hours))
    return {"crashes": rows.count(), "bugs": rows.values("fingerprint").distinct().order_by().count()}


def groups(days: int = 7, platform: str = "", fingerprint_prefix: str = "", limit: int = 20) -> list[dict[str, Any]]:
    """The bugs seen in the last ``days``, most recently seen first, each with its newest crash's text.

    Untrusted text is fenced by the caller, which knows its surface.
    """
    from auctions.models import AppCrash

    rows = AppCrash.objects.filter(createdon__gte=timezone.now() - timedelta(days=days))
    if platform:
        rows = rows.filter(platform=platform)
    if fingerprint_prefix:
        rows = rows.filter(fingerprint__startswith=fingerprint_prefix)
    summary = (
        rows.values("fingerprint")
        .annotate(
            times=Count("id"),
            people=Count("user", distinct=True),
            first_seen=Min("createdon"),
            last_seen=Max("createdon"),
        )
        .order_by("-last_seen")[:limit]
    )
    found = []
    for group in summary:
        mine = rows.filter(fingerprint=group["fingerprint"])
        newest = mine.order_by("-createdon").first()
        found.append(
            {
                **group,
                "newest": newest,
                "platforms": sorted(set(mine.values_list("platform", flat=True))),
                "app_versions": sorted(set(mine.values_list("app_version", flat=True)) - {""}),
            }
        )
    return found
