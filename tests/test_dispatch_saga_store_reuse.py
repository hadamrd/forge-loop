"""Saga store is opened once per tick, not re-``__init__``'d per worker (#227).

Each ``_dispatch_one_worker`` used to open a ``SqliteTaskSagaStore`` 2-3 times
(``_resolve_task_saga_store`` + the heartbeat thread + a policy-record fallback),
and *every* construction re-ran the full schema script + compat ALTER probing —
i.e. schema-migration work per worker per tick. These tests pin the fix:

* the tick opens one store and threads it through dispatch + heartbeat + policy,
* schema creation + compat migration run once (at tick open), not per worker,
* the shared single connection is safe across the dispatch ThreadPool's threads,
* the tick-scoped store is closed once the tick drains (no per-tick leak).
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
# AC/review #227: the tick-scoped store is owned by ``_run_workers`` and closed
# once the tick drains, so a long-running loop does not leak one sqlite
# connection per tick.
# ---------------------------------------------------------------------------


def _disable_dispatch_side_paths(monkeypatch: Any) -> list[dict[str, Any]]:
    """Force the legacy ThreadPool path and capture each dispatch's kwargs.

    Stubs ``_dispatch_one_worker`` so ``_run_workers`` exercises exactly the
    #227 open-once-and-close-the-tick-store logic without the worker/heartbeat
    internals, and pins pipeline + persistent-worker gating OFF so the run is
    deterministic regardless of ambient settings.
    """
    import forge_loop.runner._pipeline_driver as _pdrv

    monkeypatch.setattr(_pdrv, "pipeline_driven_enabled", lambda _cfg: False)
    monkeypatch.setattr(dispatch_mod, "persistent_worker_enabled", lambda: False)

    seen: list[dict[str, Any]] = []

    def _spy_dispatch(cfg: Any, issue: Any, meta: Any, **kwargs: Any) -> WorkerOutcome:
        seen.append({"issue": issue, **kwargs})
        return _ok_worker()

    monkeypatch.setattr(dispatch_mod, "_dispatch_one_worker", _spy_dispatch)
    return seen


def test_run_workers_opens_one_store_and_closes_it(monkeypatch: Any, tmp_path: Any) -> None:
    """The tick opens one saga store, shares it with every worker, then closes it.

    Proves the leak fix (#227 review): after ``_run_workers`` returns, the
    shared connection is closed, so operating on it raises
    ``sqlite3.ProgrammingError``.
    """
    cfg = _make_cfg(tmp_path)
    cfg.parallel = 2  # type: ignore[attr-defined]
    seen = _disable_dispatch_side_paths(monkeypatch)

    issues = [_issue(7), _issue(8), _issue(9)]
    metas = [_meta() for _ in issues]
    dispatch_mod._run_workers(
        cfg,
        issues,
        metas,
        tick=1,
        master_log_path=tmp_path / "master.log",
        bus_emit=lambda *a, **k: None,
    )

    # Every worker received the SAME store instance: opened once per tick.
    stores = {id(call["saga_store"]) for call in seen}
    assert len(seen) == 3
    assert len(stores) == 1, "all workers share the one tick-scoped store"
    tick_store = seen[0]["saga_store"]
    assert isinstance(tick_store, SqliteTaskSagaStore)

    # Closed in the ``finally`` once the ThreadPool drained: no per-tick leak.
    with pytest.raises(sqlite3.ProgrammingError):
        tick_store._connection.execute("SELECT 1")


def test_injected_task_store_is_not_closed_by_run_workers(monkeypatch: Any, tmp_path: Any) -> None:
    """A caller-owned ``cfg.task_store`` outlives the tick — never closed here."""
    cfg = _make_cfg(tmp_path)
    cfg.parallel = 2  # type: ignore[attr-defined]
    seen = _disable_dispatch_side_paths(monkeypatch)

    injected = SqliteTaskSagaStore(tmp_path / "injected.db")
    cfg.task_store = injected  # type: ignore[attr-defined]

    dispatch_mod._run_workers(
        cfg,
        [_issue(7)],
        [_meta()],
        tick=1,
        master_log_path=tmp_path / "master.log",
        bus_emit=lambda *a, **k: None,
    )

    # The tick reused the injected store...
    assert seen[0]["saga_store"] is injected
    # ...and left it open: the caller owns it, so it must still be usable.
    assert injected.get("task-absent") is None


# ---------------------------------------------------------------------------
# AC: the single shared connection is reused safely across the tick's threads.
# ---------------------------------------------------------------------------


def test_schema_migration_memoized_once_per_process_for_a_path(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """Schema/compat run at most once **per process** per path (#227 AC2).

    Constructing a second ``SqliteTaskSagaStore`` on the *same* file path — e.g.
    a fresh tick reopening the canonical saga DB after the prior tick closed it —
    skips the schema script + compat ALTER probing entirely thanks to the
    process-level ``_MIGRATED_PATHS`` memoize. The adversarial arm proves the
    memoize is real and not a global no-op: two distinct ``:memory:`` databases
    each migrate, because there is nothing to memoize across them.
    """
    migrations = _spy_migrations(monkeypatch)
    db = tmp_path / "sagas.db"

    first = SqliteTaskSagaStore(db)
    assert migrations["n"] == 1
    second = SqliteTaskSagaStore(db)  # later tick reopens the same path
    assert migrations["n"] == 1, "process-level memoize: not re-migrated per tick"

    # Both connections are fully usable against the one migrated schema.
    saga = first.create(
        task_id="t", saga_id="s", issue=1, branch="b", worktree="w", compensations=()
    )
    assert second.get("t") == saga
    first.close()
    second.close()

    # Adversarial: each ``:memory:`` store is a distinct DB, so the memoize must
    # NOT skip their migrations — proving the guard keys on real paths, not a
    # blanket "already ran once anywhere" flag.
    migrations["n"] = 0
    SqliteTaskSagaStore(":memory:")
    SqliteTaskSagaStore(":memory:")
    assert migrations["n"] == 2


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
