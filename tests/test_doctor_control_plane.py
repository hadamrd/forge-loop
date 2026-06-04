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
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forge_loop import cli
from forge_loop.control.doctor import (
    FAIL,
    PASS,
    WARN,
    collect_control_plane_doctor,
)
from forge_loop.eventlog import EventKind, ProjectionCursor, SqliteEventLog
from forge_loop.memory import (
    REJECTED_PATH_TAG,
    MemoryItem,
    MemoryKind,
    MemoryProvenance,
    SqliteMemoryStore,
)
from forge_loop.worker_sessions import WorkerSessionStore
from forge_loop.worker_state import WorkerState

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


def _seed_sessions(
    state_dir: Path,
    *,
    lease_expires_at: datetime,
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    store = WorkerSessionStore(state_dir / "worker-sessions.db")
    running = store.create(issue=1, branch="loop/1")
    store.transition_to(running.session_id, WorkerState.RUNNING)
    store.set_lease_expires_at(running.session_id, lease_expires_at.isoformat())
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
    _seed_sessions(
        tmp_path / "docs" / "ops",
        lease_expires_at=lease_expires_at or (_FIXED_NOW + timedelta(minutes=5)),
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


class TestReplayDeterminism:
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


class TestDoctorJsonIntegration:
    def test_json_emits_control_plane_object_and_zero_exit_when_healthy(
        self, monkeypatch: Any, tmp_path: Path, capsys: Any
    ) -> None:
        # Healthy: cursor at head, live lease. (tmux/git/orphan checks warn at
        # most; only control-plane fails drive a non-zero control-plane exit.)
        repo = _seeded_repo(tmp_path, cursor_sequence=2)
        cfg = _cfg(repo)

        rc, blob = _run_doctor_json(monkeypatch, cfg, capsys)

        assert "control_plane" in blob
        control = blob["control_plane"]
        assert set(control) == {
            "projection_lag",
            "stale_leases",
            "memory_integrity",
            "replay_determinism",
        }
        for result in control.values():
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
