"""Boot-time recovery: reconcile dead-worker sagas (the resumability payoff)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from forge_loop.control.recovery import reconcile_stale_sagas
from forge_loop.tasks import Compensation, SqliteTaskSagaStore, TaskState


def _store(tmp_path: Path) -> SqliteTaskSagaStore:
    return SqliteTaskSagaStore(tmp_path / "tasks.db")


def _stale_running(store: SqliteTaskSagaStore, *, issue: int) -> None:
    """Seed a RUNNING saga with an already-expired lease (a dead worker)."""
    store.create(
        task_id=f"task-{issue}-worker",
        saga_id=f"saga-{issue}-worker",
        issue=issue,
        branch=f"loop/{issue}",
        worktree=f"/tmp/wt-loop-{issue}",
        compensations=(
            Compensation(
                kind="remove-worktree",
                target=f"/tmp/wt-loop-{issue}",
                reason="cleanup after worker task terminal state",
            ),
        ),
    )
    acquired = datetime.now(UTC) - timedelta(minutes=10)
    store.acquire_lease(
        f"task-{issue}-worker",
        owner_id=f"worker-{issue}",
        expires_at=acquired + timedelta(minutes=1),
        acquired_at=acquired,
    )


def test_reconcile_compensates_and_closes_stale_saga(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _stale_running(store, issue=7)
    reaped: list[int] = []

    report = reconcile_stale_sagas(store, reap_worktree=reaped.append)

    assert report.recovered_count == 1
    assert report.recovered[0].saga_id == "saga-7-worker"
    assert report.recovered[0].worktrees_reaped == ("/tmp/wt-loop-7",)
    assert reaped == [7]
    # Saga is now terminal and drains from the in-flight recovery view.
    assert store.get("task-7-worker").state == TaskState.COMPENSATED
    assert store.list_in_flight() == ()


def test_reconcile_leaves_healthy_sagas_untouched(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # A live worker: leased well into the future.
    store.create(
        task_id="task-9-worker",
        saga_id="saga-9-worker",
        issue=9,
        branch="loop/9",
        worktree="/tmp/wt-loop-9",
        compensations=(),
    )
    now = datetime.now(UTC)
    store.acquire_lease(
        "task-9-worker", owner_id="w", expires_at=now + timedelta(hours=1), acquired_at=now
    )

    report = reconcile_stale_sagas(store, reap_worktree=lambda _: None)

    assert report.recovered_count == 0
    assert store.get("task-9-worker").state == TaskState.RUNNING


def test_reconcile_continues_past_a_failing_saga(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _stale_running(store, issue=1)
    _stale_running(store, issue=2)

    def flaky_reap(issue: int) -> None:
        if issue == 1:
            raise RuntimeError("worktree busy")

    report = reconcile_stale_sagas(store, reap_worktree=flaky_reap)

    # Issue 1's compensation blew up; issue 2 still got reconciled.
    assert [r.issue for r in report.recovered] == [2]
    assert any("saga-1-worker" in e for e in report.errors)
    assert store.get("task-2-worker").state == TaskState.COMPENSATED
    assert store.get("task-1-worker").state == TaskState.RUNNING  # untouched, still stale


def test_runner_boot_recovery_reconciles_and_emits_event(tmp_path: Path) -> None:
    from forge_loop.runner import boot as boot_mod

    forge_dir = tmp_path / ".forge"
    store = SqliteTaskSagaStore(forge_dir / "tasks.db")
    _stale_running(store, issue=7)

    cfg: Any = SimpleNamespaceCfg(repo=tmp_path, events_file=tmp_path / "events.jsonl")
    report = boot_mod._run_boot_recovery(cfg)

    assert report is not None
    assert report.recovered_count == 1
    assert SqliteTaskSagaStore(forge_dir / "tasks.db").get("task-7-worker").state == (
        TaskState.COMPENSATED
    )
    events = cfg.events_file.read_text()
    assert "boot_recovery" in events


def test_runner_boot_recovery_noop_without_store(tmp_path: Path) -> None:
    from forge_loop.runner import boot as boot_mod

    cfg: Any = SimpleNamespaceCfg(repo=tmp_path, events_file=tmp_path / "events.jsonl")
    assert boot_mod._run_boot_recovery(cfg) is None
    # No `.forge/tasks.db` should be materialised by a no-op recovery.
    assert not (tmp_path / ".forge" / "tasks.db").exists()


class SimpleNamespaceCfg:
    """Minimal Config-shaped stub for the boot recovery helper."""

    def __init__(self, *, repo: Path, events_file: Path) -> None:
        self.repo = repo
        self.events_file = events_file
        events_file.parent.mkdir(parents=True, exist_ok=True)
        events_file.touch()
