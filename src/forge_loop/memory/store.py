"""Durable store for curated project memory."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Protocol

from forge_loop.eventlog.models import EventId, EventRef
from forge_loop.memory.models import (
    REJECTED_PATH_TAG,
    MemoryItem,
    MemoryKind,
    MemoryProvenance,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_items (
    memory_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    source_event_id TEXT,
    source_sequence INTEGER,
    source_task_ref TEXT,
    authored_by TEXT NOT NULL,
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL,
    supersedes_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    superseded_by TEXT
);
"""


class MemoryStore(Protocol):
    """Persistence boundary for curated memory."""

    def put(self, item: MemoryItem) -> MemoryItem:
        """Persist ``item`` and return the stored shape."""
        ...

    def get(self, memory_id: str) -> MemoryItem | None:
        """Return one memory item, including superseded items."""
        ...

    def list_active(self, *, kind: MemoryKind | None = None) -> tuple[MemoryItem, ...]:
        """Return non-superseded memory items in insertion order."""
        ...

    def list_rejected_paths(self) -> tuple[MemoryItem, ...]:
        """Return active memory items tagged as rejected paths."""
        ...

    def supersede(self, memory_id: str, *, by_memory_id: str) -> MemoryItem:
        """Mark ``memory_id`` as superseded by ``by_memory_id``."""
        ...


class SqliteMemoryStore:
    """SQLite-backed durable memory store."""

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

    def put(self, item: MemoryItem) -> MemoryItem:
        source_event_id = None
        source_sequence = None
        if item.provenance.source_event is not None:
            source_event_id = str(item.provenance.source_event.event_id)
            source_sequence = item.provenance.source_event.sequence

        with self._connection:
            self._connection.execute(
                """
                INSERT INTO memory_items (
                    memory_id,
                    kind,
                    title,
                    body,
                    tags_json,
                    source_event_id,
                    source_sequence,
                    source_task_ref,
                    authored_by,
                    confidence,
                    created_at,
                    supersedes_json,
                    evidence_refs_json,
                    superseded_by
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id)
                DO UPDATE SET
                    kind = excluded.kind,
                    title = excluded.title,
                    body = excluded.body,
                    tags_json = excluded.tags_json,
                    source_event_id = excluded.source_event_id,
                    source_sequence = excluded.source_sequence,
                    source_task_ref = excluded.source_task_ref,
                    authored_by = excluded.authored_by,
                    confidence = excluded.confidence,
                    created_at = excluded.created_at,
                    supersedes_json = excluded.supersedes_json,
                    evidence_refs_json = excluded.evidence_refs_json,
                    superseded_by = excluded.superseded_by
                """,
                (
                    item.memory_id,
                    item.kind.value,
                    item.title,
                    item.body,
                    _json_tuple(item.tags),
                    source_event_id,
                    source_sequence,
                    item.provenance.source_task_ref,
                    item.provenance.authored_by,
                    item.provenance.confidence,
                    item.provenance.created_at.isoformat(),
                    _json_tuple(item.provenance.supersedes),
                    _json_tuple(item.provenance.evidence_refs),
                    item.superseded_by,
                ),
            )
        return item

    def get(self, memory_id: str) -> MemoryItem | None:
        row = self._connection.execute(
            """
            SELECT *
            FROM memory_items
            WHERE memory_id = ?
            """,
            (memory_id,),
        ).fetchone()
        if row is None:
            return None
        return _item_from_row(row)

    def list_active(self, *, kind: MemoryKind | None = None) -> tuple[MemoryItem, ...]:
        params: tuple[str, ...]
        where = "WHERE superseded_by IS NULL"
        if kind is None:
            params = ()
        else:
            where += " AND kind = ?"
            params = (kind.value,)
        return tuple(self._select(f"SELECT * FROM memory_items {where} ORDER BY rowid ASC", params))

    def list_rejected_paths(self) -> tuple[MemoryItem, ...]:
        return tuple(item for item in self.list_active() if REJECTED_PATH_TAG in item.tags)

    def supersede(self, memory_id: str, *, by_memory_id: str) -> MemoryItem:
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE memory_items
                SET superseded_by = ?
                WHERE memory_id = ?
                """,
                (by_memory_id, memory_id),
            )
        if cursor.rowcount != 1:
            raise KeyError(memory_id)
        superseded = self.get(memory_id)
        if superseded is None:
            raise KeyError(memory_id)
        return superseded

    def _select(self, query: str, params: tuple[str, ...]) -> Iterable[MemoryItem]:
        rows = self._connection.execute(query, params)
        return (_item_from_row(row) for row in rows)


def _json_tuple(values: tuple[str, ...]) -> str:
    return json.dumps(list(values), sort_keys=True, separators=(",", ":"))


def _load_tuple(raw: str, field_name: str) -> tuple[str, ...]:
    values = json.loads(raw)
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"memory field {field_name} must be a JSON list of strings")
    return tuple(values)


def _item_from_row(row: sqlite3.Row) -> MemoryItem:
    source_event = None
    if row["source_event_id"] is not None and row["source_sequence"] is not None:
        source_event = EventRef(
            event_id=EventId(row["source_event_id"]),
            sequence=row["source_sequence"],
        )
    return MemoryItem(
        memory_id=row["memory_id"],
        kind=MemoryKind(row["kind"]),
        title=row["title"],
        body=row["body"],
        tags=_load_tuple(row["tags_json"], "tags"),
        provenance=MemoryProvenance(
            source_event=source_event,
            authored_by=row["authored_by"],
            source_task_ref=row["source_task_ref"],
            confidence=row["confidence"],
            created_at=datetime.fromisoformat(row["created_at"]),
            supersedes=_load_tuple(row["supersedes_json"], "supersedes"),
            evidence_refs=_load_tuple(row["evidence_refs_json"], "evidence_refs"),
        ),
        superseded_by=row["superseded_by"],
    )
