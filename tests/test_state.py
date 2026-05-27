"""Tests for state.py — write/read/append round-trips."""

from __future__ import annotations

import json
from pathlib import Path

from forge_loop.state import (
    append_event,
    consolidate_sprint,
    now_iso,
    read_state,
    tail_events,
    write_state,
)


def test_now_iso_is_utc_z_aware() -> None:
    s = now_iso()
    assert s.endswith("+00:00") or s.endswith("Z")  # ISO UTC offset


def test_write_state_then_read_roundtrips(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    write_state(p, {"state": "running", "tick": 7})
    got = read_state(p)
    assert got["state"] == "running"
    assert got["tick"] == 7
    assert "ts" in got


def test_write_state_overwrites_atomic(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    write_state(p, {"state": "a", "tick": 1})
    write_state(p, {"state": "b", "tick": 2})
    got = read_state(p)
    assert got["state"] == "b"
    assert got["tick"] == 2


def test_read_state_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_state(tmp_path / "nope.json") == {}


def test_append_event_appends_one_line(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    append_event(p, "tick_start", tick=1, issues=[101, 102])
    append_event(p, "tick_done", tick=1, merged=[101])

    lines = p.read_text().splitlines()
    assert len(lines) == 2
    e0 = json.loads(lines[0])
    e1 = json.loads(lines[1])
    assert e0["kind"] == "tick_start"
    assert e0["tick"] == 1
    assert e0["issues"] == [101, 102]
    assert e1["kind"] == "tick_done"
    assert e1["merged"] == [101]


def test_tail_events_returns_last_n_lines(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    for i in range(10):
        append_event(p, "x", n=i)
    lines = tail_events(p, n=3)
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert [e["n"] for e in parsed] == [7, 8, 9]


def test_tail_events_missing_file_returns_empty(tmp_path: Path) -> None:
    assert tail_events(tmp_path / "absent.jsonl", n=5) == []


def test_consolidate_sprint_writes_summary_line(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    summaries = tmp_path / "summaries.jsonl"
    outcomes = [
        {"issue": 101, "status": "merged", "pr_url": "https://github.com/h/r/pull/1", "events": []},
        {"issue": 102, "status": "failed", "pr_url": None, "events": [{"kind": "bug_found"}]},
        {"issue": 103, "status": "merged", "pr_url": "https://github.com/h/r/pull/2", "events": []},
    ]
    summary = consolidate_sprint(events, summaries, tick=7, outcomes=outcomes)

    assert summary["tick"] == 7
    assert summary["total"] == 3
    assert summary["merged"] == [101, 103]
    assert summary["failed"] == [102]
    assert summary["pr_urls"] == [
        "https://github.com/h/r/pull/1",
        "https://github.com/h/r/pull/2",
    ]
    assert summary["subagent_events_count"] == 1

    lines = summaries.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["merged"] == [101, 103]


def test_consolidate_sprint_truncates_events_file(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    summaries = tmp_path / "summaries.jsonl"
    for i in range(100):
        append_event(events, "noise", n=i)

    consolidate_sprint(events, summaries, tick=1, outcomes=[], keep_recent_events=20)

    remaining = events.read_text().splitlines()
    assert len(remaining) == 20
    # The newest entries are preserved (last 20).
    last = json.loads(remaining[-1])
    assert last["n"] == 99


def test_consolidate_sprint_zero_keep_does_not_truncate(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    summaries = tmp_path / "summaries.jsonl"
    for i in range(5):
        append_event(events, "x", n=i)
    consolidate_sprint(events, summaries, tick=1, outcomes=[], keep_recent_events=0)
    assert len(events.read_text().splitlines()) == 5
