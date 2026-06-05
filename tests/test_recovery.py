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


def _add_compensation(
    store: SqliteTaskSagaStore, *, issue: int, kind: str, target: str
) -> None:
    store.append_compensation(
        f"task-{issue}-worker",
        Compensation(kind=kind, target=target, reason=f"{kind} for #{issue}"),
    )


def test_reconcile_compensation_failure_is_best_effort(tmp_path: Path) -> None:
    """A failing compensation no longer aborts the saga (#272): it still reaches
    COMPENSATED, the failure is captured, and the sweep continues."""
    store = _store(tmp_path)
    _stale_running(store, issue=1)
    _stale_running(store, issue=2)

    def flaky_reap(issue: int) -> None:
        if issue == 1:
            raise RuntimeError("worktree busy")

    report = reconcile_stale_sagas(store, reap_worktree=flaky_reap)

    # Both sagas reconciled; issue 1's reap failure is captured but non-fatal.
    assert sorted(r.issue for r in report.recovered) == [1, 2]
    assert any("saga-1-worker" in e and "remove-worktree" in e for e in report.errors)
    assert store.get("task-1-worker").state == TaskState.COMPENSATED
    assert store.get("task-2-worker").state == TaskState.COMPENSATED


def test_reconcile_runs_delete_branch_and_close_pr_callbacks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _stale_running(store, issue=7)
    _add_compensation(store, issue=7, kind="delete-branch", target="loop/7-feat")
    _add_compensation(store, issue=7, kind="close-pr", target="321")

    reaped: list[int] = []
    deleted: list[str] = []
    closed: list[str] = []

    report = reconcile_stale_sagas(
        store,
        reap_worktree=reaped.append,
        delete_branch=deleted.append,
        close_pr=closed.append,
    )

    assert reaped == [7]
    assert deleted == ["loop/7-feat"]
    assert closed == ["321"]
    rec = report.recovered[0]
    assert rec.branches_deleted == ("loop/7-feat",)
    assert rec.prs_closed == ("321",)
    assert "deleted-branch loop/7-feat" in report.summary()
    assert "closed-pr 321" in report.summary()
    assert store.get("task-7-worker").state == TaskState.COMPENSATED


def test_reconcile_runs_full_list_in_registered_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _stale_running(store, issue=3)  # seeds remove-worktree first
    _add_compensation(store, issue=3, kind="delete-branch", target="loop/3")
    _add_compensation(store, issue=3, kind="close-pr", target="99")

    order: list[str] = []
    reconcile_stale_sagas(
        store,
        reap_worktree=lambda _: order.append("worktree"),
        delete_branch=lambda b: order.append(f"branch:{b}"),
        close_pr=lambda n: order.append(f"pr:{n}"),
    )

    assert order == ["worktree", "branch:loop/3", "pr:99"]


def test_reconcile_offline_safe_without_gh_callbacks(tmp_path: Path) -> None:
    """Offline boot: absent delete_branch/close_pr callbacks, the saga still
    reaches COMPENSATED with no error (the design constraint)."""
    store = _store(tmp_path)
    _stale_running(store, issue=8)
    _add_compensation(store, issue=8, kind="delete-branch", target="loop/8")
    _add_compensation(store, issue=8, kind="close-pr", target="55")

    report = reconcile_stale_sagas(store, reap_worktree=lambda _: None)

    assert report.errors == ()
    rec = report.recovered[0]
    assert rec.branches_deleted == ()  # callback absent → skipped, not attempted
    assert rec.prs_closed == ()
    assert store.get("task-8-worker").state == TaskState.COMPENSATED


def test_reconcile_close_pr_failure_does_not_block_other_compensations(
    tmp_path: Path,
) -> None:
    """Adversarial sad-path: close-pr raises → delete-branch + remove-worktree
    still run, saga still COMPENSATED, failure captured, sweep continues."""
    store = _store(tmp_path)
    _stale_running(store, issue=4)
    _add_compensation(store, issue=4, kind="delete-branch", target="loop/4")
    _add_compensation(store, issue=4, kind="close-pr", target="77")
    _stale_running(store, issue=5)  # an unrelated saga the sweep must still reach

    reaped: list[int] = []
    deleted: list[str] = []

    def boom_close(_: str) -> None:
        raise RuntimeError("422 close failed")

    report = reconcile_stale_sagas(
        store,
        reap_worktree=reaped.append,
        delete_branch=deleted.append,
        close_pr=boom_close,
    )

    # close-pr blew up, but the other two compensations for #4 still ran.
    assert 4 in reaped and deleted == ["loop/4"]
    assert any("saga-4-worker" in e and "close-pr" in e for e in report.errors)
    assert store.get("task-4-worker").state == TaskState.COMPENSATED
    # The sweep continued to the unrelated stale saga.
    assert store.get("task-5-worker").state == TaskState.COMPENSATED


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
