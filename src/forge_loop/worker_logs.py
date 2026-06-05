"""Parse per-worker stream-json logs (#63).

Workers write JSONL events to ``<logs_dir>/worker-<issue>-<unix_ts>.log``.
Each line is a dict with at least a ``kind`` discriminant (set in
``_worker_sdk`` and ``worker``): ``turn_start``, ``assistant_text``,
``tool_use``, ``tool_result``, ``final_result``, ``error``, etc.

This module is shared by the MCP ``worker_logs`` tool and any other
consumer that wants to introspect per-attempt traces without shelling
out to ``tail`` + ``python -c``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from forge_loop.events import read_events

# Per-row payload truncation (bytes/chars). Big tool_use inputs (e.g. a 5KB
# file write) and tool_result content blow up the context window when a
# debugger asks for ``tail=50`` — truncate inline so the tool stays usable.
_PAYLOAD_TRUNC = 500
_ELLIPSIS = "…[truncated]"


def find_worker_logs(logs_dir: Path, issue: int) -> list[Path]:
    """Return all ``worker-<issue>-*.log`` files, newest first (by mtime).

    Newest-first ordering means ``logs[0]`` is the most-recent attempt,
    ``logs[1]`` is the previous attempt, etc. Empty list if none exist
    (worker still in flight before its first emit, or no attempt yet).
    """
    if not logs_dir.exists():
        return []
    matches = list(logs_dir.glob(f"worker-{issue}-*.log"))
    # Sort by mtime, newest first. Tie-break on filename (stable).
    matches.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    return matches


def _truncate(value: Any, limit: int = _PAYLOAD_TRUNC) -> Any:
    """Truncate a string-ish payload to ``limit`` chars + ellipsis marker.

    Dicts/lists are JSON-serialised first so a giant tool_use input
    (e.g. ``{"file_text": "<5KB>"}``) collapses to a single short string
    rather than recursively walking the structure.
    """
    if value is None:
        return None
    if isinstance(value, str):
        if len(value) > limit:
            return value[:limit] + _ELLIPSIS
        return value
    # Non-string (dict, list, int, ...) — stringify, then truncate.
    try:
        serialised = json.dumps(value, default=str)
    except (TypeError, ValueError):
        serialised = str(value)
    if len(serialised) > limit:
        return serialised[:limit] + _ELLIPSIS
    return serialised


def _strip_large_payloads(row: dict[str, Any]) -> dict[str, Any]:
    """In-place-ish truncation of the well-known fat fields.

    Per spec: ``tool_use.input`` and ``tool_result.content`` are the
    big offenders. Returns a NEW dict so the caller's parsed-line dict
    is not mutated.
    """
    kind = row.get("kind")
    if kind == "tool_use" and "input" in row:
        out = dict(row)
        out["input"] = _truncate(row["input"])
        return out
    if kind == "tool_result" and "content" in row:
        out = dict(row)
        out["content"] = _truncate(row["content"])
        return out
    return row


def parse_worker_log(
    log_path: Path,
    kind_filter: str | None = None,
    tail: int = 50,
) -> list[dict[str, Any]]:
    """Read ``log_path`` line-by-line, JSON-decode, filter, tail, truncate.

    Bad/blank lines are silently skipped (workers occasionally write
    partial buffers when killed mid-flush). The result is the LAST
    ``tail`` matching rows in chronological order (oldest → newest).
    """
    if not log_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for ev in read_events(log_path):
        if kind_filter is not None and ev.get("kind") != kind_filter:
            continue
        rows.append(_strip_large_payloads(ev))
    if tail is not None and tail >= 0:
        rows = rows[-tail:]
    return rows


def read_worker_logs(
    logs_dir: Path,
    issue: int,
    kind_filter: str | None = None,
    tail: int = 50,
    attempt: int | None = None,
) -> list[dict[str, Any]]:
    """Locate + parse the Nth-most-recent worker log for ``issue``.

    ``attempt=None`` (default) or ``attempt=1`` → most-recent log.
    ``attempt=2`` → previous (second-most-recent). Etc.

    Returns ``[]`` (NOT an error dict) when no such log exists — that
    is the legitimate "worker still in flight, hasn't emitted yet"
    case and callers shouldn't have to special-case it.
    """
    logs = find_worker_logs(logs_dir, issue)
    if not logs:
        return []
    idx = 0 if attempt is None else max(0, int(attempt) - 1)
    if idx >= len(logs):
        return []
    return parse_worker_log(logs[idx], kind_filter=kind_filter, tail=tail)
