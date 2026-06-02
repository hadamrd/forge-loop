"""State + event persistence — JSON state file + append-only JSONL event log."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Default rotation threshold for the events log: 10 MiB. Overridable via the
# ``LOOP_EVENTS_ROTATE_BYTES`` env var so operators can tune it without a code
# change. Kept module-level so tests can monkeypatch it directly.
DEFAULT_ROTATE_BYTES = 10 * 1024 * 1024
# Number of archive files to keep (events.jsonl.1 .. events.jsonl.MAX_ARCHIVES).
MAX_ARCHIVES = 3


def _rotate_bytes_threshold() -> int:
    """Resolve via the unified Settings layer (issue #84).
    Was ``LOOP_EVENTS_ROTATE_BYTES`` env-only; now ``misc.events_rotate_bytes``.
    """
    try:
        from forge_loop.settings import Settings

        v = Settings.load().misc.events_rotate_bytes
        return v if v > 0 else DEFAULT_ROTATE_BYTES
    except Exception:  # noqa: BLE001
        return DEFAULT_ROTATE_BYTES


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def rotate_events_file_if_needed(
    events_path: Path,
    rotate_bytes: int | None = None,
    max_archives: int = MAX_ARCHIVES,
) -> dict[str, Any] | None:
    """Rotate ``events_path`` if its size meets/exceeds ``rotate_bytes``.

    Rotation scheme is a classic numbered cascade:
        events.jsonl.<max>  → unlinked
        events.jsonl.<max-1> → events.jsonl.<max>
        ...
        events.jsonl.1      → events.jsonl.2
        events.jsonl        → events.jsonl.1
    A fresh empty ``events.jsonl`` is then created and an
    ``events_file_rotated`` event is appended as its first line.

    On OSError at any step (permission denied, disk full, read-only target,
    etc.) the helper does NOT raise — it emits an ``events_rotation_failed``
    event best-effort (which may itself swallow OSError) and returns the
    failure payload. Callers should treat the return value as informational
    telemetry only; boot must continue regardless.

    Returns:
        ``None`` if no rotation was required, otherwise a dict describing
        the outcome (``rotated``: bool, ``rotated_size``: int,
        ``archive_count``: int, ``error``: str | None).
    """
    if rotate_bytes is None:
        rotate_bytes = _rotate_bytes_threshold()

    try:
        if not events_path.exists():
            return None
        size = events_path.stat().st_size
    except OSError as e:
        # Couldn't even stat — try to record this and bail.
        _try_append_event(events_path, "events_rotation_failed", error=str(e))
        return {"rotated": False, "rotated_size": 0, "archive_count": 0, "error": str(e)}

    if size < rotate_bytes:
        return None

    error: str | None = None
    archive_count = 0
    try:
        # Walk archives from highest down: unlink the oldest that would be
        # overflowed, then shift each existing archive up by one. Finally,
        # rename the live file to .1.
        oldest = events_path.with_suffix(events_path.suffix + f".{max_archives}")
        if oldest.exists() or oldest.is_symlink():
            with contextlib.suppress(FileNotFoundError):
                oldest.unlink()

        for n in range(max_archives - 1, 0, -1):
            src = events_path.with_suffix(events_path.suffix + f".{n}")
            dst = events_path.with_suffix(events_path.suffix + f".{n + 1}")
            if src.exists() or src.is_symlink():
                src.rename(dst)

        first_archive = events_path.with_suffix(events_path.suffix + ".1")
        events_path.rename(first_archive)

        # Create a fresh empty events file.
        events_path.touch()

        # Count surviving archives for the event payload.
        for n in range(1, max_archives + 1):
            if events_path.with_suffix(events_path.suffix + f".{n}").exists():
                archive_count += 1
    except OSError as e:
        error = str(e)
        # Best-effort: try to record the failure.
        _try_append_event(events_path, "events_rotation_failed", error=error, attempted_size=size)
        return {
            "rotated": False,
            "rotated_size": size,
            "archive_count": archive_count,
            "error": error,
        }

    # Success path: stamp the first event in the fresh file.
    _try_append_event(
        events_path,
        "events_file_rotated",
        rotated_size=size,
        archive_count=archive_count,
    )
    return {
        "rotated": True,
        "rotated_size": size,
        "archive_count": archive_count,
        "error": None,
    }


def _try_append_event(events_path: Path, kind: str, **fields: Any) -> None:
    """Best-effort append — swallow OSError so rotation never raises.

    If we can't even write the failure event, there's nothing more we can
    do without aborting boot — which the contract for #59 explicitly
    forbids.
    """
    with contextlib.suppress(OSError):
        append_event(events_path, kind, **fields)


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
    """Append a single JSON object as a line to the events log.

    Delegates to :func:`forge_loop.events.append_event_with_registry_check`
    so emissions of a ``kind`` for which a typed model already exists
    surface a DeprecationWarning pointing the caller at the typed path.
    Behaviour is unchanged for the unregistered kinds.
    """
    if "durable_mirror" not in fields:
        try:
            from forge_loop.eventlog.legacy_mirror import legacy_runner_mirror_for_events_path

            mirror = legacy_runner_mirror_for_events_path(events_path)
        except (OSError, sqlite3.Error) as exc:
            fields["durable_mirror_error"] = f"{type(exc).__name__}: {exc!s}"
        else:
            if mirror is not None:
                fields["durable_mirror"] = mirror
    from forge_loop.events import append_event_with_registry_check

    append_event_with_registry_check(events_path, kind, **fields)


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
        "failed": [
            o["issue"] for o in outcomes if o.get("status") in {"failed", "timeout", "no_pr"}
        ],
        "pr_urls": [o["pr_url"] for o in outcomes if o.get("pr_url")],
        # Carry up the most-interesting subagent events (e.g. "bug_found")
        "subagent_events_count": sum(len(o.get("events") or []) for o in outcomes),
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
