"""Tests for eventdb.py — DuckDB read-only query layer over the event JSONL."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge_loop import eventdb


def _seed_events(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def test_query_select_returns_rows(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    _seed_events(p, [
        {"ts": "2026-05-26T17:00:00Z", "kind": "tick_start", "tick": 1},
        {"ts": "2026-05-26T17:01:00Z", "kind": "tick_done", "tick": 1, "merged": [101]},
    ])
    rows = eventdb.query("SELECT kind FROM events ORDER BY ts", p)
    assert [r["kind"] for r in rows] == ["tick_start", "tick_done"]


def test_query_with_aggregation(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    _seed_events(p, [
        {"ts": "t", "kind": "tick_start"},
        {"ts": "t", "kind": "tick_start"},
        {"ts": "t", "kind": "tick_done"},
        {"ts": "t", "kind": "watchdog_worker_killed"},
    ])
    rows = eventdb.query(
        "SELECT kind, COUNT(*)::INTEGER AS n FROM events GROUP BY kind ORDER BY n DESC",
        p,
    )
    counts = {r["kind"]: r["n"] for r in rows}
    assert counts["tick_start"] == 2
    assert counts["tick_done"] == 1
    assert counts["watchdog_worker_killed"] == 1


def test_query_rejects_write_statements(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    _seed_events(p, [{"ts": "t", "kind": "x"}])

    for sql in (
        "INSERT INTO events VALUES ('t', 'evil')",
        "UPDATE events SET kind = 'evil'",
        "DELETE FROM events",
        "DROP VIEW events",
        "CREATE TABLE evil AS SELECT 1",
    ):
        with pytest.raises(ValueError):
            eventdb.query(sql, p)


def test_query_accepts_with_and_pragma(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    _seed_events(p, [{"ts": "t", "kind": "a"}, {"ts": "t", "kind": "b"}])
    rows = eventdb.query(
        "WITH e AS (SELECT * FROM events) SELECT COUNT(*)::INTEGER AS n FROM e", p,
    )
    assert rows[0]["n"] == 2


def test_query_max_rows_caps_result_size(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    _seed_events(p, [{"ts": f"t{i}", "kind": "x"} for i in range(50)])
    rows = eventdb.query("SELECT * FROM events", p, max_rows=10)
    assert len(rows) == 10


def test_query_handles_empty_or_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "absent.jsonl"
    rows = eventdb.query("SELECT * FROM events", p)
    assert rows == []


def test_recent_filters_by_kind(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    _seed_events(p, [
        {"ts": "2026-05-26T17:00:00Z", "kind": "a"},
        {"ts": "2026-05-26T17:01:00Z", "kind": "b"},
        {"ts": "2026-05-26T17:02:00Z", "kind": "a"},
    ])
    rows = eventdb.recent(p, kind="a")
    assert all(r["kind"] == "a" for r in rows)
    assert len(rows) == 2
    # oldest-first
    assert rows[0]["ts"] < rows[-1]["ts"]


def test_recent_respects_limit(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    _seed_events(p, [{"ts": f"2026-05-26T17:0{i}:00Z", "kind": "x"} for i in range(8)])
    rows = eventdb.recent(p, limit=3)
    assert len(rows) == 3


def test_count_by_kind(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    _seed_events(p, [
        {"ts": "t", "kind": "a"},
        {"ts": "t", "kind": "a"},
        {"ts": "t", "kind": "b"},
        {"ts": "t", "kind": "a"},
    ])
    rows = eventdb.count_by_kind(p)
    counts = {r["kind"]: r["n"] for r in rows}
    assert counts == {"a": 3, "b": 1}
    # Descending order
    assert rows[0]["kind"] == "a"


def test_summaries_view_when_summaries_path_provided(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    summaries = tmp_path / "summaries.jsonl"
    _seed_events(events, [{"ts": "t", "kind": "tick_done"}])
    _seed_events(summaries, [
        {"ts": "2026-05-26T17:00:00Z", "tick": 1, "merged": [], "failed": [934]},
        {"ts": "2026-05-26T17:30:00Z", "tick": 2, "merged": [962], "failed": []},
    ])
    rows = eventdb.query("SELECT tick, merged, failed FROM summaries ORDER BY tick", events, summaries)
    assert rows[0]["tick"] == 1
    assert rows[1]["tick"] == 2


def test_query_quote_escape_in_recent_kind(tmp_path: Path) -> None:
    """Quote escaping prevents injection via the kind parameter."""
    p = tmp_path / "events.jsonl"
    _seed_events(p, [{"ts": "t", "kind": "ok"}])
    # An attempt with embedded single-quote should NOT raise a SQL error
    rows = eventdb.recent(p, kind="o' OR '1'='1")
    assert rows == []
