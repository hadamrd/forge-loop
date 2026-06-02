"""Durable task saga storage."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from forge_loop.tasks.saga import Compensation, TaskSaga, TaskState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_sagas (
    task_id TEXT PRIMARY KEY,
    saga_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    issue INTEGER,
    branch TEXT,
    worktree TEXT,
    compensations_json TEXT NOT NULL
);
"""


class TaskSagaStore(Protocol):
    """Persistence boundary for task saga lifecycle state."""

    def put(self, saga: TaskSaga) -> TaskSaga:
        """Persist ``saga`` and return the stored shape."""
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

    def put(self, saga: TaskSaga) -> TaskSaga:
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
                    compensations_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id)
                DO UPDATE SET
                    saga_id = excluded.saga_id,
                    state = excluded.state,
                    issue = excluded.issue,
                    branch = excluded.branch,
                    worktree = excluded.worktree,
                    compensations_json = excluded.compensations_json
                """,
                (
                    saga.task_id,
                    saga.saga_id,
                    saga.state.value,
                    saga.issue,
                    saga.branch,
                    saga.worktree,
                    _compensations_json(saga.compensations),
                ),
            )
        return saga

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

    def _select_all(self) -> Iterable[TaskSaga]:
        rows = self._connection.execute(
            """
            SELECT *
            FROM task_sagas
            ORDER BY rowid ASC
            """
        )
        return (_saga_from_row(row) for row in rows)


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


def _saga_from_row(row: sqlite3.Row) -> TaskSaga:
    return TaskSaga(
        task_id=row["task_id"],
        saga_id=row["saga_id"],
        state=TaskState(row["state"]),
        issue=row["issue"],
        branch=row["branch"],
        worktree=row["worktree"],
        compensations=_load_compensations(row["compensations_json"]),
    )
