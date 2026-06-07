"""SQLite-backed durable event log."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forge_loop.eventlog.guard import guard_prune
from forge_loop.eventlog.models import (
    EventEnvelope,
    EventId,
    EventKind,
    EventRef,
    is_load_bearing,
)
from forge_loop.eventlog.projections import ProjectionCursor, ProjectionReplayError


@dataclass(frozen=True)
class CompactionResult:
    """Outcome of a guarded compaction pass (issue #210)."""

    scanned: int
    pruned: int
    preserved_load_bearing: int
    high_water_sequence: int


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
    idempotency_key TEXT UNIQUE,
    chain_hash TEXT
);

CREATE TABLE IF NOT EXISTS projection_cursors (
    projection_name TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL
);
"""

#: Fixed empty/zero seed the genesis event chains from (#339). 64 hex zeros so
#: it is the same shape as a SHA-256 digest.
GENESIS_CHAIN_HASH = "0" * 64


def canonical_event_fields(
    *,
    event_id: str,
    kind: str,
    payload_json: str,
    schema_version: int,
    occurred_at: str,
    task_id: str | None,
    saga_id: str | None,
    idempotency_key: str | None,
) -> str:
    """Canonical JSON over the tamper-evident fields of one event (#339).

    Reuses the canonical-JSON convention (``sort_keys=True,
    separators=(",", ":")``) shared by :meth:`SqliteEventLog.append`'s
    ``payload_json`` and :func:`forge_loop.sandbox.policy.canonical_policy_json`,
    rather than inventing a new canonicaliser. ``payload_json`` is folded in as
    the already-canonical stored string so the probe can recompute the digest
    from the raw stored row without re-canonicalising the parsed payload.
    """

    return json.dumps(
        {
            "event_id": event_id,
            "idempotency_key": idempotency_key,
            "kind": kind,
            "occurred_at": occurred_at,
            "payload_json": payload_json,
            "saga_id": saga_id,
            "schema_version": schema_version,
            "task_id": task_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def compute_chain_hash(prev_chain_hash: str, canonical_fields: str) -> str:
    """``chain_hash_n = sha256(prev_chain_hash + canonical(event fields))`` (#339)."""

    return hashlib.sha256((prev_chain_hash + canonical_fields).encode("utf-8")).hexdigest()


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
        self._ensure_chain_hash_column()

    def _ensure_chain_hash_column(self) -> None:
        """Idempotent migration: add ``chain_hash`` to a pre-existing table (#339).

        ``_SCHEMA`` only adds the column on a *fresh* ``CREATE TABLE``; a DB that
        already exists on disk without it skips the create entirely, so back-fill
        the column here. Rows written before this migration keep ``NULL``
        chain_hash and are reported as unverifiable by the doctor probe.
        """

        columns = {row[1] for row in self._connection.execute("PRAGMA table_info(events)")}
        if "chain_hash" not in columns:
            with self._connection:
                self._connection.execute("ALTER TABLE events ADD COLUMN chain_hash TEXT")

    def _latest_chain_hash(self) -> str:
        """Return the chain hash of the current head event, or the genesis seed."""

        row = self._connection.execute(
            "SELECT chain_hash FROM events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        if row is None or row["chain_hash"] is None:
            return GENESIS_CHAIN_HASH
        return str(row["chain_hash"])

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
        schema_version = 1

        # Tamper-evident chain hash (#339): each event links to its predecessor
        # so the doctor probe can recompute the chain and detect a mutated row.
        chain_hash = compute_chain_hash(
            self._latest_chain_hash(),
            canonical_event_fields(
                event_id=str(event_id),
                kind=kind.value,
                payload_json=payload_json,
                schema_version=schema_version,
                occurred_at=occurred_at,
                task_id=task_id,
                saga_id=saga_id,
                idempotency_key=idempotency_key,
            ),
        )

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
                        idempotency_key,
                        chain_hash
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(event_id),
                        kind.value,
                        payload_json,
                        schema_version,
                        occurred_at,
                        task_id,
                        saga_id,
                        idempotency_key,
                        chain_hash,
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

    def latest_sequence(self) -> int:
        """Return the highest event sequence, or 0 when the log is empty."""

        row = self._connection.execute("SELECT MAX(sequence) AS sequence FROM events").fetchone()
        if row is None or row["sequence"] is None:
            return 0
        return int(row["sequence"])

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

    def advance_projection_cursor(
        self,
        projection_name: str,
        cursor: ProjectionCursor,
    ) -> None:
        """Persist a cursor only when it advances monotonically within the log."""

        current = self.get_projection_cursor(projection_name)
        if cursor.sequence < current.sequence:
            raise ProjectionReplayError(
                f"stale projection cursor for {projection_name}: "
                f"{cursor.sequence} < {current.sequence}"
            )
        latest = self.latest_sequence()
        if cursor.sequence > latest:
            raise ProjectionReplayError(
                f"projection cursor for {projection_name} is past latest event sequence: "
                f"{cursor.sequence} > {latest}"
            )
        self.set_projection_cursor(projection_name, cursor)

    def list_projection_cursors(self) -> Mapping[str, ProjectionCursor]:
        """Return saved projection cursors by projection name."""

        rows = self._connection.execute(
            """
            SELECT projection_name, sequence
            FROM projection_cursors
            ORDER BY projection_name ASC
            """
        )
        return {row["projection_name"]: ProjectionCursor(sequence=row["sequence"]) for row in rows}

    def prune(self, sequences: Iterable[int]) -> int:
        """Delete the given event sequences — GUARDED (issue #210, option a).

        Refuses the whole prune by raising
        :class:`forge_loop.eventlog.guard.LoadBearingGuardError` if ANY target
        sequence is a load-bearing event (per
        :func:`forge_loop.eventlog.models.is_load_bearing`). On refusal NOTHING
        is deleted — load-bearing events are never dropped. Returns the number
        of rows deleted on success.
        """
        targets = sorted({int(s) for s in sequences})
        if not targets:
            return 0
        placeholders = ",".join("?" for _ in targets)
        rows = self._connection.execute(
            f"SELECT sequence, kind FROM events WHERE sequence IN ({placeholders})",
            targets,
        ).fetchall()
        # guard_prune raises (refusing every deletion) if any target is
        # load-bearing — the row(s) are left untouched on disk.
        guard_prune(EventKind(row["kind"]) for row in rows)
        present = [int(row["sequence"]) for row in rows]
        if not present:
            return 0
        present_placeholders = ",".join("?" for _ in present)
        with self._connection:
            self._connection.execute(
                f"DELETE FROM events WHERE sequence IN ({present_placeholders})",
                present,
            )
        return len(present)

    def compact_noise(self, *, emit_marker: bool = True) -> CompactionResult:
        """Guarded compaction: drop telemetry/noise, keep load-bearing forever.

        Issue #210, option (b): the load-bearing remainder is forced to survive
        in the live tier (it is simply never selected for deletion) while only
        non-load-bearing rows are pruned. The current high-water-mark row is
        ALWAYS preserved regardless of kind, so ``latest_sequence()`` stays
        stable across a compaction — this keeps the boot-reconstruction
        invariant intact.

        Issue #323: compaction additionally respects the **slowest registered
        projection cursor**. A *prune floor* is computed as ``min(sequence)``
        over :meth:`list_projection_cursors`; any event whose ``sequence`` is
        **strictly greater than** that floor is protected from deletion,
        because a lagging cursor — e.g. one left far behind head after a
        crash/restart — may still need to replay it via ``since(cursor)``.
        Pruning such events would silently corrupt that projection's aggregate
        with no error raised. When **no cursors are registered** there is no
        floor and behaviour is byte-for-byte identical to the pre-#323 prune
        (load-bearing + tail preservation only). The cursor invariant is
        therefore *conditional*: a cursor's accounting survives compaction for
        load-bearing/tail rows (always) and for any event strictly above the
        slowest cursor floor (always); noise at-or-below the slowest cursor is
        still pruned.

        When ``emit_marker`` is true a :class:`EventKind.COMPACTION_PERFORMED`
        telemetry event is appended after the prune (this advances the log tail,
        so callers proving the boot invariant pass ``emit_marker=False`` to
        isolate the pure prune).
        """
        high_water = self.latest_sequence()
        # Issue #323: protect everything strictly above the slowest cursor so a
        # lagging projection can still replay it. No cursors ⇒ no floor.
        cursors = self.list_projection_cursors()
        prune_floor = min((c.sequence for c in cursors.values()), default=None)
        rows = self._connection.execute("SELECT sequence, kind FROM events").fetchall()
        scanned = len(rows)
        droppable: list[int] = []
        preserved = 0
        for row in rows:
            sequence = int(row["sequence"])
            if is_load_bearing(EventKind(row["kind"])):
                preserved += 1
                continue
            if sequence == high_water:
                # Never prune the tail: keeps latest_sequence() stable.
                continue
            if prune_floor is not None and sequence > prune_floor:
                # Protected: a lagging projection cursor may still replay it.
                continue
            droppable.append(sequence)

        if droppable:
            placeholders = ",".join("?" for _ in droppable)
            with self._connection:
                self._connection.execute(
                    f"DELETE FROM events WHERE sequence IN ({placeholders})",
                    droppable,
                )

        if emit_marker:
            self.append(
                EventKind.COMPACTION_PERFORMED,
                {
                    "scanned": scanned,
                    "pruned": len(droppable),
                    "preserved_load_bearing": preserved,
                },
            )

        return CompactionResult(
            scanned=scanned,
            pruned=len(droppable),
            preserved_load_bearing=preserved,
            high_water_sequence=high_water,
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
