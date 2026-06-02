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

    def since(self, sequence: int = 0) -> Iterable[EventEnvelope]:
        """Yield events with sequence greater than ``sequence``."""


@dataclass
class InMemoryEventLog:
    """Small contract implementation for scaffolding and early dogfood.

    This is not durable. It exists so frontier, memory, and task code can share
    one event shape before the WAL backend lands.
    """

    _events: list[EventEnvelope] = field(default_factory=list)

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
