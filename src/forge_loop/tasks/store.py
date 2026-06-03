"""Durable task saga storage."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from forge_loop.tasks.saga import (
    Compensation,
    LeaseConflictError,
    TaskSaga,
    TaskState,
    TerminalTaskMutationError,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_sagas (
    task_id TEXT PRIMARY KEY,
    saga_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    issue INTEGER,
    branch TEXT,
    worktree TEXT,
    compensations_json TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    last_heartbeat_at TEXT,
    terminal_reason TEXT
);
"""

_COMPAT_COLUMNS = {
    "lease_owner": "TEXT",
    "lease_expires_at": "TEXT",
    "last_heartbeat_at": "TEXT",
    "terminal_reason": "TEXT",
}

_TERMINAL_STATE_VALUES = (
    TaskState.COMPLETED.value,
    TaskState.FAILED.value,
    TaskState.COMPENSATED.value,
    TaskState.QUARANTINED.value,
)


class TaskSagaStore(Protocol):
    """Persistence boundary for task saga lifecycle state."""

    def put(self, saga: TaskSaga) -> TaskSaga:
        """Persist ``saga`` and return the stored shape."""
        ...

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
        """Create a planned, leaseable task saga."""
        ...

    def acquire_lease(
        self,
        task_id: str,
        *,
        owner_id: str,
        expires_at: datetime,
        acquired_at: datetime,
    ) -> TaskSaga:
        """Acquire a task lease and mark the saga running."""
        ...

    def heartbeat(
        self,
        task_id: str,
        *,
        owner_id: str,
        heartbeat_at: datetime,
        expires_at: datetime,
    ) -> TaskSaga:
        """Extend the active lease before it expires."""
        ...

    def list_stale(self, *, now: datetime) -> tuple[TaskSaga, ...]:
        """Return non-terminal sagas with expired leases."""
        ...

    def mark_completed(self, task_id: str, *, reason: str | None = None) -> TaskSaga:
        """Mark a task completed."""
        ...

    def mark_failed(
        self,
        task_id: str,
        *,
        reason: str,
        compensation: Compensation | None = None,
    ) -> TaskSaga:
        """Mark a task failed, requiring a compensation record."""
        ...

    def mark_compensated(self, task_id: str, *, reason: str | None = None) -> TaskSaga:
        """Mark a task compensated."""
        ...

    def mark_quarantined(self, task_id: str, *, reason: str) -> TaskSaga:
        """Mark a task quarantined."""
        ...

    def get(self, task_id: str) -> TaskSaga | None:
        """Return one saga by task id."""
        ...

    def list_in_flight(self) -> tuple[TaskSaga, ...]:
        """Return non-terminal task sagas in insertion order."""
        ...


class SqliteTaskSagaStore:
    """SQLite-backed durable task saga store."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        connect_path: str | Path = ":memory:" if str(path) == ":memory:" else self.path
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(connect_path)
        self._connection.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(_SCHEMA)
        self._ensure_compat_columns()

    def put(self, saga: TaskSaga) -> TaskSaga:
        if saga.state is TaskState.FAILED and not saga.compensations:
            raise LeaseConflictError(f"task {saga.task_id} failure requires compensation")
        existing = self.get(saga.task_id)
        if (
            existing is not None
            and existing.is_terminal
            and saga != existing
            and not _is_terminal_audit_update(existing, saga)
        ):
            raise TerminalTaskMutationError(
                f"task {saga.task_id} is terminal: {existing.state.value}"
            )
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO task_sagas (
                    task_id,
                    saga_id,
                    state,
                    issue,
                    branch,
                    worktree,
                    compensations_json,
                    lease_owner,
                    lease_expires_at,
                    last_heartbeat_at,
                    terminal_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id)
                DO UPDATE SET
                    saga_id = excluded.saga_id,
                    state = excluded.state,
                    issue = excluded.issue,
                    branch = excluded.branch,
                    worktree = excluded.worktree,
                    compensations_json = excluded.compensations_json,
                    lease_owner = excluded.lease_owner,
                    lease_expires_at = excluded.lease_expires_at,
                    last_heartbeat_at = excluded.last_heartbeat_at,
                    terminal_reason = excluded.terminal_reason
                WHERE (
                        task_sagas.state NOT IN (?, ?, ?, ?)
                        AND (
                            (
                                task_sagas.lease_owner IS NULL
                                AND task_sagas.lease_expires_at IS NULL
                                AND task_sagas.last_heartbeat_at IS NULL
                            )
                            OR (
                                task_sagas.lease_owner IS excluded.lease_owner
                                AND task_sagas.lease_expires_at IS excluded.lease_expires_at
                                AND task_sagas.last_heartbeat_at IS excluded.last_heartbeat_at
                            )
                        )
                    )
                    OR (
                        task_sagas.saga_id IS excluded.saga_id
                        AND task_sagas.state IS excluded.state
                        AND task_sagas.issue IS excluded.issue
                        AND task_sagas.branch IS excluded.branch
                        AND task_sagas.worktree IS excluded.worktree
                        AND task_sagas.compensations_json IS excluded.compensations_json
                        AND task_sagas.lease_owner IS excluded.lease_owner
                        AND task_sagas.lease_expires_at IS excluded.lease_expires_at
                        AND task_sagas.last_heartbeat_at IS excluded.last_heartbeat_at
                        AND task_sagas.terminal_reason IS excluded.terminal_reason
                    )
                    OR (
                        task_sagas.saga_id IS excluded.saga_id
                        AND task_sagas.state IS excluded.state
                        AND task_sagas.issue IS excluded.issue
                        AND task_sagas.branch IS excluded.branch
                        AND task_sagas.worktree IS excluded.worktree
                        AND task_sagas.compensations_json IS excluded.compensations_json
                        AND task_sagas.lease_owner IS excluded.lease_owner
                        AND task_sagas.lease_expires_at IS excluded.lease_expires_at
                        AND task_sagas.last_heartbeat_at IS excluded.last_heartbeat_at
                        AND excluded.terminal_reason IS NOT NULL
                    )
                """,
                (
                    saga.task_id,
                    saga.saga_id,
                    saga.state.value,
                    saga.issue,
                    saga.branch,
                    saga.worktree,
                    _compensations_json(saga.compensations),
                    saga.lease_owner,
                    _datetime_to_text(saga.lease_expires_at),
                    _datetime_to_text(saga.last_heartbeat_at),
                    saga.terminal_reason,
                    *_TERMINAL_STATE_VALUES,
                ),
            )
        if cursor.rowcount != 1:
            current = self.get(saga.task_id)
            if current is not None and current.is_terminal:
                raise TerminalTaskMutationError(
                    f"task {saga.task_id} is terminal: {current.state.value}"
                )
            raise LeaseConflictError(f"task {saga.task_id} update conflicted")
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
        saga = TaskSaga(
            task_id=task_id,
            saga_id=saga_id,
            state=TaskState.PLANNED,
            issue=issue,
            branch=branch,
            worktree=worktree,
            compensations=compensations,
        )
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO task_sagas (
                        task_id,
                        saga_id,
                        state,
                        issue,
                        branch,
                        worktree,
                        compensations_json,
                        lease_owner,
                        lease_expires_at,
                        last_heartbeat_at,
                        terminal_reason
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        saga.task_id,
                        saga.saga_id,
                        saga.state.value,
                        saga.issue,
                        saga.branch,
                        saga.worktree,
                        _compensations_json(saga.compensations),
                        saga.lease_owner,
                        _datetime_to_text(saga.lease_expires_at),
                        _datetime_to_text(saga.last_heartbeat_at),
                        saga.terminal_reason,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise LeaseConflictError(f"task {task_id} or saga {saga_id} already exists") from exc
        return saga

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
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE task_sagas
                SET
                    state = ?,
                    lease_owner = ?,
                    lease_expires_at = ?,
                    last_heartbeat_at = ?
                WHERE task_id = ?
                    AND state NOT IN (?, ?, ?, ?)
                    AND (
                        lease_expires_at IS NULL
                        OR lease_expires_at <= ?
                    )
                """,
                (
                    TaskState.RUNNING.value,
                    owner_id,
                    _datetime_to_text(expires_at),
                    _datetime_to_text(acquired_at),
                    task_id,
                    *_TERMINAL_STATE_VALUES,
                    _datetime_to_text(acquired_at),
                ),
            )
        if cursor.rowcount != 1:
            raise LeaseConflictError(f"task {task_id} lease was claimed concurrently")
        leased = self.get(task_id)
        if leased is None:
            raise KeyError(task_id)
        return leased

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
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE task_sagas
                SET
                    state = ?,
                    lease_expires_at = ?,
                    last_heartbeat_at = ?
                WHERE task_id = ?
                    AND state NOT IN (?, ?, ?, ?)
                    AND lease_owner = ?
                    AND lease_expires_at IS ?
                    AND lease_expires_at > ?
                    AND lease_expires_at < ?
                """,
                (
                    TaskState.RUNNING.value,
                    _datetime_to_text(expires_at),
                    _datetime_to_text(heartbeat_at),
                    task_id,
                    *_TERMINAL_STATE_VALUES,
                    owner_id,
                    _datetime_to_text(saga.lease_expires_at),
                    _datetime_to_text(heartbeat_at),
                    _datetime_to_text(expires_at),
                ),
            )
        if cursor.rowcount != 1:
            raise LeaseConflictError(f"task {task_id} lease changed before heartbeat")
        heartbeaten = self.get(task_id)
        if heartbeaten is None:
            raise KeyError(task_id)
        return heartbeaten

    def get(self, task_id: str) -> TaskSaga | None:
        row = self._connection.execute(
            """
            SELECT *
            FROM task_sagas
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return _saga_from_row(row)

    def list_in_flight(self) -> tuple[TaskSaga, ...]:
        return tuple(saga for saga in self._select_all() if not saga.is_terminal)

    def list_stale(self, *, now: datetime) -> tuple[TaskSaga, ...]:
        return tuple(
            saga
            for saga in self._select_all()
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

    def _select_all(self) -> Iterable[TaskSaga]:
        rows = self._connection.execute(
            """
            SELECT *
            FROM task_sagas
            ORDER BY rowid ASC
            """
        )
        return (_saga_from_row(row) for row in rows)

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

    def _ensure_compat_columns(self) -> None:
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(task_sagas)").fetchall()
        }
        with self._connection:
            for column, definition in _COMPAT_COLUMNS.items():
                if column not in columns:
                    self._connection.execute(
                        f"ALTER TABLE task_sagas ADD COLUMN {column} {definition}"
                    )


def _compensations_json(compensations: tuple[Compensation, ...]) -> str:
    return json.dumps(
        [
            {
                "kind": compensation.kind,
                "target": compensation.target,
                "reason": compensation.reason,
            }
            for compensation in compensations
        ],
        sort_keys=True,
        separators=(",", ":"),
    )


def _load_compensations(raw: str) -> tuple[Compensation, ...]:
    values = json.loads(raw)
    if not isinstance(values, list):
        raise ValueError("task saga compensations must be a JSON list")
    compensations = []
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("task saga compensation entries must be mappings")
        kind = value.get("kind")
        target = value.get("target")
        reason = value.get("reason")
        if not isinstance(kind, str):
            raise ValueError("task saga compensation entry missing kind")
        if not isinstance(target, str):
            raise ValueError("task saga compensation entry missing target")
        if not isinstance(reason, str):
            raise ValueError("task saga compensation entry missing reason")
        compensations.append(Compensation(kind=kind, target=target, reason=reason))
    return tuple(compensations)


def _datetime_to_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.isoformat()


def _datetime_from_text(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


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


def _saga_from_row(row: sqlite3.Row) -> TaskSaga:
    return TaskSaga(
        task_id=row["task_id"],
        saga_id=row["saga_id"],
        state=TaskState(row["state"]),
        issue=row["issue"],
        branch=row["branch"],
        worktree=row["worktree"],
        compensations=_load_compensations(row["compensations_json"]),
        lease_owner=row["lease_owner"],
        lease_expires_at=_datetime_from_text(row["lease_expires_at"]),
        last_heartbeat_at=_datetime_from_text(row["last_heartbeat_at"]),
        terminal_reason=row["terminal_reason"],
    )
