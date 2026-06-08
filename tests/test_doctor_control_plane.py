"""Control-plane health checks for ``forge-loop doctor`` (issue #202).

These exercise the four durable control-plane probes added to ``doctor``:
``projection_lag``, ``stale_leases``, ``memory_integrity`` and
``replay_determinism`` — at the unit level (``collect_control_plane_doctor``),
the integration level (``forge-loop doctor --json``) and adversarially (the
hard-reset simulation, a missing ``.forge`` directory, and the must-not-mutate
invariant).
"""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge_loop import cli
from forge_loop._testing.mutation_checker import FakeMutationChecker
from forge_loop.control.doctor import (
    COMPACT_REMEDIATION,
    DEFAULT_MUTATION_MODULE,
    FAIL,
    PASS,
    PRUNABLE_MEMORY_WARN_THRESHOLD,
    WARN,
    _memory_integrity_check,
    _replay_determinism_check,
    collect_control_plane_doctor,
    mutation_survivors_check,
)
from forge_loop.control.status import collect_control_plane_status
from forge_loop.eventlog import EventKind, ProjectionCursor, SqliteEventLog
from forge_loop.memory import (
    REJECTED_PATH_TAG,
    MemoryItem,
    MemoryKind,
    MemoryProvenance,
    SqliteMemoryStore,
)
from forge_loop.tasks import SqliteTaskSagaStore, TaskSaga, TaskState

_FIXED_NOW = datetime(2026, 6, 4, 12, 0, 0, tzinfo=UTC)


def _checkpoint_and_close(events_db: Path) -> None:
    """Fold pending WAL frames into the main db so its bytes are stable."""
    import gc
    import sqlite3

    gc.collect()  # drop any lingering SqliteEventLog connection from seeding
    connection = sqlite3.connect(events_db)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.commit()
    finally:
        connection.close()


def _read_cursors_readonly(events_db: Path) -> dict[str, int]:
    """Read projection cursors via a read-only connection (no WAL writes)."""
    import sqlite3

    connection = sqlite3.connect(f"file:{events_db}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT projection_name, sequence FROM projection_cursors"
        ).fetchall()
    finally:
        connection.close()
    return {str(name): int(sequence) for name, sequence in rows}


def _cfg(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        repo=tmp_path,
        pid_file=tmp_path / "docs" / "ops" / "loop-runner.pid",
        state_dir=tmp_path / "docs" / "ops",
        stop_file=tmp_path / "docs" / "ops" / "loop-runner.stop",
        state_file=tmp_path / "docs" / "ops" / "loop-runner.json",
        events_file=tmp_path / "docs" / "ops" / "loop-runner-events.jsonl",
        github_repo=None,
        labels=SimpleNamespace(ready="loop:ready"),
    )


def _seed_event_log(forge_dir: Path, *, cursor_sequence: int | None) -> SqliteEventLog:
    forge_dir.mkdir(parents=True, exist_ok=True)
    event_log = SqliteEventLog(forge_dir / "events.db")
    event_log.append(EventKind.FRONTIER_ADVANCED, {"frontier": "durable"})
    event_log.append(EventKind.MEMORY_PROMOTED, {"memory_id": "m1"})
    if cursor_sequence is not None:
        event_log.set_projection_cursor("frontier", ProjectionCursor(sequence=cursor_sequence))
    return event_log


def _seed_memory(forge_dir: Path) -> None:
    store = SqliteMemoryStore(forge_dir / "memory.db")
    store.put(
        MemoryItem(
            memory_id="m1",
            kind=MemoryKind.SEMANTIC,
            title="active decision",
            body="body",
            tags=("boot-context",),
            provenance=MemoryProvenance(
                source_event=None, authored_by="test", source_task_ref="task:#202"
            ),
        )
    )
    store.put(
        MemoryItem(
            memory_id="m2",
            kind=MemoryKind.SEMANTIC,
            title="rejected path",
            body="body",
            tags=(REJECTED_PATH_TAG,),
            provenance=MemoryProvenance(
                source_event=None, authored_by="test", source_task_ref="task:#202"
            ),
        )
    )
    store.put(
        MemoryItem(
            memory_id="m3",
            kind=MemoryKind.EPISODIC,
            title="an episode",
            body="body",
            tags=(),
            provenance=MemoryProvenance(
                source_event=None, authored_by="test", source_task_ref="task:#202"
            ),
        )
    )


def _episodic_is_prunable(item: MemoryItem) -> bool:
    """Stand-in for #427's ``is_load_bearing_memory``: episodes are NOT load-bearing.

    #427 is not merged when this test was authored (issue #429 consumes that
    predicate, it does not define it), so the suite injects this fake to exercise
    the classification branch. The fake encodes the epic-#426 intent: episodic
    items are the prunable backlog; decisions/rejected-paths/skills are load-bearing.
    """
    return item.kind is not MemoryKind.EPISODIC


def _seed_memory_backlog(forge_dir: Path, *, prunable: int, load_bearing: int = 2) -> Path:
    """Seed a store with load-bearing decisions + ``prunable`` episodic items.

    Returns the ``memory.db`` path. A rejected-path + ``load_bearing`` semantic
    decisions are load-bearing under :func:`_episodic_is_prunable`; the
    ``prunable`` episodes are the backlog the probe must surface.
    """
    forge_dir.mkdir(parents=True, exist_ok=True)
    path = forge_dir / "memory.db"
    store = SqliteMemoryStore(path)
    # The store commits (and fsyncs) once per ``put``; on slow CI disks seeding
    # an above-threshold backlog row-by-row costs ~1s/row. These are throwaway
    # test dbs, so drop the durability fsync to keep seeding fast.
    store._connection.execute("PRAGMA synchronous=OFF")
    prov = MemoryProvenance(source_event=None, authored_by="test", source_task_ref="task:#429")
    store.put(
        MemoryItem(
            memory_id="rej",
            kind=MemoryKind.SEMANTIC,
            title="rejected path",
            body="b",
            tags=(REJECTED_PATH_TAG,),
            provenance=prov,
        )
    )
    for i in range(load_bearing):
        store.put(
            MemoryItem(
                memory_id=f"dec-{i}",
                kind=MemoryKind.SEMANTIC,
                title=f"decision {i}",
                body="b",
                provenance=prov,
            )
        )
    for i in range(prunable):
        store.put(
            MemoryItem(
                memory_id=f"ep-{i}",
                kind=MemoryKind.EPISODIC,
                title=f"episode {i}",
                body="b",
                provenance=prov,
            )
        )
    return path


def _seed_sessions(
    state_dir: Path,
    *,
    lease_expires_at: datetime,
) -> None:
    """Seed one RUNNING saga with the given lease in the canonical store.

    Issue #373: doctor/status read task health from ``.forge/tasks.db`` (the
    canonical ``SqliteTaskSagaStore``), not the legacy ``worker-sessions.db``.
    ``state_dir`` is ``<repo>/docs/ops`` in every caller, so the repo root —
    and thus the canonical saga path — is ``state_dir.parent.parent``.
    """
    repo = state_dir.parent.parent
    store = SqliteTaskSagaStore(repo / ".forge" / "tasks.db")
    store.put(
        TaskSaga(
            task_id="task-1",
            saga_id="saga-task-1",
            state=TaskState.DISPATCHED,
            issue=1,
            branch="loop/1",
            worktree="/tmp/task-1",
        )
    )
    # ``acquire_lease`` requires the expiry to be strictly after acquisition, so
    # anchor acquisition just before the lease instant (works for past leases).
    store.acquire_lease(
        "task-1",
        owner_id="worker-task-1",
        acquired_at=lease_expires_at - timedelta(minutes=1),
        expires_at=lease_expires_at,
    )
    store.close()


def _seeded_repo(
    tmp_path: Path,
    *,
    cursor_sequence: int | None = 2,
    lease_expires_at: datetime | None = None,
) -> Path:
    forge_dir = tmp_path / ".forge"
    _seed_event_log(forge_dir, cursor_sequence=cursor_sequence)
    _seed_memory(forge_dir)
    # TIME-BOMB TRAP (#251): the default seeded lease MUST be relative to the
    # real wall-clock (``datetime.now(UTC)``), NOT to the fixed test clock
    # ``_FIXED_NOW``. Unit tests inject ``_FIXED_NOW`` into
    # ``collect_control_plane_doctor`` so any future lease is "live" for them.
    # But the CLI path (``cli._cmd_doctor``) hard-codes ``datetime.now(UTC)``
    # (cli_operator_commands.py ~L245). A fixed-clock lease (``_FIXED_NOW + 5m``
    # = 2026-06-04 12:05 UTC) pairs a *real-clock* CLI probe with a *frozen*
    # fixture: once real time passes that instant the seeded lease reads as
    # expired, ``stale_leases`` correctly FAILs, and the "healthy" integration
    # tests flake by calendar date. Fixed-clock fixture lease + real-clock CLI
    # path = date-dependent failure. Future seeded leases for any CLI-path test
    # MUST be relative to ``datetime.now(UTC)``.
    default_lease = datetime.now(UTC) + timedelta(hours=1)
    _seed_sessions(
        tmp_path / "docs" / "ops",
        lease_expires_at=lease_expires_at or default_lease,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Unit — collect_control_plane_doctor
# ---------------------------------------------------------------------------


class TestControlPlaneDoctorShape:
    def test_block_has_all_four_checks_with_required_keys(self, tmp_path: Path) -> None:
        repo = _seeded_repo(tmp_path)
        checks = collect_control_plane_doctor(repo, _FIXED_NOW, state_dir=tmp_path / "docs" / "ops")

        assert set(checks) == {
            "projection_lag",
            "stale_leases",
            "memory_integrity",
            "replay_determinism",
        }
        for result in checks.values():
            assert set(result) == {"status", "detail", "remediation"}
            assert result["status"] in {PASS, FAIL, WARN}
            assert isinstance(result["detail"], str) and result["detail"]
            assert result["remediation"] is None or isinstance(result["remediation"], str)


class TestProjectionLag:
    def test_fails_and_emits_remediation_when_cursor_behind_head(self, tmp_path: Path) -> None:
        repo = _seeded_repo(tmp_path, cursor_sequence=1)  # head is 2 → lag 1
        checks = collect_control_plane_doctor(repo, _FIXED_NOW, state_dir=tmp_path / "docs" / "ops")

        lag = checks["projection_lag"]
        assert lag["status"] == FAIL
        assert "behind" in lag["detail"]
        assert lag["remediation"] is not None
        assert "boot" in lag["remediation"]

    def test_passes_at_lag_zero(self, tmp_path: Path) -> None:
        repo = _seeded_repo(tmp_path, cursor_sequence=2)  # cursor at head
        checks = collect_control_plane_doctor(repo, _FIXED_NOW, state_dir=tmp_path / "docs" / "ops")

        lag = checks["projection_lag"]
        assert lag["status"] == PASS
        assert lag["remediation"] is None


class TestStaleLeases:
    def test_fails_with_expired_lease_and_remediation_is_recover(self, tmp_path: Path) -> None:
        repo = _seeded_repo(tmp_path, lease_expires_at=_FIXED_NOW - timedelta(minutes=5))
        checks = collect_control_plane_doctor(repo, _FIXED_NOW, state_dir=tmp_path / "docs" / "ops")

        stale = checks["stale_leases"]
        assert stale["status"] == FAIL
        assert stale["remediation"] is not None
        assert stale["remediation"].startswith("forge-loop recover")

    def test_passes_with_live_lease(self, tmp_path: Path) -> None:
        repo = _seeded_repo(tmp_path, lease_expires_at=_FIXED_NOW + timedelta(minutes=5))
        checks = collect_control_plane_doctor(repo, _FIXED_NOW, state_dir=tmp_path / "docs" / "ops")

        stale = checks["stale_leases"]
        assert stale["status"] == PASS
        assert stale["remediation"] is None


class TestMemoryIntegrity:
    def test_reports_counts_on_populated_store(self, tmp_path: Path) -> None:
        repo = _seeded_repo(tmp_path)
        checks = collect_control_plane_doctor(repo, _FIXED_NOW, state_dir=tmp_path / "docs" / "ops")

        mem = checks["memory_integrity"]
        assert mem["status"] == PASS
        assert "decisions/active=" in mem["detail"]
        assert "rejected_paths=1" in mem["detail"]
        assert "episodes=1" in mem["detail"]

    def test_prunable_backlog_reported_below_threshold_passes(self, tmp_path: Path) -> None:
        # Sub-threshold backlog: the new prunable field appears in the detail but
        # the probe stays PASS with no remediation.
        memory_path = _seed_memory_backlog(tmp_path / ".forge", prunable=3)
        status = {"memory": {"available": True}}
        result = _memory_integrity_check(
            status, memory_path, is_load_bearing=_episodic_is_prunable
        )
        assert result["status"] == PASS
        assert result["remediation"] is None
        assert "prunable=3" in result["detail"]

    def test_prunable_backlog_above_threshold_warns(self, tmp_path: Path) -> None:
        # Above-threshold backlog → WARN naming the count + compaction, remediation
        # is the compaction constant.
        count = PRUNABLE_MEMORY_WARN_THRESHOLD + 5
        memory_path = _seed_memory_backlog(tmp_path / ".forge", prunable=count)
        status = {"memory": {"available": True}}
        result = _memory_integrity_check(
            status, memory_path, is_load_bearing=_episodic_is_prunable
        )
        assert result["status"] == WARN
        assert f"prunable={count}" in result["detail"]
        assert "compact" in result["detail"].lower()
        assert result["remediation"] == COMPACT_REMEDIATION
        assert result["remediation"] is not None

    def test_prunable_threshold_boundary_uses_strict_greater_than(self, tmp_path: Path) -> None:
        # Exactly-at-threshold stays PASS (proves ``>`` not ``>=``); threshold+1 WARNs.
        status = {"memory": {"available": True}}

        at = _seed_memory_backlog(
            tmp_path / "at" / ".forge", prunable=PRUNABLE_MEMORY_WARN_THRESHOLD
        )
        at_result = _memory_integrity_check(status, at, is_load_bearing=_episodic_is_prunable)
        assert at_result["status"] == PASS
        assert at_result["remediation"] is None

        over = _seed_memory_backlog(
            tmp_path / "over" / ".forge", prunable=PRUNABLE_MEMORY_WARN_THRESHOLD + 1
        )
        over_result = _memory_integrity_check(status, over, is_load_bearing=_episodic_is_prunable)
        assert over_result["status"] == WARN

    def test_warn_detail_preserves_existing_breakdown_fields(self, tmp_path: Path) -> None:
        # The new field is additive: the WARN detail still carries every existing
        # field unrenamed and unreordered.
        memory_path = _seed_memory_backlog(
            tmp_path / ".forge", prunable=PRUNABLE_MEMORY_WARN_THRESHOLD + 2
        )
        status = {"memory": {"available": True}}
        detail = _memory_integrity_check(
            status, memory_path, is_load_bearing=_episodic_is_prunable
        )["detail"]
        assert "decisions/active=" in detail
        assert "rejected_paths=" in detail
        assert "episodes=" in detail
        assert "skills/procedural=" in detail

    def test_unavailable_predicate_degrades_to_pass(self, tmp_path: Path) -> None:
        # #427 predicate not wired (the real path until it merges): report the
        # backlog as unmeasured and stay PASS — never a WARN we cannot back.
        memory_path = _seed_memory_backlog(
            tmp_path / ".forge", prunable=PRUNABLE_MEMORY_WARN_THRESHOLD + 10
        )
        status = {"memory": {"available": True}}
        result = _memory_integrity_check(status, memory_path, is_load_bearing=None)
        assert result["status"] == PASS
        assert result["remediation"] is None
        assert "prunable=unmeasured" in result["detail"]

    def test_prunable_logic_does_not_mask_corrupt_store_fail(self, tmp_path: Path) -> None:
        # Even with a predicate wired, a present-but-unopenable store must FAIL —
        # the new prunable logic runs only on the clean-open path.
        forge_dir = tmp_path / ".forge"
        forge_dir.mkdir(parents=True, exist_ok=True)
        memory_path = forge_dir / "memory.db"
        memory_path.write_bytes(b"this is not a sqlite database at all")
        status = {"memory": {"available": False, "error": "file is not a database"}}
        result = _memory_integrity_check(
            status, memory_path, is_load_bearing=_episodic_is_prunable
        )
        assert result["status"] == FAIL
        assert result["remediation"] is not None

    def test_fails_cleanly_on_corrupt_db(self, tmp_path: Path) -> None:
        # Seed the event log + sessions but make memory.db a present-but-corrupt
        # (non-SQLite) file. Crucially we do NOT seed a real memory store first
        # — a leftover WAL sidecar would otherwise let SQLite recover the file.
        forge_dir = tmp_path / ".forge"
        _seed_event_log(forge_dir, cursor_sequence=2)
        _seed_sessions(tmp_path / "docs" / "ops", lease_expires_at=_FIXED_NOW + timedelta(hours=1))
        (forge_dir / "memory.db").write_bytes(b"this is not a sqlite database at all")

        checks = collect_control_plane_doctor(
            tmp_path, _FIXED_NOW, state_dir=tmp_path / "docs" / "ops"
        )

        mem = checks["memory_integrity"]
        assert mem["status"] == FAIL
        assert mem["remediation"] is not None


@dataclass
class _DeterministicProbe:
    """Replay-order-independent probe: ``state()`` folds set into sorted list."""

    cursor: ProjectionCursor = field(default_factory=ProjectionCursor)
    seen: set[int] = field(default_factory=set)

    def apply(self, event: Any) -> None:
        self.cursor = ProjectionCursor(sequence=event.sequence)
        self.seen.add(event.sequence)

    def state(self) -> dict[str, Any]:
        # Sorted ⇒ canonical regardless of set iteration order.
        return {"sequences": sorted(self.seen)}


# A shared monotonic "clock": each ``state()`` call reads a new value, so two
# replays of the SAME log produce DIFFERENT serialisations — standing in for a
# wall-clock / set-iteration / dict-ordering non-determinism, but deterministic
# for the test (it always diverges, never flakes).
_PROBE_CLOCK = itertools.count()


@dataclass
class _NonDeterministicProbe:
    """Order/clock-dependent probe: ``state()`` embeds a fresh clock tick."""

    cursor: ProjectionCursor = field(default_factory=ProjectionCursor)

    def apply(self, event: Any) -> None:
        self.cursor = ProjectionCursor(sequence=event.sequence)

    def state(self) -> dict[str, Any]:
        return {"stamp": next(_PROBE_CLOCK)}


class TestReplayDeterminism:
    def test_deterministic_projection_state_passes(self, tmp_path: Path) -> None:
        # A projection whose state() is replay-order-independent yields
        # byte-identical canonical JSON across two replays → PASS.
        repo = _seeded_repo(tmp_path, cursor_sequence=2)
        status = collect_control_plane_status(repo, _FIXED_NOW)
        result = _replay_determinism_check(
            status,
            repo / ".forge" / "events.db",
            projection_factory=_DeterministicProbe,
        )
        assert result["status"] == PASS
        assert result["remediation"] is None

    def test_nondeterministic_projection_state_fails(self, tmp_path: Path) -> None:
        # Adversarial / sad path: two replays of the same log produce divergent
        # serialised state → FAIL with a state-divergence detail + remediation.
        repo = _seeded_repo(tmp_path, cursor_sequence=2)
        status = collect_control_plane_status(repo, _FIXED_NOW)
        result = _replay_determinism_check(
            status,
            repo / ".forge" / "events.db",
            projection_factory=_NonDeterministicProbe,
        )
        assert result["status"] == FAIL
        assert result["remediation"] is not None
        assert "divergent projection state" in result["detail"]

    def test_passes_when_reprojection_matches_cursor(self, tmp_path: Path) -> None:
        repo = _seeded_repo(tmp_path, cursor_sequence=2)
        checks = collect_control_plane_doctor(repo, _FIXED_NOW, state_dir=tmp_path / "docs" / "ops")

        replay = checks["replay_determinism"]
        assert replay["status"] == PASS
        assert replay["remediation"] is None

    def test_fails_on_divergent_cursor_ahead_of_head(self, tmp_path: Path) -> None:
        # A cursor *ahead* of the event-log head cannot be reproduced by a
        # clean replay; lag (which floors at 0) cannot see this — only the
        # determinism probe catches it.
        repo = _seeded_repo(tmp_path, cursor_sequence=99)
        checks = collect_control_plane_doctor(repo, _FIXED_NOW, state_dir=tmp_path / "docs" / "ops")

        replay = checks["replay_determinism"]
        assert replay["status"] == FAIL
        assert "diverged" in replay["detail"]
        assert replay["remediation"] is not None


class TestMissingForgeStores:
    def test_all_checks_degrade_to_warn_when_forge_absent(self, tmp_path: Path) -> None:
        # No .forge directory at all — every probe must warn, never crash.
        checks = collect_control_plane_doctor(
            tmp_path, _FIXED_NOW, state_dir=tmp_path / "docs" / "ops"
        )

        assert {result["status"] for result in checks.values()} == {WARN}
        for result in checks.values():
            assert result["remediation"] is None


# ---------------------------------------------------------------------------
# Integration / E2E — forge-loop doctor --json
# ---------------------------------------------------------------------------


def _run_doctor_json(monkeypatch: Any, cfg: SimpleNamespace, capsys: Any) -> tuple[int, dict]:
    monkeypatch.setattr(cli, "load", lambda: cfg)
    rc = cli._cmd_doctor(SimpleNamespace(json=True))
    return rc, json.loads(capsys.readouterr().out)


class TestMutationSurvivors:
    """Unit coverage for the ``mutation_survivors`` probe (issue #380)."""

    def test_pinned_module_passes_with_zero_count(self) -> None:
        check = mutation_survivors_check(FakeMutationChecker.pinned())
        assert check["status"] == PASS
        assert check["count"] == 0
        assert check["module"] == DEFAULT_MUTATION_MODULE
        assert check["remediation"] is None

    def test_named_module_is_carried_through(self) -> None:
        check = mutation_survivors_check(FakeMutationChecker.pinned("forge_loop.frontier.store"))
        assert check["module"] == "forge_loop.frontier.store"
        assert "forge_loop.frontier.store" in check["detail"]

    def test_survivor_fails_with_positive_count_and_remediation(self) -> None:
        check = mutation_survivors_check(FakeMutationChecker.with_survivors(3))
        assert check["status"] == FAIL
        assert check["count"] == 3
        assert check["module"] == DEFAULT_MUTATION_MODULE
        assert check["remediation"]
        assert "3" in check["detail"]

    def test_unavailable_checker_degrades_to_warn(self) -> None:
        # No #379 checker wired → warn with a null count, never a crash.
        check = mutation_survivors_check(None)
        assert check["status"] == WARN
        assert check["count"] is None
        assert check["module"] == DEFAULT_MUTATION_MODULE
        assert check["remediation"]

    def test_checker_that_raises_degrades_to_warn(self) -> None:
        # Adversarial: the scoped check blows up (e.g. subprocess error). The
        # probe must degrade to warn, not propagate and crash doctor.
        checker = FakeMutationChecker(raises=RuntimeError("mutmut exploded"))
        check = mutation_survivors_check(checker)
        assert check["status"] == WARN
        assert check["count"] is None
        assert "exploded" in check["detail"]


class TestDoctorJsonIntegration:
    def test_json_emits_control_plane_object_and_zero_exit_when_healthy(
        self, monkeypatch: Any, tmp_path: Path, capsys: Any
    ) -> None:
        # Healthy: cursor at head, live lease. (tmux/git/orphan checks warn at
        # most; only control-plane fails drive a non-zero control-plane exit.)
        #
        # ``stale_leases`` guards in-flight sagas whose worker lease has lapsed
        # — i.e. dead-worker candidates, remediated by ``forge-loop recover``.
        # It runs over the *real* wall-clock via the CLI path, so the seeded
        # lease must be real-clock-relative to stay live (see #251 trap note in
        # ``_seeded_repo``).
        #
        # Seed the lease EXPLICITLY relative to the real clock the CLI uses and
        # assert it is strictly in the future at setup time. This guards against
        # silent date-rot: the test stays green regardless of the calendar date
        # the suite runs on (today is well past ``_FIXED_NOW``).
        live_lease = datetime.now(UTC) + timedelta(hours=1)
        assert live_lease > datetime.now(UTC), "seeded lease must be live at probe time"
        repo = _seeded_repo(tmp_path, cursor_sequence=2, lease_expires_at=live_lease)
        cfg = _cfg(repo)

        rc, blob = _run_doctor_json(monkeypatch, cfg, capsys)

        assert "control_plane" in blob
        control = blob["control_plane"]
        assert set(control) == {
            "projection_lag",
            "stale_leases",
            "memory_integrity",
            "replay_determinism",
            "mutation_survivors",
        }
        # The mutation-survivor probe degrades to ``warn`` (no real #379 checker
        # wired) and names the configured module with a null count.
        assert control["mutation_survivors"]["status"] == WARN
        assert control["mutation_survivors"]["module"]
        assert control["mutation_survivors"]["count"] is None
        for name, result in control.items():
            if name == "mutation_survivors":
                continue
            assert result["status"] != FAIL
        # No control-plane failure → control plane does not force exit 1.
        # (rc may still be 0 here since git/tmux/orphans only warn.)
        assert rc == 0

    def test_hard_reset_simulation_exits_one_with_failing_checks(
        self, monkeypatch: Any, tmp_path: Path, capsys: Any
    ) -> None:
        # In-flight saga + expired lease + cursor behind head.
        repo = _seeded_repo(
            tmp_path,
            cursor_sequence=1,
            lease_expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        cfg = _cfg(repo)

        rc, blob = _run_doctor_json(monkeypatch, cfg, capsys)

        control = blob["control_plane"]
        assert control["stale_leases"]["status"] == FAIL
        assert control["projection_lag"]["status"] == FAIL
        assert control["stale_leases"]["remediation"]
        assert control["projection_lag"]["remediation"]
        assert blob["ok"] is False
        assert rc == 1

    def test_json_memory_integrity_warns_on_above_threshold_prunable_backlog(
        self, monkeypatch: Any, tmp_path: Path, capsys: Any
    ) -> None:
        # End-to-end over the real CLI path: with the #427 predicate resolvable
        # (injected here as it is not yet merged), an above-threshold prunable
        # backlog drives ``memory_integrity`` to WARN with a non-null remediation.
        # A WARN does not flip the control-plane exit, so rc stays 0.
        live_lease = datetime.now(UTC) + timedelta(hours=1)
        repo = _seeded_repo(tmp_path, cursor_sequence=2, lease_expires_at=live_lease)
        _seed_memory_backlog(repo / ".forge", prunable=PRUNABLE_MEMORY_WARN_THRESHOLD + 1)
        cfg = _cfg(repo)

        import forge_loop.memory as memory_module

        monkeypatch.setattr(
            memory_module, "is_load_bearing_memory", _episodic_is_prunable, raising=False
        )

        rc, blob = _run_doctor_json(monkeypatch, cfg, capsys)

        mem = blob["control_plane"]["memory_integrity"]
        assert mem["status"] == WARN
        assert mem["remediation"] is not None
        assert "prunable=" in mem["detail"]
        assert rc == 0

    def test_missing_forge_dir_runs_other_checks_and_warns_control_plane(
        self, monkeypatch: Any, tmp_path: Path, capsys: Any
    ) -> None:
        # No .forge — non-control checks run, control-plane warns, no traceback.
        cfg = _cfg(tmp_path)
        cfg.state_dir.mkdir(parents=True, exist_ok=True)

        rc, blob = _run_doctor_json(monkeypatch, cfg, capsys)

        control = blob["control_plane"]
        assert {r["status"] for r in control.values()} == {WARN}
        # Non-control checks still present (e.g. orphan-worktree / deploy-drift).
        assert any("orphan" in c["name"] or "drift" in c["name"] for c in blob["checks"])
        # No control-plane failure → exit reflects only real (non-CP) failures.
        assert rc == 0

    def test_doctor_does_not_mutate_event_log_or_cursors(
        self, monkeypatch: Any, tmp_path: Path, capsys: Any
    ) -> None:
        # Reaches the real-clock CLI path with the DEFAULT seeded lease. This is
        # safe (no latent time-bomb) only because ``_seeded_repo``'s default is
        # now real-clock-relative (``datetime.now(UTC) + 1h``); see #251.
        repo = _seeded_repo(tmp_path, cursor_sequence=2)
        cfg = _cfg(repo)
        events_db = repo / ".forge" / "events.db"

        # Collapse any pending WAL into the main db and drop lingering
        # connections so the fixture is byte-stable BEFORE the probe — otherwise
        # an unrelated checkpoint (not doctor) would change the bytes.
        _checkpoint_and_close(events_db)

        before_hash = hashlib.sha256(events_db.read_bytes()).hexdigest()
        before_cursors = _read_cursors_readonly(events_db)

        _run_doctor_json(monkeypatch, cfg, capsys)

        after_hash = hashlib.sha256(events_db.read_bytes()).hexdigest()
        after_cursors = _read_cursors_readonly(events_db)
        assert after_hash == before_hash
        assert after_cursors == before_cursors


class TestDoctorHumanTable:
    def test_human_table_has_control_plane_section_with_remediation(
        self, monkeypatch: Any, tmp_path: Path, capsys: Any
    ) -> None:
        repo = _seeded_repo(
            tmp_path,
            cursor_sequence=1,
            lease_expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        cfg = _cfg(repo)
        monkeypatch.setattr(cli, "load", lambda: cfg)
        # Render wide so Rich does not wrap the copy-pasteable remediation
        # command across a line boundary (Console honours $COLUMNS).
        monkeypatch.setenv("COLUMNS", "240")

        rc = cli._cmd_doctor(SimpleNamespace(json=False))
        out = capsys.readouterr().out

        assert "control-plane" in out
        assert "forge-loop recover" in out
        assert rc == 1

    def test_human_table_renders_prunable_count_and_compaction_remediation(
        self, monkeypatch: Any, tmp_path: Path, capsys: Any
    ) -> None:
        # The enriched ``memory_integrity`` detail (prunable count) and its
        # compaction remediation must render in the human table when the backlog
        # is over threshold and the #427 predicate is resolvable.
        live_lease = datetime.now(UTC) + timedelta(hours=1)
        repo = _seeded_repo(tmp_path, cursor_sequence=2, lease_expires_at=live_lease)
        _seed_memory_backlog(repo / ".forge", prunable=PRUNABLE_MEMORY_WARN_THRESHOLD + 1)
        cfg = _cfg(repo)

        import forge_loop.memory as memory_module

        monkeypatch.setattr(
            memory_module, "is_load_bearing_memory", _episodic_is_prunable, raising=False
        )
        monkeypatch.setattr(cli, "load", lambda: cfg)
        monkeypatch.setenv("COLUMNS", "240")

        rc = cli._cmd_doctor(SimpleNamespace(json=False))
        out = capsys.readouterr().out

        assert "prunable=" in out
        assert "compact prunable episodic memory" in out
        # memory_integrity WARN does not flip the exit; the seeded repo is
        # otherwise healthy → rc 0.
        assert rc == 0


@pytest.mark.parametrize("want_json", [True, False])
def test_doctor_never_crashes_when_config_load_fails(
    monkeypatch: Any, capsys: Any, want_json: bool
) -> None:
    def _boom() -> Any:
        raise RuntimeError("LOOP_GH_REPO unset")

    monkeypatch.setattr(cli, "load", _boom)
    rc = cli._cmd_doctor(SimpleNamespace(json=want_json))
    out = capsys.readouterr().out

    # Config load failure is red → rc 1, but control-plane probes must warn,
    # not raise.
    assert rc == 1
    if want_json:
        blob = json.loads(out)
        assert {r["status"] for r in blob["control_plane"].values()} == {WARN}
