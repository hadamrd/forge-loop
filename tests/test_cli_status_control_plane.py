from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from forge_loop import cli
from forge_loop.control.status import collect_control_plane_status
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
from forge_loop.worker_sessions import WorkerSessionStore
from forge_loop.worker_state import WorkerState


def _seed_running_saga(
    store: SqliteTaskSagaStore,
    task_id: str,
    *,
    issue: int,
    acquired_at: datetime,
    expires_at: datetime,
) -> None:
    """Put a DISPATCHED saga then drive it RUNNING via ``acquire_lease``.

    Mirrors the real dispatch path (``put`` then ``acquire_lease``), so the
    lease-expiry semantics under test are the production ones, not a hand-rolled
    RUNNING row.
    """

    store.put(
        TaskSaga(
            task_id=task_id,
            saga_id=f"saga-{task_id}",
            state=TaskState.DISPATCHED,
            issue=issue,
            branch=f"loop/{issue}",
            worktree=f"/tmp/{task_id}",
        )
    )
    store.acquire_lease(
        task_id,
        owner_id=f"worker-{task_id}",
        acquired_at=acquired_at,
        expires_at=expires_at,
    )


def _seed_legacy_worker_sessions(ops_dir: Path, now: datetime) -> None:
    """Seed the legacy ``worker-sessions.db`` (the store #373 stops reading)."""

    ops_dir.mkdir(parents=True, exist_ok=True)
    session_store = WorkerSessionStore(ops_dir / "worker-sessions.db")
    running = session_store.create(issue=1, branch="loop/1")
    session_store.transition_to(running.session_id, WorkerState.RUNNING)
    session_store.set_lease_expires_at(running.session_id, (now - timedelta(minutes=5)).isoformat())
    awaiting = session_store.create(issue=2, branch="loop/2")
    session_store.transition_to(awaiting.session_id, WorkerState.RUNNING)
    session_store.set_lease_expires_at(
        awaiting.session_id, (now + timedelta(minutes=5)).isoformat()
    )
    session_store.transition_to(awaiting.session_id, WorkerState.AWAITING_CRITIC)
    abandoned = session_store.create(issue=3, branch="loop/3")
    session_store.transition_to(abandoned.session_id, WorkerState.ABANDONED)
    session_store.close()


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


def _status_json(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: Any,
) -> dict[str, Any]:
    cfg = _cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "load", lambda: cfg)

    rc = cli._cmd_status(SimpleNamespace(json=True, axis=[]))

    assert rc == 0
    return json.loads(capsys.readouterr().out)


def _status_table(
    monkeypatch: Any,
    tmp_path: Path,
    capsys: Any,
) -> str:
    cfg = _cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "load", lambda: cfg)

    rc = cli._cmd_status(SimpleNamespace(json=False, axis=[]))

    assert rc == 0
    return capsys.readouterr().out


class TestStatusControlPlane:
    def test_status_json_reports_seeded_durable_stores(
        self,
        monkeypatch: Any,
        tmp_path: Path,
        capsys: Any,
    ) -> None:
        forge_dir = tmp_path / ".forge"
        forge_dir.mkdir()
        event_log = SqliteEventLog(forge_dir / "events.db")
        event_log.append(EventKind.FRONTIER_ADVANCED, {"frontier": "durable"})
        event_log.append(EventKind.MEMORY_PROMOTED, {"memory_id": "m1"})
        event_log.set_projection_cursor("frontier", ProjectionCursor(sequence=1))
        FrontierStore(forge_dir / "frontier.yaml").save(
            FrontierCursor(
                product_goal="recover after reset",
                current_problem="operators cannot inspect recovery state",
                next_expansion="surface control-plane status",
                why_now="status is the operator entrypoint",
            )
        )
        memory_store = SqliteMemoryStore(forge_dir / "memory.db")
        memory_store.put(
            MemoryItem(
                memory_id="m1",
                kind=MemoryKind.SEMANTIC,
                title="Status reads durable memory",
                body="The boot summary can name curated memory ids.",
                tags=("boot-context",),
                provenance=MemoryProvenance(
                    source_event=None,
                    authored_by="test",
                    source_task_ref="task:#171",
                ),
            )
        )
        memory_store.put(
            MemoryItem(
                memory_id="m2",
                kind=MemoryKind.SEMANTIC,
                title="Rejected path remains queryable",
                body="Rejected paths are counted separately for operators.",
                tags=(REJECTED_PATH_TAG,),
                provenance=MemoryProvenance(
                    source_event=None,
                    authored_by="test",
                    source_task_ref="task:#171",
                ),
            )
        )
        now = datetime.now(UTC)
        # Seed BOTH stores: the legacy worker-sessions.db (which #373 makes the
        # status path ignore) AND the canonical .forge/tasks.db. The tasks block
        # must reflect ONLY the canonical store.
        _seed_legacy_worker_sessions(tmp_path / "docs" / "ops", now)
        saga_store = SqliteTaskSagaStore(forge_dir / "tasks.db")
        _seed_running_saga(
            saga_store,
            "task-expired",
            issue=10,
            acquired_at=now - timedelta(minutes=10),
            expires_at=now - timedelta(seconds=1),
        )
        _seed_running_saga(
            saga_store,
            "task-fresh",
            issue=11,
            acquired_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        _seed_running_saga(
            saga_store,
            "task-done",
            issue=12,
            acquired_at=now - timedelta(minutes=3),
            expires_at=now + timedelta(minutes=5),
        )
        saga_store.mark_completed("task-done")
        saga_store.close()

        blob = _status_json(monkeypatch, tmp_path, capsys)

        control = blob["control_plane"]
        assert control["event_log"] == {
            "available": True,
            "path": str(forge_dir / "events.db"),
            "last_sequence": 2,
        }
        assert control["projections"]["frontier"] == {"sequence": 1, "lag": 1}
        assert control["frontier"] == {
            "available": True,
            "path": str(forge_dir / "frontier.yaml"),
            "current_problem": "operators cannot inspect recovery state",
            "next_expansion": "surface control-plane status",
        }
        assert control["memory"] == {
            "available": True,
            "path": str(forge_dir / "memory.db"),
            "active_count": 2,
            "rejected_count": 1,
        }
        assert control["tasks"] == {
            "available": True,
            "path": str(forge_dir / "tasks.db"),
            "in_flight_count": 2,
            "stale_lease_count": 1,
        }
        assert control["boot"]["available"] is True
        assert "memory: m1, m2" in control["boot"]["summary"]
        assert "in_flight:" in control["boot"]["summary"]
        # Boot summary names the canonical saga task ids, not legacy session ids.
        assert "task-expired" in control["boot"]["summary"]
        assert "task-fresh" in control["boot"]["summary"]

    def test_status_json_reports_missing_control_plane_stores_as_unavailable(
        self,
        monkeypatch: Any,
        tmp_path: Path,
        capsys: Any,
    ) -> None:
        blob = _status_json(monkeypatch, tmp_path, capsys)

        control = blob["control_plane"]
        assert control["event_log"] == {
            "available": False,
            "path": str(tmp_path / ".forge" / "events.db"),
            "last_sequence": None,
        }
        assert control["projections"] == {}
        assert control["frontier"] == {
            "available": False,
            "path": str(tmp_path / ".forge" / "frontier.yaml"),
            "current_problem": None,
            "next_expansion": None,
        }
        assert control["memory"] == {
            "available": False,
            "path": str(tmp_path / ".forge" / "memory.db"),
            "active_count": None,
            "rejected_count": None,
        }
        assert control["tasks"] == {
            "available": False,
            "path": str(tmp_path / ".forge" / "tasks.db"),
            "in_flight_count": None,
            "stale_lease_count": None,
        }
        assert control["boot"] == {"available": False, "summary": None}


class TestStatusOperationalEntropy:
    """Issue #402 — the four operational-entropy counts surface on CLI status."""

    def test_status_json_exposes_the_four_entropy_keys(
        self,
        monkeypatch: Any,
        tmp_path: Path,
        capsys: Any,
    ) -> None:
        blob = _status_json(monkeypatch, tmp_path, capsys)

        oe = blob["control_plane"]["operational_entropy"]
        assert set(oe) == {
            "open_branches",
            "live_worktrees",
            "open_epics",
            "backlog_age_days",
        }
        # tmp_path is not a git repo and no github_repo is configured, so every
        # source degrades to None — but the block is always present.
        assert oe["open_epics"] is None
        assert oe["backlog_age_days"] is None

    def test_rich_table_renders_operational_entropy_row(
        self,
        monkeypatch: Any,
        tmp_path: Path,
        capsys: Any,
    ) -> None:
        out = _status_table(monkeypatch, tmp_path, capsys)

        assert "operational-entropy" in out
        # the degraded counts render as "?" placeholders, never crash the table
        assert "epics=" in out

    def test_status_json_exposes_top_level_entropy_snapshot(
        self,
        monkeypatch: Any,
        tmp_path: Path,
        capsys: Any,
    ) -> None:
        """Issue #414 — ``status --json`` carries a top-level ``entropy`` object
        whose fields equal the operational-entropy snapshot, so operators reach
        it via ``status --json | jq .entropy`` without the deep control_plane
        path."""
        blob = _status_json(monkeypatch, tmp_path, capsys)

        assert "entropy" in blob
        entropy = blob["entropy"]
        assert set(entropy) == {
            "open_branches",
            "live_worktrees",
            "open_epics",
            "backlog_age_days",
        }
        # The top-level alias IS the nested snapshot — one computed source of
        # truth, never a divergent recompute (manifesto Q7).
        assert entropy == blob["control_plane"]["operational_entropy"]

    def test_status_table_renders_entropy_row_with_all_four_fields(
        self,
        monkeypatch: Any,
        tmp_path: Path,
        capsys: Any,
    ) -> None:
        """Issue #414 — the Rich status table renders an entropy row showing all
        four snapshot fields; degraded counts render as ``?`` and never crash."""
        out = _status_table(monkeypatch, tmp_path, capsys)

        assert "entropy" in out
        for field in ("branches=", "worktrees=", "epics=", "backlog_age_days="):
            assert field in out


class TestTasksStatusReadsCanonicalSagaStore:
    """Focused coverage of the ``['tasks']`` block per issue #373.

    Each test asserts the task block resolves from ``.forge/tasks.db`` via the
    canonical ``SqliteTaskSagaStore`` APIs (``list_in_flight`` / ``list_stale``),
    never the legacy ``docs/ops/worker-sessions.db``.
    """

    def test_happy_path_running_with_expired_lease_is_in_flight_and_stale(
        self, tmp_path: Path
    ) -> None:
        now = datetime.now(UTC)
        store = SqliteTaskSagaStore(tmp_path / ".forge" / "tasks.db")
        _seed_running_saga(
            store,
            "task-1",
            issue=1,
            acquired_at=now - timedelta(minutes=10),
            expires_at=now - timedelta(seconds=1),
        )
        store.close()

        tasks = collect_control_plane_status(tmp_path, now)["tasks"]

        assert tasks == {
            "available": True,
            "path": str(tmp_path / ".forge" / "tasks.db"),
            "in_flight_count": 1,
            "stale_lease_count": 1,
        }

    def test_mixed_excludes_terminal_and_counts_only_expired_as_stale(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        store = SqliteTaskSagaStore(tmp_path / ".forge" / "tasks.db")
        _seed_running_saga(
            store,
            "task-expired",
            issue=1,
            acquired_at=now - timedelta(minutes=10),
            expires_at=now - timedelta(seconds=1),
        )
        _seed_running_saga(
            store,
            "task-fresh",
            issue=2,
            acquired_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        _seed_running_saga(
            store,
            "task-done",
            issue=3,
            acquired_at=now - timedelta(minutes=3),
            expires_at=now + timedelta(minutes=5),
        )
        store.mark_completed("task-done")
        store.close()

        tasks = collect_control_plane_status(tmp_path, now)["tasks"]

        assert tasks["available"] is True
        assert tasks["in_flight_count"] == 2  # terminal excluded
        assert tasks["stale_lease_count"] == 1  # only the expired lease

    def test_legacy_worker_sessions_db_does_not_change_tasks_block(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        store = SqliteTaskSagaStore(tmp_path / ".forge" / "tasks.db")
        _seed_running_saga(
            store,
            "task-1",
            issue=1,
            acquired_at=now - timedelta(minutes=10),
            expires_at=now - timedelta(seconds=1),
        )
        store.close()

        without_legacy = collect_control_plane_status(tmp_path, now)["tasks"]

        # Now seed a populated legacy store; the tasks block must be byte-for-byte
        # identical — the legacy store is fully ignored.
        _seed_legacy_worker_sessions(tmp_path / "docs" / "ops", now)
        with_legacy = collect_control_plane_status(tmp_path, now)["tasks"]

        assert with_legacy == without_legacy
        assert with_legacy["path"] == str(tmp_path / ".forge" / "tasks.db")

    def test_absent_canonical_store_degrades_to_unavailable(self, tmp_path: Path) -> None:
        # Even a populated legacy store must not make tasks "available".
        now = datetime.now(UTC)
        _seed_legacy_worker_sessions(tmp_path / "docs" / "ops", now)

        tasks = collect_control_plane_status(tmp_path, now)["tasks"]

        assert tasks == {
            "available": False,
            "path": str(tmp_path / ".forge" / "tasks.db"),
            "in_flight_count": None,
            "stale_lease_count": None,
        }

    def test_corrupt_canonical_store_degrades_with_error_no_raise(self, tmp_path: Path) -> None:
        now = datetime.now(UTC)
        forge_dir = tmp_path / ".forge"
        forge_dir.mkdir()
        # Non-sqlite bytes at the canonical path: opening must degrade, not crash.
        (forge_dir / "tasks.db").write_bytes(b"this is not a sqlite database")

        tasks = collect_control_plane_status(tmp_path, now)["tasks"]

        assert tasks["available"] is False
        assert tasks["in_flight_count"] is None
        assert tasks["stale_lease_count"] is None
        assert tasks["error"]
        assert tasks["path"] == str(forge_dir / "tasks.db")
