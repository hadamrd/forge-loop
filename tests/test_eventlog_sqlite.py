import sqlite3
from pathlib import Path

from forge_loop.eventlog import EventKind, ProjectionCursor, SqliteEventLog


def test_sqlite_event_log_creates_schema(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    log = SqliteEventLog(db)

    event = log.append(
        EventKind.FRONTIER_ADVANCED,
        {"frontier": "wal"},
        task_id="task-1",
        saga_id="saga-1",
        idempotency_key="frontier:wal",
    )

    assert event.sequence == 1
    assert event.kind is EventKind.FRONTIER_ADVANCED
    assert event.payload["frontier"] == "wal"
    assert event.task_id == "task-1"
    assert event.saga_id == "saga-1"
    assert event.idempotency_key == "frontier:wal"
    assert db.exists()
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_sqlite_event_log_replays_after_reopen(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    SqliteEventLog(db).append(
        EventKind.DECISION_MADE,
        {"decision": "use sqlite wal"},
        task_id="task-1",
        saga_id="saga-1",
        idempotency_key="decision:sqlite",
    )

    reopened = SqliteEventLog(db)
    events = list(reopened.since(0))

    assert len(events) == 1
    assert events[0].sequence == 1
    assert events[0].kind is EventKind.DECISION_MADE
    assert events[0].payload["decision"] == "use sqlite wal"
    assert events[0].task_id == "task-1"
    assert events[0].saga_id == "saga-1"
    assert events[0].idempotency_key == "decision:sqlite"


def test_sqlite_event_log_returns_existing_event_for_duplicate_idempotency_key(
    tmp_path: Path,
) -> None:
    log = SqliteEventLog(tmp_path / "events.db")

    first = log.append(
        EventKind.TASK_DISPATCHED,
        {"task": "ship-m1"},
        task_id="task-1",
        saga_id="saga-1",
        idempotency_key="dispatch:task-1",
    )
    duplicate = log.append(
        EventKind.TASK_DISPATCHED,
        {"task": "ship-m1-again"},
        task_id="task-2",
        saga_id="saga-2",
        idempotency_key="dispatch:task-1",
    )

    assert duplicate == first
    assert [event.sequence for event in log.since(0)] == [1]


def test_projection_cursor_round_trips(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    log = SqliteEventLog(db)

    assert log.get_projection_cursor("frontier").sequence == 0

    log.set_projection_cursor("frontier", ProjectionCursor(sequence=42))

    reopened = SqliteEventLog(db)
    assert reopened.get_projection_cursor("frontier").sequence == 42
