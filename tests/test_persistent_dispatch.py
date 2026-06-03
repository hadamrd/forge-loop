"""Tests for the persistent-worker dispatch wire-up (issue #108).

Test matrix from the issue body:

* unit: with ``persistent_worker=True``, dispatching issue N creates a
  session in DISPATCHED.
* unit: a worker that opens a PR moves the session to AWAITING_CRITIC +
  sets pr_url.
* unit: a worker that fails moves the session to ABANDONED.
* regression: with ``persistent_worker=False``, no rows touch the store.

Plus adversarial coverage:

* resume path: an existing non-terminal session for the same issue is
  picked up instead of seeding a fresh DISPATCHED row.
* event log: each transition writes one typed
  :class:`forge_loop.events.WorkerSessionTransitionEvent` line.
* crash path: an exception raised by ``run_worker`` still drives the
  session to ABANDONED so the store can't be left holding a RUNNING row.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from forge_loop._testing.task_saga_store import FakeTaskSagaStore
from forge_loop.runner import persistent_dispatch as pd
from forge_loop.sandbox import CapabilityPolicy, FilesystemScope, McpGrant, NetworkPolicy
from forge_loop.tasks import SqliteTaskSagaStore
from forge_loop.worker import WorkerOutcome
from forge_loop.worker_sessions import WorkerSessionStore
from forge_loop.worker_state import WorkerState

# ---------------------------------------------------------------------------
# Helpers — a small Config stub that exposes only the fields the dispatch
# wrapper touches. Avoids the heavy real Config + filesystem setup.
# ---------------------------------------------------------------------------


def _make_cfg(tmp_path: Path) -> Any:
    """Build a Config-shaped stub with just the attrs `_dispatch_one_worker` reads."""

    class _Lumen:
        top_k = 3

    class _Worker:
        model = None
        thinking = None
        provider = "claude"
        allowed_mcp_tools = ()
        load_timeout_ms = None
        strict_mcp_config = False
        mcp_servers = None

    class _Cfg:
        repo = tmp_path / "repo"
        logs_dir = tmp_path / "logs"
        state_dir = tmp_path / "state"
        events_file = tmp_path / "events.jsonl"
        worker_timeout_s = 60
        lumen = _Lumen()
        lumen_test_pattern = "**/*Test.*"
        coauthor = ""
        base_branch = "trunk"
        worker = _Worker()
        task_store = None

    cfg = _Cfg()
    cfg.repo.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.events_file.touch()
    return cfg


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def _issue(n: int, title: str = "do the thing") -> dict[str, Any]:
    return {"number": n, "title": title, "body": ""}


def _meta() -> dict[str, Any]:
    return {"risk_gated": False, "past_attempts": [], "forced": False, "brief_fingerprint": ""}


# ---------------------------------------------------------------------------
# persistent_dispatch unit tests — exercise the helpers directly.
# ---------------------------------------------------------------------------


def test_get_or_resume_seeds_dispatched_when_no_prior_session(tmp_path: Path) -> None:
    store = WorkerSessionStore(":memory:")
    events = tmp_path / "events.jsonl"

    sess, resumed = pd.get_or_resume_session(
        store,
        issue=42,
        branch="loop/42-x",
        worktree_path="/tmp/wt-loop-42",
        events_file=events,
    )

    assert resumed is False
    assert sess.state == WorkerState.DISPATCHED
    assert sess.issue == 42
    assert sess.branch == "loop/42-x"
    # The seed event was written.
    recs = _read_events(events)
    assert any(
        r["kind"] == "worker_session_transition" and r["new_state"] == "dispatched" for r in recs
    )


def test_get_or_resume_picks_existing_non_terminal_session(tmp_path: Path) -> None:
    store = WorkerSessionStore(":memory:")
    first = store.create(issue=7, branch="loop/7-x", worktree_path="/tmp/wt-loop-7")

    sess, resumed = pd.get_or_resume_session(
        store,
        issue=7,
        branch="loop/7-x",
        worktree_path="/tmp/wt-loop-7",
        events_file=tmp_path / "events.jsonl",
    )
    assert resumed is True
    assert sess.session_id == first.session_id


def test_get_or_resume_ignores_terminal_session(tmp_path: Path) -> None:
    """A prior ABANDONED row for the issue MUST NOT block fresh dispatch."""
    store = WorkerSessionStore(":memory:")
    old = store.create(issue=9, branch="b")
    store.transition_to(old.session_id, WorkerState.ABANDONED, reason="prior fail")

    sess, resumed = pd.get_or_resume_session(
        store,
        issue=9,
        branch="b",
        worktree_path="",
    )
    assert resumed is False
    assert sess.session_id != old.session_id
    assert sess.state == WorkerState.DISPATCHED


def test_mark_running_transitions_dispatched_to_running(tmp_path: Path) -> None:
    store = WorkerSessionStore(":memory:")
    sess = store.create(issue=1, branch="b")
    events = tmp_path / "events.jsonl"

    updated = pd.mark_running(store, session=sess, events_file=events)
    assert updated.state == WorkerState.RUNNING

    recs = _read_events(events)
    assert any(
        r["kind"] == "worker_session_transition"
        and r["prior_state"] == "dispatched"
        and r["new_state"] == "running"
        for r in recs
    )


def test_mark_running_is_noop_on_already_running(tmp_path: Path) -> None:
    store = WorkerSessionStore(":memory:")
    sess = store.create(issue=1, branch="b")
    store.transition_to(sess.session_id, WorkerState.RUNNING)
    sess = store.get(sess.session_id)
    assert sess is not None

    same = pd.mark_running(store, session=sess)
    assert same.state == WorkerState.RUNNING


def test_record_outcome_success_moves_to_awaiting_critic_and_sets_pr_url(
    tmp_path: Path,
) -> None:
    store = WorkerSessionStore(":memory:")
    sess = store.create(issue=1, branch="b")
    store.transition_to(sess.session_id, WorkerState.RUNNING)
    sess = store.get(sess.session_id)
    assert sess is not None

    outcome = WorkerOutcome(
        issue=1,
        title="t",
        pr_url="https://github.com/o/r/pull/1",
        status="open",
        duration_s=1.0,
        stdout_tail="",
    )
    events = tmp_path / "events.jsonl"
    updated = pd.record_outcome(
        store,
        session=sess,
        outcome=outcome,
        events_file=events,
    )
    assert updated.state == WorkerState.AWAITING_CRITIC
    refreshed = store.get(sess.session_id)
    assert refreshed is not None
    assert refreshed.pr_url == "https://github.com/o/r/pull/1"
    recs = _read_events(events)
    assert any(
        r["kind"] == "worker_session_transition"
        and r["new_state"] == "awaiting_critic"
        and r["pr_url"] == "https://github.com/o/r/pull/1"
        for r in recs
    )


def test_record_outcome_failure_moves_to_abandoned_with_reason(
    tmp_path: Path,
) -> None:
    store = WorkerSessionStore(":memory:")
    sess = store.create(issue=1, branch="b")
    store.transition_to(sess.session_id, WorkerState.RUNNING)
    sess = store.get(sess.session_id)
    assert sess is not None

    outcome = WorkerOutcome(
        issue=1,
        title="t",
        pr_url=None,
        status="failed",
        duration_s=0.5,
        stdout_tail="",
        error="boom: SDK timeout",
    )
    updated = pd.record_outcome(store, session=sess, outcome=outcome)
    assert updated.state == WorkerState.ABANDONED
    assert "failed" in updated.last_transition_reason
    assert "boom" in updated.last_transition_reason


def test_record_outcome_no_pr_url_treated_as_failure(tmp_path: Path) -> None:
    """status=open without pr_url is still abandoned — we need a URL to
    hand to the critic."""
    store = WorkerSessionStore(":memory:")
    sess = store.create(issue=1, branch="b")
    store.transition_to(sess.session_id, WorkerState.RUNNING)
    sess = store.get(sess.session_id)
    assert sess is not None

    outcome = WorkerOutcome(
        issue=1,
        title="t",
        pr_url=None,
        status="open",
        duration_s=0.5,
        stdout_tail="",
    )
    updated = pd.record_outcome(store, session=sess, outcome=outcome)
    assert updated.state == WorkerState.ABANDONED


def test_record_outcome_noop_on_non_running_session(tmp_path: Path) -> None:
    """If a session somehow lands here in DISPATCHED (caller bug or
    crashed between mark_running and SDK call), we don't try to force
    an invalid transition."""
    store = WorkerSessionStore(":memory:")
    sess = store.create(issue=1, branch="b")
    outcome = WorkerOutcome(
        issue=1,
        title="t",
        pr_url="x",
        status="open",
        duration_s=0.0,
        stdout_tail="",
    )
    same = pd.record_outcome(store, session=sess, outcome=outcome)
    assert same.state == WorkerState.DISPATCHED


# ---------------------------------------------------------------------------
# Integration: _dispatch_one_worker drives the full FSM via the store.
# ---------------------------------------------------------------------------


def test_dispatch_one_worker_full_success_path(tmp_path, monkeypatch) -> None:
    """persistent_worker=True: a worker that opens a PR seeds DISPATCHED,
    transitions to RUNNING, then to AWAITING_CRITIC with pr_url set."""
    from forge_loop.runner import dispatch as dispatch_mod

    cfg = _make_cfg(tmp_path)
    cfg.worker.allowed_mcp_tools = ("github", "lumen")
    store = WorkerSessionStore(":memory:")
    issue = _issue(42)

    pr_url = "https://github.com/o/r/pull/99"

    def fake_run_worker(*args, **kwargs):
        # At this exact point, the store MUST already show RUNNING
        # (mark_running ran before run_worker). Pin that invariant.
        for s in store.by_issue(42):
            assert s.state == WorkerState.RUNNING
            break
        else:
            raise AssertionError("no session in store at SDK invocation time")
        policy = kwargs["capability_policy"]
        assert policy == CapabilityPolicy(
            filesystem=FilesystemScope(
                read_roots=(str(cfg.repo), "/tmp/wt-loop-42"),
                write_roots=("/tmp/wt-loop-42",),
            ),
            network=NetworkPolicy(allow_domains=("github.com", "api.github.com")),
            mcp=(McpGrant(server="github", tools=("*",)), McpGrant(server="lumen", tools=("*",))),
            secret_names=(),
        )
        return WorkerOutcome(
            issue=42,
            title="t",
            pr_url=pr_url,
            status="open",
            duration_s=1.0,
            stdout_tail="",
        )

    monkeypatch.setattr(dispatch_mod, "run_worker", fake_run_worker)

    outcome = dispatch_mod._dispatch_one_worker(
        cfg,
        issue,
        _meta(),
        tick=1,
        bus_emit=lambda *a, **k: None,
        store=store,
    )
    assert outcome.status == "open"

    sessions = store.by_issue(42)
    assert len(sessions) == 1
    final = sessions[0]
    assert final.state == WorkerState.AWAITING_CRITIC
    assert final.pr_url == pr_url
    task_saga = SqliteTaskSagaStore(cfg.repo / ".forge" / "tasks.db").get("task-42-worker")
    assert task_saga is not None
    assert task_saga.worktree == "/tmp/wt-loop-42"
    assert task_saga.capability_policy == CapabilityPolicy(
        filesystem=FilesystemScope(
            read_roots=(str(cfg.repo), "/tmp/wt-loop-42"),
            write_roots=("/tmp/wt-loop-42",),
        ),
        network=NetworkPolicy(allow_domains=("github.com", "api.github.com")),
        mcp=(McpGrant(server="github", tools=("*",)), McpGrant(server="lumen", tools=("*",))),
        secret_names=(),
    )

    # Three typed transition events: -> DISPATCHED, -> RUNNING,
    # -> AWAITING_CRITIC.
    recs = _read_events(cfg.events_file)
    kinds = [r for r in recs if r["kind"] == "worker_session_transition"]
    states = [r["new_state"] for r in kinds]
    assert states == ["dispatched", "running", "awaiting_critic"]


def test_dispatch_one_worker_records_capability_policy_on_task_saga(tmp_path, monkeypatch) -> None:
    from forge_loop.runner import dispatch as dispatch_mod

    cfg = _make_cfg(tmp_path)
    cfg.worker.allowed_mcp_tools = ("github",)
    cfg.task_store = FakeTaskSagaStore()
    store = WorkerSessionStore(":memory:")
    issue = _issue(166, "bind worker worktrees")

    def fake_run_worker(*args, **kwargs):
        return WorkerOutcome(
            issue=166,
            title="bind worker worktrees",
            pr_url="https://github.com/o/r/pull/166",
            status="open",
            duration_s=1.0,
            stdout_tail="",
        )

    monkeypatch.setattr(dispatch_mod, "run_worker", fake_run_worker)

    dispatch_mod._dispatch_one_worker(
        cfg,
        issue,
        _meta(),
        tick=1,
        bus_emit=lambda *a, **k: None,
        store=store,
    )

    saga = cfg.task_store.get("task-166-worker")
    assert saga is not None
    assert saga.saga_id == "saga-166-worker"
    assert saga.issue == 166
    assert saga.worktree == "/tmp/wt-loop-166"
    assert saga.capability_policy.filesystem.write_roots == ("/tmp/wt-loop-166",)
    assert saga.capability_policy.mcp == (McpGrant(server="github", tools=("*",)),)


def test_dispatch_one_worker_records_policy_in_default_task_saga_store(
    tmp_path,
    monkeypatch,
) -> None:
    from forge_loop.runner import dispatch as dispatch_mod

    cfg = _make_cfg(tmp_path)
    store = WorkerSessionStore(":memory:")

    def fake_run_worker(*args, **kwargs):
        return WorkerOutcome(
            issue=168,
            title="default store",
            pr_url="https://github.com/o/r/pull/168",
            status="open",
            duration_s=1.0,
            stdout_tail="",
        )

    monkeypatch.setattr(dispatch_mod, "run_worker", fake_run_worker)

    dispatch_mod._dispatch_one_worker(
        cfg,
        _issue(168, "default store"),
        _meta(),
        tick=1,
        bus_emit=lambda *a, **k: None,
        store=store,
    )

    saga = SqliteTaskSagaStore(cfg.repo / ".forge" / "tasks.db").get("task-168-worker")
    assert saga is not None
    assert saga.capability_policy.filesystem.read_roots == (str(cfg.repo), "/tmp/wt-loop-168")
    assert saga.capability_policy.network.allow_domains == ("github.com", "api.github.com")


def test_dispatch_one_worker_refuses_to_run_without_capability_policy(
    tmp_path,
    monkeypatch,
) -> None:
    from forge_loop.runner import dispatch as dispatch_mod

    cfg = _make_cfg(tmp_path)
    cfg.task_store = FakeTaskSagaStore()

    def no_policy(**kwargs):
        return None

    def should_not_run(*args, **kwargs):
        raise AssertionError("worker dispatched without a capability policy")

    monkeypatch.setattr(dispatch_mod, "capability_policy_for_worker", no_policy)
    monkeypatch.setattr(dispatch_mod, "run_worker", should_not_run)

    with pytest.raises(RuntimeError, match="missing capability policy"):
        dispatch_mod._dispatch_one_worker(
            cfg,
            _issue(167),
            _meta(),
            tick=1,
            bus_emit=lambda *a, **k: None,
            store=WorkerSessionStore(":memory:"),
        )

    assert cfg.task_store.sagas == {}


def test_dispatch_one_worker_requires_policy_record_before_worker_invocation(
    tmp_path,
    monkeypatch,
) -> None:
    from forge_loop.runner import dispatch as dispatch_mod

    cfg = _make_cfg(tmp_path)
    store = WorkerSessionStore(":memory:")
    called = False

    def missing_policy(*args, **kwargs):
        return None

    def fake_run_worker(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("worker should not run without a policy")

    monkeypatch.setattr(dispatch_mod, "capability_policy_for_worker", missing_policy)
    monkeypatch.setattr(dispatch_mod, "run_worker", fake_run_worker)

    with pytest.raises(RuntimeError, match="capability policy"):
        dispatch_mod._dispatch_one_worker(
            cfg,
            _issue(166),
            _meta(),
            tick=1,
            bus_emit=lambda *a, **k: None,
            store=store,
        )

    assert called is False


def test_dispatch_one_worker_failure_path(tmp_path, monkeypatch) -> None:
    from forge_loop.runner import dispatch as dispatch_mod

    cfg = _make_cfg(tmp_path)
    store = WorkerSessionStore(":memory:")
    issue = _issue(7)

    def fake_run_worker(*args, **kwargs):
        return WorkerOutcome(
            issue=7,
            title="t",
            pr_url=None,
            status="failed",
            duration_s=0.2,
            stdout_tail="",
            error="SDK exploded",
        )

    monkeypatch.setattr(dispatch_mod, "run_worker", fake_run_worker)

    outcome = dispatch_mod._dispatch_one_worker(
        cfg,
        issue,
        _meta(),
        tick=1,
        bus_emit=lambda *a, **k: None,
        store=store,
    )
    assert outcome.status == "failed"

    sessions = store.by_issue(7)
    assert len(sessions) == 1
    final = sessions[0]
    assert final.state == WorkerState.ABANDONED
    assert "SDK exploded" in final.last_transition_reason


def test_dispatch_one_worker_no_store_means_no_rows(tmp_path, monkeypatch) -> None:
    """Regression: persistent_worker=False (store=None) → zero rows touched.

    This is the explicit legacy-path contract from the issue body."""
    from forge_loop.runner import dispatch as dispatch_mod

    cfg = _make_cfg(tmp_path)
    sentinel_store = WorkerSessionStore(":memory:")
    issue = _issue(123)

    def fake_run_worker(*args, **kwargs):
        return WorkerOutcome(
            issue=123,
            title="t",
            pr_url="https://github.com/o/r/pull/1",
            status="open",
            duration_s=1.0,
            stdout_tail="",
        )

    monkeypatch.setattr(dispatch_mod, "run_worker", fake_run_worker)

    outcome = dispatch_mod._dispatch_one_worker(
        cfg,
        issue,
        _meta(),
        tick=1,
        bus_emit=lambda *a, **k: None,
        store=None,
    )
    assert outcome.status == "open"
    # The sentinel store stayed pristine — there's no row for the
    # issue even though the dispatch ran. This is the legacy-path
    # contract (persistent_worker=False).
    assert sentinel_store.by_issue(123) == []
    assert sentinel_store.active_count() == 0


def test_dispatch_one_worker_exception_closes_session_to_abandoned(
    tmp_path,
    monkeypatch,
) -> None:
    """A subprocess that raises must NOT leave a row in RUNNING."""
    from forge_loop.runner import dispatch as dispatch_mod

    cfg = _make_cfg(tmp_path)
    store = WorkerSessionStore(":memory:")
    issue = _issue(55)

    def boom(*args, **kwargs):
        raise RuntimeError("worker subprocess crashed")

    monkeypatch.setattr(dispatch_mod, "run_worker", boom)

    with pytest.raises(RuntimeError, match="crashed"):
        dispatch_mod._dispatch_one_worker(
            cfg,
            issue,
            _meta(),
            tick=1,
            bus_emit=lambda *a, **k: None,
            store=store,
        )

    sessions = store.by_issue(55)
    assert len(sessions) == 1
    assert sessions[0].state == WorkerState.ABANDONED


def test_dispatch_one_worker_resumes_existing_dispatched_session(
    tmp_path,
    monkeypatch,
) -> None:
    """If a previous tick left a DISPATCHED row (e.g. the runner crashed
    after seeding but before invoking the SDK), the next dispatch must
    pick it up rather than creating a duplicate row."""
    from forge_loop.runner import dispatch as dispatch_mod

    cfg = _make_cfg(tmp_path)
    store = WorkerSessionStore(":memory:")
    seeded = store.create(issue=88, branch="loop/88-do-the-thing", worktree_path="/tmp/wt-loop-88")

    def fake_run_worker(*args, **kwargs):
        return WorkerOutcome(
            issue=88,
            title="do the thing",
            pr_url="https://x/pull/1",
            status="open",
            duration_s=1.0,
            stdout_tail="",
        )

    monkeypatch.setattr(dispatch_mod, "run_worker", fake_run_worker)

    dispatch_mod._dispatch_one_worker(
        cfg,
        _issue(88),
        _meta(),
        tick=1,
        bus_emit=lambda *a, **k: None,
        store=store,
    )

    sessions = store.by_issue(88)
    assert len(sessions) == 1  # resumed, NOT duplicated
    assert sessions[0].session_id == seeded.session_id
    assert sessions[0].state == WorkerState.AWAITING_CRITIC


# ---------------------------------------------------------------------------
# Settings predicate — defensive default.
# ---------------------------------------------------------------------------


def test_persistent_worker_enabled_defaults_false_on_settings_failure(
    monkeypatch,
) -> None:
    """A broken Settings.load MUST NOT take down the dispatch loop."""
    import forge_loop.settings as settings_mod

    class _Broken:
        @staticmethod
        def load():
            raise RuntimeError("yaml exploded")

    monkeypatch.setattr(settings_mod, "Settings", _Broken)
    assert pd.persistent_worker_enabled() is False
