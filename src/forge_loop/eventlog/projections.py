"""Projection contracts for event-log replay."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from forge_loop.eventlog.models import EventEnvelope


class ProjectionReplayError(RuntimeError):
    """Raised when replay or cursor advancement would make a projection unsafe."""


@dataclass(frozen=True)
class ProjectionCursor:
    """Where a projection last caught up to the durable event log."""

    sequence: int = 0


class Projection(Protocol):
    """State rebuilt from the append-only event log."""

    cursor: ProjectionCursor

    def apply(self, event: EventEnvelope) -> None:
        """Apply one event. Implementations should be idempotent by sequence."""


class ProjectionEventLog(Protocol):
    """Event-log operations needed for deterministic projection replay."""

    def since(self, sequence: int = 0) -> Iterable[EventEnvelope]:
        """Yield events with sequence greater than ``sequence`` in log order."""
        ...

    def advance_projection_cursor(
        self,
        projection_name: str,
        cursor: ProjectionCursor,
    ) -> None:
        """Persist a cursor only when it is monotonic and within the log tail."""
        ...


def replay_projection(
    event_log: ProjectionEventLog,
    projection_name: str,
    projection: Projection,
) -> ProjectionCursor:
    """Replay events from a projection cursor and persist the replayed tail.

    The cursor advances only after every yielded event is applied. A projection
    that receives events out of order should raise before the stored cursor
    moves, which keeps restart recovery from blessing a partial replay.
    """

    cursor = projection.cursor
    for event in event_log.since(cursor.sequence):
        projection.apply(event)
        cursor = ProjectionCursor(sequence=event.sequence)
    event_log.advance_projection_cursor(projection_name, cursor)
    return cursor
