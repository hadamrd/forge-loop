"""Guarded SQLite compaction tests (issue #210).

Covers the integration path (mixed kinds → only noise pruned, load-bearing rows
survive in the live tier) and the headline adversarial path (a forced drop of a
``decision.made`` row is refused and the row survives).
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from forge_loop.eventlog import (
    EventKind,
    LoadBearingGuardError,
    ProjectionCursor,
    SqliteEventLog,
)


def _seed_mixed(log: SqliteEventLog) -> dict[str, int]:
    """Append a realistic mix and return {label: sequence}."""
    seqs = {
        "frontier": log.append(EventKind.FRONTIER_ADVANCED, {"goal": "g"}).sequence,
        "tick_start": log.append(EventKind.TICK_STARTED, {"tick": 1}).sequence,
        "decision": log.append(EventKind.DECISION_MADE, {"choice": "x"}).sequence,
        "heartbeat": log.append(EventKind.TASK_HEARTBEAT, {"issue": 1}).sequence,
        "superseded": log.append(EventKind.MEMORY_SUPERSEDED, {"memory_id": "m1"}).sequence,
        "observation": log.append(EventKind.WORKER_OBSERVATION, {"issue": 1}).sequence,
        # Terminal load-bearing event last → it is also the high-water mark.
        "completed": log.append(EventKind.TASK_COMPLETED, {"status": "merged"}).sequence,
    }
    return seqs


class TestGuardedCompaction:
    def test_compaction_keeps_load_bearing_removes_noise(self, tmp_path: Path) -> None:
        log = SqliteEventLog(tmp_path / "events.db")
        seqs = _seed_mixed(log)

        result = log.compact_noise(emit_marker=False)

        assert result.scanned == len(seqs)
        # 3 noise rows removed: tick_start, heartbeat, observation.
        assert result.pruned == 3
        assert result.preserved_load_bearing == 4  # frontier, decision, superseded, completed

        surviving = {e.sequence: e.kind for e in log.since(0)}
        # Load-bearing rows survive.
        assert seqs["frontier"] in surviving
        assert seqs["decision"] in surviving
        assert seqs["superseded"] in surviving
        assert seqs["completed"] in surviving
        # Noise rows are gone.
        assert seqs["tick_start"] not in surviving
        assert seqs["heartbeat"] not in surviving
        assert seqs["observation"] not in surviving

    def test_compaction_preserves_high_water_sequence(self, tmp_path: Path) -> None:
        log = SqliteEventLog(tmp_path / "events.db")
        _seed_mixed(log)
        # Append a trailing NOISE event so the high-water mark is itself noise.
        tail = log.append(EventKind.TICK_COMPLETED, {"tick": 1}).sequence
        assert log.latest_sequence() == tail

        result = log.compact_noise(emit_marker=False)

        # The high-water row is never pruned, so latest_sequence is stable.
        assert log.latest_sequence() == tail
        assert result.high_water_sequence == tail
        assert tail in {e.sequence for e in log.since(0)}

    def test_compaction_emits_marker_when_requested(self, tmp_path: Path) -> None:
        log = SqliteEventLog(tmp_path / "events.db")
        _seed_mixed(log)

        log.compact_noise(emit_marker=True)

        kinds = [e.kind for e in log.since(0)]
        assert EventKind.COMPACTION_PERFORMED in kinds

    def test_compaction_on_all_load_bearing_drops_nothing(self, tmp_path: Path) -> None:
        log = SqliteEventLog(tmp_path / "events.db")
        log.append(EventKind.DECISION_MADE, {"a": 1})
        log.append(EventKind.MEMORY_PROMOTED, {"memory_id": "m"})
        before = {e.sequence for e in log.since(0)}

        result = log.compact_noise(emit_marker=False)

        assert result.pruned == 0
        assert {e.sequence for e in log.since(0)} == before


class TestPruneGuardAdversarial:
    def test_force_drop_load_bearing_decision_is_refused(self, tmp_path: Path) -> None:
        # Headline negative test: an attempt to prune a load-bearing
        # decision.made must FAIL the guard and NOT delete the row.
        log = SqliteEventLog(tmp_path / "events.db")
        decision_seq = log.append(EventKind.DECISION_MADE, {"choice": "settled"}).sequence
        noise_seq = log.append(EventKind.TICK_STARTED, {"tick": 1}).sequence

        with pytest.raises(LoadBearingGuardError):
            log.prune([decision_seq])

        # The decision survives — nothing was deleted.
        survivors = {e.sequence: e.kind for e in log.since(0)}
        assert survivors[decision_seq] is EventKind.DECISION_MADE
        assert noise_seq in survivors

    def test_force_drop_refuses_whole_batch_when_one_is_load_bearing(
        self, tmp_path: Path
    ) -> None:
        log = SqliteEventLog(tmp_path / "events.db")
        noise_seq = log.append(EventKind.TICK_STARTED, {"tick": 1}).sequence
        decision_seq = log.append(EventKind.DECISION_MADE, {"choice": "x"}).sequence

        with pytest.raises(LoadBearingGuardError):
            log.prune([noise_seq, decision_seq])

        # All-or-nothing: the noise row is ALSO preserved because the batch
        # was refused outright.
        survivors = {e.sequence for e in log.since(0)}
        assert survivors == {noise_seq, decision_seq}

    def test_prune_of_pure_noise_succeeds(self, tmp_path: Path) -> None:
        log = SqliteEventLog(tmp_path / "events.db")
        n1 = log.append(EventKind.TICK_STARTED, {"tick": 1}).sequence
        keep = log.append(EventKind.DECISION_MADE, {"choice": "x"}).sequence
        n2 = log.append(EventKind.WORKER_OBSERVATION, {"issue": 1}).sequence

        deleted = log.prune([n1, n2])

        assert deleted == 2
        assert {e.sequence for e in log.since(0)} == {keep}

    def test_prune_empty_is_noop(self, tmp_path: Path) -> None:
        log = SqliteEventLog(tmp_path / "events.db")
        log.append(EventKind.DECISION_MADE, {"choice": "x"})
        assert log.prune([]) == 0


def _replay_kind_counts(log: SqliteEventLog, *, since: int) -> Counter[EventKind]:
    """A toy projection aggregate: count event kinds visible from ``since``.

    Mirrors how a real projection (e.g. the scorecard) folds the events it
    has not yet consumed. Used to prove a cursor's replay reconstructs the
    same aggregate before and after a compaction.
    """
    return Counter(e.kind for e in log.since(since))


class TestCompactionRespectsProjectionCursors:
    """Issue #323: compaction must not prune events above the slowest cursor.

    A projection whose cursor lags behind the noise being pruned would, on its
    next ``since(cursor)`` replay, silently skip the pruned events and corrupt
    its aggregate with no error raised. The prune floor protects everything
    strictly above the slowest registered cursor.
    """

    def test_noise_above_cursor_is_protected(self, tmp_path: Path) -> None:
        # Cursor at N, noise at sequences > N → none of those noise events
        # pruned; all remain visible via since(N).
        log = SqliteEventLog(tmp_path / "events.db")
        seqs = _seed_mixed(log)
        # Lagging cursor at the decision row (sequence 3); noise at 4 and 6 is
        # strictly above it and must be protected.
        log.set_projection_cursor("scorecard", ProjectionCursor(sequence=seqs["decision"]))

        result = log.compact_noise(emit_marker=False)

        surviving = {e.sequence for e in log.since(0)}
        # Noise above the cursor floor survives.
        assert seqs["heartbeat"] in surviving
        assert seqs["observation"] in surviving
        # Noise at-or-below the floor is still pruned.
        assert seqs["tick_start"] not in surviving
        # Only one noise row (tick_start at seq 2) was droppable.
        assert result.pruned == 1
        # All protected noise remains visible to a replay from the cursor.
        replayed = {e.sequence for e in log.since(seqs["decision"])}
        assert seqs["heartbeat"] in replayed
        assert seqs["observation"] in replayed

    def test_no_cursors_is_identical_to_today(self, tmp_path: Path) -> None:
        # Regression guard: with no registered cursors, prune count and the
        # surviving set must match the pre-#323 behaviour exactly.
        log = SqliteEventLog(tmp_path / "events.db")
        seqs = _seed_mixed(log)
        assert log.list_projection_cursors() == {}

        result = log.compact_noise(emit_marker=False)

        assert result.pruned == 3  # tick_start, heartbeat, observation
        assert result.preserved_load_bearing == 4
        surviving = {e.sequence for e in log.since(0)}
        assert surviving == {
            seqs["frontier"],
            seqs["decision"],
            seqs["superseded"],
            seqs["completed"],
        }

    def test_all_cursors_at_high_water_matches_no_cursor(self, tmp_path: Path) -> None:
        # All cursors at high-water ⇒ floor == high-water ⇒ nothing above it is
        # droppable except the tail (already preserved) ⇒ identical prune count.
        log = SqliteEventLog(tmp_path / "events.db")
        _seed_mixed(log)
        log.set_projection_cursor(
            "scorecard", ProjectionCursor(sequence=log.latest_sequence())
        )

        result = log.compact_noise(emit_marker=False)

        assert result.pruned == 3  # same as the no-cursor case

    def test_floor_is_minimum_of_multiple_cursors(self, tmp_path: Path) -> None:
        # Multiple cursors at different sequences → floor is the MINIMUM; noise
        # strictly above the slowest cursor is protected, noise at-or-below it
        # is still pruned.
        log = SqliteEventLog(tmp_path / "events.db")
        seqs = _seed_mixed(log)
        log.set_projection_cursor("fast", ProjectionCursor(sequence=seqs["completed"]))
        log.set_projection_cursor("slow", ProjectionCursor(sequence=seqs["decision"]))

        result = log.compact_noise(emit_marker=False)

        surviving = {e.sequence for e in log.since(0)}
        # Floor = slow cursor (decision, seq 3). Noise above survives.
        assert seqs["heartbeat"] in surviving
        assert seqs["observation"] in surviving
        # Noise at-or-below the floor is pruned.
        assert seqs["tick_start"] not in surviving
        assert result.pruned == 1

    def test_event_exactly_at_floor_is_pruneable(self, tmp_path: Path) -> None:
        # Boundary: protection is STRICTLY ABOVE the floor. A noise event whose
        # own sequence equals the floor has already been consumed by the cursor
        # (since() returns > floor), so it is pruneable.
        log = SqliteEventLog(tmp_path / "events.db")
        seqs = _seed_mixed(log)
        # Floor exactly at a noise row (heartbeat, seq 4).
        log.set_projection_cursor("scorecard", ProjectionCursor(sequence=seqs["heartbeat"]))

        result = log.compact_noise(emit_marker=False)

        surviving = {e.sequence for e in log.since(0)}
        # tick_start (2) and heartbeat (4) are at-or-below the floor → pruned.
        assert seqs["tick_start"] not in surviving
        assert seqs["heartbeat"] not in surviving
        # observation (6) is above the floor → protected.
        assert seqs["observation"] in surviving
        assert result.pruned == 2

    def test_reopen_replay_reconstructs_aggregate_with_lagging_cursor(
        self, tmp_path: Path
    ) -> None:
        # Integration / persistence: seed mixed kinds + a lagging cursor, compact
        # (no marker), reopen the SQLite file, and assert a replay from the cursor
        # reconstructs the SAME aggregate an uncompacted log would produce.
        db_path = tmp_path / "events.db"
        ref_path = tmp_path / "events_ref.db"

        log = SqliteEventLog(db_path)
        ref = SqliteEventLog(ref_path)
        for target in (log, ref):
            _seed_mixed(target)

        cursor = ProjectionCursor(sequence=2)  # lagging behind the noise above it
        expected = _replay_kind_counts(ref, since=cursor.sequence)

        log.set_projection_cursor("scorecard", cursor)
        log.compact_noise(emit_marker=False)
        del log  # drop the handle before reopening the file

        reopened = SqliteEventLog(db_path)
        reconstructed = _replay_kind_counts(reopened, since=cursor.sequence)

        assert reconstructed == expected

    def test_crash_restart_cursor_at_zero_protects_everything(
        self, tmp_path: Path
    ) -> None:
        # Adversarial / sad-path: a cursor deliberately left far behind head
        # (at 0) while a large run of noise accumulates above it. compact_noise
        # must drop NOTHING above the floor and the replay must reconstruct
        # identical state. Removing the floor logic makes this test go red.
        db_path = tmp_path / "events.db"
        ref_path = tmp_path / "events_ref.db"

        log = SqliteEventLog(db_path)
        ref = SqliteEventLog(ref_path)
        for target in (log, ref):
            target.append(EventKind.DECISION_MADE, {"choice": "anchor"})  # seq 1
            for tick in range(50):
                target.append(EventKind.TICK_STARTED, {"tick": tick})
                target.append(EventKind.WORKER_OBSERVATION, {"issue": tick})

        # Cursor stuck at the very beginning — simulates a crash before the
        # projection caught up.
        log.set_projection_cursor("scorecard", ProjectionCursor(sequence=0))
        expected = _replay_kind_counts(ref, since=0)

        result = log.compact_noise(emit_marker=False)

        # Floor is 0 ⇒ everything with sequence > 0 is protected ⇒ nothing pruned.
        assert result.pruned == 0
        assert {e.sequence for e in log.since(0)} == {e.sequence for e in ref.since(0)}

        del log
        reopened = SqliteEventLog(db_path)
        assert _replay_kind_counts(reopened, since=0) == expected
