from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from forge_loop.control.boot import (
    BootContext,
    BootContextError,
    BootSources,
    assemble_boot_context,
)
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


@dataclass
class _FailingProjection:
    cursor: ProjectionCursor
    fail_on_sequence: int
    applied_sequences: tuple[int, ...] = ()

    def apply(self, event: EventEnvelope) -> None:
        if event.sequence == self.fail_on_sequence:
            raise RuntimeError(f"projection boom at {event.sequence}")
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

    def test_projection_failure_does_not_advance_stored_cursor(
        self,
        tmp_path: Path,
    ) -> None:
        log = SqliteEventLog(tmp_path / "events.db")
        appended = _append_interleaved_control_events(log)
        original_cursor = ProjectionCursor(sequence=appended[0].sequence)
        log.advance_projection_cursor("control-boot", original_cursor)

        projection = _FailingProjection(
            cursor=original_cursor,
            fail_on_sequence=appended[2].sequence,
        )

        with pytest.raises(RuntimeError, match="projection boom"):
            replay_projection(log, "control-boot", projection)

        assert projection.applied_sequences == (appended[1].sequence,)
        assert log.get_projection_cursor("control-boot") == original_cursor


@dataclass
class _GapTolerantProjection:
    """Frontier+memory projection that tolerates pruned (non-contiguous) logs.

    Issue #210: after compaction removes noise rows, ``since()`` yields gaps.
    A genuine boot replay of the *surviving* (load-bearing) tail must still
    reach the same frontier/memory state.
    """

    frontier_store: FrontierStore
    memory_store: SqliteMemoryStore
    cursor: ProjectionCursor = ProjectionCursor()

    def apply(self, event: EventEnvelope) -> None:
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
                        authored_by="boot-equivalence-test",
                        source_task_ref=event.task_id,
                        confidence=event.payload["confidence"],
                    ),
                )
            )
        # Noise (tick/observation/heartbeat) is intentionally ignored.
        self.cursor = ProjectionCursor(sequence=event.sequence)


class TestCompactionBootEquivalence:
    """The headline invariant: compaction does not change boot reconstruction."""

    def _seed_log_with_noise(self, log: SqliteEventLog) -> None:
        log.append(
            EventKind.FRONTIER_ADVANCED,
            {
                "product_goal": "ship resumable loop",
                "current_problem": "early problem",
                "next_expansion": "x",
                "why_now": "y",
                "active_decisions": ["decide once"],
            },
            idempotency_key="f1",
        )
        log.append(EventKind.TICK_STARTED, {"tick": 1}, idempotency_key="t1")
        log.append(
            EventKind.MEMORY_PROMOTED,
            {
                "memory_id": "mem-keep",
                "kind": MemoryKind.SEMANTIC.value,
                "title": "keep me",
                "body": "load-bearing memory",
                "tags": [REJECTED_PATH_TAG],
                "confidence": 0.9,
            },
            idempotency_key="m1",
        )
        log.append(EventKind.WORKER_OBSERVATION, {"issue": 1}, idempotency_key="o1")
        log.append(EventKind.TASK_HEARTBEAT, {"issue": 1}, idempotency_key="h1")
        # Final event is load-bearing → also the high-water mark.
        log.append(
            EventKind.FRONTIER_ADVANCED,
            {
                "product_goal": "ship resumable loop",
                "current_problem": "final problem",
                "next_expansion": "close the learning loop",
                "why_now": "amnesia must be impossible",
                "active_decisions": ["project from durable events only"],
            },
            idempotency_key="f2",
        )

    def _boot(
        self, eventlog_path: Path, frontier_path: Path, memory_path: Path, task_path: Path
    ) -> object:
        return assemble_boot_context(
            BootSources(
                frontier_store=FrontierStore(frontier_path),
                event_log=SqliteEventLog(eventlog_path),
                memory_store=SqliteMemoryStore(memory_path),
                task_store=SqliteTaskSagaStore(task_path),
            )
        )

    def test_boot_context_identical_before_and_after_compaction(self, tmp_path: Path) -> None:
        eventlog_path = tmp_path / "events.db"
        frontier_path = tmp_path / "frontier.yaml"
        memory_path = tmp_path / "memory.db"
        task_path = tmp_path / "tasks.db"

        log = SqliteEventLog(eventlog_path)
        self._seed_log_with_noise(log)

        # Independent saga store, written directly as the dispatcher does.
        task_store = SqliteTaskSagaStore(task_path)
        task_store.put(
            TaskSaga(
                task_id="task-210",
                saga_id="saga-210",
                state=TaskState.RUNNING,
                issue=210,
                branch="loop/210",
                worktree="/tmp/wt-loop-210",
            )
        )

        # Derive frontier/memory by replaying the FULL log; record a cursor.
        replay_projection(
            SqliteEventLog(eventlog_path),
            "control-boot",
            _GapTolerantProjection(FrontierStore(frontier_path), SqliteMemoryStore(memory_path)),
        )

        boot_before = self._boot(eventlog_path, frontier_path, memory_path, task_path)
        summary_before = boot_before.summary()  # type: ignore[attr-defined]
        cursors_before = {
            name: (status.sequence, status.lag)
            for name, status in boot_before.projection_cursors.items()  # type: ignore[attr-defined]
        }

        # --- Force a compaction that drops the noise rows. ---
        result = SqliteEventLog(eventlog_path).compact_noise(emit_marker=False)
        assert result.pruned == 3  # tick_started, worker_observation, task_heartbeat

        # Re-derive frontier/memory from the PRUNED log into FRESH stores; the
        # surviving load-bearing tail must reconstruct identical state.
        frontier_after = tmp_path / "frontier_after.yaml"
        memory_after = tmp_path / "memory_after.db"
        replay_projection(
            SqliteEventLog(eventlog_path),
            "control-boot-after",
            _GapTolerantProjection(FrontierStore(frontier_after), SqliteMemoryStore(memory_after)),
        )

        boot_after = self._boot(eventlog_path, frontier_after, memory_after, task_path)
        summary_after = boot_after.summary()  # type: ignore[attr-defined]
        cursors_after = {
            name: (status.sequence, status.lag)
            for name, status in boot_after.projection_cursors.items()  # type: ignore[attr-defined]
        }

        # The boot summaries reference different projection-cursor NAMES only
        # because the test uses fresh stores; normalise that out and compare
        # the load-bearing reconstruction.
        assert boot_after.frontier == boot_before.frontier  # type: ignore[attr-defined]
        assert boot_after.active_memory_ids == boot_before.active_memory_ids  # type: ignore[attr-defined]
        assert boot_after.rejected_path_memory_ids == boot_before.rejected_path_memory_ids  # type: ignore[attr-defined]
        assert boot_after.in_flight_task_ids == boot_before.in_flight_task_ids  # type: ignore[attr-defined]
        assert boot_after.in_flight_saga_ids == boot_before.in_flight_saga_ids  # type: ignore[attr-defined]
        # High-water sequence is invariant across compaction.
        assert boot_after.latest_event_sequence == boot_before.latest_event_sequence  # type: ignore[attr-defined]
        # Both cursors are caught up (lag 0) at the same high-water sequence.
        assert {seq for seq, _ in cursors_before.values()} == {
            seq for seq, _ in cursors_after.values()
        }
        assert all(lag == 0 for _, lag in cursors_before.values())
        assert all(lag == 0 for _, lag in cursors_after.values())
        # Sanity: active memory genuinely survived the prune.
        assert boot_after.active_memory_ids == ("mem-keep",)  # type: ignore[attr-defined]
        # Frontier/memory boot lines are byte-identical pre/post.
        assert summary_before.splitlines()[0] == summary_after.splitlines()[0]


@dataclass
class _RejectingProjection:
    """Projection that rejects an out-of-order/unexpected event mid-tail.

    Mirrors a real projection guard: when it is handed an event it cannot
    safely apply it raises ``ProjectionReplayError`` BEFORE advancing, so the
    stored cursor must never move past the last cleanly-applied event.
    """

    cursor: ProjectionCursor
    reject_on_sequence: int
    applied_sequences: tuple[int, ...] = ()

    def apply(self, event: EventEnvelope) -> None:
        if event.sequence == self.reject_on_sequence:
            raise ProjectionReplayError(f"rejected event {event.sequence} out of order")
        self.cursor = ProjectionCursor(sequence=event.sequence)
        self.applied_sequences = (*self.applied_sequences, event.sequence)


def _comparable(ctx: BootContext) -> tuple[object, ...]:
    """The byte-for-byte-equivalence surface of a reconstructed boot context."""
    return (
        ctx.frontier,
        ctx.active_memory_ids,
        ctx.rejected_path_memory_ids,
        ctx.in_flight_task_ids,
        ctx.in_flight_saga_ids,
        ctx.stale_saga_ids,
        ctx.latest_event_sequence,
        dict(ctx.projection_cursors),
    )


class TestBootDrivesProjectionsToTail:
    """Boot itself closes projection lag — no manual pre-boot replay needed."""

    def _control_projection(self, tmp_path: Path, tag: str) -> _ControlProjection:
        return _ControlProjection(
            FrontierStore(tmp_path / f"{tag}-frontier.yaml"),
            SqliteMemoryStore(tmp_path / f"{tag}-memory.db"),
            SqliteTaskSagaStore(tmp_path / f"{tag}-tasks.db"),
        )

    def _boot(
        self,
        event_path: Path,
        projection: _ControlProjection,
    ) -> BootContext:
        return assemble_boot_context(
            BootSources(
                frontier_store=projection.frontier_store,
                event_log=SqliteEventLog(event_path),
                memory_store=projection.memory_store,
                task_store=projection.task_store,
                projections={"control-boot": projection},
            )
        )

    def test_partial_cursor_then_boot_equals_clean_full_replay(self, tmp_path: Path) -> None:
        # --- Scenario A: a projection crashed partway, leaving a lagging cursor.
        partial_path = tmp_path / "partial-events.db"
        appended = _append_interleaved_control_events(SqliteEventLog(partial_path))
        latest = appended[-1].sequence

        partial = self._control_projection(tmp_path, "a")
        events = list(SqliteEventLog(partial_path).since(0))
        for event in events[:3]:  # cleanly apply only sequences 1..3 to the stores
            partial.apply(event)
        SqliteEventLog(partial_path).advance_projection_cursor(
            "control-boot", ProjectionCursor(sequence=partial.cursor.sequence)
        )
        assert partial.cursor.sequence == 3 < latest

        # Hard-reopen: a FRESH projection over the SAME (state-at-3) stores.
        booted = self._boot(partial_path, self._control_projection(tmp_path, "a"))

        # --- Scenario B: a clean full replay from sequence 0 on separate stores.
        clean_path = tmp_path / "clean-events.db"
        _append_interleaved_control_events(SqliteEventLog(clean_path))
        clean = self._boot(clean_path, self._control_projection(tmp_path, "b"))

        # Boot drove the lagging cursor to the tail: zero lag, head reached.
        assert booted.projection_cursors["control-boot"].lag == 0
        assert booted.projection_cursors["control-boot"].sequence == latest
        # And the reconstructed context is byte-for-byte equal to a clean replay.
        assert _comparable(booted) == _comparable(clean)

    def test_projection_that_cannot_reach_tail_is_a_hard_boot_fault(self, tmp_path: Path) -> None:
        frontier_path = tmp_path / "frontier.yaml"
        FrontierStore(frontier_path).save(_frontier())
        event_path = tmp_path / "events.db"
        appended = _append_interleaved_control_events(SqliteEventLog(event_path))
        latest = appended[-1].sequence
        SqliteEventLog(event_path).advance_projection_cursor(
            "control-boot", ProjectionCursor(sequence=3)
        )

        rejecting = _RejectingProjection(
            cursor=ProjectionCursor(sequence=0),  # boot re-seeds this from the saved cursor
            reject_on_sequence=5,  # 4 applies cleanly, 5 is rejected mid-tail
        )

        with pytest.raises(BootContextError, match="could not be replayed"):
            assemble_boot_context(
                BootSources(
                    frontier_store=FrontierStore(frontier_path),
                    event_log=SqliteEventLog(event_path),
                    projections={"control-boot": rejecting},
                )
            )

        # All-or-nothing: the stored cursor never moved past its pre-boot value,
        # and boot raised rather than returning a context with residual lag.
        assert SqliteEventLog(event_path).get_projection_cursor("control-boot").sequence == 3
        assert latest == appended[-1].sequence


def _frontier() -> FrontierCursor:
    return FrontierCursor(
        product_goal="make restart replay trustworthy",
        current_problem="boot must drive lagging cursors to the tail",
        next_expansion="hard-fault when a projection cannot reach the head",
        why_now="stale projection state must never boot silently",
        active_decisions=("replay-to-tail is synchronous and all-or-nothing",),
    )
