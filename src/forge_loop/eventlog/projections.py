"""Projection contracts for event-log replay."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from forge_loop.eventlog.models import EventEnvelope


@dataclass(frozen=True)
class ProjectionCursor:
    """Where a projection last caught up to the durable event log."""

    sequence: int = 0


class Projection(Protocol):
    """State rebuilt from the append-only event log."""

    cursor: ProjectionCursor

    def apply(self, event: EventEnvelope) -> None:
        """Apply one event. Implementations should be idempotent by sequence."""
