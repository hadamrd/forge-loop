"""Control-plane health summary for operator status output."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forge_loop.control.boot import BootContext
from forge_loop.frontier import FrontierCursor, FrontierStore
from forge_loop.memory import SqliteMemoryStore
from forge_loop.worker_sessions import WorkerSessionStore, recoverable_sessions


def collect_control_plane_status(
    repo: Path,
    now: datetime,
    *,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Return durable control-plane health for ``forge-loop status --json``."""

    forge_dir = repo / ".forge"
    runner_state_dir = state_dir or repo / "docs" / "ops"
    event_log_path = forge_dir / "events.db"
    frontier_path = forge_dir / "frontier.yaml"
    memory_path = forge_dir / "memory.db"
    tasks_path = runner_state_dir / "worker-sessions.db"

    event_log, projections, last_sequence = _event_log_status(event_log_path)
    frontier, frontier_cursor = _frontier_status(frontier_path)
    memory, active_memory_ids = _memory_status(memory_path)
    tasks, in_flight_task_ids = _tasks_status(tasks_path, now)
    boot = _boot_status(
        event_log_available=bool(event_log["available"]),
        frontier_cursor=frontier_cursor,
        memory_available=bool(memory["available"]),
        tasks_available=bool(tasks["available"]),
        active_memory_ids=active_memory_ids,
        in_flight_task_ids=in_flight_task_ids,
        last_sequence=last_sequence,
    )

    return {
        "event_log": event_log,
        "projections": projections,
        "frontier": frontier,
        "memory": memory,
        "tasks": tasks,
        "boot": boot,
    }


def _event_log_status(path: Path) -> tuple[dict[str, Any], dict[str, Any], int | None]:
    if not path.exists():
        return _unavailable_event_log(path), {}, None

    try:
        with sqlite3.connect(path) as connection:
            last_sequence = _last_event_sequence(connection)
            projections = _projection_status(connection, last_sequence)
    except sqlite3.Error as exc:
        status = _unavailable_event_log(path)
        status["error"] = str(exc)
        return status, {}, None

    return (
        {
            "available": True,
            "path": str(path),
            "last_sequence": last_sequence,
        },
        projections,
        last_sequence,
    )


def _unavailable_event_log(path: Path) -> dict[str, Any]:
    return {
        "available": False,
        "path": str(path),
        "last_sequence": None,
    }


def _last_event_sequence(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COALESCE(MAX(sequence), 0) FROM events").fetchone()
    return int(row[0]) if row is not None else 0


def _projection_status(connection: sqlite3.Connection, last_sequence: int) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT projection_name, sequence FROM projection_cursors ORDER BY projection_name"
    ).fetchall()
    return {
        str(name): {
            "sequence": int(sequence),
            "lag": max(0, last_sequence - int(sequence)),
        }
        for name, sequence in rows
    }


def _frontier_status(path: Path) -> tuple[dict[str, Any], FrontierCursor | None]:
    if not path.exists():
        return _unavailable_frontier(path), None

    try:
        cursor = FrontierStore(path).load()
    except (OSError, ValueError) as exc:
        status = _unavailable_frontier(path)
        status["error"] = str(exc)
        return status, None

    return (
        {
            "available": True,
            "path": str(path),
            "current_problem": cursor.current_problem,
            "next_expansion": cursor.next_expansion,
        },
        cursor,
    )


def _unavailable_frontier(path: Path) -> dict[str, Any]:
    return {
        "available": False,
        "path": str(path),
        "current_problem": None,
        "next_expansion": None,
    }


def _memory_status(path: Path) -> tuple[dict[str, Any], tuple[str, ...]]:
    if not path.exists():
        return _unavailable_memory(path), ()

    try:
        store = SqliteMemoryStore(path)
        active = store.list_active()
        rejected = store.list_rejected_paths()
    except (OSError, sqlite3.Error, ValueError) as exc:
        status = _unavailable_memory(path)
        status["error"] = str(exc)
        return status, ()

    return (
        {
            "available": True,
            "path": str(path),
            "active_count": len(active),
            "rejected_count": len(rejected),
        },
        tuple(item.memory_id for item in active),
    )


def _unavailable_memory(path: Path) -> dict[str, Any]:
    return {
        "available": False,
        "path": str(path),
        "active_count": None,
        "rejected_count": None,
    }


def _tasks_status(path: Path, now: datetime) -> tuple[dict[str, Any], tuple[str, ...]]:
    if not path.exists():
        return _unavailable_tasks(path), ()

    try:
        store = WorkerSessionStore(path)
        sessions = list(recoverable_sessions(store))
        store.close()
    except (OSError, sqlite3.Error, ValueError) as exc:
        status = _unavailable_tasks(path)
        status["error"] = str(exc)
        return status, ()

    in_flight_ids = tuple(session.session_id for session in sessions)
    stale_lease_count = sum(
        1
        for session in sessions
        if (lease_expires_at := _parse_datetime(session.lease_expires_at)) is not None
        and lease_expires_at < now
    )

    return (
        {
            "available": True,
            "path": str(path),
            "in_flight_count": len(sessions),
            "stale_lease_count": stale_lease_count,
        },
        in_flight_ids,
    )


def _unavailable_tasks(path: Path) -> dict[str, Any]:
    return {
        "available": False,
        "path": str(path),
        "in_flight_count": None,
        "stale_lease_count": None,
    }


def _parse_datetime(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _boot_status(
    *,
    event_log_available: bool,
    frontier_cursor: FrontierCursor | None,
    memory_available: bool,
    tasks_available: bool,
    active_memory_ids: tuple[str, ...],
    in_flight_task_ids: tuple[str, ...],
    last_sequence: int | None,
) -> dict[str, Any]:
    if (
        not event_log_available
        or frontier_cursor is None
        or not memory_available
        or not tasks_available
        or last_sequence is None
    ):
        return {"available": False, "summary": None}

    context = BootContext(
        frontier=frontier_cursor,
        active_memory_ids=active_memory_ids,
        in_flight_task_ids=in_flight_task_ids,
        last_event_sequence=last_sequence,
    )
    return {"available": True, "summary": context.summary()}
