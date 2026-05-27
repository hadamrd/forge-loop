"""Integration tests for the per-tick budget gate and worker budget kill.

Exercises:
* Runner skips dispatching new workers once the projected tick spend would
  blow the per-tick ceiling, but lets in-flight workers continue.
* Worker is killed mid-run when the per-ticket ceiling is crossed (via the
  log-tailing budget watcher).
* Label override (``budget:0.1``) is respected by both the worker and the
  runner's projection.
* Multi-worker tick: one blows its ticket budget, others still finish.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from forge_loop import budget as B
from forge_loop import worker as W


def _write_event_log(path: Path, events: list[dict[str, Any]]) -> None:
    """Append a batch of stream-json events to a log file (worker log shape)."""
    with open(path, "ab") as f:
        for e in events:
            f.write((json.dumps(e) + "\n").encode())


class _FakeProc:
    """Minimal Popen-like object the BudgetWatcher can poke."""

    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.returncode: int | None = None

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def poll(self) -> int | None:
        return self.returncode


# ---------------------------------------------------------------------------
# _BudgetWatcher: log-tailing + kill on ceiling
# ---------------------------------------------------------------------------


def test_budget_watcher_kills_proc_when_ticket_ceiling_crossed(tmp_path: Path) -> None:
    log = tmp_path / "worker.log"
    log.touch()
    proc = _FakeProc()
    tracker = B.TicketBudgetTracker(ceiling_usd=2.0)
    events: list[tuple[str, dict[str, Any]]] = []

    def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))

    watcher = W._BudgetWatcher(
        proc=proc, log_path=log, tracker=tracker,
        emit=emit, issue=42,
    )

    # 1M sonnet input @ $3 → 1 call crosses $2 ceiling.
    _write_event_log(log, [{
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-6",
            "usage": {"input_tokens": 1_000_000, "output_tokens": 0,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }])
    watcher.scan_once()
    assert watcher.tripped is True
    assert proc.terminated is True
    assert any(k == "budget_worker_killed" for k, _ in events)
    assert tracker.exceeded


def test_budget_watcher_quiet_until_ceiling(tmp_path: Path) -> None:
    log = tmp_path / "worker.log"
    log.touch()
    proc = _FakeProc()
    tracker = B.TicketBudgetTracker(ceiling_usd=10.0)
    emitted: list[tuple[str, dict[str, Any]]] = []
    watcher = W._BudgetWatcher(
        proc=proc, log_path=log, tracker=tracker,
        emit=lambda k, p: emitted.append((k, p)),
        issue=1,
    )
    # 500k sonnet input -> $1.50, well under $10.
    _write_event_log(log, [{
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-6",
            "usage": {"input_tokens": 500_000, "output_tokens": 0,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }])
    watcher.scan_once()
    assert watcher.tripped is False
    assert proc.terminated is False
    assert emitted == []


def test_budget_watcher_label_override(tmp_path: Path) -> None:
    """A budget:0.1 label must cut the ceiling 50× below the default."""
    labels = [{"name": "budget:0.1"}, {"name": "loop:ready"}]
    cap = B.ticket_budget_for(labels)
    assert cap == 0.1

    tracker = B.TicketBudgetTracker(ceiling_usd=cap)
    # Sonnet 50k input -> $0.15 > $0.10 -> trips.
    crossed = tracker.add("claude-sonnet-4-6", {
        "input_tokens": 50_000, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    })
    assert crossed is True


def test_budget_watcher_handles_partial_lines(tmp_path: Path) -> None:
    """A half-written log line must not crash the watcher — wait for the newline."""
    log = tmp_path / "worker.log"
    log.write_bytes(b'{"type":"assistant","message":{"model":"claude-sonnet-4-6"')
    proc = _FakeProc()
    tracker = B.TicketBudgetTracker(ceiling_usd=100.0)
    watcher = W._BudgetWatcher(
        proc=proc, log_path=log, tracker=tracker, emit=None, issue=1,
    )
    watcher.scan_once()  # should not raise, no events processed
    assert tracker.snapshot.cost_usd == 0.0
    # Now finish the line + add a real usage event.
    with open(log, "ab") as f:
        f.write(b',"usage":{"input_tokens":1000000,"output_tokens":0,'
                b'"cache_creation_input_tokens":0,"cache_read_input_tokens":0}}}\n')
    watcher.scan_once()
    assert tracker.snapshot.cost_usd == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Per-tick gate (runner-level projection)
# ---------------------------------------------------------------------------


def _issue(num: int, label_budget: float | None = None) -> dict[str, Any]:
    labels = [{"name": "loop:ready"}]
    if label_budget is not None:
        labels.append({"name": f"budget:{label_budget}"})
    return {"number": num, "title": f"issue {num}", "labels": labels, "body": ""}


def test_tick_gate_skips_when_projection_exceeds_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Projection-based gate: with a $5 ticket cap and $11 tick cap, the
    third worker is deferred (5 + 5 + 5 = 15 > 11). First two get dispatched.

    We re-create the gate logic locally to keep the test pure — no
    subprocesses, no gh, no claude. The real runner.py uses the same
    `_budget.ticket_budget_for` / `_budget.tick_budget` primitives.
    """
    monkeypatch.setenv("LOOP_TICKET_BUDGET_USD", "5.0")
    monkeypatch.setenv("LOOP_TICK_BUDGET_USD", "11.0")
    issues = [_issue(1), _issue(2), _issue(3)]
    tick_cap = B.tick_budget()
    projected = 0.0
    dispatched: list[int] = []
    deferred: list[int] = []
    for i in issues:
        labels = i.get("labels") or []
        ticket_cap = B.ticket_budget_for(labels)
        if projected + ticket_cap > tick_cap and dispatched:
            deferred.append(i["number"])
            continue
        dispatched.append(i["number"])
        projected += ticket_cap
    assert dispatched == [1, 2]
    assert deferred == [3]


def test_tick_gate_label_override_lets_more_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When per-issue caps are tiny via labels, the tick cap admits more."""
    monkeypatch.setenv("LOOP_TICKET_BUDGET_USD", "5.0")
    monkeypatch.setenv("LOOP_TICK_BUDGET_USD", "2.0")
    issues = [_issue(1, 0.5), _issue(2, 0.5), _issue(3, 0.5), _issue(4, 0.5)]
    tick_cap = B.tick_budget()
    projected = 0.0
    dispatched: list[int] = []
    for i in issues:
        labels = i.get("labels") or []
        cap = B.ticket_budget_for(labels)
        if projected + cap > tick_cap and dispatched:
            continue
        dispatched.append(i["number"])
        projected += cap
    # 0.5 * 4 = 2.0 == cap → all four admitted.
    assert dispatched == [1, 2, 3, 4]


def test_tick_gate_always_admits_first_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even when one ticket alone would exceed the tick cap, the first worker
    must still get dispatched — otherwise a misconfigured tick cap stalls the
    loop entirely. Subsequent workers are deferred."""
    monkeypatch.setenv("LOOP_TICKET_BUDGET_USD", "50.0")
    monkeypatch.setenv("LOOP_TICK_BUDGET_USD", "5.0")
    issues = [_issue(1), _issue(2)]
    tick_cap = B.tick_budget()
    projected = 0.0
    dispatched: list[int] = []
    for i in issues:
        cap = B.ticket_budget_for(i.get("labels") or [])
        if projected + cap > tick_cap and dispatched:
            continue
        dispatched.append(i["number"])
        projected += cap
    assert dispatched == [1]


# ---------------------------------------------------------------------------
# End-to-end-ish: worker budget kill leaves a ledger record + no PR
# ---------------------------------------------------------------------------


def test_budget_exceeded_writes_ledger_record(tmp_path: Path) -> None:
    """When a watcher trips, the spend ledger receives a 'budget_exceeded'
    row. The runner's per-tick gate and the CLI both rely on this ledger."""
    ledger = tmp_path / "spend.jsonl"
    rec = B.SpendRecord(
        ts=B.utc_now_iso(), issue=99, cost_usd=2.5,
        status="budget_exceeded", model="claude-sonnet-4-6", tick=3,
    )
    B.append_spend(ledger, rec)
    rows = B.read_spend(ledger)
    assert rows[0]["status"] == "budget_exceeded"
    assert B.tick_spend(ledger, 3) == pytest.approx(2.5)


def test_concurrent_worker_one_busts_others_continue(tmp_path: Path) -> None:
    """Multi-worker tick where worker A trips its ticket cap but worker B's
    tracker is unaffected — independence of TicketBudgetTrackers, which is
    what the runner relies on for the 'in-flight continue' guarantee."""
    a_proc, b_proc = _FakeProc(), _FakeProc()
    a_log, b_log = tmp_path / "a.log", tmp_path / "b.log"
    a_log.touch()
    b_log.touch()
    a_track = B.TicketBudgetTracker(ceiling_usd=0.5)
    b_track = B.TicketBudgetTracker(ceiling_usd=100.0)
    a_w = W._BudgetWatcher(proc=a_proc, log_path=a_log, tracker=a_track,
                           emit=None, issue=1)
    b_w = W._BudgetWatcher(proc=b_proc, log_path=b_log, tracker=b_track,
                           emit=None, issue=2)
    # Both workers receive the same big usage event.
    big = [{
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-6",
            "usage": {"input_tokens": 1_000_000, "output_tokens": 0,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }]
    _write_event_log(a_log, big)
    _write_event_log(b_log, big)
    a_w.scan_once()
    b_w.scan_once()
    assert a_w.tripped and a_proc.terminated
    assert not b_w.tripped and not b_proc.terminated


def test_full_run_worker_smoke(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Smoke that run_worker accepts the new kwargs without exploding.
    We monkeypatch _prep_worktree to short-circuit before subprocess launch."""

    def fake_prep(repo: Path, n: int, branch: str) -> tuple[Path, str]:
        return tmp_path / "wt", "fake-prep-failure"

    monkeypatch.setattr(W, "_prep_worktree", fake_prep)
    outcome = W.run_worker(
        {"number": 1, "title": "x", "labels": [{"name": "budget:0.5"}]},
        repo=tmp_path, logs_dir=tmp_path / "logs", timeout_s=1,
        spend_ledger=tmp_path / "spend.jsonl", tick=7,
    )
    assert outcome.status == "failed"
    assert outcome.error == "worktree-create-failed"
    # When prep fails we don't write a ledger row (nothing was spent).
    assert not (tmp_path / "spend.jsonl").exists()


def test_full_run_worker_records_ledger_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """run_worker writes a ledger row with the tracker's cost when the
    worker exits normally. We stub out subprocess + worktree prep + outcome
    extraction so the test stays under a second."""
    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setattr(W, "_prep_worktree", lambda repo, n, b: (wt, None))
    monkeypatch.setattr(W, "_read_subagent_events", lambda wt: [])

    # Stub out the SDK session: return a successful merged outcome directly,
    # bypassing the real claude_agent_sdk transport so the test stays hermetic.
    from forge_loop._worker_sdk import SDKRunResult
    from forge_loop import _worker_sdk as _wsdk

    async def _fake_session(prompt: str, **kw: Any) -> SDKRunResult:
        return SDKRunResult(
            pr_url="https://github.com/x/y/pull/1",
            status="merged",
            cost_usd=0.05,
            usage={"input_tokens": 100, "output_tokens": 20},
            model="claude-sonnet-4-6",
            final_result_text='{"issue":77,"pr":"https://github.com/x/y/pull/1","status":"merged"}',
            error=None,
            events=[],
            duration_s=0.01,
        )
    monkeypatch.setattr(_wsdk, "run_sdk_session", _fake_session)

    ledger = tmp_path / "spend.jsonl"
    outcome = W.run_worker(
        {"number": 77, "title": "x", "labels": []},
        repo=tmp_path, logs_dir=tmp_path / "logs", timeout_s=30,
        ticket_budget_usd=5.0,
        spend_ledger=ledger, tick=3,
    )
    assert outcome.status == "merged"
    rows = B.read_spend(ledger)
    assert len(rows) == 1
    assert rows[0]["issue"] == 77
    assert rows[0]["tick"] == 3
    assert rows[0]["status"] == "merged"
