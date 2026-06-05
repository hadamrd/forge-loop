"""Task lifecycle and compensation contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from forge_loop.sandbox import CapabilityPolicy


class TaskState(StrEnum):
    """High-level task saga states."""

    PLANNED = "planned"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    COMPENSATED = "compensated"
    QUARANTINED = "quarantined"


class CompensationKind(StrEnum):
    """Cross-module discriminator for a saga compensation's action.

    ``Compensation.kind`` stays a free-form ``str`` (no model/schema change,
    issue #272), but the *known* kinds are declared here as a shared enum so
    producer (``runner/dispatch.py``) and consumer (``control/recovery.py``)
    cannot drift on a string literal — the cross-module-enum manifesto rule.
    Being a :class:`StrEnum`, each member compares equal to its wire string and
    JSON-serialises to it, so the stored shape is unchanged.
    """

    REMOVE_WORKTREE = "remove-worktree"
    DELETE_BRANCH = "delete-branch"
    CLOSE_PR = "close-pr"


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
    capability_policy: CapabilityPolicy = field(default_factory=CapabilityPolicy)

    @property
    def is_terminal(self) -> bool:
        return self.state in {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.COMPENSATED,
            TaskState.QUARANTINED,
        }
