"""`forge-loop boot` — reload the maestro reset-recovery context on demand.

This is the first production call site for ``assemble_boot_context``: an
operator (or a booting maestro) reloads compact strategic context from the
durable ``.forge`` stores instead of from transcript memory.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from forge_loop import cli
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


def _cfg(repo: Path) -> SimpleNamespace:
    return SimpleNamespace(repo=repo, state_dir=repo / "docs" / "ops")


def _seed_forge(repo: Path) -> None:
    forge_dir = repo / ".forge"
    forge_dir.mkdir(parents=True)
    event_log = SqliteEventLog(forge_dir / "events.db")
    event_log.append(EventKind.FRONTIER_ADVANCED, {"frontier": "durable"})
    event_log.append(EventKind.MEMORY_PROMOTED, {"memory_id": "m1"})
    event_log.set_projection_cursor("frontier", ProjectionCursor(sequence=1))
    FrontierStore(forge_dir / "frontier.yaml").save(
        FrontierCursor(
            product_goal="make long-running agents recoverable",
            current_problem="boot context has no operator entrypoint",
            next_expansion="reload compact maestro context on demand",
            why_now="reset recovery must not depend on transcript memory",
        )
    )
    memory_store = SqliteMemoryStore(forge_dir / "memory.db")
    memory_store.put(_memory("m1"))
    memory_store.put(_memory("m2", tags=(REJECTED_PATH_TAG,)))


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
            source_task_ref="task:#boot",
        ),
    )


class TestCliBoot:
    def test_boot_json_reassembles_durable_context(
        self,
        monkeypatch: Any,
        tmp_path: Path,
        capsys: Any,
    ) -> None:
        _seed_forge(tmp_path)
        monkeypatch.setattr(cli, "load", lambda: _cfg(tmp_path))

        rc = cli._cmd_boot(SimpleNamespace(json=True))

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["frontier"]["next_expansion"] == "reload compact maestro context on demand"
        assert payload["active_memory_ids"] == ["m1", "m2"]
        assert payload["rejected_path_memory_ids"] == ["m2"]
        assert payload["latest_event_sequence"] == 2
        assert payload["projection_cursors"]["frontier"] == {"sequence": 1, "lag": 1}

    def test_boot_reports_in_flight_saga_from_canonical_store(
        self,
        monkeypatch: Any,
        tmp_path: Path,
        capsys: Any,
    ) -> None:
        _seed_forge(tmp_path)
        task_store = SqliteTaskSagaStore(tmp_path / ".forge" / "tasks.db")
        task_store.put(
            TaskSaga(
                task_id="task-42",
                saga_id="saga-42",
                state=TaskState.RUNNING,
                issue=42,
                branch="loop/42",
            )
        )
        task_store.put(
            TaskSaga(
                task_id="task-41-done",
                saga_id="saga-41-done",
                state=TaskState.COMPLETED,
                issue=41,
            )
        )
        monkeypatch.setattr(cli, "load", lambda: _cfg(tmp_path))

        rc = cli._cmd_boot(SimpleNamespace(json=True))

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["in_flight_task_ids"] == ["task-42"]
        assert payload["in_flight_saga_ids"] == ["saga-42"]
        # An unleased running saga is in flight but not (yet) presumed dead.
        assert payload["stale_saga_ids"] == []

    def test_boot_flags_expired_lease_saga_as_stale(
        self,
        monkeypatch: Any,
        tmp_path: Path,
        capsys: Any,
    ) -> None:
        _seed_forge(tmp_path)
        task_store = SqliteTaskSagaStore(tmp_path / ".forge" / "tasks.db")
        task_store.put(
            TaskSaga(
                task_id="task-dead",
                saga_id="saga-dead",
                state=TaskState.RUNNING,
                issue=7,
                branch="loop/7",
                lease_owner="worker-7",
                lease_expires_at=datetime.now(UTC) - timedelta(minutes=30),
            )
        )
        monkeypatch.setattr(cli, "load", lambda: _cfg(tmp_path))

        rc = cli._cmd_boot(SimpleNamespace(json=True))

        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["in_flight_saga_ids"] == ["saga-dead"]
        assert payload["stale_saga_ids"] == ["saga-dead"]

    def test_boot_summary_renders_human_text(
        self,
        monkeypatch: Any,
        tmp_path: Path,
        capsys: Any,
    ) -> None:
        _seed_forge(tmp_path)
        monkeypatch.setattr(cli, "load", lambda: _cfg(tmp_path))

        rc = cli._cmd_boot(SimpleNamespace(json=False))

        assert rc == 0
        out = capsys.readouterr().out
        assert "goal: make long-running agents recoverable" in out
        assert "memory: m1, m2" in out
        assert "event_sequence: 2" in out

    def test_boot_uninitialised_repo_fails_fast(
        self,
        monkeypatch: Any,
        tmp_path: Path,
        capsys: Any,
    ) -> None:
        monkeypatch.setattr(cli, "load", lambda: _cfg(tmp_path))

        rc = cli._cmd_boot(SimpleNamespace(json=False))

        assert rc == 1
        captured = capsys.readouterr()
        assert "frontier state is required" in captured.err
        assert "forge-loop init" in captured.err
        # The failed boot must not materialise empty durable stores.
        assert not (tmp_path / ".forge" / "events.db").exists()
        assert not (tmp_path / ".forge" / "memory.db").exists()
