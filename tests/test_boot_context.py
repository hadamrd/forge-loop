from __future__ import annotations

from pathlib import Path

import pytest

from forge_loop.control.boot import BootContextError, BootSources, assemble_boot_context
from forge_loop.eventlog import EventKind, ProjectionCursor, SqliteEventLog
from forge_loop.frontier import FrontierCursor, FrontierStore
from forge_loop.memory import (
    REJECTED_PATH_TAG,
    MemoryItem,
    MemoryKind,
    MemoryProvenance,
    SqliteMemoryStore,
)
from forge_loop.tasks import SqliteTaskSagaStore, TaskSaga, TaskState


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
