"""Integration tests for fingerprint-based retry guards (issue #4).

Covers the contract from the issue body:

  * runner.tick() with simulated worker failure → next tick skips
    (cooldown), → after cooldown expiry picks up again;
  * in-flight skip fires when prior attempt has the same fingerprint and
    status=open, surfaces the open PR URL on the event;
  * --force bypasses both guards via the force-retry marker file;
  * corrupted attempts history rows surface as ``attempts_corrupt`` and
    do NOT crash the tick.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from forge_loop import attempts as _attempts
from forge_loop import runner as _runner
from forge_loop import worker as _worker
from forge_loop.config import (
    AttemptsConfig,
    Briefs,
    Config,
    CriticConfig,
    Labels,
    LumenConfig,
    POConfig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cfg(tmp_path: Path) -> Config:
    return Config(
        repo=tmp_path,
        github_repo="o/r",
        parallel=1,
        tick_interval_s=0,
        max_ticks=1,
        worker_timeout_s=60,
        deploy_task="",
        labels=Labels(),
        briefs=Briefs(),
        critic=CriticConfig(enabled=False, timeout_s=10),
        po=POConfig(enabled=False, timeout_s=10, max_to_expand_per_tick=0),
        attempts=AttemptsConfig(enabled=True, max_history_in_brief=5),
        lumen=LumenConfig(),
    )


def _read_events(cfg: Config) -> list[dict[str, Any]]:
    if not cfg.events_file.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in cfg.events_file.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def _kinds(events: list[dict[str, Any]]) -> list[str]:
    return [e["kind"] for e in events]


class _State:
    """Mutable shared state for fakes — history per-issue + dispatch count."""
    def __init__(self) -> None:
        self.history: dict[int, list[dict[str, Any]]] = {}
        self.corrupt: dict[int, int] = {}
        self.dispatched: list[int] = []
        # Worker outcome scripted by the test:
        self.scripted_status: str = "failed"
        self.scripted_pr: str | None = None
        self.scripted_error: str | None = "boom"


@pytest.fixture
def fake_world(monkeypatch, tmp_path: Path):
    """Stub network + worker dispatch; expose mutable state for assertions."""
    state = _State()
    cfg = _make_cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.events_file.touch()

    issue = {
        "number": 99,
        "title": "demo",
        "body": "do the thing",
        "labels": [],
    }

    def fake_top_issues(label: str, limit: int, repo: str | None = None):
        return [dict(issue)]

    def fake_fetch_history_strict(num: int, repo: str | None = None):
        return list(state.history.get(num, [])), state.corrupt.get(num, 0)

    def fake_record(num: int, *, status, pr_url, duration_s, note, event_count,
                    repo=None, brief_fingerprint=""):
        state.history.setdefault(num, []).append({
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "status": status,
            "pr_url": pr_url,
            "duration_s": duration_s,
            "note": note,
            "event_count": event_count,
            "brief_fingerprint": brief_fingerprint,
        })

    def fake_run_worker(issue_, repo, logs_dir, timeout_s, **kwargs):
        from forge_loop.worker import WorkerOutcome
        state.dispatched.append(issue_["number"])
        return WorkerOutcome(
            issue=issue_["number"], title=issue_["title"],
            pr_url=state.scripted_pr, status=state.scripted_status,
            duration_s=1.0, stdout_tail="",
            error=state.scripted_error, events=[],
        )

    # Patch points: top_issues + attempts.fetch_history_strict +
    # attempts.record + ThreadPoolExecutor's task (run_worker is captured by
    # name in runner via `from forge_loop.worker import run_worker`).
    monkeypatch.setattr(_runner, "top_issues", fake_top_issues)
    monkeypatch.setattr(_attempts, "fetch_history_strict", fake_fetch_history_strict)
    monkeypatch.setattr(_attempts, "record", fake_record)
    monkeypatch.setattr(_runner, "run_worker", fake_run_worker)
    # Defang side effects:
    monkeypatch.setattr(_runner, "_reap_worktree", lambda *a, **k: None)
    monkeypatch.setattr(_runner, "redeploy", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(_runner, "_short_sleep", lambda *a, **k: None)

    return state, cfg, issue


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_first_tick_dispatches_and_records_fingerprint(fake_world) -> None:
    state, cfg, _ = fake_world
    state.scripted_status = "open"
    state.scripted_pr = "https://github.com/o/r/pull/100"
    state.scripted_error = None

    _runner._tick(cfg, tick=1)
    assert state.dispatched == [99]
    # History gained one record carrying a non-empty fingerprint
    assert len(state.history[99]) == 1
    assert len(state.history[99][0]["brief_fingerprint"]) == 64


def test_failure_then_cooldown_skip_then_release(fake_world, monkeypatch) -> None:
    state, cfg, _ = fake_world
    monkeypatch.setenv("LOOP_RETRY_COOLDOWN_S", "3600")

    # Tick 1: worker fails → history records the failed attempt.
    state.scripted_status = "failed"
    state.scripted_error = "boom"
    _runner._tick(cfg, tick=1)
    assert state.dispatched == [99]

    # Tick 2: same fingerprint, recent failure → cooldown skip.
    state.dispatched.clear()
    _runner._tick(cfg, tick=2)
    assert state.dispatched == []  # NOT redispatched
    events = _read_events(cfg)
    assert "worker_skip_cooldown" in _kinds(events)
    cooldown_evt = next(e for e in events if e["kind"] == "worker_skip_cooldown")
    assert cooldown_evt["issue"] == 99
    assert cooldown_evt["cooldown_remaining_s"] > 0

    # Tick 3: backdate the failed record to outside the window → release.
    older = datetime.now(UTC) - timedelta(hours=2)
    state.history[99][0]["ts"] = older.isoformat(timespec="seconds")
    state.dispatched.clear()
    _runner._tick(cfg, tick=3)
    assert state.dispatched == [99]  # redispatched after cooldown expiry


def test_in_flight_skip_emits_pr_url(fake_world) -> None:
    state, cfg, issue = fake_world
    # Seed history with an "open" attempt that matches today's fingerprint.
    fp = _attempts.compute_fingerprint(
        issue["number"], issue["body"], _worker.brief_template_hash(),
    )
    state.history[99] = [{
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "open",
        "pr_url": "https://github.com/o/r/pull/777",
        "brief_fingerprint": fp,
    }]
    _runner._tick(cfg, tick=1)
    assert state.dispatched == []
    events = _read_events(cfg)
    assert "worker_skip_in_flight" in _kinds(events)
    skip = next(e for e in events if e["kind"] == "worker_skip_in_flight")
    assert skip["issue"] == 99
    assert skip["pr_url"] == "https://github.com/o/r/pull/777"


def test_body_change_invalidates_in_flight_skip(fake_world) -> None:
    """An open PR with a *stale* fingerprint must NOT block re-dispatch.

    This is the safety net: when the operator rewrites the issue body, the
    in-flight skip must release so the new spec gets a new attempt.
    """
    state, cfg, _ = fake_world
    # Stale fingerprint that no longer matches the issue body.
    state.history[99] = [{
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "open",
        "pr_url": "https://x/p/1",
        "brief_fingerprint": "stale" * 13,  # 65 chars, distinct from current
    }]
    _runner._tick(cfg, tick=1)
    assert state.dispatched == [99]


def test_force_marker_bypasses_both_guards(fake_world) -> None:
    state, cfg, issue = fake_world
    # Seed an in-flight attempt that would normally skip.
    fp = _attempts.compute_fingerprint(
        issue["number"], issue["body"], _worker.brief_template_hash(),
    )
    state.history[99] = [{
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "open",
        "pr_url": "https://x/p/1",
        "brief_fingerprint": fp,
    }]
    # Write the force-retry marker (what `forge-loop retry --force` does).
    marker = _runner._force_retry_file(cfg)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"issues": [99]}))

    _runner._tick(cfg, tick=1)
    assert state.dispatched == [99]  # forced through
    # Marker is one-shot: consumed and cleared.
    assert not marker.exists()


def test_corrupt_history_emits_event_and_continues(fake_world) -> None:
    """Adversarial: corrupted history rows must NOT crash the tick.

    Emit ``attempts_corrupt`` and treat as no-history (dispatch as normal).
    """
    state, cfg, _ = fake_world
    state.corrupt[99] = 2  # two malformed rows in history
    state.scripted_status = "failed"

    _runner._tick(cfg, tick=1)
    # Despite corruption, the worker is dispatched (no history → no skip).
    assert state.dispatched == [99]
    events = _read_events(cfg)
    assert "attempts_corrupt" in _kinds(events)
    corrupt_evt = next(e for e in events if e["kind"] == "attempts_corrupt")
    assert corrupt_evt["issue"] == 99
    assert corrupt_evt["rows"] == 2
