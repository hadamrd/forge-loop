"""Event-log storage interfaces.

The first implementation is intentionally in-memory so contracts can settle
before durable storage is wired into the runner. Production storage should be
append-only and crash-safe.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from forge_loop.eventlog.models import EventEnvelope, EventId, EventKind
from forge_loop.eventlog.projections import ProjectionCursor, ProjectionReplayError


class EventLog(Protocol):
    """Append-only event log used by projections and control-plane replay."""

    def append(
        self,
        kind: EventKind,
        payload: Mapping[str, Any],
        *,
        task_id: str | None = None,
        saga_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> EventEnvelope:
        """Append one event and return its durable envelope."""
        ...

    def since(self, sequence: int = 0) -> Iterable[EventEnvelope]:
        """Yield events with sequence greater than ``sequence``."""
        ...

    def latest_sequence(self) -> int:
        """Return the highest durable event sequence, or 0 when empty."""
        ...

    def get_projection_cursor(self, projection_name: str) -> ProjectionCursor:
        """Return one projection cursor, or sequence 0 when absent."""
        ...

    def set_projection_cursor(
        self,
        projection_name: str,
        cursor: ProjectionCursor,
    ) -> None:
        """Persist a projection cursor."""
        ...

    def advance_projection_cursor(
        self,
        projection_name: str,
        cursor: ProjectionCursor,
    ) -> None:
        """Persist a cursor only when it is monotonic and within the log tail."""
        ...

    def list_projection_cursors(self) -> Mapping[str, ProjectionCursor]:
        """Return all saved projection cursors by projection name."""
        ...


@dataclass
class InMemoryEventLog:
    """Small contract implementation for scaffolding and early dogfood.

    This is not durable. It exists so frontier, memory, and task code can share
    one event shape before the WAL backend lands.
    """

    _events: list[EventEnvelope] = field(default_factory=list)
    _projection_cursors: dict[str, ProjectionCursor] = field(default_factory=dict)

    def append(
        self,
        kind: EventKind,
        payload: Mapping[str, Any],
        *,
        task_id: str | None = None,
        saga_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> EventEnvelope:
        event = EventEnvelope(
            event_id=EventId(uuid.uuid4().hex),
            sequence=len(self._events) + 1,
            kind=kind,
            payload=payload,
            task_id=task_id,
            saga_id=saga_id,
            idempotency_key=idempotency_key,
        )
        self._events.append(event)
        return event

    def since(self, sequence: int = 0) -> Iterable[EventEnvelope]:
        return (event for event in self._events if event.sequence > sequence)

    def latest_sequence(self) -> int:
        if not self._events:
            return 0
        return self._events[-1].sequence

    def get_projection_cursor(self, projection_name: str) -> ProjectionCursor:
        return self._projection_cursors.get(projection_name, ProjectionCursor(sequence=0))

    def set_projection_cursor(
        self,
        projection_name: str,
        cursor: ProjectionCursor,
    ) -> None:
        self._projection_cursors[projection_name] = cursor

    def advance_projection_cursor(
        self,
        projection_name: str,
        cursor: ProjectionCursor,
    ) -> None:
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
        return dict(self._projection_cursors)
