from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from forge_loop import cli
from forge_loop.eventlog import EventKind, ProjectionCursor, SqliteEventLog
from forge_loop.frontier import FrontierCursor, FrontierStore
from forge_loop.worker_sessions import WorkerSessionStore
from forge_loop.worker_state import WorkerState


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
        ops_dir = tmp_path / "docs" / "ops"
        ops_dir.mkdir(parents=True)
        session_store = WorkerSessionStore(ops_dir / "worker-sessions.db")
        now = datetime.now(UTC)
        running = session_store.create(issue=1, branch="loop/1")
        session_store.transition_to(running.session_id, WorkerState.RUNNING)
        session_store.set_lease_expires_at(
            running.session_id, (now - timedelta(minutes=5)).isoformat()
        )
        awaiting = session_store.create(issue=2, branch="loop/2")
        session_store.transition_to(awaiting.session_id, WorkerState.RUNNING)
        session_store.set_lease_expires_at(
            awaiting.session_id, (now + timedelta(minutes=5)).isoformat()
        )
        session_store.transition_to(awaiting.session_id, WorkerState.AWAITING_CRITIC)
        abandoned = session_store.create(issue=3, branch="loop/3")
        session_store.transition_to(abandoned.session_id, WorkerState.ABANDONED)
        session_store.close()

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
            "available": False,
            "path": str(forge_dir / "memory.yaml"),
            "active_count": None,
            "rejected_count": None,
        }
        assert control["tasks"] == {
            "available": True,
            "path": str(tmp_path / "docs" / "ops" / "worker-sessions.db"),
            "in_flight_count": 2,
            "stale_lease_count": 1,
        }
        assert control["boot"] == {"available": False, "summary": None}

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
            "path": str(tmp_path / ".forge" / "memory.yaml"),
            "active_count": None,
            "rejected_count": None,
        }
        assert control["tasks"] == {
            "available": False,
            "path": str(tmp_path / "docs" / "ops" / "worker-sessions.db"),
            "in_flight_count": None,
            "stale_lease_count": None,
        }
        assert control["boot"] == {"available": False, "summary": None}
