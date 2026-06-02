"""Test fake for task saga storage."""

from __future__ import annotations

from dataclasses import dataclass, field

from forge_loop.tasks.saga import TaskSaga


@dataclass
class FakeTaskSagaStore:
    sagas: dict[str, TaskSaga] = field(default_factory=dict)

    def put(self, saga: TaskSaga) -> TaskSaga:
        self.sagas[saga.task_id] = saga
        return saga

    def get(self, task_id: str) -> TaskSaga | None:
        return self.sagas.get(task_id)

    def list_in_flight(self) -> tuple[TaskSaga, ...]:
        return tuple(saga for saga in self.sagas.values() if not saga.is_terminal)
