"""Guarded SQLite compaction tests (issue #210).

Covers the integration path (mixed kinds → only noise pruned, load-bearing rows
survive in the live tier) and the headline adversarial path (a forced drop of a
``decision.made`` row is refused and the row survives).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_loop.eventlog import EventKind, LoadBearingGuardError, SqliteEventLog


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
