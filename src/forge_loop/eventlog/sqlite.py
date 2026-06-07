"""SQLite-backed durable event log."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forge_loop.eventlog.chain import (
    GENESIS_HASH,
    EventChainIntegrityError,
    compute_event_hash,
)
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
    prev_hash TEXT,
    event_hash TEXT
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
        self._migrate_hash_chain_columns()

    def _migrate_hash_chain_columns(self) -> None:
        """Add the hash-chain columns to logs created before issue #338.

        Rows written before this migration keep ``NULL`` hashes; the read path
        treats a ``NULL`` ``event_hash`` as an unchained legacy row and skips
        verification for it (there is nothing to recompute against) while still
        verifying every event appended after the migration.
        """

        columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(events)")}
        with self._connection:
            if "prev_hash" not in columns:
                self._connection.execute("ALTER TABLE events ADD COLUMN prev_hash TEXT")
            if "event_hash" not in columns:
                self._connection.execute("ALTER TABLE events ADD COLUMN event_hash TEXT")

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
        prev_hash = self._tail_hash()
        event_hash = compute_event_hash(
            prev_hash=prev_hash,
            event_id=str(event_id),
            kind=kind.value,
            payload_json=payload_json,
            schema_version=1,
            occurred_at=occurred_at,
            task_id=task_id,
            saga_id=saga_id,
            idempotency_key=idempotency_key,
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
                        prev_hash,
                        event_hash
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        prev_hash,
                        event_hash,
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
        """Yield events with sequence greater than ``sequence`` in log order.

        The hash chain is verified as events are streamed (issue #338): each
        stored ``event_hash`` is recomputed from its canonical payload and the
        running prev-hash and compared to the stored value. The first sequence
        whose hash/chain no longer recomputes raises
        :class:`EventChainIntegrityError` instead of yielding a poisoned event,
        so replay refuses to fold a tampered, truncated, or reordered log into
        projections. An untouched log replays without raising.
        """

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
                idempotency_key,
                prev_hash,
                event_hash
            FROM events
            WHERE sequence > ?
            ORDER BY sequence ASC
            """,
            (sequence,),
        )
        return self._verified_stream(rows, self._chain_anchor(sequence))

    def _verified_stream(
        self, rows: Iterable[sqlite3.Row], running: str
    ) -> Iterable[EventEnvelope]:
        for row in rows:
            stored = row["event_hash"]
            if stored is not None:
                expected = self._row_hash(row, prev_hash=running)
                if expected != stored:
                    raise EventChainIntegrityError(
                        int(row["sequence"]),
                        "stored event_hash does not recompute from payload and "
                        "running prev-hash (payload tampered, row deleted, or "
                        "events reordered)",
                    )
                running = stored
            yield self._envelope_from_row(row)

    def _chain_anchor(self, sequence: int) -> str:
        """Return the running prev-hash to seed verification of a ``since`` window.

        For ``since(0)`` the anchor is the genesis hash. For a mid-log window the
        anchor is the stored ``event_hash`` of the last event at or before
        ``sequence`` (the predecessor of the first yielded row), so a partial
        replay verifies against the same prev-hash the writer chained from. A
        legacy (``NULL``-hash) predecessor falls back to genesis.
        """

        if sequence <= 0:
            return GENESIS_HASH
        row = self._connection.execute(
            "SELECT event_hash FROM events WHERE sequence <= ? ORDER BY sequence DESC LIMIT 1",
            (sequence,),
        ).fetchone()
        if row is None or row["event_hash"] is None:
            return GENESIS_HASH
        return str(row["event_hash"])

    @staticmethod
    def _row_hash(row: sqlite3.Row, *, prev_hash: str) -> str:
        return compute_event_hash(
            prev_hash=prev_hash,
            event_id=row["event_id"],
            kind=row["kind"],
            payload_json=row["payload_json"],
            schema_version=row["schema_version"],
            occurred_at=row["occurred_at"],
            task_id=row["task_id"],
            saga_id=row["saga_id"],
            idempotency_key=row["idempotency_key"],
        )

    def _tail_hash(self) -> str:
        """Return the ``event_hash`` of the latest event, or genesis when empty."""

        row = self._connection.execute(
            "SELECT event_hash FROM events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        if row is None or row["event_hash"] is None:
            return GENESIS_HASH
        return str(row["event_hash"])

    def _rechain(self) -> None:
        """Recompute the hash chain over the surviving rows in sequence order.

        Called after a *legitimate* deletion (guarded prune / compaction) so the
        survivors form a valid chain again — otherwise the read-path verifier
        could not distinguish an authorised compaction from out-of-band tampering
        and would refuse to replay a freshly compacted log.
        """

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
                idempotency_key
            FROM events
            ORDER BY sequence ASC
            """
        ).fetchall()
        running = GENESIS_HASH
        with self._connection:
            for row in rows:
                event_hash = self._row_hash(row, prev_hash=running)
                self._connection.execute(
                    "UPDATE events SET prev_hash = ?, event_hash = ? WHERE sequence = ?",
                    (running, event_hash, int(row["sequence"])),
                )
                running = event_hash

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
        # Re-link the surviving rows so the read-path verifier accepts an
        # authorised prune (issue #338).
        self._rechain()
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
            # Re-link survivors so an authorised compaction still replays cleanly
            # through the read-path integrity verifier (issue #338).
            self._rechain()

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
