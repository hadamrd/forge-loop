"""Worker lease heartbeat: fast liveness so dead workers go stale in minutes."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from forge_loop.runner import dispatch as dispatch_mod
from forge_loop.tasks import Compensation, SqliteTaskSagaStore, TaskState
from forge_loop.worker import WorkerOutcome
from tests.test_persistent_dispatch import _issue, _make_cfg, _meta


def _seed_leased(cfg: Any, *, task_id: str, owner_id: str, ttl_s: float) -> SqliteTaskSagaStore:
    store = SqliteTaskSagaStore(dispatch_mod.canonical_task_saga_path(cfg.repo))
    store.create(
        task_id=task_id,
        saga_id="saga-x",
        issue=7,
        branch="loop/7",
        worktree="/tmp/wt-loop-7",
        compensations=(Compensation(kind="remove-worktree", target="/tmp/wt-loop-7", reason="x"),),
    )
    now = datetime.now(UTC)
    store.acquire_lease(
        task_id, owner_id=owner_id, expires_at=now + timedelta(seconds=ttl_s), acquired_at=now
    )
    return store


def test_heartbeat_extends_a_live_lease(tmp_path: Any) -> None:
    cfg = _make_cfg(tmp_path)
    store = _seed_leased(cfg, task_id="task-7-worker", owner_id="w", ttl_s=10)
    before = store.get("task-7-worker").lease_expires_at

    # #227: the heartbeat reuses the tick-scoped store rather than opening its
    # own connection, so it now takes the store directly.
    handle = dispatch_mod._start_worker_heartbeat(
        store, task_id="task-7-worker", owner_id="w", interval_s=0.05, lease_ttl_s=10
    )
    time.sleep(0.25)  # ~4-5 beats
    dispatch_mod._stop_worker_heartbeat(handle)

    after = SqliteTaskSagaStore(dispatch_mod.canonical_task_saga_path(cfg.repo)).get(
        "task-7-worker"
    )
    assert after.lease_expires_at > before  # the lease was renewed forward
    assert after.last_heartbeat_at is not None


def test_lease_ttl_is_heartbeat_based_not_wall_ceiling(tmp_path: Any) -> None:
    cfg = _make_cfg(tmp_path)
    cfg.worker_timeout_s = 7200  # 2h wall ceiling
    store = SqliteTaskSagaStore(dispatch_mod.canonical_task_saga_path(cfg.repo))
    store.create(
        task_id="task-7-worker",
        saga_id="s",
        issue=7,
        branch="loop/7",
        worktree="/tmp/wt-loop-7",
        compensations=(),
    )
    ttl = dispatch_mod._heartbeat_interval_s(cfg) * dispatch_mod._HEARTBEAT_LEASE_FACTOR
    dispatch_mod._lease_worker_saga(store, task_id="task-7-worker", owner_id="w", lease_ttl_s=ttl)

    saga = store.get("task-7-worker")
    grace = (saga.lease_expires_at - datetime.now(UTC)).total_seconds()
    # Lease is minutes, not the 2h wall ceiling — dead workers go stale fast.
    assert grace < 3600
    assert saga.state == TaskState.RUNNING


def test_dispatch_completes_cleanly_with_heartbeat_running(monkeypatch: Any, tmp_path: Any) -> None:
    cfg = _make_cfg(tmp_path)
    cfg.worker_heartbeat_interval_s = 0.05

    def slow_worker(*args: Any, **kwargs: Any) -> WorkerOutcome:
        time.sleep(0.2)  # heartbeats fire during the run
        return WorkerOutcome(
            issue=7, title="t", pr_url="https://x/1", status="open", duration_s=1.0, stdout_tail=""
        )

    monkeypatch.setattr(dispatch_mod, "run_worker", slow_worker)
    outcome = dispatch_mod._dispatch_one_worker(
        cfg, _issue(7), _meta(), tick=1, bus_emit=lambda *a, **k: None, store=None
    )

    assert outcome.status == "open"
    saga = SqliteTaskSagaStore(dispatch_mod.canonical_task_saga_path(cfg.repo)).get("task-7-worker")
    # Heartbeat thread stopped cleanly; saga still reached its terminal edge.
    assert saga.state == TaskState.COMPLETED
