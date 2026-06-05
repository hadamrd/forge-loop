"""Tests for the per-tick stuck-issue sweep (issue #129)."""

from __future__ import annotations

import json
from pathlib import Path

from forge_loop.events import StuckSweepDemotedEvent
from forge_loop.gh_client import GhError, Issue, MockGhClient
from forge_loop.stuck_sweep import sweep

READY = "loop:ready"
NEEDS_HUMAN = "loop:needs-human"


def _write_events(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def _exhausted(issue: int, *, final_state: str = "committed_not_pushed", pr_url: str | None = None) -> dict:
    return {
        "ts": "2026-05-28T00:00:00.000Z",
        "kind": "worker_iterations_exhausted",
        "issue": issue,
        "attempts": 3,
        "final_state": final_state,
        "pr_url": pr_url,
    }


def _success(issue: int, kind: str = "worker_merged") -> dict:
    return {"ts": "2026-05-28T00:00:01.000Z", "kind": kind, "issue": issue}


def _ready_issue(number: int) -> Issue:
    return Issue(
        number=number,
        title=f"issue {number}",
        body="",
        state="open",
        labels=[READY],
    )


# ---------------------------------------------------------------------------
# Test matrix from issue #129
# ---------------------------------------------------------------------------


def test_two_exhausted_events_demote(tmp_path):
    events = tmp_path / "events.jsonl"
    _write_events(events, [_exhausted(42), _exhausted(42, final_state="pushed_no_pr", pr_url="http://x/1")])
    gh = MockGhClient(issues={("o", "r", 42): _ready_issue(42)})
    captured: list[StuckSweepDemotedEvent] = []

    rep = sweep(events, gh, owner="o", repo="r", threshold=2, emit_fn=captured.append)

    assert [d.issue for d in rep.demotions] == [42]
    d = rep.demotions[0]
    assert d.ok is True
    assert d.attempts == 2
    assert d.last_state == "pushed_no_pr"
    assert d.pr_url == "http://x/1"
    # Labels flipped: needs-human added, ready removed, comment posted.
    method_calls = [m for m, _ in gh.calls]
    assert "add_labels" in method_calls
    assert "remove_label" in method_calls
    assert "add_comment" in method_calls
    # Typed event fired with ok=True.
    assert len(captured) == 1
    assert captured[0].issue == 42
    assert captured[0].ok is True
    assert captured[0].attempts == 2


def test_recovered_issue_not_demoted(tmp_path):
    """1 exhausted + success after → NOT demoted (it recovered)."""
    events = tmp_path / "events.jsonl"
    _write_events(events, [_exhausted(7), _success(7), _exhausted(7)])
    gh = MockGhClient(issues={("o", "r", 7): _ready_issue(7)})

    rep = sweep(events, gh, owner="o", repo="r", threshold=2)

    # After the success the count reset to 0, then one exhausted → 1, below 2.
    assert rep.demotions == []
    # No label calls made.
    assert all(m not in ("add_labels", "remove_label") for m, _ in gh.calls)


def test_zero_exhausted_no_demotion(tmp_path):
    events = tmp_path / "events.jsonl"
    _write_events(events, [{"ts": "...", "kind": "tick_start", "issue": 5}])
    gh = MockGhClient(issues={("o", "r", 5): _ready_issue(5)})

    rep = sweep(events, gh, owner="o", repo="r", threshold=2)

    assert rep.demotions == []
    assert gh.calls == []


def test_demotion_fires_typed_event_and_labels(tmp_path):
    events = tmp_path / "events.jsonl"
    _write_events(events, [_exhausted(101), _exhausted(101)])
    gh = MockGhClient(issues={("o", "r", 101): _ready_issue(101)})
    captured: list[StuckSweepDemotedEvent] = []

    sweep(events, gh, owner="o", repo="r", threshold=2, emit_fn=captured.append)

    # Find the add_labels + remove_label calls and check args.
    add = [kw for m, kw in gh.calls if m == "add_labels"][0]
    rem = [kw for m, kw in gh.calls if m == "remove_label"][0]
    assert add["labels"] == [NEEDS_HUMAN]
    assert rem["label"] == READY
    assert add["number"] == 101 and rem["number"] == 101
    # Typed event has the right kind + payload.
    assert len(captured) == 1
    ev = captured[0]
    assert ev.KIND == "stuck_sweep_demoted"
    assert ev.ok is True


def test_gh_failure_during_demotion_caught(tmp_path):
    """GhClient blows up → sweep records the miss + emits ok=False, never raises."""
    events = tmp_path / "events.jsonl"
    _write_events(events, [_exhausted(9), _exhausted(9)])
    gh = MockGhClient(
        issues={("o", "r", 9): _ready_issue(9)},
        raise_on={"add_labels": GhError("add_labels", 500, "boom")},
    )
    captured: list[StuckSweepDemotedEvent] = []

    rep = sweep(events, gh, owner="o", repo="r", threshold=2, emit_fn=captured.append)

    # Recorded the attempt but flagged it failed.
    assert len(rep.demotions) == 1
    assert rep.demotions[0].ok is False
    assert 9 in rep.errors
    assert "add_labels" in rep.errors[9] or "label" in rep.errors[9]
    # Typed failure event surfaced for the operator log.
    assert len(captured) == 1
    assert captured[0].ok is False
    assert captured[0].reason


def test_idempotent_skip_when_already_not_ready(tmp_path):
    """Issue lost loop:ready since last tick → no double demotion."""
    events = tmp_path / "events.jsonl"
    _write_events(events, [_exhausted(11), _exhausted(11)])
    cur = Issue(number=11, title="x", body="", state="open", labels=[NEEDS_HUMAN])
    gh = MockGhClient(issues={("o", "r", 11): cur})
    captured: list[StuckSweepDemotedEvent] = []

    rep = sweep(events, gh, owner="o", repo="r", threshold=2, emit_fn=captured.append)

    # Demotion attempted but flagged ok=False with reason "not_ready" — no
    # typed event emitted, no label mutations.
    assert len(rep.demotions) == 1
    assert rep.demotions[0].ok is False
    method_calls = [m for m, _ in gh.calls]
    assert "add_labels" not in method_calls
    assert "remove_label" not in method_calls
    # not_ready is a silent skip — no typed event.
    assert captured == []


def test_missing_events_file_returns_empty_report(tmp_path):
    events = tmp_path / "absent.jsonl"
    gh = MockGhClient()
    rep = sweep(events, gh, owner="o", repo="r")
    assert rep.demotions == []
    assert rep.scanned == 0


def test_tail_window_limits_scan(tmp_path):
    """Old exhausted events past the tail window don't count."""
    events = tmp_path / "events.jsonl"
    # 5 noise records, then 1 exhausted. Tail=3 → only sees noise + exhausted
    # past the last 3? With tail=3 we only see the last 3 records (2 noise +
    # 1 exhausted) → count = 1, below threshold = 2.
    noise = [{"ts": "...", "kind": "tick_idle", "tick": i} for i in range(5)]
    _write_events(events, [_exhausted(3), _exhausted(3), *noise])
    gh = MockGhClient(issues={("o", "r", 3): _ready_issue(3)})

    rep = sweep(events, gh, owner="o", repo="r", threshold=2, tail=3)

    assert rep.demotions == []
    assert rep.scanned == 3


def test_malformed_json_lines_skipped(tmp_path):
    events = tmp_path / "events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    # First line is garbage, second is a real exhausted event x2.
    lines = [
        "{not json",
        json.dumps(_exhausted(77)),
        json.dumps(_exhausted(77)),
    ]
    events.write_text("\n".join(lines) + "\n")
    gh = MockGhClient(issues={("o", "r", 77): _ready_issue(77)})

    rep = sweep(events, gh, owner="o", repo="r", threshold=2)

    assert [d.issue for d in rep.demotions] == [77]


def test_threshold_clamps_to_one(tmp_path):
    """threshold=0 is nonsense; gets clamped, doesn't crash."""
    events = tmp_path / "events.jsonl"
    _write_events(events, [_exhausted(50)])
    gh = MockGhClient(issues={("o", "r", 50): _ready_issue(50)})
    rep = sweep(events, gh, owner="o", repo="r", threshold=0)
    assert [d.issue for d in rep.demotions] == [50]


def test_multiple_issues_demoted_in_stable_order(tmp_path):
    events = tmp_path / "events.jsonl"
    _write_events(events, [
        _exhausted(20), _exhausted(20),
        _exhausted(10), _exhausted(10),
        _exhausted(30), _exhausted(30),
    ])
    gh = MockGhClient(issues={
        ("o", "r", 10): _ready_issue(10),
        ("o", "r", 20): _ready_issue(20),
        ("o", "r", 30): _ready_issue(30),
    })

    rep = sweep(events, gh, owner="o", repo="r", threshold=2)
    # Sorted by issue number for determinism.
    assert [d.issue for d in rep.demotions] == [10, 20, 30]


def test_get_issue_failure_caught(tmp_path):
    """get_issue raises → sweep records the miss, no crash."""
    events = tmp_path / "events.jsonl"
    _write_events(events, [_exhausted(99), _exhausted(99)])
    gh = MockGhClient(
        issues={("o", "r", 99): _ready_issue(99)},
        raise_on={"get_issue": GhError("get_issue", 502, "down")},
    )
    captured: list[StuckSweepDemotedEvent] = []
    rep = sweep(events, gh, owner="o", repo="r", threshold=2, emit_fn=captured.append)
    assert len(rep.demotions) == 1
    assert rep.demotions[0].ok is False
    assert 99 in rep.errors
    assert len(captured) == 1
    assert captured[0].ok is False


def test_comment_failure_does_not_fail_demotion(tmp_path):
    """add_comment blows up but labels landed → still ok=True."""
    events = tmp_path / "events.jsonl"
    _write_events(events, [_exhausted(8), _exhausted(8)])
    gh = MockGhClient(
        issues={("o", "r", 8): _ready_issue(8)},
        raise_on={"add_comment": GhError("add_comment", 500, "nope")},
    )
    captured: list[StuckSweepDemotedEvent] = []
    rep = sweep(events, gh, owner="o", repo="r", threshold=2, emit_fn=captured.append)
    assert rep.demotions[0].ok is True
    assert captured[0].ok is True
