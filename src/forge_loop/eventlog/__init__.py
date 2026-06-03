"""Durable event-log contracts for long-running forge-loop state.

This package is the future WAL spine. The current runner still writes its
legacy JSONL events through :mod:`forge_loop.state` and
:mod:`forge_loop.events`; this package defines the stricter contracts new
control-plane work should use.
"""

from forge_loop.eventlog.legacy_mirror import LegacyEventMirror, LegacyRunnerEventKind
from forge_loop.eventlog.models import EventEnvelope, EventId, EventKind, EventRef
from forge_loop.eventlog.projections import (
    ProjectionCursor,
    ProjectionReplayError,
    replay_projection,
)
from forge_loop.eventlog.sqlite import SqliteEventLog
from forge_loop.eventlog.store import EventLog, InMemoryEventLog

__all__ = [
    "EventEnvelope",
    "EventId",
    "EventKind",
    "EventLog",
    "EventRef",
    "InMemoryEventLog",
    "LegacyEventMirror",
    "LegacyRunnerEventKind",
    "ProjectionCursor",
    "ProjectionReplayError",
    "SqliteEventLog",
    "replay_projection",
]
