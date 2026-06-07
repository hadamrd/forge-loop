"""Durable event-log contracts for long-running forge-loop state.

This package is the future WAL spine. The current runner still writes its
legacy JSONL events through :mod:`forge_loop.state` and
:mod:`forge_loop.events`; this package defines the stricter contracts new
control-plane work should use.
"""

from forge_loop.eventlog.chain import (
    GENESIS_HASH,
    EventChainIntegrityError,
    compute_event_hash,
)
from forge_loop.eventlog.guard import (
    LoadBearingGuardError,
    PrunePartition,
    guard_prune,
    partition_for_prune,
)
from forge_loop.eventlog.legacy_mirror import LegacyEventMirror, LegacyRunnerEventKind
from forge_loop.eventlog.models import (
    CAPABILITY_GRANT_EVENT_KIND,
    LEGACY_TELEMETRY_KINDS,
    LOAD_BEARING_EVENT_KINDS,
    TELEMETRY_EVENT_KINDS,
    EventEnvelope,
    EventId,
    EventKind,
    EventRef,
    is_load_bearing,
)
from forge_loop.eventlog.projections import (
    ProjectionCursor,
    ProjectionReplayError,
    replay_projection,
)
from forge_loop.eventlog.sqlite import CompactionResult, SqliteEventLog
from forge_loop.eventlog.store import EventLog, InMemoryEventLog

__all__ = [
    "CAPABILITY_GRANT_EVENT_KIND",
    "LEGACY_TELEMETRY_KINDS",
    "LOAD_BEARING_EVENT_KINDS",
    "TELEMETRY_EVENT_KINDS",
    "GENESIS_HASH",
    "CompactionResult",
    "EventChainIntegrityError",
    "EventEnvelope",
    "EventId",
    "EventKind",
    "EventLog",
    "EventRef",
    "InMemoryEventLog",
    "LegacyEventMirror",
    "LegacyRunnerEventKind",
    "LoadBearingGuardError",
    "ProjectionCursor",
    "ProjectionReplayError",
    "PrunePartition",
    "SqliteEventLog",
    "compute_event_hash",
    "guard_prune",
    "is_load_bearing",
    "partition_for_prune",
    "replay_projection",
]
