"""Task lifecycle and compensation contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class TaskState(StrEnum):
    """High-level task saga states."""

    PLANNED = "planned"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    COMPENSATED = "compensated"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class Compensation:
    """A cleanup or rollback action registered for a task saga."""

    kind: str
    target: str
    reason: str


@dataclass(frozen=True)
class TaskSaga:
    """Durable task identity and lifecycle metadata."""

    task_id: str
    saga_id: str
    state: TaskState
    issue: int | None = None
    branch: str | None = None
    worktree: str | None = None
    compensations: tuple[Compensation, ...] = field(default_factory=tuple)

    @property
    def is_terminal(self) -> bool:
        return self.state in {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.COMPENSATED,
            TaskState.QUARANTINED,
        }
