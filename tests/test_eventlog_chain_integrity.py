"""Read-path hash-chain integrity tests for the durable event log (issue #338).

Replay (``SqliteEventLog.since``) must refuse to reconstruct state from a broken
chain: a mutated payload, a deleted middle row, or a reordered log raises
:class:`EventChainIntegrityError` naming the first untrustworthy sequence, while
an untouched log — and an *authorised* prune/compaction — replays cleanly.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from forge_loop.eventlog import (
    EventChainIntegrityError,
    EventKind,
    SqliteEventLog,
    compute_event_hash,
)
from forge_loop.eventlog.chain import GENESIS_HASH


def _seed(log: SqliteEventLog, n: int = 3) -> None:
    for i in range(n):
        log.append(EventKind.DECISION_MADE, {"choice": i})


# --- happy path ----------------------------------------------------------


def test_untouched_log_replays_without_raising(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    _seed(log, 3)

    events = list(log.since(0))

    assert [e.sequence for e in events] == [1, 2, 3]


def test_untouched_log_replays_after_reopen(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    _seed(SqliteEventLog(db), 3)

    events = list(SqliteEventLog(db).since(0))

    assert [e.payload["choice"] for e in events] == [0, 1, 2]


def test_stored_hash_chains_to_predecessor(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    _seed(log, 2)

    rows = log._connection.execute(
        "SELECT sequence, prev_hash, event_hash FROM events ORDER BY sequence"
    ).fetchall()

    assert rows[0]["prev_hash"] == GENESIS_HASH
    assert rows[1]["prev_hash"] == rows[0]["event_hash"]


# --- adversarial: payload tampering --------------------------------------


def test_mutating_stored_payload_raises_at_that_sequence(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    _seed(SqliteEventLog(db), 3)

    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE events SET payload_json = ? WHERE sequence = 2",
            ('{"choice":999}',),
        )

    with pytest.raises(EventChainIntegrityError) as excinfo:
        list(SqliteEventLog(db).since(0))
    assert excinfo.value.sequence == 2


# --- adversarial: middle-row deletion ------------------------------------


def test_deleting_a_middle_row_raises_at_following_sequence(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    _seed(SqliteEventLog(db), 3)

    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM events WHERE sequence = 2")

    with pytest.raises(EventChainIntegrityError) as excinfo:
        list(SqliteEventLog(db).since(0))
    # Sequence 3's stored prev-hash points at the now-missing row 2, so its
    # hash no longer recomputes against the running prev-hash (row 1).
    assert excinfo.value.sequence == 3


def test_deleting_the_tail_row_still_replays(tmp_path: Path) -> None:
    # Truncating the tail removes no chain link the survivors depend on, so the
    # remaining prefix is still internally consistent.
    db = tmp_path / "events.db"
    _seed(SqliteEventLog(db), 3)

    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM events WHERE sequence = 3")

    assert [e.sequence for e in SqliteEventLog(db).since(0)] == [1, 2]


# --- adversarial: reordering ---------------------------------------------


def test_swapping_two_payloads_raises(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    _seed(SqliteEventLog(db), 3)

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE events SET payload_json = ? WHERE sequence = 1", ('{"choice":1}',))
        conn.execute("UPDATE events SET payload_json = ? WHERE sequence = 2", ('{"choice":0}',))

    with pytest.raises(EventChainIntegrityError) as excinfo:
        list(SqliteEventLog(db).since(0))
    assert excinfo.value.sequence == 1


# --- partial replay windows ----------------------------------------------


def test_partial_since_window_verifies_against_predecessor(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    _seed(log, 4)

    events = list(log.since(2))

    assert [e.sequence for e in events] == [3, 4]


def test_partial_since_window_detects_tamper(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    _seed(SqliteEventLog(db), 4)

    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE events SET payload_json = ? WHERE sequence = 3", ('{"x":1}',))

    with pytest.raises(EventChainIntegrityError) as excinfo:
        list(SqliteEventLog(db).since(2))
    assert excinfo.value.sequence == 3


# --- authorised deletion still replays (compaction / prune re-chains) -----


def test_compaction_keeps_log_replayable(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    log.append(EventKind.DECISION_MADE, {"a": 1})
    log.append(EventKind.TICK_STARTED, {"tick": 1})  # prunable noise
    log.append(EventKind.DECISION_MADE, {"b": 2})
    log.append(EventKind.TICK_COMPLETED, {"tick": 1})  # tail, preserved

    log.compact_noise(emit_marker=False)

    # No raise: survivors were re-chained.
    survivors = [e.kind for e in log.since(0)]
    assert EventKind.DECISION_MADE in survivors
    assert EventKind.TICK_STARTED not in survivors


def test_guarded_prune_keeps_log_replayable(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    log.append(EventKind.DECISION_MADE, {"a": 1})
    noise = log.append(EventKind.TICK_STARTED, {"tick": 1}).sequence
    log.append(EventKind.DECISION_MADE, {"b": 2})

    log.prune([noise])

    assert [e.payload for e in log.since(0)] == [{"a": 1}, {"b": 2}]


# --- legacy (pre-#338) rows with NULL hashes ------------------------------


def test_legacy_null_hash_rows_replay_without_raising(tmp_path: Path) -> None:
    # Simulate a log written before the hash-chain migration: rows exist but
    # carry NULL event_hash. Replay must not raise on unchained legacy rows.
    db = tmp_path / "events.db"
    SqliteEventLog(db)  # creates schema
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO events (event_id, kind, payload_json, schema_version, "
            "occurred_at) VALUES (?, ?, ?, ?, ?)",
            ("legacy-1", EventKind.DECISION_MADE.value, '{"x":1}', 1, "2020-01-01T00:00:00+00:00"),
        )

    events = list(SqliteEventLog(db).since(0))

    assert len(events) == 1
    assert events[0].payload == {"x": 1}


# --- chain hash determinism ----------------------------------------------


def test_compute_event_hash_is_deterministic_and_prev_sensitive() -> None:
    base = dict(
        event_id="e1",
        kind="decision.made",
        payload_json='{"a":1}',
        schema_version=1,
        occurred_at="2026-01-01T00:00:00+00:00",
        task_id=None,
        saga_id=None,
        idempotency_key=None,
    )
    h1 = compute_event_hash(prev_hash=GENESIS_HASH, **base)
    h2 = compute_event_hash(prev_hash=GENESIS_HASH, **base)
    h3 = compute_event_hash(prev_hash="deadbeef", **base)

    assert h1 == h2
    assert h1 != h3
