"""Worker-dispatch -> task-saga lifecycle (the runner cut-over).

The runner now records a durable task saga around every worker dispatch,
at the canonical `.forge/tasks.db` that `init` seeds and `boot` reads:

* dispatch seeds + leases the saga (RUNNING) so a dead worker becomes stale,
* a worker outcome drives the saga to a terminal state (completed/failed),
* a caught crash marks it terminal; a hard kill (no `except`) leaves a leased
  saga that the next boot reads as stale,

so `assemble_boot_context` reflects what was really running after a reset.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from forge_loop.control.boot import assemble_boot_context, build_boot_sources
from forge_loop.frontier import FrontierCursor, FrontierStore
from forge_loop.runner import dispatch as dispatch_mod
from forge_loop.tasks import SqliteTaskSagaStore, TaskState
from forge_loop.worker import WorkerOutcome
from forge_loop.worker_sessions import WorkerSessionStore

# Reuse the Config/issue/meta scaffolding from the dispatch FSM tests.
from tests.test_persistent_dispatch import _issue, _make_cfg, _meta


def _saga_store(cfg: Any) -> SqliteTaskSagaStore:
    return SqliteTaskSagaStore(dispatch_mod.canonical_task_saga_path(cfg.repo))


def test_dispatch_seeds_leases_then_completes_saga(monkeypatch: Any, tmp_path: Any) -> None:
    cfg = _make_cfg(tmp_path)

    def fake_run_worker(*args: Any, **kwargs: Any) -> WorkerOutcome:
        # Mid-flight the saga must be RUNNING under a lease (so a crash here
        # would later read as stale rather than vanish).
        live = _saga_store(cfg).get("task-7-worker")
        assert live is not None
        assert live.state == TaskState.RUNNING
        assert live.lease_owner == "worker-7-tick-1"
        assert live.lease_expires_at is not None
        return WorkerOutcome(
            issue=7,
            title="t",
            pr_url="https://x/pull/1",
            status="open",
            duration_s=1.0,
            stdout_tail="",
        )

    monkeypatch.setattr(dispatch_mod, "run_worker", fake_run_worker)

    outcome = dispatch_mod._dispatch_one_worker(
        cfg, _issue(7), _meta(), tick=1, bus_emit=lambda *a, **k: None, store=None
    )

    assert outcome.status == "open"
    saga = _saga_store(cfg).get("task-7-worker")
    assert saga is not None
    assert saga.state == TaskState.COMPLETED
    assert saga.is_terminal
    # A completed saga drains from the in-flight recovery view.
    assert _saga_store(cfg).list_in_flight() == ()


def test_dispatch_seeds_delete_branch_and_appends_close_pr(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """#272: dispatch seeds a delete-branch compensation (target=branch) at the
    saga, and appends a close-pr compensation (target=PR number) once the worker
    opens its PR — so a later abandonment can reverse both side-effects."""
    cfg = _make_cfg(tmp_path)

    seeded_branch: dict[str, str] = {}

    def fake_run_worker(*args: Any, **kwargs: Any) -> WorkerOutcome:
        live = _saga_store(cfg).get("task-7-worker")
        assert live is not None
        kinds = {c.kind for c in live.compensations}
        assert "remove-worktree" in kinds
        assert "delete-branch" in kinds
        # The delete-branch target is the branch ref recorded on the saga.
        branch_comp = next(c for c in live.compensations if c.kind == "delete-branch")
        assert branch_comp.target == live.branch
        seeded_branch["b"] = live.branch or ""
        return WorkerOutcome(
            issue=7,
            title="t",
            pr_url="https://github.com/o/r/pull/4242",
            status="open",
            duration_s=1.0,
            stdout_tail="",
        )

    monkeypatch.setattr(dispatch_mod, "run_worker", fake_run_worker)

    dispatch_mod._dispatch_one_worker(
        cfg, _issue(7), _meta(), tick=1, bus_emit=lambda *a, **k: None, store=None
    )

    saga = _saga_store(cfg).get("task-7-worker")
    assert saga is not None
    kinds = [c.kind for c in saga.compensations]
    assert kinds == ["remove-worktree", "delete-branch", "close-pr"]
    close = next(c for c in saga.compensations if c.kind == "close-pr")
    assert close.target == "4242"
    assert seeded_branch["b"]  # the branch was actually seeded mid-flight


def test_record_worker_task_policy_seeds_delete_branch(tmp_path: Any) -> None:
    """#272 (both seed paths): the fallback policy-recording path also seeds a
    delete-branch compensation alongside remove-worktree."""
    from forge_loop.sandbox import CapabilityPolicy

    repo = tmp_path
    saga = dispatch_mod.record_worker_task_policy(
        repo=repo,
        task_id="task-7-worker",
        saga_id="saga-7-worker",
        issue=7,
        branch="loop/7-feat",
        worktree_path="/tmp/wt-loop-7",
        capability_policy=CapabilityPolicy(),
    )
    by_kind = {c.kind: c.target for c in saga.compensations}
    assert by_kind["remove-worktree"] == "/tmp/wt-loop-7"
    assert by_kind["delete-branch"] == "loop/7-feat"


def test_legacy_crash_marks_saga_failed(monkeypatch: Any, tmp_path: Any) -> None:
    cfg = _make_cfg(tmp_path)

    def boom(*args: Any, **kwargs: Any) -> WorkerOutcome:
        raise RuntimeError("sdk init died")

    monkeypatch.setattr(dispatch_mod, "run_worker", boom)

    with pytest.raises(RuntimeError):
        dispatch_mod._dispatch_one_worker(
            cfg, _issue(7), _meta(), tick=1, bus_emit=lambda *a, **k: None, store=None
        )

    # A caught exception is a known failure on both dispatch paths: the saga is
    # driven terminal (FAILED, carrying its compensation) and drains from the
    # in-flight recovery view. The lease->stale safety net is for *hard* kills
    # (SIGKILL) where no `except` can run; that is exercised at the boot level.
    store = _saga_store(cfg)
    saga = store.get("task-7-worker")
    assert saga is not None
    assert saga.state == TaskState.FAILED
    assert store.list_in_flight() == ()


def test_fsm_crash_marks_saga_failed(monkeypatch: Any, tmp_path: Any) -> None:
    cfg = _make_cfg(tmp_path)

    def boom(*args: Any, **kwargs: Any) -> WorkerOutcome:
        raise RuntimeError("sdk crashed")

    monkeypatch.setattr(dispatch_mod, "run_worker", boom)

    with pytest.raises(RuntimeError):
        dispatch_mod._dispatch_one_worker(
            cfg,
            _issue(7),
            _meta(),
            tick=1,
            bus_emit=lambda *a, **k: None,
            store=WorkerSessionStore(":memory:"),
        )

    saga = _saga_store(cfg).get("task-7-worker")
    assert saga is not None
    assert saga.state == TaskState.FAILED  # carries the remove-worktree compensation


def test_boot_reads_runner_saga_from_canonical_path(monkeypatch: Any, tmp_path: Any) -> None:
    """`build_boot_sources` reads the very file the runner writes sagas to.

    A successful dispatch lands a saga at `.forge/tasks.db`; boot (which opens
    `.forge/tasks.db`) must therefore see it — proving the path realignment.
    """
    cfg = _make_cfg(tmp_path)
    FrontierStore(cfg.repo / ".forge" / "frontier.yaml").save(
        FrontierCursor(product_goal="g", current_problem="c", next_expansion="n", why_now="w")
    )

    def ok(*args: Any, **kwargs: Any) -> WorkerOutcome:
        return WorkerOutcome(
            issue=7,
            title="t",
            pr_url="https://x/pull/1",
            status="open",
            duration_s=1.0,
            stdout_tail="",
        )

    monkeypatch.setattr(dispatch_mod, "run_worker", ok)
    dispatch_mod._dispatch_one_worker(
        cfg, _issue(7), _meta(), tick=1, bus_emit=lambda *a, **k: None, store=None
    )

    # Same-file proof: the saga the runner wrote is in the store boot opens.
    assert build_boot_sources(cfg.repo).task_store is not None
    assert _saga_store(cfg).get("task-7-worker") is not None


def test_boot_surfaces_hard_killed_worker_as_stale(tmp_path: Any) -> None:
    """A SIGKILLed worker leaves a leased RUNNING saga; boot reads it stale.

    Simulates a crashed prior loop process (no `except` ran): a RUNNING saga
    with an already-expired lease at the canonical path. The next boot must
    surface it as dead-worker work needing compensation/re-dispatch.
    """
    cfg = _make_cfg(tmp_path)
    FrontierStore(cfg.repo / ".forge" / "frontier.yaml").save(
        FrontierCursor(product_goal="g", current_problem="c", next_expansion="n", why_now="w")
    )
    store = _saga_store(cfg)
    store.create(
        task_id="task-7-worker",
        saga_id="saga-7-worker",
        issue=7,
        branch="loop/7",
        worktree="/tmp/wt-loop-7",
        compensations=(),
    )
    acquired = datetime.now(UTC) - timedelta(minutes=10)
    store.acquire_lease(
        "task-7-worker",
        owner_id="worker-7-tick-1",
        expires_at=acquired + timedelta(minutes=1),  # expired 9 minutes ago
        acquired_at=acquired,
    )

    context = assemble_boot_context(build_boot_sources(cfg.repo))
    assert "saga-7-worker" in context.in_flight_saga_ids
    assert context.stale_saga_ids == ("saga-7-worker",)
