import gc
import sqlite3
from pathlib import Path

from forge_loop.eventlog import EventKind, ProjectionCursor, SqliteEventLog
from forge_loop.eventlog.sqlite import (
    GENESIS_CHAIN_HASH,
    canonical_event_fields,
    compute_chain_hash,
)


def _read_chain_rows(db: Path) -> list[sqlite3.Row]:
    connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            "SELECT sequence, chain_hash, event_id, kind, payload_json, "
            "schema_version, occurred_at, task_id, saga_id, idempotency_key "
            "FROM events ORDER BY sequence ASC"
        ).fetchall()
    finally:
        connection.close()


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


def test_append_links_chain_hashes_and_recompute_reproduces_them(tmp_path: Path) -> None:
    # #339: two appends produce linked hashes (event 2 chains from event 1) and
    # recomputing the chain from the stored rows reproduces the stored hashes.
    db = tmp_path / "events.db"
    log = SqliteEventLog(db)
    log.append(EventKind.FRONTIER_ADVANCED, {"frontier": "a"})
    log.append(EventKind.MEMORY_PROMOTED, {"memory_id": "m1"})
    del log
    gc.collect()

    rows = _read_chain_rows(db)
    assert len(rows) == 2
    assert rows[0]["chain_hash"] and rows[1]["chain_hash"]
    assert rows[0]["chain_hash"] != rows[1]["chain_hash"]

    # Recompute the whole chain from genesis; it must reproduce stored hashes.
    prev = GENESIS_CHAIN_HASH
    for row in rows:
        expected = compute_chain_hash(
            prev,
            canonical_event_fields(
                event_id=row["event_id"],
                kind=row["kind"],
                payload_json=row["payload_json"],
                schema_version=int(row["schema_version"]),
                occurred_at=row["occurred_at"],
                task_id=row["task_id"],
                saga_id=row["saga_id"],
                idempotency_key=row["idempotency_key"],
            ),
        )
        assert expected == row["chain_hash"]
        prev = row["chain_hash"]

    # Event 2's hash genuinely DEPENDS on event 1: recomputing it as if it were
    # the genesis event yields a different digest than the stored one.
    as_if_genesis = compute_chain_hash(
        GENESIS_CHAIN_HASH,
        canonical_event_fields(
            event_id=rows[1]["event_id"],
            kind=rows[1]["kind"],
            payload_json=rows[1]["payload_json"],
            schema_version=int(rows[1]["schema_version"]),
            occurred_at=rows[1]["occurred_at"],
            task_id=rows[1]["task_id"],
            saga_id=rows[1]["saga_id"],
            idempotency_key=rows[1]["idempotency_key"],
        ),
    )
    assert as_if_genesis != rows[1]["chain_hash"]


def test_chain_hash_column_migration_is_idempotent_and_backfills_null(tmp_path: Path) -> None:
    # #339 adversarial: a DB that already exists WITHOUT the chain_hash column
    # (pre-migration) must gain the column on open, its old row stays NULL, and
    # re-opening does not raise (ALTER TABLE is idempotent).
    db = tmp_path / "events.db"
    connection = sqlite3.connect(db)
    connection.executescript(
        """
        CREATE TABLE events (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            task_id TEXT,
            saga_id TEXT,
            causal_event_id TEXT,
            causal_sequence INTEGER,
            idempotency_key TEXT UNIQUE
        );
        CREATE TABLE projection_cursors (
            projection_name TEXT PRIMARY KEY,
            sequence INTEGER NOT NULL
        );
        """
    )
    connection.execute(
        "INSERT INTO events (event_id, kind, payload_json, schema_version, occurred_at) "
        "VALUES ('old1', 'frontier.advanced', '{}', 1, '2026-01-01T00:00:00+00:00')"
    )
    connection.commit()
    connection.close()

    log = SqliteEventLog(db)
    columns = {row[1] for row in log._connection.execute("PRAGMA table_info(events)")}
    assert "chain_hash" in columns
    # The pre-existing row keeps a NULL chain hash (unverifiable, not rewritten).
    pre = log._connection.execute(
        "SELECT chain_hash FROM events WHERE event_id = 'old1'"
    ).fetchone()
    assert pre["chain_hash"] is None
    del log
    gc.collect()

    # Idempotent: re-opening the now-migrated DB does not raise.
    reopened = SqliteEventLog(db)
    columns2 = {row[1] for row in reopened._connection.execute("PRAGMA table_info(events)")}
    assert "chain_hash" in columns2
