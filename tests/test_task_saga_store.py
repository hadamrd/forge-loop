from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from forge_loop._testing.task_saga_store import FakeTaskSagaStore
from forge_loop.tasks import (
    Compensation,
    LeaseConflictError,
    SqliteTaskSagaStore,
    TaskSaga,
    TaskState,
    TerminalTaskMutationError,
)


def _saga(
    task_id: str,
    *,
    saga_id: str | None = None,
    state: TaskState = TaskState.RUNNING,
) -> TaskSaga:
    return TaskSaga(
        task_id=task_id,
        saga_id=saga_id or f"saga-{task_id}",
        state=state,
        issue=165,
        branch="loop/165-feat-control-assemble-bootcontext-from-d",
        worktree=f"/tmp/{task_id}",
        compensations=(
            Compensation(
                kind="remove-worktree",
                target=f"/tmp/{task_id}",
                reason="cleanup after task terminal state",
            ),
        ),
    )


def test_task_saga_store_round_trips_saga_after_reopen(tmp_path: Path) -> None:
    db = tmp_path / "tasks.db"
    expected = _saga("task-165-a")

    SqliteTaskSagaStore(db).put(expected)

    reopened = SqliteTaskSagaStore(db)
    assert reopened.get("task-165-a") == expected


def test_task_saga_store_get_missing_task_returns_none_after_reopen(
    tmp_path: Path,
) -> None:
    db = tmp_path / "tasks.db"
    SqliteTaskSagaStore(db).put(_saga("task-existing"))

    reopened = SqliteTaskSagaStore(db)
    fake = FakeTaskSagaStore()
    fake.put(_saga("task-existing"))

    assert reopened.get("task-missing") is None
    assert fake.get("task-missing") is None


def test_task_saga_store_lists_only_non_terminal_sagas(tmp_path: Path) -> None:
    store = SqliteTaskSagaStore(tmp_path / "tasks.db")
    running = _saga("task-running", state=TaskState.RUNNING)
    planned = _saga("task-planned", state=TaskState.PLANNED)
    completed = _saga("task-completed", state=TaskState.COMPLETED)
    failed = _saga("task-failed", state=TaskState.FAILED)

    for saga in (running, completed, planned, failed):
        store.put(saga)

    assert store.list_in_flight() == (running, planned)


def test_fake_and_real_task_saga_stores_return_same_in_flight_shape(
    tmp_path: Path,
) -> None:
    real = SqliteTaskSagaStore(tmp_path / "tasks.db")
    fake = FakeTaskSagaStore()
    running = _saga("task-running", state=TaskState.RUNNING)
    terminal = _saga("task-terminal", state=TaskState.QUARANTINED)

    for store in (real, fake):
        store.put(running)
        store.put(terminal)

    assert real.list_in_flight() == fake.list_in_flight()
    assert real.get("task-running") == fake.get("task-running")


class TestTaskSagaLeaseLifecycle:
    def test_acquire_lease_does_not_overwrite_concurrent_claim(
        self,
        tmp_path: Path,
    ) -> None:
        db = tmp_path / "tasks.db"
        task_id = "task-raced-lease"
        owner_a_time = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)
        owner_b_time = owner_a_time + timedelta(seconds=1)
        SqliteTaskSagaStore(db).put(_saga(task_id, state=TaskState.DISPATCHED))

        class RacingStore(SqliteTaskSagaStore):
            def _require_mutable(self, task_id: str) -> TaskSaga:
                stale = super()._require_mutable(task_id)
                SqliteTaskSagaStore(db).acquire_lease(
                    task_id,
                    owner_id="worker-b",
                    expires_at=owner_b_time + timedelta(minutes=30),
                    acquired_at=owner_b_time,
                )
                return stale

        with pytest.raises(LeaseConflictError):
            RacingStore(db).acquire_lease(
                task_id,
                owner_id="worker-a",
                expires_at=owner_a_time + timedelta(minutes=30),
                acquired_at=owner_a_time,
            )

        persisted = SqliteTaskSagaStore(db).get(task_id)
        assert persisted is not None
        assert persisted.lease_owner == "worker-b"
        assert persisted.last_heartbeat_at == owner_b_time

    def test_new_task_starts_leaseable_and_becomes_running_after_acquisition(
        self,
        tmp_path: Path,
    ) -> None:
        store = SqliteTaskSagaStore(tmp_path / "tasks.db")
        created = store.create(
            task_id="task-168-a",
            saga_id="saga-168-a",
            issue=168,
            branch="loop/168-feat-tasks-persist-saga-leases",
            worktree="/tmp/wt-loop-168-a",
            compensations=(
                Compensation(
                    kind="remove-worktree",
                    target="/tmp/wt-loop-168-a",
                    reason="cleanup stale task worktree",
                ),
            ),
        )

        assert created.state is TaskState.PLANNED
        assert created.lease_owner is None
        assert created.lease_expires_at is None

        now = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)
        leased = store.acquire_lease(
            "task-168-a",
            owner_id="worker-a",
            expires_at=now + timedelta(minutes=30),
            acquired_at=now,
        )

        assert leased.state is TaskState.RUNNING
        assert leased.lease_owner == "worker-a"
        assert leased.lease_expires_at == now + timedelta(minutes=30)
        assert leased.last_heartbeat_at == now

    def test_heartbeat_extends_lease_before_expiry(self, tmp_path: Path) -> None:
        store = SqliteTaskSagaStore(tmp_path / "tasks.db")
        store.put(_saga("task-168-heartbeat", state=TaskState.DISPATCHED))
        acquired_at = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)
        store.acquire_lease(
            "task-168-heartbeat",
            owner_id="worker-a",
            expires_at=acquired_at + timedelta(minutes=10),
            acquired_at=acquired_at,
        )

        heartbeat_at = acquired_at + timedelta(minutes=5)
        extended = store.heartbeat(
            "task-168-heartbeat",
            owner_id="worker-a",
            heartbeat_at=heartbeat_at,
            expires_at=heartbeat_at + timedelta(minutes=20),
        )

        assert extended.lease_expires_at == heartbeat_at + timedelta(minutes=20)
        assert extended.last_heartbeat_at == heartbeat_at

    def test_heartbeat_wrong_owner_preserves_existing_lease(self, tmp_path: Path) -> None:
        store = SqliteTaskSagaStore(tmp_path / "tasks.db")
        task_id = "task-168-wrong-owner"
        store.put(_saga(task_id, state=TaskState.DISPATCHED))
        acquired_at = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)
        leased = store.acquire_lease(
            task_id,
            owner_id="worker-a",
            expires_at=acquired_at + timedelta(minutes=10),
            acquired_at=acquired_at,
        )

        with pytest.raises(LeaseConflictError):
            store.heartbeat(
                task_id,
                owner_id="worker-b",
                heartbeat_at=acquired_at + timedelta(minutes=5),
                expires_at=acquired_at + timedelta(minutes=20),
            )

        assert store.get(task_id) == leased

    def test_expired_task_is_reported_stale(self, tmp_path: Path) -> None:
        store = SqliteTaskSagaStore(tmp_path / "tasks.db")
        stale = _saga("task-stale", state=TaskState.RUNNING)
        fresh = _saga("task-fresh", state=TaskState.RUNNING)
        completed = _saga("task-completed-stale", state=TaskState.COMPLETED)
        base = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)
        for saga in (stale, fresh, completed):
            store.put(saga)
        store.acquire_lease(
            stale.task_id,
            owner_id="worker-stale",
            expires_at=base - timedelta(seconds=1),
            acquired_at=base - timedelta(minutes=10),
        )
        store.acquire_lease(
            fresh.task_id,
            owner_id="worker-fresh",
            expires_at=base + timedelta(seconds=1),
            acquired_at=base - timedelta(minutes=10),
        )

        assert store.list_stale(now=base) == (store.get(stale.task_id),)

    def test_completed_task_cannot_be_leased_again(self, tmp_path: Path) -> None:
        store = SqliteTaskSagaStore(tmp_path / "tasks.db")
        store.put(_saga("task-terminal", state=TaskState.COMPLETED))

        try:
            store.acquire_lease(
                "task-terminal",
                owner_id="worker-a",
                expires_at=datetime(2026, 6, 3, 10, 30, tzinfo=UTC),
                acquired_at=datetime(2026, 6, 3, 10, 0, tzinfo=UTC),
            )
        except TerminalTaskMutationError as exc:
            assert "task-terminal" in str(exc)
        else:  # pragma: no cover - assertion guard
            raise AssertionError("completed task was leased again")

    def test_terminal_markers_record_state_reason_and_reject_later_lease(
        self,
        tmp_path: Path,
    ) -> None:
        cases = (
            ("task-complete-marker", TaskState.COMPLETED, "done"),
            ("task-compensated-marker", TaskState.COMPENSATED, "worktree removed"),
            ("task-quarantined-marker", TaskState.QUARANTINED, "manual review"),
        )
        store = SqliteTaskSagaStore(tmp_path / "tasks.db")
        for task_id, _state, _reason in cases:
            store.put(_saga(task_id, state=TaskState.RUNNING))

        marked = (
            store.mark_completed("task-complete-marker", reason="done"),
            store.mark_compensated("task-compensated-marker", reason="worktree removed"),
            store.mark_quarantined("task-quarantined-marker", reason="manual review"),
        )

        for saga, (_task_id, state, reason) in zip(marked, cases, strict=True):
            assert saga.state is state
            assert saga.terminal_reason == reason
            with pytest.raises(TerminalTaskMutationError):
                store.acquire_lease(
                    saga.task_id,
                    owner_id="worker-a",
                    expires_at=datetime(2026, 6, 3, 10, 30, tzinfo=UTC),
                    acquired_at=datetime(2026, 6, 3, 10, 0, tzinfo=UTC),
                )

    def test_terminal_task_cannot_be_mutated_except_audit_reason(
        self,
        tmp_path: Path,
    ) -> None:
        store = SqliteTaskSagaStore(tmp_path / "tasks.db")
        completed = _saga("task-terminal-put", state=TaskState.COMPLETED)
        store.put(completed)

        try:
            store.put(
                TaskSaga(
                    task_id=completed.task_id,
                    saga_id=completed.saga_id,
                    state=TaskState.RUNNING,
                    issue=completed.issue,
                    branch="loop/mutated",
                    worktree=completed.worktree,
                    compensations=completed.compensations,
                )
            )
        except TerminalTaskMutationError as exc:
            assert "task-terminal-put" in str(exc)
        else:  # pragma: no cover - assertion guard
            raise AssertionError("terminal task mutation was allowed")

        audited = store.put(
            TaskSaga(
                task_id=completed.task_id,
                saga_id=completed.saga_id,
                state=completed.state,
                issue=completed.issue,
                branch=completed.branch,
                worktree=completed.worktree,
                compensations=completed.compensations,
                terminal_reason="operator recorded cleanup note",
            )
        )

        assert audited.terminal_reason == "operator recorded cleanup note"

    def test_failed_task_requires_or_preserves_compensation_record(
        self,
        tmp_path: Path,
    ) -> None:
        store = SqliteTaskSagaStore(tmp_path / "tasks.db")
        with_existing = _saga("task-with-comp", state=TaskState.RUNNING)
        without_existing = TaskSaga(
            task_id="task-without-comp",
            saga_id="saga-task-without-comp",
            state=TaskState.RUNNING,
            issue=168,
            branch="loop/168-feat-tasks-persist-saga-leases",
            worktree="/tmp/task-without-comp",
        )
        store.put(with_existing)
        store.put(without_existing)

        failed = store.mark_failed("task-with-comp", reason="worker crashed")
        assert failed.compensations == with_existing.compensations

        try:
            store.mark_failed("task-without-comp", reason="worker crashed")
        except LeaseConflictError as exc:
            assert "compensation" in str(exc)
        else:  # pragma: no cover - assertion guard
            raise AssertionError("failure without compensation was allowed")

        compensation = Compensation(
            kind="remove-worktree",
            target="/tmp/task-without-comp",
            reason="cleanup failed worker",
        )
        failed_with_new_compensation = store.mark_failed(
            "task-without-comp",
            reason="worker crashed",
            compensation=compensation,
        )
        assert failed_with_new_compensation.compensations == (compensation,)

    def test_reopening_store_preserves_lease_and_compensations(
        self,
        tmp_path: Path,
    ) -> None:
        db = tmp_path / "tasks.db"
        store = SqliteTaskSagaStore(db)
        compensation = Compensation(
            kind="delete-branch",
            target="loop/168-feat-tasks-persist-saga-leases",
            reason="remove abandoned branch after quarantine",
        )
        created = store.create(
            task_id="task-168-reopen",
            saga_id="saga-168-reopen",
            issue=168,
            branch="loop/168-feat-tasks-persist-saga-leases",
            worktree="/tmp/wt-loop-168-reopen",
            compensations=(compensation,),
        )
        acquired_at = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)
        store.acquire_lease(
            created.task_id,
            owner_id="worker-a",
            expires_at=acquired_at + timedelta(minutes=30),
            acquired_at=acquired_at,
        )

        reopened = SqliteTaskSagaStore(db)
        persisted = reopened.get(created.task_id)

        assert persisted is not None
        assert persisted.lease_owner == "worker-a"
        assert persisted.lease_expires_at == acquired_at + timedelta(minutes=30)
        assert persisted.last_heartbeat_at == acquired_at
        assert persisted.compensations == (compensation,)
