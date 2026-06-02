"""Task lifecycle and compensation contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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


class TaskSagaError(RuntimeError):
    """Base error for task saga transition failures."""


class TerminalTaskMutationError(TaskSagaError):
    """Raised when code tries to mutate a terminal task saga."""


class LeaseConflictError(TaskSagaError):
    """Raised when a lease transition conflicts with current saga state."""


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
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    terminal_reason: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.COMPENSATED,
            TaskState.QUARANTINED,
        }
