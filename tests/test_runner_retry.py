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
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from forge_loop import attempts as _attempts
from forge_loop import gh as _gh
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
from forge_loop.runner import dispatch as _dispatch_mod
from forge_loop.runner import tick as _tick_mod
from forge_loop.worker import WorkerOutcome

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
        self.unlabeled: list[tuple[int, str, str | None]] = []
        self.blocking_comments: dict[int, list[str]] = {}
        self.worker_kwargs: list[dict[str, Any]] = []
        self.automerge_calls: list[tuple[str, str | None]] = []
        # Issue #213 — orphaned-PR adoption scan fakes.
        self.open_prs: list[dict[str, Any]] = []
        self.pr_labels: dict[str, list[str]] = {}
        self.unresolved_threads: dict[str, list[dict[str, Any]]] = {}
        self.issue_states: dict[int, str] = {}
        self.critic_runs: list[str] = []


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

    def fake_fetch_blocking_comments(num: int, repo: str | None = None):
        return list(state.blocking_comments.get(num, []))

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
        state.worker_kwargs.append(kwargs)
        return WorkerOutcome(
            issue=issue_["number"], title=issue_["title"],
            pr_url=state.scripted_pr, status=state.scripted_status,
            duration_s=1.0, stdout_tail="",
            error=state.scripted_error, events=[],
        )

    def fake_unlabel(num: int, label: str, repo: str | None = None) -> None:
        state.unlabeled.append((num, label, repo))

    # Patch points: top_issues + attempts.fetch_history_strict +
    # attempts.record + ThreadPoolExecutor's task (run_worker is captured by
    # name in runner via `from forge_loop.worker import run_worker`).
    monkeypatch.setattr(_runner, "top_issues", fake_top_issues)
    monkeypatch.setattr(_attempts, "fetch_history_strict", fake_fetch_history_strict)
    monkeypatch.setattr(_attempts, "fetch_blocking_comments", fake_fetch_blocking_comments)
    monkeypatch.setattr(_attempts, "record", fake_record)
    monkeypatch.setattr(_runner, "run_worker", fake_run_worker)
    monkeypatch.setattr(_runner, "unlabel", fake_unlabel)
    monkeypatch.setattr(_gh, "get_issue_state", lambda *_a, **_kw: "OPEN")
    monkeypatch.setattr(
        _gh,
        "enable_pr_auto_merge",
        lambda pr, repo=None: state.automerge_calls.append((pr, repo)) is None,
    )
    # Issue #213 — orphaned-PR adoption scan fakes. By default no open PRs, so
    # the scan is a no-op for legacy tests. Adoption tests populate
    # ``state.open_prs`` / ``state.issue_states`` to exercise the path.
    def fake_open_prs(limit: int, repo: str | None = None):
        out: list[dict[str, Any]] = []
        for pr in state.open_prs:
            enriched = dict(pr)
            url = enriched.get("url")
            # Reflect any labels added during this run (idempotency marker).
            base = list(enriched.get("labels") or [])
            for lab in state.pr_labels.get(str(url), []):
                if {"name": lab} not in base:
                    base.append({"name": lab})
            enriched["labels"] = base
            out.append(enriched)
        return out

    def fake_fetch_issue(num: int, repo: str | None = None):
        st = state.issue_states.get(num, "open")
        return {
            "number": num,
            "title": "demo",
            "body": "do the thing",
            "state": st,
            "labels": [],
        }

    def fake_add_pr_label(pr, labels, repo=None):
        state.pr_labels.setdefault(str(pr), []).extend(labels)
        return True

    def fake_unresolved_threads(pr, repo=None):
        return list(state.unresolved_threads.get(str(pr), []))

    def fake_critic(_cfg, outcomes, _emit):
        for o in outcomes:
            if o.pr_url:
                state.critic_runs.append(o.pr_url)

    monkeypatch.setattr(_tick_mod, "open_prs", fake_open_prs)
    monkeypatch.setattr(_tick_mod, "fetch_issue", fake_fetch_issue)
    monkeypatch.setattr(_gh, "add_pr_label", fake_add_pr_label)
    monkeypatch.setattr(_gh, "unresolved_review_threads", fake_unresolved_threads)
    state.fake_critic = fake_critic  # type: ignore[attr-defined]

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


def test_open_pr_removes_ready_label(fake_world) -> None:
    state, cfg, _ = fake_world
    state.scripted_status = "open"
    state.scripted_pr = "https://github.com/o/r/pull/100"
    state.scripted_error = None

    _runner._tick(cfg, tick=1)

    assert state.unlabeled == [(99, "loop:ready", "o/r")]
    events = _read_events(cfg)
    removed = next(e for e in events if e["kind"] == "issue_ready_label_removed")
    assert removed["issue"] == 99
    assert removed["status"] == "open"
    assert removed["pr_url"] == "https://github.com/o/r/pull/100"


def test_open_pr_automerge_is_runner_owned_after_gates(fake_world) -> None:
    state, cfg, _ = fake_world
    state.scripted_status = "open"
    state.scripted_pr = "https://github.com/o/r/pull/100"
    state.scripted_error = None

    _runner._tick(cfg, tick=1)

    assert state.automerge_calls == [("https://github.com/o/r/pull/100", "o/r")]
    assert state.history[99][0]["status"] == "merged"
    events = _read_events(cfg)
    assert "post_critic_automerge_enabled" in _kinds(events)


def test_risk_gated_open_pr_does_not_automerge(fake_world) -> None:
    state, cfg, issue = fake_world
    issue["labels"] = [{"name": "risk:high"}]
    state.scripted_status = "open"
    state.scripted_pr = "https://github.com/o/r/pull/100"
    state.scripted_error = None

    _runner._tick(cfg, tick=1)

    assert state.automerge_calls == []
    assert state.history[99][0]["status"] == "open"


def test_attempt_record_uses_post_critic_status(fake_world, monkeypatch) -> None:
    state, cfg, _ = fake_world
    cfg = replace(cfg, critic=replace(cfg.critic, enabled=True))
    state.scripted_status = "merged"
    state.scripted_pr = "https://github.com/o/r/pull/100"
    state.scripted_error = None

    def critic_blocks(_cfg, outcomes, _emit):
        outcomes[0].status = "open"
        outcomes[0].error = "critic blocked"

    monkeypatch.setattr(_tick_mod, "_run_critic_for_outcomes", critic_blocks)

    _runner._tick(cfg, tick=1)

    assert state.history[99][0]["status"] == "open"
    assert state.history[99][0]["note"] == "critic blocked"


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
    assert state.unlabeled == [(99, "loop:ready", "o/r")]
    removed = next(e for e in events if e["kind"] == "issue_ready_label_removed")
    assert removed["issue"] == 99
    assert removed["status"] == "in_flight"
    assert removed["pr_url"] == "https://github.com/o/r/pull/777"


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


def test_ready_issue_existing_open_pr_repairs_instead_of_redispatch(
    fake_world, monkeypatch
) -> None:
    state, cfg, _ = fake_world
    state.history[99] = [{
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "open",
        "pr_url": "https://github.com/o/r/pull/777",
        "brief_fingerprint": "stale",
    }]
    repair_calls: list[tuple[int, int, str]] = []
    pr_url = "https://github.com/o/r/pull/777"

    monkeypatch.setattr(
        _tick_mod,
        "prs_by_label",
        lambda *_a, **_k: [{
            "number": 777,
            "url": pr_url,
            "headRefName": "loop/99-retarget-existing-pr",
        }],
    )
    monkeypatch.setattr(_tick_mod, "pr_review_context", lambda *_a, **_k: "review context")
    monkeypatch.setattr(_gh, "unresolved_review_threads", lambda *_a, **_k: [])

    def fake_run_repair_worker(issue, pr, review_context, *_args, **_kwargs):
        repair_calls.append((issue["number"], pr["number"], review_context))
        return WorkerOutcome(
            issue=issue["number"],
            title=issue["title"],
            pr_url=pr_url,
            status="open",
            duration_s=1.0,
            stdout_tail="",
            events=[],
        )

    monkeypatch.setattr(_dispatch_mod, "run_repair_worker", fake_run_repair_worker)

    _runner._tick(cfg, tick=1)

    assert state.dispatched == []
    assert repair_calls == [(99, 777, "review context")]
    assert state.unlabeled == [(99, "loop:ready", "o/r")]
    events = _read_events(cfg)
    assert "ready_issue_open_pr_repair_tick_start" in _kinds(events)
    assert "ready_issue_open_pr_selected" in _kinds(events)


def test_blocking_comment_invalidates_cooldown_and_reaches_worker(fake_world, monkeypatch) -> None:
    state, cfg, issue = fake_world
    monkeypatch.setenv("LOOP_RETRY_COOLDOWN_S", "3600")
    stale_fp = _attempts.compute_fingerprint(
        issue["number"], issue["body"], _worker.brief_template_hash(),
    )
    state.history[99] = [{
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": "no_pr",
        "pr_url": None,
        "brief_fingerprint": stale_fp,
    }]
    state.blocking_comments[99] = [
        "Post-merge critic found this incomplete.\n"
        "Required repair: add the exact native proof, not an adjacent edge test."
    ]

    _runner._tick(cfg, tick=1)

    assert state.dispatched == [99]
    assert state.worker_kwargs[0]["blocking_comments"] == state.blocking_comments[99]
    assert "worker_skip_cooldown" not in _kinds(_read_events(cfg))


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


# ---------------------------------------------------------------------------
# Issue #213 — orphaned clean PR adoption (integration)
# ---------------------------------------------------------------------------

def _adoptable_pr(num: int, *, labels=None, merge_state="CLEAN") -> dict[str, Any]:
    return {
        "number": num,
        "url": f"https://github.com/o/r/pull/{num}",
        "headRefName": f"loop/{num}-add-thing",
        "labels": labels or [],
        "mergeStateStatus": merge_state,
    }


def test_orphaned_clean_pr_adopted_runs_critic_and_automerge(fake_world, monkeypatch) -> None:
    state, cfg, _ = fake_world
    cfg = replace(cfg, critic=replace(cfg.critic, enabled=True))
    pr_url = "https://github.com/o/r/pull/205"
    state.open_prs = [_adoptable_pr(205)]
    monkeypatch.setattr(_tick_mod, "_run_critic_for_outcomes", state.fake_critic)

    _runner._tick(cfg, tick=1)

    assert state.critic_runs == [pr_url]
    assert state.automerge_calls == [(pr_url, "o/r")]
    assert state.dispatched == []  # adoption path, no fresh worker dispatch
    events = _read_events(cfg)
    assert "orphan_pr_adopted" in _kinds(events)
    assert "orphan_pr_automerge_enabled" in _kinds(events)
    # Idempotency marker stamped.
    assert state.pr_labels.get(pr_url) == ["loop:adopted"]


def test_orphaned_blocking_pr_is_not_adopted(fake_world, monkeypatch) -> None:
    state, cfg, _ = fake_world
    cfg = replace(cfg, critic=replace(cfg.critic, enabled=True))
    state.open_prs = [_adoptable_pr(207, labels=[{"name": "critic:blocking"}])]
    monkeypatch.setattr(_tick_mod, "_run_critic_for_outcomes", state.fake_critic)

    _runner._tick(cfg, tick=1)

    assert state.automerge_calls == []
    assert "https://github.com/o/r/pull/207" not in state.critic_runs
    events = _read_events(cfg)
    skip = next(e for e in events if e["kind"] == "orphan_pr_skipped")
    assert skip["reason"] == "critic_blocked"


def test_orphan_adoption_is_idempotent_across_ticks(fake_world, monkeypatch) -> None:
    state, cfg, _ = fake_world
    cfg = replace(cfg, critic=replace(cfg.critic, enabled=True))
    pr_url = "https://github.com/o/r/pull/211"
    state.open_prs = [_adoptable_pr(211)]
    monkeypatch.setattr(_tick_mod, "_run_critic_for_outcomes", state.fake_critic)

    _runner._tick(cfg, tick=1)
    _runner._tick(cfg, tick=2)

    assert state.critic_runs == [pr_url]  # critic ran exactly once
    assert state.automerge_calls == [(pr_url, "o/r")]  # auto-merge enabled once
    reasons = [e.get("reason") for e in _read_events(cfg) if e["kind"] == "orphan_pr_skipped"]
    assert "already_adopted" in reasons


def test_orphan_adoption_skips_closed_issue(fake_world, monkeypatch) -> None:
    state, cfg, _ = fake_world
    cfg = replace(cfg, critic=replace(cfg.critic, enabled=True))
    state.open_prs = [_adoptable_pr(205)]
    state.issue_states[205] = "closed"
    monkeypatch.setattr(_tick_mod, "_run_critic_for_outcomes", state.fake_critic)

    _runner._tick(cfg, tick=1)

    assert state.automerge_calls == []
    assert "https://github.com/o/r/pull/205" not in state.critic_runs
    skip = next(e for e in _read_events(cfg) if e["kind"] == "orphan_pr_skipped")
    assert skip["reason"] == "issue_closed"


def test_orphan_adoption_ignores_human_pr(fake_world, monkeypatch) -> None:
    state, cfg, _ = fake_world
    cfg = replace(cfg, critic=replace(cfg.critic, enabled=True))
    state.open_prs = [{
        "number": 300,
        "url": "https://github.com/o/r/pull/300",
        "headRefName": "feature/manual-fix",
        "labels": [],
        "mergeStateStatus": "CLEAN",
    }]
    monkeypatch.setattr(_tick_mod, "_run_critic_for_outcomes", state.fake_critic)

    _runner._tick(cfg, tick=1)

    assert "https://github.com/o/r/pull/300" not in state.critic_runs
    assert state.automerge_calls == []
    assert "orphan_pr_adopted" not in _kinds(_read_events(cfg))


def test_orphan_adoption_unresolved_threads_blocks_automerge(fake_world, monkeypatch) -> None:
    state, cfg, _ = fake_world
    cfg = replace(cfg, critic=replace(cfg.critic, enabled=True))
    pr_url = "https://github.com/o/r/pull/205"
    state.open_prs = [_adoptable_pr(205)]
    state.unresolved_threads[pr_url] = [{"id": "t1"}]
    monkeypatch.setattr(_tick_mod, "_run_critic_for_outcomes", state.fake_critic)

    _runner._tick(cfg, tick=1)

    assert state.critic_runs == [pr_url]  # critic still runs
    assert state.automerge_calls == []  # but auto-merge is NOT enabled
    skip = [
        e for e in _read_events(cfg)
        if e["kind"] == "orphan_pr_skipped" and e.get("reason") == "unresolved_review_threads"
    ]
    assert skip


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
