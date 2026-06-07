"""Unit tests for the scorecard projection (issue #307).

The scorecard folds the durable event stream into trend metrics. These tests
feed hand-built fixtures (constructed ``EventEnvelope`` streams replayed via
``InMemoryEventLog``) and assert exact metric values, idempotency by sequence,
determinism, and honest ``None`` reporting for absent signals.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from forge_loop.eventlog import (
    EventEnvelope,
    EventKind,
    InMemoryEventLog,
    ProjectionCursor,
    ScorecardMetrics,
    ScorecardProjection,
    replay_projection,
)
from forge_loop.eventlog.legacy_mirror import LegacyRunnerEventKind
from forge_loop.eventlog.models import EventId

_T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _evt(
    sequence: int,
    kind: EventKind,
    *,
    task_id: str | None = None,
    payload: dict | None = None,
    offset_seconds: float = 0.0,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=EventId(f"e{sequence}"),
        sequence=sequence,
        kind=kind,
        payload=payload or {},
        task_id=task_id,
        occurred_at=_T0 + timedelta(seconds=offset_seconds),
    )


_MERGED = {"legacy_kind": LegacyRunnerEventKind.CRITIC_VERDICT_MERGED.value}
_BLOCKED = {"legacy_kind": LegacyRunnerEventKind.CRITIC_VERDICT_BLOCKED.value}


def _rich_stream() -> list[EventEnvelope]:
    """Four tasks exercising every metric branch."""
    return [
        # issue:1 — accepted on first critique, completed after 100s.
        _evt(1, EventKind.TASK_PLANNED, task_id="issue:1", offset_seconds=0),
        _evt(2, EventKind.CRITIQUE_ISSUED, task_id="issue:1", payload=_MERGED),
        _evt(3, EventKind.TASK_COMPLETED, task_id="issue:1", offset_seconds=100),
        # issue:2 — blocked once, then merged, completed after 200s (1 repair round).
        _evt(4, EventKind.TASK_PLANNED, task_id="issue:2", offset_seconds=0),
        _evt(5, EventKind.CRITIQUE_ISSUED, task_id="issue:2", payload=_BLOCKED),
        _evt(6, EventKind.CRITIQUE_ISSUED, task_id="issue:2", payload=_MERGED),
        _evt(7, EventKind.TASK_COMPLETED, task_id="issue:2", offset_seconds=200),
        # issue:3 — failed (abandoned), no critique, no lead time.
        _evt(8, EventKind.TASK_PLANNED, task_id="issue:3", offset_seconds=0),
        _evt(9, EventKind.TASK_FAILED, task_id="issue:3", offset_seconds=50),
        # issue:4 — still in flight (planned only): excluded from everything terminal.
        _evt(10, EventKind.TASK_PLANNED, task_id="issue:4", offset_seconds=0),
        # tick-level event with no task identity: must not raise / not affect metrics.
        _evt(11, EventKind.TICK_COMPLETED, payload={"tick": 7}),
    ]


def _replay(stream: list[EventEnvelope]) -> ScorecardProjection:
    projection = ScorecardProjection()
    log = InMemoryEventLog(_events=list(stream))
    replay_projection(log, "scorecard", projection)
    return projection


def test_metrics_exact_values_over_rich_stream() -> None:
    metrics = _replay(_rich_stream()).metrics()

    # issue:1 first critique accepted, issue:2 first critique blocked → 1/2.
    assert metrics.first_pass_critic_acceptance_rate == 0.5
    # converged tasks issue:1 (0 repair) + issue:2 (1 repair) → mean 0.5.
    assert metrics.mean_repair_rounds_to_converge == 0.5
    # lead time only for completed: (100 + 200) / 2 = 150s.
    assert metrics.mean_lead_time_seconds == 150.0
    # terminal tasks: issue:1, issue:2 (completed) + issue:3 (failed) → 1/3 abandoned.
    assert metrics.abandonment_rate == 1 / 3
    # No payload field backs sev2-regeneration — must be explicitly unknown.
    assert metrics.sev2_regeneration_rate is None


def test_apply_is_idempotent_by_sequence_on_double_replay() -> None:
    stream = _rich_stream()
    single = _replay(stream)

    # Replay the SAME stream a second time through the SAME instance.
    log = InMemoryEventLog(_events=list(stream))
    log.set_projection_cursor("scorecard", single.cursor)
    rebuilt = replay_projection(log, "scorecard", single)

    assert single.metrics() == _replay(stream).metrics()
    assert single.cursor == ProjectionCursor(sequence=11)
    assert rebuilt == ProjectionCursor(sequence=11)


def test_apply_below_cursor_is_a_noop() -> None:
    projection = ScorecardProjection()
    projection.apply(_evt(1, EventKind.TASK_PLANNED, task_id="issue:1"))
    projection.apply(_evt(2, EventKind.TASK_COMPLETED, task_id="issue:1", offset_seconds=10))
    snapshot = projection.metrics()

    # Re-applying already-seen sequences must not double-count.
    projection.apply(_evt(1, EventKind.TASK_PLANNED, task_id="issue:1"))
    projection.apply(_evt(2, EventKind.TASK_COMPLETED, task_id="issue:1", offset_seconds=10))

    assert projection.metrics() == snapshot
    assert projection.cursor == ProjectionCursor(sequence=2)


def test_determinism_two_fresh_projections_agree() -> None:
    stream = _rich_stream()
    assert _replay(stream).metrics() == _replay(stream).metrics()


def test_lead_time_excludes_in_flight_task() -> None:
    stream = [
        _evt(1, EventKind.TASK_PLANNED, task_id="issue:1", offset_seconds=0),
        _evt(2, EventKind.TASK_COMPLETED, task_id="issue:1", offset_seconds=90),
        # in flight: planned, never terminal — excluded from lead-time stats.
        _evt(3, EventKind.TASK_PLANNED, task_id="issue:2", offset_seconds=0),
    ]
    metrics = _replay(stream).metrics()
    assert metrics.mean_lead_time_seconds == 90.0


def test_failed_task_counts_abandonment_not_lead_time() -> None:
    stream = [
        _evt(1, EventKind.TASK_PLANNED, task_id="issue:1", offset_seconds=0),
        _evt(2, EventKind.TASK_FAILED, task_id="issue:1", offset_seconds=30),
    ]
    metrics = _replay(stream).metrics()
    assert metrics.abandonment_rate == 1.0
    assert metrics.mean_lead_time_seconds is None


def test_compensated_task_counts_abandonment_not_lead_time() -> None:
    stream = [
        _evt(1, EventKind.TASK_PLANNED, task_id="issue:1", offset_seconds=0),
        _evt(2, EventKind.TASK_COMPENSATED, task_id="issue:1", offset_seconds=30),
    ]
    metrics = _replay(stream).metrics()
    assert metrics.abandonment_rate == 1.0
    assert metrics.mean_lead_time_seconds is None


def test_missing_verdict_field_leaves_acceptance_unknown_and_does_not_raise() -> None:
    stream = [
        _evt(1, EventKind.TASK_PLANNED, task_id="issue:1", offset_seconds=0),
        # CRITIQUE_ISSUED with no legacy_kind verdict field backing it.
        _evt(2, EventKind.CRITIQUE_ISSUED, task_id="issue:1", payload={}),
        _evt(3, EventKind.TASK_COMPLETED, task_id="issue:1", offset_seconds=10),
    ]
    metrics = _replay(stream).metrics()
    # No usable verdict → acceptance is unknown, not a fabricated 0.0.
    assert metrics.first_pass_critic_acceptance_rate is None
    assert metrics.sev2_regeneration_rate is None


def test_empty_stream_reports_all_unknown() -> None:
    metrics = ScorecardProjection().metrics()
    assert metrics == ScorecardMetrics(
        first_pass_critic_acceptance_rate=None,
        mean_repair_rounds_to_converge=None,
        sev2_regeneration_rate=None,
        mean_lead_time_seconds=None,
        abandonment_rate=None,
    )
