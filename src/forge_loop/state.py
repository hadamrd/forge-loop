"""State + event persistence — JSON state file + append-only JSONL event log."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def write_state(path: Path, state: dict[str, Any]) -> None:
    """Atomically overwrite the state file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {"ts": now_iso(), **state}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(path)


def read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    result: dict[str, Any] = json.loads(path.read_text())
    return result


def append_event(events_path: Path, kind: str, **fields: Any) -> None:
    """Append a single JSON object as a line to the events log."""
    events_path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": now_iso(), "kind": kind, **fields}
    with open(events_path, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def tail_events(events_path: Path, n: int = 30) -> list[str]:
    if not events_path.exists():
        return []
    with open(events_path) as f:
        lines = f.readlines()
    return lines[-n:]


def consolidate_sprint(
    events_path: Path,
    summaries_path: Path,
    tick: int,
    outcomes: list[dict[str, Any]],
    keep_recent_events: int = 50,
) -> dict[str, Any]:
    """End-of-tick distillation: write a 1-line summary + rotate events to flush context.

    Writes one append-only line to ``summaries_path``:
        {ts, tick, merged: [...], failed: [...], total, pr_urls: [...]}

    Then truncates ``events_path`` to its last ``keep_recent_events`` lines so the
    next sprint doesn't see pollution from this one. Full historical events are
    available via the per-worker logs in ``loop-runner-logs/``.
    """
    summary = {
        "ts": now_iso(),
        "tick": tick,
        "total": len(outcomes),
        "merged": [o["issue"] for o in outcomes if o.get("status") == "merged"],
        "open": [o["issue"] for o in outcomes if o.get("status") == "open"],
        "failed": [o["issue"] for o in outcomes if o.get("status") in {"failed", "timeout", "no_pr"}],
        "pr_urls": [o["pr_url"] for o in outcomes if o.get("pr_url")],
        # Carry up the most-interesting subagent events (e.g. "bug_found")
        "subagent_events_count": sum(
            len(o.get("events") or []) for o in outcomes
        ),
    }
    summaries_path.parent.mkdir(parents=True, exist_ok=True)
    with open(summaries_path, "a") as f:
        f.write(json.dumps(summary, default=str) + "\n")

    if events_path.exists() and keep_recent_events > 0:
        lines = events_path.read_text().splitlines()
        if len(lines) > keep_recent_events:
            kept = lines[-keep_recent_events:]
            events_path.write_text("\n".join(kept) + "\n")

    return summary
