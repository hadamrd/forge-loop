"""Saga store is opened once per tick, not re-``__init__``'d per worker (#227).

Each ``_dispatch_one_worker`` used to open a ``SqliteTaskSagaStore`` 2-3 times
(``_resolve_task_saga_store`` + the heartbeat thread + a policy-record fallback),
and *every* construction re-ran the full schema script + compat ALTER probing —
i.e. schema-migration work per worker per tick. These tests pin the fix:

* the tick opens one store and threads it through dispatch + heartbeat + policy,
* schema creation + compat migration run once (at tick open), not per worker,
* the shared single connection is safe across the dispatch ThreadPool's threads,
* the ``ensure_schema`` guard genuinely skips migration on both branches.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from forge_loop.runner import dispatch as dispatch_mod
from forge_loop.tasks import SqliteTaskSagaStore, TaskState
from forge_loop.worker import WorkerOutcome

# Reuse the Config/issue/meta scaffolding from the dispatch FSM tests.
from tests.test_persistent_dispatch import _issue, _make_cfg, _meta


def _ok_worker(*args: Any, **kwargs: Any) -> WorkerOutcome:
    return WorkerOutcome(
        issue=7,
        title="t",
        pr_url="https://x/pull/1",
        status="open",
        duration_s=1.0,
        stdout_tail="",
    )


def _spy_migrations(monkeypatch: Any) -> dict[str, int]:
    """Count every schema/compat migration (== every schema-running __init__)."""
    counter = {"n": 0}
    real = SqliteTaskSagaStore._ensure_compat_columns

    def _counting(self: SqliteTaskSagaStore) -> None:
        counter["n"] += 1
        real(self)

    monkeypatch.setattr(SqliteTaskSagaStore, "_ensure_compat_columns", _counting)
    return counter


# ---------------------------------------------------------------------------
# AC: schema creation + compat migration run once across multiple dispatches.
# ---------------------------------------------------------------------------


def test_schema_migration_runs_once_across_multiple_dispatches(
    monkeypatch: Any, tmp_path: Any
) -> None:
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(dispatch_mod, "run_worker", _ok_worker)

    migrations = _spy_migrations(monkeypatch)

    # The tick opens the saga store exactly once -> one schema migration.
    tick_store = dispatch_mod._resolve_task_saga_store(cfg)
    assert tick_store is not None
    assert migrations["n"] == 1

    # Measure only what the per-worker dispatches add on top.
    migrations["n"] = 0
    for n in (7, 8, 9):
        outcome = dispatch_mod._dispatch_one_worker(
            cfg,
            _issue(n),
            _meta(),
            tick=1,
            bus_emit=lambda *a, **k: None,
            store=None,
            saga_store=tick_store,
        )
        assert outcome.status == "open"

    # Zero further migrations: dispatch + heartbeat + policy recording all reuse
    # the single tick store instead of re-__init__-ing it per worker.
    assert migrations["n"] == 0

    # ...and the sagas were really recorded through that shared store.
    for n in (7, 8, 9):
        saga = tick_store.get(f"task-{n}-worker")
        assert saga is not None
        assert saga.state == TaskState.COMPLETED


def test_dispatch_without_shared_store_falls_back_to_its_own(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """Legacy/unit-test callers that pass no ``saga_store`` still record sagas.

    Sad-path counterpart: with no tick store threaded in, each dispatch resolves
    its own store (one migration) so behaviour is unchanged for direct callers.
    """
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(dispatch_mod, "run_worker", _ok_worker)

    migrations = _spy_migrations(monkeypatch)
    outcome = dispatch_mod._dispatch_one_worker(
        cfg,
        _issue(11),
        _meta(),
        tick=1,
        bus_emit=lambda *a, **k: None,
        store=None,  # no saga_store -> internal resolve
    )

    assert outcome.status == "open"
    # The single self-resolved store ran its migration once (not 2-3x): the
    # heartbeat thread now reuses it instead of opening its own connection.
    assert migrations["n"] == 1
    assert dispatch_mod._resolve_task_saga_store(cfg).get("task-11-worker") is not None


# ---------------------------------------------------------------------------
# AC: schema creation + compat migration are guarded (ensure_schema) - both arms.
# ---------------------------------------------------------------------------


def test_ensure_schema_true_runs_migration_false_skips_it(monkeypatch: Any, tmp_path: Any) -> None:
    db = tmp_path / "tasks.db"
    SqliteTaskSagaStore(db)  # primary creates the schema on disk

    migrations = _spy_migrations(monkeypatch)

    # The false branch: a sibling connection onto the already-initialised DB
    # skips schema + compat entirely.
    sibling = SqliteTaskSagaStore(db, ensure_schema=False)
    assert migrations["n"] == 0
    assert sibling._schema_ensured is False
    # It still reads the shared on-disk table (no migration needed).
    assert sibling.get("task-absent") is None

    # The true branch: an explicit ensure_schema=True does run the migration.
    primary2 = SqliteTaskSagaStore(db, ensure_schema=True)
    assert migrations["n"] == 1
    assert primary2._schema_ensured is True


def test_ensure_schema_false_on_fresh_db_leaves_table_uncreated(tmp_path: Any) -> None:
    """Adversarial: ensure_schema=False truly skips creation.

    On a brand-new DB with no sibling having created the schema, the table does
    not exist, so an operation must raise rather than silently succeeding —
    proof the guard is real and not a no-op.
    """
    store = SqliteTaskSagaStore(tmp_path / "fresh.db", ensure_schema=False)
    with pytest.raises(sqlite3.OperationalError):
        store.get("task-x")


# ---------------------------------------------------------------------------
# AC: the single shared connection is reused safely across the tick's threads.
# ---------------------------------------------------------------------------


def test_shared_store_is_thread_safe_across_concurrent_dispatches(
    monkeypatch: Any, tmp_path: Any
) -> None:
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(dispatch_mod, "run_worker", _ok_worker)

    tick_store = dispatch_mod._resolve_task_saga_store(cfg)
    assert tick_store is not None
    issues = list(range(20, 28))

    # Mirrors the real dispatch ThreadPoolExecutor: many workers hitting one
    # shared connection concurrently. Without check_same_thread=False + the
    # per-instance lock this raises sqlite3.ProgrammingError / "database is
    # locked"; the assertions below would then never be reached.
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [
            ex.submit(
                dispatch_mod._dispatch_one_worker,
                cfg,
                _issue(n),
                _meta(),
                tick=1,
                bus_emit=lambda *a, **k: None,
                store=None,
                saga_store=tick_store,
            )
            for n in issues
        ]
        outcomes = [f.result() for f in futures]

    assert all(o.status == "open" for o in outcomes)
    for n in issues:
        saga = tick_store.get(f"task-{n}-worker")
        assert saga is not None
        assert saga.state == TaskState.COMPLETED
