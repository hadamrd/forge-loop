"""Test fake for task saga storage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from forge_loop.tasks.saga import (
    Compensation,
    LeaseConflictError,
    TaskSaga,
    TaskState,
    TerminalTaskMutationError,
)


@dataclass
class FakeTaskSagaStore:
    sagas: dict[str, TaskSaga] = field(default_factory=dict)

    def put(self, saga: TaskSaga) -> TaskSaga:
        if saga.state is TaskState.FAILED and not saga.compensations:
            raise LeaseConflictError(f"task {saga.task_id} failure requires compensation")
        existing = self.get(saga.task_id)
        if (
            existing is not None
            and not existing.is_terminal
            and _has_lease_metadata(existing)
            and not _has_same_lease_metadata(existing, saga)
        ):
            raise LeaseConflictError(f"task {saga.task_id} lease changed before update")
        if (
            existing is not None
            and existing.is_terminal
            and saga != existing
            and not _is_terminal_audit_update(existing, saga)
        ):
            raise TerminalTaskMutationError(
                f"task {saga.task_id} is terminal: {existing.state.value}"
            )
        self.sagas[saga.task_id] = saga
        return saga

    def create(
        self,
        *,
        task_id: str,
        saga_id: str,
        issue: int,
        branch: str,
        worktree: str,
        compensations: tuple[Compensation, ...],
    ) -> TaskSaga:
        if task_id in self.sagas or any(saga.saga_id == saga_id for saga in self.sagas.values()):
            raise LeaseConflictError(f"task {task_id} or saga {saga_id} already exists")
        return self.put(
            TaskSaga(
                task_id=task_id,
                saga_id=saga_id,
                state=TaskState.PLANNED,
                issue=issue,
                branch=branch,
                worktree=worktree,
                compensations=compensations,
            )
        )

    def get(self, task_id: str) -> TaskSaga | None:
        return self.sagas.get(task_id)

    def list_in_flight(self) -> tuple[TaskSaga, ...]:
        return tuple(saga for saga in self.sagas.values() if not saga.is_terminal)

    def acquire_lease(
        self,
        task_id: str,
        *,
        owner_id: str,
        expires_at: datetime,
        acquired_at: datetime,
    ) -> TaskSaga:
        if expires_at <= acquired_at:
            raise LeaseConflictError("lease expiry must be after acquisition time")
        saga = self._require_mutable(task_id)
        if saga.lease_expires_at is not None and saga.lease_expires_at > acquired_at:
            raise LeaseConflictError(f"task {task_id} already has an active lease")
        leased = _replace_saga(
            saga,
            state=TaskState.RUNNING,
            lease_owner=owner_id,
            lease_expires_at=expires_at,
            last_heartbeat_at=acquired_at,
        )
        return self.put(leased)

    def heartbeat(
        self,
        task_id: str,
        *,
        owner_id: str,
        heartbeat_at: datetime,
        expires_at: datetime,
    ) -> TaskSaga:
        saga = self._require_mutable(task_id)
        if saga.lease_owner != owner_id:
            raise LeaseConflictError(f"task {task_id} lease is not held by {owner_id}")
        if saga.lease_expires_at is None:
            raise LeaseConflictError(f"task {task_id} has no active lease")
        if heartbeat_at >= saga.lease_expires_at:
            raise LeaseConflictError(f"task {task_id} lease expired before heartbeat")
        if expires_at <= saga.lease_expires_at:
            raise LeaseConflictError(f"task {task_id} heartbeat did not extend lease")
        heartbeaten = _replace_saga(
            saga,
            state=TaskState.RUNNING,
            lease_expires_at=expires_at,
            last_heartbeat_at=heartbeat_at,
        )
        self.sagas[task_id] = heartbeaten
        return heartbeaten

    def list_stale(self, *, now: datetime) -> tuple[TaskSaga, ...]:
        return tuple(
            saga
            for saga in self.sagas.values()
            if not saga.is_terminal
            and saga.lease_expires_at is not None
            and saga.lease_expires_at <= now
        )

    def mark_completed(self, task_id: str, *, reason: str | None = None) -> TaskSaga:
        return self._mark_terminal(task_id, TaskState.COMPLETED, reason=reason)

    def mark_failed(
        self,
        task_id: str,
        *,
        reason: str,
        compensation: Compensation | None = None,
    ) -> TaskSaga:
        saga = self._require_mutable(task_id)
        compensations = saga.compensations
        if compensation is not None:
            compensations = (*compensations, compensation)
        if not compensations:
            raise LeaseConflictError(f"task {task_id} failure requires compensation")
        failed = _replace_saga(
            saga,
            state=TaskState.FAILED,
            compensations=compensations,
            terminal_reason=reason,
        )
        return self.put(failed)

    def mark_compensated(self, task_id: str, *, reason: str | None = None) -> TaskSaga:
        return self._mark_terminal(task_id, TaskState.COMPENSATED, reason=reason)

    def mark_quarantined(self, task_id: str, *, reason: str) -> TaskSaga:
        return self._mark_terminal(task_id, TaskState.QUARANTINED, reason=reason)

    def _mark_terminal(
        self,
        task_id: str,
        state: TaskState,
        *,
        reason: str | None,
    ) -> TaskSaga:
        saga = self._require_mutable(task_id)
        terminal = _replace_saga(saga, state=state, terminal_reason=reason)
        return self.put(terminal)

    def _require_mutable(self, task_id: str) -> TaskSaga:
        saga = self.get(task_id)
        if saga is None:
            raise KeyError(task_id)
        if saga.is_terminal:
            raise TerminalTaskMutationError(f"task {task_id} is terminal: {saga.state.value}")
        return saga


def _replace_saga(
    saga: TaskSaga,
    *,
    state: TaskState | None = None,
    compensations: tuple[Compensation, ...] | None = None,
    lease_owner: str | None = None,
    lease_expires_at: datetime | None = None,
    last_heartbeat_at: datetime | None = None,
    terminal_reason: str | None = None,
) -> TaskSaga:
    return TaskSaga(
        task_id=saga.task_id,
        saga_id=saga.saga_id,
        state=state or saga.state,
        issue=saga.issue,
        branch=saga.branch,
        worktree=saga.worktree,
        compensations=compensations if compensations is not None else saga.compensations,
        lease_owner=lease_owner if lease_owner is not None else saga.lease_owner,
        lease_expires_at=(
            lease_expires_at if lease_expires_at is not None else saga.lease_expires_at
        ),
        last_heartbeat_at=(
            last_heartbeat_at if last_heartbeat_at is not None else saga.last_heartbeat_at
        ),
        terminal_reason=terminal_reason if terminal_reason is not None else saga.terminal_reason,
    )


def _is_terminal_audit_update(existing: TaskSaga, incoming: TaskSaga) -> bool:
    return (
        existing.task_id == incoming.task_id
        and existing.saga_id == incoming.saga_id
        and existing.state is incoming.state
        and existing.issue == incoming.issue
        and existing.branch == incoming.branch
        and existing.worktree == incoming.worktree
        and existing.compensations == incoming.compensations
        and existing.lease_owner == incoming.lease_owner
        and existing.lease_expires_at == incoming.lease_expires_at
        and existing.last_heartbeat_at == incoming.last_heartbeat_at
        and incoming.terminal_reason is not None
    )


def _has_lease_metadata(saga: TaskSaga) -> bool:
    return (
        saga.lease_owner is not None
        or saga.lease_expires_at is not None
        or saga.last_heartbeat_at is not None
    )


def _has_same_lease_metadata(existing: TaskSaga, incoming: TaskSaga) -> bool:
    return (
        existing.lease_owner == incoming.lease_owner
        and existing.lease_expires_at == incoming.lease_expires_at
        and existing.last_heartbeat_at == incoming.last_heartbeat_at
    )
