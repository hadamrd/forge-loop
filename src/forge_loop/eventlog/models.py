"""Typed event envelopes for the future durable event log.

The existing event bus records useful facts but does not yet carry enough
metadata for deterministic replay, projection cursors, or idempotent external
effects. These models define the stricter shape without forcing an immediate
runner migration.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, NewType

EventId = NewType("EventId", str)


class EventKind(StrEnum):
    """Canonical high-level event kinds for long-running control-plane work."""

    VISION_UPDATED = "vision.updated"
    DECISION_MADE = "decision.made"
    IDEA_REJECTED = "idea.rejected"
    FRONTIER_ADVANCED = "frontier.advanced"
    TASK_PLANNED = "task.planned"
    TASK_DISPATCHED = "task.dispatched"
    TASK_HEARTBEAT = "task.heartbeat"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_COMPENSATED = "task.compensated"
    WORKER_OBSERVATION = "worker.observation"
    CRITIQUE_ISSUED = "critique.issued"
    TICK_STARTED = "tick.started"
    TICK_COMPLETED = "tick.completed"
    PR_OPENED = "pr.opened"
    PR_MERGED = "pr.merged"
    MERGE_BLOCKED = "merge.blocked"
    WORKTREE_REAPED = "worktree.reaped"
    LOOP_HALTED = "loop.halted"
    MEMORY_PROMOTED = "memory.promoted"
    MEMORY_SUPERSEDED = "memory.superseded"
    COMPACTION_PERFORMED = "compaction.performed"


@dataclass(frozen=True)
class EventRef:
    """Pointer to an event already written to the durable log."""

    event_id: EventId
    sequence: int


@dataclass(frozen=True)
class EventEnvelope:
    """Durable event envelope.

    The envelope metadata is intentionally more important than the payload.
    Payload schemas can evolve by kind, but replay and projection safety depend
    on sequence, causal references, saga/task identity, schema version, and
    idempotency keys.
    """

    event_id: EventId
    sequence: int
    kind: EventKind
    payload: Mapping[str, Any]
    schema_version: int = 1
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    task_id: str | None = None
    saga_id: str | None = None
    causal_parent: EventRef | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("event sequence must be >= 1")
        if self.schema_version < 1:
            raise ValueError("event schema_version must be >= 1")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @property
    def ref(self) -> EventRef:
        return EventRef(event_id=self.event_id, sequence=self.sequence)
