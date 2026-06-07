"""Control-plane health summary for operator status output."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from forge_loop.control.boot import BootContext, canonical_task_saga_path
from forge_loop.frontier import FrontierCursor, FrontierStore
from forge_loop.memory import SqliteMemoryStore
from forge_loop.tasks import SqliteTaskSagaStore


def collect_control_plane_status(
    repo: Path,
    now: datetime,
    *,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Return durable control-plane health for ``forge-loop status --json``.

    ``state_dir`` is accepted for caller compatibility (doctor passes it) but
    no longer locates task health: task in-flight / stale-lease facts are read
    from the canonical saga store at ``.forge/tasks.db`` — the same store the
    runner dispatch path, :func:`assemble_boot_context`, and ``forge-loop
    recover`` use — never the legacy ``worker-sessions.db`` (issue #373).
    """

    del state_dir  # legacy worker-sessions.db location is no longer consulted
    forge_dir = repo / ".forge"
    event_log_path = forge_dir / "events.db"
    frontier_path = forge_dir / "frontier.yaml"
    memory_path = forge_dir / "memory.db"
    tasks_path = canonical_task_saga_path(repo)

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
    """Read task health from the canonical saga store at ``.forge/tasks.db``.

    Uses the same APIs dispatch / boot / recover use — ``list_in_flight`` and
    ``list_stale(now=...)`` — so there is no reimplemented lease-expiry math and
    the operator's numbers match the store work is actually dispatched to.
    """

    if not path.exists():
        return _unavailable_tasks(path), ()

    try:
        store = SqliteTaskSagaStore(path)
        try:
            in_flight = store.list_in_flight()
            stale = store.list_stale(now=now)
        finally:
            store.close()
    except (OSError, sqlite3.Error, ValueError) as exc:
        status = _unavailable_tasks(path)
        status["error"] = str(exc)
        return status, ()

    # Boot summary consistency: the ids fed into ``_boot_status`` are the saga
    # task ids (``saga.task_id``), matching ``assemble_boot_context``.
    in_flight_task_ids = tuple(saga.task_id for saga in in_flight)

    return (
        {
            "available": True,
            "path": str(path),
            "in_flight_count": len(in_flight),
            "stale_lease_count": len(stale),
        },
        in_flight_task_ids,
    )


def _unavailable_tasks(path: Path) -> dict[str, Any]:
    return {
        "available": False,
        "path": str(path),
        "in_flight_count": None,
        "stale_lease_count": None,
    }


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
