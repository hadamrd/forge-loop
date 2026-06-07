"""Crash-mid-replay invariant: a projection applies each event EXACTLY once.

Issue #382. The exactly-once replay guarantee underwrites crash recovery:
when a forge-loop process is killed mid-replay and restarts, the projection
cursor must resume from its persisted position and re-apply each event exactly
once — never double-applying (corrupts aggregate state) and never skipping
(leaves projections stale).

This module simulates a *restart* the way no other suite does: it discards every
in-memory reference to the :class:`SqliteEventLog` (``del log``) and opens a
BRAND-NEW instance/connection against the same WAL file mid-replay. The
persisted cursor + idempotency-key dedup must then deliver exactly-once. This is
a PINNING test — it makes no production change. If an assertion fails it has
surfaced a real recovery bug; the fix belongs in a follow-up, not here.

Re-uses the counting-projection pattern from
``tests/test_control_restart_replay.py`` (``_FailingProjection``): a projection
that records ``applied_sequences`` and guards contiguity at apply time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from forge_loop.eventlog import (
    EventEnvelope,
    EventKind,
    ProjectionCursor,
    SqliteEventLog,
    replay_projection,
)

_PROJECTION = "crash-replay"


@dataclass
class _CountingProjection:
    """Counts applies and records the order events arrive, guarding contiguity.

    The ``expected``-sequence check makes a double-apply or an out-of-order
    delivery a hard ``AssertionError`` at apply time, so a single pass can never
    silently dedup or reorder underneath the test's own bookkeeping.
    """

    cursor: ProjectionCursor = ProjectionCursor()
    applied_sequences: tuple[int, ...] = ()

    def apply(self, event: EventEnvelope) -> None:
        expected = self.cursor.sequence + 1
        if event.sequence != expected:
            raise AssertionError(f"projection received event {event.sequence}, expected {expected}")
        self.cursor = ProjectionCursor(sequence=event.sequence)
        self.applied_sequences = (*self.applied_sequences, event.sequence)


def _append_range(log: SqliteEventLog, start: int, end: int) -> tuple[EventEnvelope, ...]:
    """Append events for the inclusive sequence range ``[start, end]``.

    Each event carries a stable ``idempotency_key`` (``e<i>``) so a duplicate
    re-append after a restart is recognised by the dedup path.
    """

    return tuple(
        log.append(EventKind.WORKER_OBSERVATION, {"i": i}, idempotency_key=f"e{i}")
        for i in range(start, end + 1)
    )


class TestCrashMidReplayExactlyOnce:
    def test_partial_replay_then_restart_applies_each_event_once(self, tmp_path: Path) -> None:
        path = tmp_path / "events.db"
        n, k = 6, 3

        log = SqliteEventLog(path)
        _append_range(log, 1, k)  # only k events durable when the first pass runs
        first = _CountingProjection()
        replay_projection(log, _PROJECTION, first)
        assert first.applied_sequences == tuple(range(1, k + 1))
        assert log.get_projection_cursor(_PROJECTION).sequence == k

        _append_range(log, k + 1, n)  # tail lands while the "process" is alive
        first_seqs = first.applied_sequences

        # --- Simulate a crash/restart: drop every in-memory reference and open a
        # brand-new connection against the same WAL file. No state carries over.
        del log, first
        reopened = SqliteEventLog(path)
        second = _CountingProjection(cursor=reopened.get_projection_cursor(_PROJECTION))
        replay_projection(reopened, _PROJECTION, second)
        second_seqs = second.applied_sequences

        combined = first_seqs + second_seqs
        # Exactly-once: total applies == N, strictly contiguous 1..N, no gap/dup.
        assert len(combined) == n
        assert combined == tuple(range(1, n + 1))
        assert len(set(combined)) == len(combined)
        assert reopened.get_projection_cursor(_PROJECTION).sequence == n

    def test_restart_cursor_resumes_from_persisted_position(self, tmp_path: Path) -> None:
        path = tmp_path / "events.db"
        n, k = 6, 3

        log = SqliteEventLog(path)
        _append_range(log, 1, k)
        replay_projection(log, _PROJECTION, _CountingProjection())
        _append_range(log, k + 1, n)
        del log

        reopened = SqliteEventLog(path)
        assert reopened.get_projection_cursor(_PROJECTION).sequence == k
        second = _CountingProjection(cursor=reopened.get_projection_cursor(_PROJECTION))
        replay_projection(reopened, _PROJECTION, second)

        # The second pass sees ONLY the un-cursored tail, never a re-replay from 0.
        assert second.applied_sequences == tuple(range(k + 1, n + 1))
        assert all(sequence > k for sequence in second.applied_sequences)
        assert 1 not in second.applied_sequences

    def test_full_replay_then_restart_is_a_noop(self, tmp_path: Path) -> None:
        path = tmp_path / "events.db"
        n = 6

        log = SqliteEventLog(path)
        _append_range(log, 1, n)
        first = _CountingProjection()
        replay_projection(log, _PROJECTION, first)
        assert first.applied_sequences == tuple(range(1, n + 1))
        assert log.get_projection_cursor(_PROJECTION).sequence == n
        del log, first

        reopened = SqliteEventLog(path)
        second = _CountingProjection(cursor=reopened.get_projection_cursor(_PROJECTION))
        replay_projection(reopened, _PROJECTION, second)

        # Cursor already at head: a post-restart replay applies zero events.
        assert second.applied_sequences == ()
        assert reopened.get_projection_cursor(_PROJECTION).sequence == n

    def test_duplicate_idempotency_key_after_restart_does_not_double_apply(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "events.db"
        n = 6

        log = SqliteEventLog(path)
        appended = _append_range(log, 1, n)
        first = _CountingProjection()
        replay_projection(log, _PROJECTION, first)
        assert first.applied_sequences == tuple(range(1, n + 1))
        del log, first

        reopened = SqliteEventLog(path)
        # Re-append an event whose idempotency key was already used pre-restart.
        duplicate = reopened.append(EventKind.WORKER_OBSERVATION, {"i": 3}, idempotency_key="e3")
        # Dedup returns the EXISTING envelope: same sequence, no new row, no
        # inflation of the log tail.
        assert duplicate.sequence == appended[2].sequence
        assert reopened.latest_sequence() == n

        second = _CountingProjection(cursor=reopened.get_projection_cursor(_PROJECTION))
        replay_projection(reopened, _PROJECTION, second)
        # The duplicate produced no new sequence, so there is nothing to apply.
        assert second.applied_sequences == ()
        assert reopened.get_projection_cursor(_PROJECTION).sequence == n

    def test_crash_between_apply_and_cursor_persist_does_not_skip(self, tmp_path: Path) -> None:
        path = tmp_path / "events.db"
        n, k = 6, 3

        log = SqliteEventLog(path)
        _append_range(log, 1, n)

        # Pass 1: cleanly apply and PERSIST the cursor only up to k.
        clean = _CountingProjection()
        for event in log.since(0):
            if event.sequence > k:
                break
            clean.apply(event)
        log.advance_projection_cursor(_PROJECTION, clean.cursor)
        assert log.get_projection_cursor(_PROJECTION).sequence == k

        # Crash window: the tail is applied IN MEMORY but the cursor is NOT
        # persisted (the process dies before ``advance_projection_cursor``).
        crashed = _CountingProjection(cursor=clean.cursor)
        for event in log.since(k):
            crashed.apply(event)
        assert crashed.applied_sequences == tuple(range(k + 1, n + 1))
        assert log.get_projection_cursor(_PROJECTION).sequence == k  # never advanced
        del log, clean, crashed

        # Restart from the persisted cursor (still k): the un-cursored tail must
        # be re-applied in full — no event is silently skipped.
        reopened = SqliteEventLog(path)
        assert reopened.get_projection_cursor(_PROJECTION).sequence == k
        recovered = _CountingProjection(cursor=reopened.get_projection_cursor(_PROJECTION))
        replay_projection(reopened, _PROJECTION, recovered)
        assert recovered.applied_sequences == tuple(range(k + 1, n + 1))
        assert reopened.get_projection_cursor(_PROJECTION).sequence == n
