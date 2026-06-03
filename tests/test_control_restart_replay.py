from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from forge_loop.control.boot import BootSources, assemble_boot_context
from forge_loop.eventlog import EventEnvelope, EventKind, ProjectionCursor, SqliteEventLog
from forge_loop.eventlog.projections import ProjectionReplayError, replay_projection
from forge_loop.frontier import FrontierCursor, FrontierStore
from forge_loop.memory import (
    REJECTED_PATH_TAG,
    MemoryItem,
    MemoryKind,
    MemoryProvenance,
    SqliteMemoryStore,
)
from forge_loop.tasks import SqliteTaskSagaStore, TaskSaga, TaskState


@dataclass
class _ControlProjection:
    frontier_store: FrontierStore
    memory_store: SqliteMemoryStore
    task_store: SqliteTaskSagaStore
    cursor: ProjectionCursor = ProjectionCursor()
    applied_sequences: tuple[int, ...] = ()

    def apply(self, event: EventEnvelope) -> None:
        expected = self.cursor.sequence + 1
        if event.sequence != expected:
            raise AssertionError(f"projection received event {event.sequence}, expected {expected}")

        if event.kind is EventKind.FRONTIER_ADVANCED:
            self.frontier_store.save(
                FrontierCursor(
                    product_goal=event.payload["product_goal"],
                    current_problem=event.payload["current_problem"],
                    next_expansion=event.payload["next_expansion"],
                    why_now=event.payload["why_now"],
                    active_decisions=tuple(event.payload.get("active_decisions", ())),
                )
            )
        elif event.kind is EventKind.MEMORY_PROMOTED:
            self.memory_store.put(
                MemoryItem(
                    memory_id=event.payload["memory_id"],
                    kind=MemoryKind(event.payload["kind"]),
                    title=event.payload["title"],
                    body=event.payload["body"],
                    tags=tuple(event.payload.get("tags", ())),
                    provenance=MemoryProvenance(
                        source_event=event.ref,
                        authored_by="control-replay-test",
                        source_task_ref=event.task_id,
                        confidence=event.payload["confidence"],
                    ),
                )
            )
        elif event.kind is EventKind.TASK_DISPATCHED:
            self.task_store.put(
                TaskSaga(
                    task_id=event.task_id or event.payload["task_id"],
                    saga_id=event.saga_id or event.payload["saga_id"],
                    state=TaskState.RUNNING,
                    issue=event.payload["issue"],
                    branch=event.payload["branch"],
                    worktree=event.payload["worktree"],
                )
            )
        elif event.kind is EventKind.TASK_COMPLETED:
            task_id = event.task_id or event.payload["task_id"]
            existing = self.task_store.get(task_id)
            assert existing is not None
            self.task_store.put(
                replace(
                    existing,
                    state=TaskState.COMPLETED,
                    terminal_reason=event.payload["status"],
                )
            )
        elif event.kind in {EventKind.CRITIQUE_ISSUED, EventKind.WORKER_OBSERVATION}:
            pass

        self.cursor = ProjectionCursor(sequence=event.sequence)
        self.applied_sequences = (*self.applied_sequences, event.sequence)


def _append_interleaved_control_events(log: SqliteEventLog) -> tuple[EventEnvelope, ...]:
    first_frontier = log.append(
        EventKind.FRONTIER_ADVANCED,
        {
            "product_goal": "make restart replay trustworthy",
            "current_problem": "initial projection is incomplete",
            "next_expansion": "append adversarial replay fixtures",
            "why_now": "restart must not trust transcript memory",
            "active_decisions": ["project from durable events only"],
        },
        idempotency_key="frontier:first",
    )
    task = log.append(
        EventKind.TASK_DISPATCHED,
        {
            "task_id": "task-169-a",
            "saga_id": "saga-169-a",
            "issue": 169,
            "branch": "loop/169-test-control-adversarial-restart-replay",
            "worktree": "/tmp/wt-loop-169",
        },
        task_id="task-169-a",
        saga_id="saga-169-a",
        idempotency_key="task:169:dispatch",
    )
    duplicate_task = log.append(
        EventKind.TASK_DISPATCHED,
        {
            "task_id": "task-169-a",
            "saga_id": "saga-169-a",
            "issue": 169,
            "branch": "loop/169-test-control-adversarial-restart-replay",
            "worktree": "/tmp/wt-loop-169",
        },
        task_id="task-169-a",
        saga_id="saga-169-a",
        idempotency_key="task:169:dispatch",
    )
    memory = log.append(
        EventKind.MEMORY_PROMOTED,
        {
            "memory_id": "mem-169-rejected",
            "kind": MemoryKind.SEMANTIC.value,
            "title": "Rejected replay shortcut",
            "body": "Do not trust stored projection state without a zero replay check.",
            "tags": [REJECTED_PATH_TAG],
            "confidence": 0.95,
        },
        task_id="task-169-a",
        saga_id="saga-169-a",
        idempotency_key="memory:169:rejected-shortcut",
    )
    critic = log.append(
        EventKind.CRITIQUE_ISSUED,
        {"issue": 169, "verdict": "blocked", "reason": "cursor stale"},
        task_id="task-169-a",
        saga_id="saga-169-a",
        idempotency_key="critic:169:block",
    )
    worker = log.append(
        EventKind.WORKER_OBSERVATION,
        {"issue": 169, "worker_id": "worker-a", "status": "patching"},
        task_id="task-169-a",
        saga_id="saga-169-a",
        idempotency_key="worker:169:observation",
    )
    final_frontier = log.append(
        EventKind.FRONTIER_ADVANCED,
        {
            "product_goal": "make restart replay trustworthy",
            "current_problem": "restart replay lacks adversarial invariants",
            "next_expansion": "compare zero replay with boot context",
            "why_now": "semantic trust depends on durable replay",
            "active_decisions": ["stored cursors must match replayed tails"],
        },
        idempotency_key="frontier:final",
    )

    assert duplicate_task.sequence == task.sequence
    return (first_frontier, task, memory, critic, worker, final_frontier)


class TestControlRestartReplayInvariants:
    def test_zero_replay_after_reopen_matches_projection_cursors_and_boot_context(
        self,
        tmp_path: Path,
    ) -> None:
        eventlog_path = tmp_path / "events.db"
        frontier_path = tmp_path / "frontier.yaml"
        memory_path = tmp_path / "memory.db"
        task_path = tmp_path / "tasks.db"

        appended = _append_interleaved_control_events(SqliteEventLog(eventlog_path))
        reopened = SqliteEventLog(eventlog_path)
        projection = _ControlProjection(
            FrontierStore(frontier_path),
            SqliteMemoryStore(memory_path),
            SqliteTaskSagaStore(task_path),
        )

        replay_projection(reopened, "control-boot", projection)

        assert projection.applied_sequences == tuple(event.sequence for event in appended)
        assert reopened.get_projection_cursor("control-boot").sequence == appended[-1].sequence

        boot = assemble_boot_context(
            BootSources(
                frontier_store=FrontierStore(frontier_path),
                event_log=SqliteEventLog(eventlog_path),
                memory_store=SqliteMemoryStore(memory_path),
                task_store=SqliteTaskSagaStore(task_path),
            )
        )
        assert boot.latest_event_sequence == appended[-1].sequence
        assert boot.projection_cursors["control-boot"].sequence == appended[-1].sequence
        assert boot.projection_cursors["control-boot"].lag == 0
        assert boot.frontier.current_problem == "restart replay lacks adversarial invariants"
        assert boot.active_memory_ids == ("mem-169-rejected",)
        assert boot.rejected_path_memory_ids == ("mem-169-rejected",)
        assert boot.in_flight_task_ids == ("task-169-a",)
        assert boot.in_flight_saga_ids == ("saga-169-a",)

    def test_projection_cursor_cannot_advance_out_of_order_or_past_log_tail(
        self,
        tmp_path: Path,
    ) -> None:
        log = SqliteEventLog(tmp_path / "events.db")
        last = _append_interleaved_control_events(log)[-1]
        log.advance_projection_cursor("control-boot", ProjectionCursor(sequence=last.sequence))

        with pytest.raises(ProjectionReplayError, match="stale projection cursor"):
            log.advance_projection_cursor(
                "control-boot",
                ProjectionCursor(sequence=last.sequence - 1),
            )

        with pytest.raises(ProjectionReplayError, match="past latest event sequence"):
            log.advance_projection_cursor(
                "control-boot",
                ProjectionCursor(sequence=last.sequence + 1),
            )

        assert log.get_projection_cursor("control-boot").sequence == last.sequence
