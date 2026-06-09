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
    """Known compensation kinds, shared across the saga producer (dispatch) and
    consumer (recovery).

    The discriminator is shared as an enum per the manifesto rule on
    stringly-typed cross-module boundaries: dispatch emits the kind and recovery
    branches on it. ``Compensation.kind`` stays a plain ``str`` so a saga written
    by a *newer* loop version (carrying a kind this version has never seen) is
    still representable — recovery treats any kind it has no handler for as
    unhandled rather than crashing.
    """

    REMOVE_WORKTREE = "remove-worktree"
    # #433 (epic "Compensate the branch a failed worker abandons"): the dispatch
    # path plants a ``loop/<n>`` branch for every worker and registers this
    # compensation at saga-creation time so a failed worker can never leak a
    # branch the control plane doesn't know to delete. The recovery *handler*
    # for this kind is a sibling epic issue; until it lands, recovery classifies
    # DELETE_BRANCH as deferred — it still reaps the worktree and drives the saga
    # COMPENSATED, skipping (never falsely claiming) the branch deletion (see
    # ``control/recovery.py``).
    DELETE_BRANCH = "delete-branch"


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
