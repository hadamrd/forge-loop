"""SQLite-backed durable event log."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forge_loop.eventlog.models import EventEnvelope, EventId, EventKind, EventRef
from forge_loop.eventlog.projections import ProjectionCursor

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    task_id TEXT,
    saga_id TEXT,
    causal_event_id TEXT,
    causal_sequence INTEGER,
    idempotency_key TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS projection_cursors (
    projection_name TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL
);
"""


class SqliteEventLog:
    """Durable append-only event log stored in SQLite."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        connect_path: str | Path = ":memory:" if str(path) == ":memory:" else self.path
        self._connection = sqlite3.connect(connect_path)
        self._connection.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(_SCHEMA)

    def append(
        self,
        kind: EventKind,
        payload: Mapping[str, Any],
        *,
        task_id: str | None = None,
        saga_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> EventEnvelope:
        """Append one event transactionally and return its durable envelope."""

        if idempotency_key is not None:
            existing = self._find_by_idempotency_key(idempotency_key)
            if existing is not None:
                return existing

        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        event_id = EventId(uuid.uuid4().hex)
        occurred_at = datetime.now(UTC).isoformat()

        try:
            with self._connection:
                cursor = self._connection.execute(
                    """
                    INSERT INTO events (
                        event_id,
                        kind,
                        payload_json,
                        schema_version,
                        occurred_at,
                        task_id,
                        saga_id,
                        idempotency_key
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(event_id),
                        kind.value,
                        payload_json,
                        1,
                        occurred_at,
                        task_id,
                        saga_id,
                        idempotency_key,
                    ),
                )
        except sqlite3.IntegrityError:
            if idempotency_key is None:
                raise
            existing = self._find_by_idempotency_key(idempotency_key)
            if existing is None:
                raise
            return existing

        sequence = cursor.lastrowid
        if sequence is None:
            raise RuntimeError("SQLite did not assign an event sequence")
        return EventEnvelope(
            event_id=event_id,
            sequence=sequence,
            kind=kind,
            payload=payload,
            occurred_at=datetime.fromisoformat(occurred_at),
            task_id=task_id,
            saga_id=saga_id,
            idempotency_key=idempotency_key,
        )

    def since(self, sequence: int = 0) -> Iterable[EventEnvelope]:
        """Yield events with sequence greater than ``sequence`` in log order."""

        rows = self._connection.execute(
            """
            SELECT
                sequence,
                event_id,
                kind,
                payload_json,
                schema_version,
                occurred_at,
                task_id,
                saga_id,
                causal_event_id,
                causal_sequence,
                idempotency_key
            FROM events
            WHERE sequence > ?
            ORDER BY sequence ASC
            """,
            (sequence,),
        )
        return (self._envelope_from_row(row) for row in rows)

    def get_projection_cursor(self, projection_name: str) -> ProjectionCursor:
        """Return the saved projection cursor, or sequence 0 when absent."""

        row = self._connection.execute(
            "SELECT sequence FROM projection_cursors WHERE projection_name = ?",
            (projection_name,),
        ).fetchone()
        if row is None:
            return ProjectionCursor(sequence=0)
        return ProjectionCursor(sequence=row["sequence"])

    def set_projection_cursor(
        self,
        projection_name: str,
        cursor: ProjectionCursor,
    ) -> None:
        """Persist a projection cursor."""

        with self._connection:
            self._connection.execute(
                """
                INSERT INTO projection_cursors (projection_name, sequence)
                VALUES (?, ?)
                ON CONFLICT(projection_name)
                DO UPDATE SET sequence = excluded.sequence
                """,
                (projection_name, cursor.sequence),
            )

    def _find_by_idempotency_key(self, idempotency_key: str) -> EventEnvelope | None:
        row = self._connection.execute(
            """
            SELECT
                sequence,
                event_id,
                kind,
                payload_json,
                schema_version,
                occurred_at,
                task_id,
                saga_id,
                causal_event_id,
                causal_sequence,
                idempotency_key
            FROM events
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        return self._envelope_from_row(row)

    @staticmethod
    def _envelope_from_row(row: sqlite3.Row) -> EventEnvelope:
        causal_parent = None
        if row["causal_event_id"] is not None and row["causal_sequence"] is not None:
            causal_parent = EventRef(
                event_id=EventId(row["causal_event_id"]),
                sequence=row["causal_sequence"],
            )
        return EventEnvelope(
            event_id=EventId(row["event_id"]),
            sequence=row["sequence"],
            kind=EventKind(row["kind"]),
            payload=json.loads(row["payload_json"]),
            schema_version=row["schema_version"],
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            task_id=row["task_id"],
            saga_id=row["saga_id"],
            causal_parent=causal_parent,
            idempotency_key=row["idempotency_key"],
        )
