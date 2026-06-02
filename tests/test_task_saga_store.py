from __future__ import annotations

from pathlib import Path

from forge_loop._testing.task_saga_store import FakeTaskSagaStore
from forge_loop.tasks import Compensation, SqliteTaskSagaStore, TaskSaga, TaskState


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
