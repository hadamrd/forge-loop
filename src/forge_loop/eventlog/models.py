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


# ---------------------------------------------------------------------------
# Load-bearing classification (issue #210)
#
# The durable event log is the substrate ``control/boot.py`` reconstructs
# project cognition from after a context loss. Rotation (NDJSON cascade in
# ``state.py``) and compaction (``eventlog/sqlite.py``) both prune the log; if
# they drop a *decision* the maestro re-litigates a settled choice — the exact
# amnesia failure mode the product exists to prevent.
#
# ``is_load_bearing`` is the SINGLE SOURCE OF TRUTH for "must this event survive
# a prune?". Both prune surfaces classify through it. The two frozensets below
# partition every registered :class:`EventKind` so adding a future kind without
# classifying it is caught by a test (``test_eventlog_load_bearing``), and the
# fail-safe default for anything unrecognised is *preserve*.
# ---------------------------------------------------------------------------

# The capability-grant event from #200 lives in the legacy NDJSON registry
# (``forge_loop.events.WorkerPolicyEnforcedEvent``), not in ``EventKind`` — but
# it is load-bearing: it records the exact grant a worker ran under, which boot
# replay reads to confirm confinement. Kept here as a string constant (the one
# classifier home) so we don't import the heavy ``events`` module.
CAPABILITY_GRANT_EVENT_KIND = "worker_policy_enforced"

#: Registered kinds whose loss would erase reconstructable project cognition:
#: strategic decisions, frontier advances, memory promotion/supersession,
#: terminal saga states, and loop-halt safety records.
LOAD_BEARING_EVENT_KINDS: frozenset[EventKind] = frozenset(
    {
        EventKind.VISION_UPDATED,
        EventKind.DECISION_MADE,
        EventKind.IDEA_REJECTED,
        EventKind.FRONTIER_ADVANCED,
        EventKind.TASK_COMPLETED,
        EventKind.TASK_FAILED,
        EventKind.TASK_COMPENSATED,
        EventKind.MEMORY_PROMOTED,
        EventKind.MEMORY_SUPERSEDED,
        EventKind.LOOP_HALTED,
    }
)

#: Registered kinds that are telemetry/noise — derivable, high-volume, and not
#: required to reconstruct boot cognition. Safe to prune.
TELEMETRY_EVENT_KINDS: frozenset[EventKind] = frozenset(
    {
        EventKind.TASK_PLANNED,
        EventKind.TASK_DISPATCHED,
        EventKind.TASK_HEARTBEAT,
        EventKind.WORKER_OBSERVATION,
        EventKind.CRITIQUE_ISSUED,
        EventKind.TICK_STARTED,
        EventKind.TICK_COMPLETED,
        EventKind.PR_OPENED,
        EventKind.PR_MERGED,
        EventKind.MERGE_BLOCKED,
        EventKind.WORKTREE_REAPED,
        EventKind.COMPACTION_PERFORMED,
    }
)

#: A deliberately small, unambiguous set of pure-telemetry *legacy* NDJSON
#: record kinds (the runner stream uses strings, not ``EventKind``). These are
#: safe to drop. Anything NOT here and NOT a load-bearing ``EventKind`` is
#: preserved fail-safe — over-retention is the safe failure mode.
LEGACY_TELEMETRY_KINDS: frozenset[str] = frozenset(
    {
        "tick_start",
        "tick_done",
        "heartbeat",
        "noise",
        "events_file_rotated",
        "events_rotation_failed",
        "orphan_worktrees_reaped",
        "redeploy",
    }
)


def is_load_bearing(event: EventEnvelope | EventKind | str) -> bool:
    """Return ``True`` when ``event`` must survive rotation/compaction.

    Single source of truth for the prune guards (issue #210). Accepts a durable
    :class:`EventEnvelope`, a bare :class:`EventKind`, or a raw ``kind`` string
    (as found on a legacy NDJSON record).

    Load-bearing: strategic decisions, frontier advances, memory
    promotion/supersession, terminal saga states (completed/failed/compensated),
    loop-halt safety records, and capability grants (#200).

    Fail-safe: an *unknown / unregistered* kind is classified load-bearing — we
    preserve when unsure rather than silently erase cognition. Only explicitly
    enumerated telemetry kinds return ``False``.
    """
    kind: EventKind | str = event.kind if isinstance(event, EventEnvelope) else event

    if isinstance(kind, EventKind):
        # A registered kind not yet placed in either set defaults to preserve.
        return kind not in TELEMETRY_EVENT_KINDS

    # Raw string kind (legacy NDJSON record `kind` field).
    if kind == CAPABILITY_GRANT_EVENT_KIND:
        return True
    if kind in LEGACY_TELEMETRY_KINDS:
        return False
    try:
        resolved = EventKind(kind)
    except ValueError:
        # Unknown/unregistered kind → fail-safe: preserve.
        return True
    return resolved not in TELEMETRY_EVENT_KINDS
