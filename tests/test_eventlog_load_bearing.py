"""Unit tests for the load-bearing classifier + prune guard (issue #210).

The classifier is the single source of truth deciding which events rotation
(``state.py``) and compaction (``eventlog/sqlite.py``) may discard. The
table-driven test below forces a *deliberate* verdict for every registered
:class:`EventKind` — adding a future kind without classifying it fails the
completeness assertion, which is exactly the "no boiling-frog" guarantee the
manifesto asks for.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from forge_loop.eventlog.guard import (
    LoadBearingGuardError,
    PrunePartition,
    guard_prune,
    partition_for_prune,
)
from forge_loop.eventlog.models import (
    CAPABILITY_GRANT_EVENT_KIND,
    LOAD_BEARING_EVENT_KINDS,
    TELEMETRY_EVENT_KINDS,
    EventEnvelope,
    EventId,
    EventKind,
    is_load_bearing,
)

# One deliberate verdict per registered EventKind. Keep in sync with the enum:
# the completeness test below fails loudly if a kind is added without a row.
EXPECTED_VERDICTS: dict[EventKind, bool] = {
    EventKind.VISION_UPDATED: True,
    EventKind.DECISION_MADE: True,
    EventKind.IDEA_REJECTED: True,
    EventKind.FRONTIER_ADVANCED: True,
    EventKind.TASK_PLANNED: False,
    EventKind.TASK_DISPATCHED: False,
    EventKind.TASK_HEARTBEAT: False,
    EventKind.TASK_COMPLETED: True,
    EventKind.TASK_FAILED: True,
    EventKind.TASK_COMPENSATED: True,
    EventKind.WORKER_OBSERVATION: False,
    EventKind.CRITIQUE_ISSUED: False,
    EventKind.TICK_STARTED: False,
    EventKind.TICK_COMPLETED: False,
    EventKind.PR_OPENED: False,
    EventKind.PR_MERGED: False,
    EventKind.MERGE_BLOCKED: False,
    EventKind.WORKTREE_REAPED: False,
    EventKind.LOOP_HALTED: True,
    EventKind.MEMORY_PROMOTED: True,
    EventKind.MEMORY_SUPERSEDED: True,
    EventKind.COMPACTION_PERFORMED: False,
}


def _envelope(kind: EventKind, sequence: int = 1) -> EventEnvelope:
    return EventEnvelope(
        event_id=EventId(f"evt-{sequence}"),
        sequence=sequence,
        kind=kind,
        payload={"n": sequence},
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


class TestIsLoadBearingTable:
    def test_every_event_kind_has_a_deliberate_verdict(self) -> None:
        # Forces a verdict for every kind — a new EventKind with no row here
        # fails CI rather than silently inheriting a default.
        assert set(EXPECTED_VERDICTS) == set(EventKind)

    @pytest.mark.parametrize("kind", list(EventKind), ids=lambda k: k.name)
    def test_verdict_for_kind(self, kind: EventKind) -> None:
        expected = EXPECTED_VERDICTS[kind]
        # All three accepted input shapes must agree.
        assert is_load_bearing(kind) is expected
        assert is_load_bearing(kind.value) is expected
        assert is_load_bearing(_envelope(kind)) is expected

    def test_load_bearing_and_telemetry_sets_partition_the_enum(self) -> None:
        assert LOAD_BEARING_EVENT_KINDS.isdisjoint(TELEMETRY_EVENT_KINDS)
        assert set(EventKind) == LOAD_BEARING_EVENT_KINDS | TELEMETRY_EVENT_KINDS


class TestIsLoadBearingEdges:
    def test_unknown_kind_is_load_bearing_failsafe(self) -> None:
        # AC: an unknown/unregistered kind must be preserved (fail-safe).
        assert is_load_bearing("totally.unregistered.kind") is True
        assert is_load_bearing("") is True

    def test_capability_grant_from_200_is_load_bearing(self) -> None:
        assert is_load_bearing(CAPABILITY_GRANT_EVENT_KIND) is True
        assert is_load_bearing("worker_policy_enforced") is True

    def test_known_legacy_telemetry_is_not_load_bearing(self) -> None:
        assert is_load_bearing("tick_start") is False
        assert is_load_bearing("events_file_rotated") is False


class TestGuardHelpers:
    def test_partition_splits_without_mutating(self) -> None:
        events = [
            _envelope(EventKind.DECISION_MADE, 1),
            _envelope(EventKind.TICK_STARTED, 2),
            _envelope(EventKind.MEMORY_SUPERSEDED, 3),
            _envelope(EventKind.WORKER_OBSERVATION, 4),
        ]
        snapshot = list(events)
        partition = partition_for_prune(events)

        assert isinstance(partition, PrunePartition)
        assert partition.has_load_bearing is True
        assert tuple(e.sequence for e in partition.preserve) == (1, 3)  # type: ignore[attr-defined]
        assert tuple(e.sequence for e in partition.droppable) == (2, 4)  # type: ignore[attr-defined]
        # Input list is untouched.
        assert events == snapshot

    def test_partition_all_noise_has_no_load_bearing(self) -> None:
        partition = partition_for_prune([EventKind.TICK_STARTED, EventKind.TICK_COMPLETED])
        assert partition.has_load_bearing is False
        assert partition.droppable == (EventKind.TICK_STARTED, EventKind.TICK_COMPLETED)

    def test_partition_empty_input(self) -> None:
        partition = partition_for_prune([])
        assert partition.preserve == ()
        assert partition.droppable == ()
        assert partition.has_load_bearing is False

    def test_guard_prune_returns_noise_untouched(self) -> None:
        noise = (EventKind.TICK_STARTED, EventKind.WORKER_OBSERVATION)
        assert guard_prune(noise) == noise

    def test_guard_prune_refuses_load_bearing(self) -> None:
        batch = [
            _envelope(EventKind.TICK_STARTED, 1),
            _envelope(EventKind.DECISION_MADE, 2),
        ]
        with pytest.raises(LoadBearingGuardError) as excinfo:
            guard_prune(batch)
        # The error carries exactly the protected (load-bearing) item.
        assert len(excinfo.value.protected) == 1
        assert excinfo.value.protected[0].kind is EventKind.DECISION_MADE  # type: ignore[attr-defined]

    def test_guard_prune_empty_is_noop(self) -> None:
        assert guard_prune([]) == ()
