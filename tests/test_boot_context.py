from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from forge_loop.control.boot import (
    BootContextError,
    BootSources,
    assemble_boot_context,
    build_boot_sources,
)
from forge_loop.eventlog import (
    EventEnvelope,
    EventKind,
    ProjectionCursor,
    ScorecardProjection,
    SqliteEventLog,
)
from forge_loop.frontier import FrontierCursor, FrontierStore
from forge_loop.memory import (
    REJECTED_PATH_TAG,
    MemoryItem,
    MemoryKind,
    MemoryProvenance,
    SqliteMemoryStore,
)
from forge_loop.tasks import Compensation, SqliteTaskSagaStore, TaskSaga, TaskState


def _frontier() -> FrontierCursor:
    return FrontierCursor(
        product_goal="make long-running agents recoverable",
        current_problem="boot context is not assembled from durable stores",
        next_expansion="load compact maestro context after restart",
        why_now="reset recovery must not depend on transcript memory",
        active_decisions=("assemble from explicit projections",),
    )


def _memory(memory_id: str, *, tags: tuple[str, ...] = ()) -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        kind=MemoryKind.SEMANTIC,
        title=f"{memory_id} title",
        body="Boot context keeps strategic facts compact.",
        tags=tags,
        provenance=MemoryProvenance(
            source_event=None,
            authored_by="test",
            source_task_ref="task:#165",
            confidence=0.9,
        ),
    )


@dataclass
class _RecordingProjection:
    """Minimal projection that records the tail it replays (cursor-only)."""

    cursor: ProjectionCursor = ProjectionCursor()
    applied_sequences: tuple[int, ...] = ()

    def apply(self, event: EventEnvelope) -> None:
        self.cursor = ProjectionCursor(sequence=event.sequence)
        self.applied_sequences = (*self.applied_sequences, event.sequence)


@dataclass
class _ExplodingProjection:
    """Projection that fails if replay is ever attempted against it."""

    cursor: ProjectionCursor = ProjectionCursor()

    def apply(self, event: EventEnvelope) -> None:
        raise AssertionError(f"replay must not run; got event {event.sequence}")


def _seed_events(eventlog: SqliteEventLog, count: int) -> int:
    last = 0
    for _ in range(count):
        last = eventlog.append(EventKind.FRONTIER_ADVANCED, {"cursor": "seed"}).sequence
    return last


def test_boot_drives_lagging_projection_cursor_to_tail(tmp_path: Path) -> None:
    frontier_path = tmp_path / "frontier.yaml"
    eventlog_path = tmp_path / "events.db"
    FrontierStore(frontier_path).save(_frontier())

    eventlog = SqliteEventLog(eventlog_path)
    latest = _seed_events(eventlog, 10)
    eventlog.set_projection_cursor("control", ProjectionCursor(sequence=4))

    projection = _RecordingProjection()
    context = assemble_boot_context(
        BootSources(
            frontier_store=FrontierStore(frontier_path),
            event_log=SqliteEventLog(eventlog_path),
            projections={"control": projection},
        )
    )

    assert context.projection_cursors["control"].sequence == latest
    assert context.projection_cursors["control"].lag == 0
    # Only the lagging tail (5..10) is replayed — events 1..4 are NOT re-applied.
    assert projection.applied_sequences == (5, 6, 7, 8, 9, 10)
    assert "projections: control@10 lag=0" in context.summary()


def test_boot_projection_already_at_tail_is_a_noop(tmp_path: Path) -> None:
    frontier_path = tmp_path / "frontier.yaml"
    eventlog_path = tmp_path / "events.db"
    FrontierStore(frontier_path).save(_frontier())

    eventlog = SqliteEventLog(eventlog_path)
    latest = _seed_events(eventlog, 6)
    eventlog.set_projection_cursor("control", ProjectionCursor(sequence=latest))

    projection = _RecordingProjection()
    context = assemble_boot_context(
        BootSources(
            frontier_store=FrontierStore(frontier_path),
            event_log=SqliteEventLog(eventlog_path),
            projections={"control": projection},
        )
    )

    assert projection.applied_sequences == ()
    assert context.projection_cursors["control"].sequence == latest
    assert context.projection_cursors["control"].lag == 0


def test_boot_empty_event_log_succeeds_without_replay(tmp_path: Path) -> None:
    frontier_path = tmp_path / "frontier.yaml"
    FrontierStore(frontier_path).save(_frontier())

    context = assemble_boot_context(
        BootSources(
            frontier_store=FrontierStore(frontier_path),
            event_log=SqliteEventLog(tmp_path / "events.db"),
            projections={"control": _ExplodingProjection()},
        )
    )

    assert context.latest_event_sequence == 0
    assert context.projection_cursors == {}


def test_boot_context_assembles_from_reopened_durable_stores(tmp_path: Path) -> None:
    frontier_path = tmp_path / "frontier.yaml"
    eventlog_path = tmp_path / "events.db"
    memory_path = tmp_path / "memory.db"
    task_path = tmp_path / "tasks.db"

    FrontierStore(frontier_path).save(_frontier())
    eventlog = SqliteEventLog(eventlog_path)
    eventlog.append(EventKind.FRONTIER_ADVANCED, {"cursor": "seed"})
    latest_event = eventlog.append(
        EventKind.TASK_DISPATCHED,
        {"task_id": "task-165-a"},
        task_id="task-165-a",
        saga_id="saga-165-a",
    )
    eventlog.set_projection_cursor("frontier", ProjectionCursor(sequence=1))
    eventlog.set_projection_cursor("memory", ProjectionCursor(sequence=latest_event.sequence))

    memory_store = SqliteMemoryStore(memory_path)
    memory_store.put(_memory("mem-active"))
    memory_store.put(_memory("mem-rejected", tags=(REJECTED_PATH_TAG,)))

    task_store = SqliteTaskSagaStore(task_path)
    task_store.put(
        TaskSaga(
            task_id="task-165-a",
            saga_id="saga-165-a",
            state=TaskState.RUNNING,
            issue=165,
            branch="loop/165-feat-control-assemble-bootcontext-from-d",
        )
    )
    task_store.put(
        TaskSaga(
            task_id="task-165-done",
            saga_id="saga-165-done",
            state=TaskState.COMPLETED,
            issue=165,
        )
    )

    context = assemble_boot_context(
        BootSources(
            frontier_store=FrontierStore(frontier_path),
            event_log=SqliteEventLog(eventlog_path),
            memory_store=SqliteMemoryStore(memory_path),
            task_store=SqliteTaskSagaStore(task_path),
        )
    )

    assert context.frontier.current_problem == ("boot context is not assembled from durable stores")
    assert context.active_memory_ids == ("mem-active", "mem-rejected")
    assert context.rejected_path_memory_ids == ("mem-rejected",)
    assert context.in_flight_task_ids == ("task-165-a",)
    assert context.in_flight_saga_ids == ("saga-165-a",)
    assert context.latest_event_sequence == latest_event.sequence
    assert context.projection_cursors["frontier"].sequence == 1
    assert context.projection_cursors["frontier"].lag == 1
    assert context.projection_cursors["memory"].lag == 0

    summary = context.summary()
    assert "current: boot context is not assembled from durable stores" in summary
    assert "next: load compact maestro context after restart" in summary
    assert "memory: mem-active, mem-rejected" in summary
    assert "rejected_memory: mem-rejected" in summary
    assert "in_flight: task-165-a/saga-165-a" in summary
    assert f"event_sequence: {latest_event.sequence}" in summary
    assert "projections: frontier@1 lag=1, memory@2 lag=0" in summary


def test_boot_context_missing_frontier_errors_clearly(tmp_path: Path) -> None:
    with pytest.raises(BootContextError, match="frontier state is required"):
        assemble_boot_context(
            BootSources(
                frontier_store=FrontierStore(tmp_path / "missing.yaml"),
                event_log=SqliteEventLog(tmp_path / "events.db"),
            )
        )


def test_boot_context_accepts_no_active_tasks_after_reopen(tmp_path: Path) -> None:
    frontier_path = tmp_path / "frontier.yaml"
    task_path = tmp_path / "tasks.db"
    FrontierStore(frontier_path).save(_frontier())
    SqliteTaskSagaStore(task_path).put(
        TaskSaga(
            task_id="task-165-failed",
            saga_id="saga-165-failed",
            state=TaskState.FAILED,
            issue=165,
            compensations=(
                Compensation(
                    kind="cleanup-worktree",
                    target="/tmp/wt-loop-165",
                    reason="failed terminal tasks must keep their cleanup record",
                ),
            ),
        )
    )

    context = assemble_boot_context(
        BootSources(
            frontier_store=FrontierStore(frontier_path),
            event_log=SqliteEventLog(tmp_path / "events.db"),
            task_store=SqliteTaskSagaStore(task_path),
        )
    )

    assert context.in_flight_task_ids == ()
    assert context.in_flight_saga_ids == ()
    assert "in_flight:" not in context.summary()


def test_boot_context_treats_missing_optional_stores_as_empty(tmp_path: Path) -> None:
    frontier_path = tmp_path / "frontier.yaml"
    FrontierStore(frontier_path).save(_frontier())

    context = assemble_boot_context(
        BootSources(
            frontier_store=FrontierStore(frontier_path),
            event_log=SqliteEventLog(tmp_path / "events.db"),
        )
    )

    assert context.active_memory_ids == ()
    assert context.rejected_path_memory_ids == ()
    assert context.in_flight_task_ids == ()


def _seed_task_events(eventlog: SqliteEventLog) -> int:
    """Append a small mirror-shaped task stream; return latest sequence."""
    eventlog.append(EventKind.TASK_PLANNED, {"issue": 1}, task_id="issue:1")
    eventlog.append(
        EventKind.CRITIQUE_ISSUED,
        {"legacy_kind": "critic_verdict_merged"},
        task_id="issue:1",
    )
    return eventlog.append(
        EventKind.TASK_COMPLETED, {"status": "merged"}, task_id="issue:1"
    ).sequence


def test_boot_drives_scorecard_projection_to_tail(tmp_path: Path) -> None:
    frontier_path = tmp_path / "frontier.yaml"
    eventlog_path = tmp_path / "events.db"
    FrontierStore(frontier_path).save(_frontier())

    latest = _seed_task_events(SqliteEventLog(eventlog_path))

    context = assemble_boot_context(
        BootSources(
            frontier_store=FrontierStore(frontier_path),
            event_log=SqliteEventLog(eventlog_path),
            projections={"scorecard": ScorecardProjection()},
        )
    )

    status = context.projection_cursors["scorecard"]
    assert status.sequence == latest
    assert status.lag == 0


def test_boot_drives_scorecard_from_partial_cursor_to_head(tmp_path: Path) -> None:
    frontier_path = tmp_path / "frontier.yaml"
    eventlog_path = tmp_path / "events.db"
    FrontierStore(frontier_path).save(_frontier())

    latest = _seed_task_events(SqliteEventLog(eventlog_path))
    # Pre-existing partial cursor mid-log: boot must drive only the remaining tail.
    SqliteEventLog(eventlog_path).set_projection_cursor("scorecard", ProjectionCursor(sequence=1))

    context = assemble_boot_context(
        BootSources(
            frontier_store=FrontierStore(frontier_path),
            event_log=SqliteEventLog(eventlog_path),
            projections={"scorecard": ScorecardProjection()},
        )
    )

    assert context.projection_cursors["scorecard"].sequence == latest
    assert context.projection_cursors["scorecard"].lag == 0


def test_build_boot_sources_registers_scorecard(tmp_path: Path) -> None:
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()
    FrontierStore(forge_dir / "frontier.yaml").save(_frontier())

    sources = build_boot_sources(tmp_path)

    assert "scorecard" in sources.projections
    assert isinstance(sources.projections["scorecard"], ScorecardProjection)


@dataclass
class _GappyEventLog:
    """Event log whose ``since`` stops one event short of the head.

    Simulates a replay that cannot reach the log tail (a gap/truncation),
    exercising the head-mismatch guard in ``_drive_projections_to_tail``.
    """

    inner: SqliteEventLog

    def latest_sequence(self) -> int:
        return self.inner.latest_sequence()

    def since(self, sequence: int = 0) -> Iterable[EventEnvelope]:
        events = list(self.inner.since(sequence))
        return events[:-1]  # never yields the head event

    def advance_projection_cursor(self, projection_name: str, cursor: ProjectionCursor) -> None:
        self.inner.advance_projection_cursor(projection_name, cursor)

    def list_projection_cursors(self) -> Mapping[str, ProjectionCursor]:
        return self.inner.list_projection_cursors()

    def get_projection_cursor(self, projection_name: str) -> ProjectionCursor:
        return self.inner.get_projection_cursor(projection_name)


def test_boot_aborts_when_scorecard_cannot_reach_head(tmp_path: Path) -> None:
    frontier_path = tmp_path / "frontier.yaml"
    eventlog_path = tmp_path / "events.db"
    FrontierStore(frontier_path).save(_frontier())

    latest = _seed_task_events(SqliteEventLog(eventlog_path))
    gappy = _GappyEventLog(SqliteEventLog(eventlog_path))

    with pytest.raises(BootContextError, match="reconstruction is incomplete"):
        assemble_boot_context(
            BootSources(
                frontier_store=FrontierStore(frontier_path),
                event_log=gappy,
                projections={"scorecard": ScorecardProjection()},
            )
        )

    # The cursor must NOT be advanced to/past the head on an incomplete replay.
    persisted = SqliteEventLog(eventlog_path).get_projection_cursor("scorecard")
    assert persisted.sequence < latest
