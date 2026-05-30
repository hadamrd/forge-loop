"""Fixture helpers for replay sessions."""

from __future__ import annotations

import re
from typing import Any

_DIFF_HEAD_RE = re.compile(r"^(?:diff --git |--- |\+\+\+ |@@ )")


def extract_diff_from_events(events: list[dict[str, Any]]) -> tuple[str, str | None]:
    """Best-effort diff + commit-hash extraction from recorded SDK events."""
    best_diff = ""
    commit_hash: str | None = None

    for event in events:
        msg = event.get("message") or {}
        content_items = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content_items, list):
            continue
        for item in content_items:
            if not isinstance(item, dict) or item.get("type") != "tool_result":
                continue
            text = _tool_result_text(item.get("content"))
            if not text:
                continue

            best_diff = _longest_diff_block(text, best_diff)
            if commit_hash is None:
                match = re.search(r"\b([0-9a-f]{40})\b", text)
                if match:
                    commit_hash = match.group(1)

    return best_diff, commit_hash


def _tool_result_text(raw: object) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in raw)
    return ""


def _longest_diff_block(text: str, current_best: str) -> str:
    lines = text.splitlines()
    best = current_best
    run_start: int | None = None
    for index, line in enumerate(lines):
        if _is_diff_line(line, active=run_start is not None):
            if run_start is None:
                run_start = index
        elif run_start is not None:
            best = _pick_longer_diff(best, "\n".join(lines[run_start:index]))
            run_start = None
    if run_start is not None:
        best = _pick_longer_diff(best, "\n".join(lines[run_start:]))
    return best


def _is_diff_line(line: str, *, active: bool) -> bool:
    return bool(
        _DIFF_HEAD_RE.match(line)
        or (active and (line.startswith(("+", "-", " ", "@")) or line == ""))
    )


def _pick_longer_diff(best: str, candidate: str) -> str:
    if "diff --git " in candidate and len(candidate) > len(best):
        return candidate
    return best
