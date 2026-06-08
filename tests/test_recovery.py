"""Boot-time recovery: reconcile dead-worker sagas (the resumability payoff)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from forge_loop.control.recovery import reconcile_stale_sagas
from forge_loop.tasks import Compensation, CompensationKind, SqliteTaskSagaStore, TaskState


def _stale_with_compensations(
    store: SqliteTaskSagaStore, *, issue: int, compensations: tuple[Compensation, ...]
) -> None:
    """Seed a RUNNING saga with an expired lease and arbitrary compensations."""
    store.create(
        task_id=f"task-{issue}-worker",
        saga_id=f"saga-{issue}-worker",
        issue=issue,
        branch=f"loop/{issue}",
        worktree=f"/tmp/wt-loop-{issue}",
        compensations=compensations,
    )
    acquired = datetime.now(UTC) - timedelta(minutes=10)
    store.acquire_lease(
        f"task-{issue}-worker",
        owner_id=f"worker-{issue}",
        expires_at=acquired + timedelta(minutes=1),
        acquired_at=acquired,
    )


def _store(tmp_path: Path) -> SqliteTaskSagaStore:
    return SqliteTaskSagaStore(tmp_path / "tasks.db")


def _stale_running(store: SqliteTaskSagaStore, *, issue: int) -> None:
    """Seed a RUNNING saga with an already-expired lease (a dead worker)."""
    store.create(
        task_id=f"task-{issue}-worker",
        saga_id=f"saga-{issue}-worker",
        issue=issue,
        branch=f"loop/{issue}",
        worktree=f"/tmp/wt-loop-{issue}",
        compensations=(
            Compensation(
                kind="remove-worktree",
                target=f"/tmp/wt-loop-{issue}",
                reason="cleanup after worker task terminal state",
            ),
        ),
    )
    acquired = datetime.now(UTC) - timedelta(minutes=10)
    store.acquire_lease(
        f"task-{issue}-worker",
        owner_id=f"worker-{issue}",
        expires_at=acquired + timedelta(minutes=1),
        acquired_at=acquired,
    )


def test_reconcile_compensates_and_closes_stale_saga(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _stale_running(store, issue=7)
    reaped: list[int] = []

    report = reconcile_stale_sagas(store, reap_worktree=reaped.append)

    assert report.recovered_count == 1
    assert report.recovered[0].saga_id == "saga-7-worker"
    assert report.recovered[0].worktrees_reaped == ("/tmp/wt-loop-7",)
    assert reaped == [7]
    # Saga is now terminal and drains from the in-flight recovery view.
    assert store.get("task-7-worker").state == TaskState.COMPENSATED
    assert store.list_in_flight() == ()


def test_reconcile_leaves_healthy_sagas_untouched(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # A live worker: leased well into the future.
    store.create(
        task_id="task-9-worker",
        saga_id="saga-9-worker",
        issue=9,
        branch="loop/9",
        worktree="/tmp/wt-loop-9",
        compensations=(),
    )
    now = datetime.now(UTC)
    store.acquire_lease(
        "task-9-worker", owner_id="w", expires_at=now + timedelta(hours=1), acquired_at=now
    )

    report = reconcile_stale_sagas(store, reap_worktree=lambda _: None)

    assert report.recovered_count == 0
    assert store.get("task-9-worker").state == TaskState.RUNNING


def test_reconcile_continues_past_a_failing_saga(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _stale_running(store, issue=1)
    _stale_running(store, issue=2)

    def flaky_reap(issue: int) -> None:
        if issue == 1:
            raise RuntimeError("worktree busy")

    report = reconcile_stale_sagas(store, reap_worktree=flaky_reap)

    # Issue 1's compensation blew up; issue 2 still got reconciled.
    assert [r.issue for r in report.recovered] == [2]
    assert any("saga-1-worker" in e for e in report.errors)
    assert store.get("task-2-worker").state == TaskState.COMPENSATED
    assert store.get("task-1-worker").state == TaskState.RUNNING  # untouched, still stale


def test_reconcile_quarantines_saga_with_unhandled_compensation(tmp_path: Path) -> None:
    """#360: an unhandled kind drives the saga TERMINAL (QUARANTINED), not COMPENSATED.

    Liveness: the saga must stop being immortal. Integrity: it must not claim
    the side effect was undone. QUARANTINED satisfies both.
    """
    store = _store(tmp_path)
    _stale_with_compensations(
        store,
        issue=42,
        compensations=(
            Compensation(
                kind="close-pr",  # a kind the recovery engine has no handler for
                target="https://github.com/o/r/pull/42",
                reason="close orphaned PR opened by dead worker",
            ),
        ),
    )
    reaped: list[int] = []

    report = reconcile_stale_sagas(store, reap_worktree=reaped.append)

    # Driven terminal (QUARANTINED), NOT COMPENSATED, NOT left RUNNING.
    assert report.recovered == ()
    saga = store.get("task-42-worker")
    assert saga.state == TaskState.QUARANTINED
    assert saga.is_terminal is True
    # The terminal reason names the unhandled kind so an operator knows why.
    assert "close-pr" in (saga.terminal_reason or "")
    # The integrity hole is surfaced: the saga id + the unhandled kind are named.
    assert len(report.errors) == 1
    assert "saga-42-worker" in report.errors[0]
    assert "close-pr" in report.errors[0]
    # No handled compensation was run for a saga we refuse to compensate.
    assert reaped == []
    # Liveness: it has drained from the in-flight view.
    assert store.list_in_flight() == ()


def test_reconcile_quarantines_saga_with_mixed_handled_and_unhandled_kinds(
    tmp_path: Path,
) -> None:
    """#360: any unhandled kind taints the saga → QUARANTINED, no reap claimed."""
    store = _store(tmp_path)
    _stale_with_compensations(
        store,
        issue=43,
        compensations=(
            Compensation(
                kind=CompensationKind.REMOVE_WORKTREE,
                target="/tmp/wt-loop-43",
                reason="cleanup worktree",
            ),
            Compensation(
                kind="delete-branch",  # unhandled — taints the whole saga
                target="loop/43",
                reason="delete orphaned branch",
            ),
        ),
    )
    reaped: list[int] = []

    report = reconcile_stale_sagas(store, reap_worktree=reaped.append)

    assert report.recovered == ()
    saga = store.get("task-43-worker")
    assert saga.state == TaskState.QUARANTINED
    assert saga.is_terminal is True
    assert any("delete-branch" in e for e in report.errors)
    # We do not run the handled remove-worktree when another kind is unhandled:
    # the saga is quarantined whole, no side effect claimed.
    assert reaped == []


def test_reconcile_quarantine_reason_lists_every_unhandled_kind(tmp_path: Path) -> None:
    """#360: multiple unhandled kinds → the terminal reason names them all."""
    store = _store(tmp_path)
    _stale_with_compensations(
        store,
        issue=44,
        compensations=(
            Compensation(kind="close-pr", target="pr/44", reason="close pr"),
            Compensation(kind="delete-branch", target="loop/44", reason="del branch"),
        ),
    )
    reaped: list[int] = []

    report = reconcile_stale_sagas(store, reap_worktree=reaped.append)

    saga = store.get("task-44-worker")
    assert saga.state == TaskState.QUARANTINED
    reason = saga.terminal_reason or ""
    assert "close-pr" in reason
    assert "delete-branch" in reason
    assert reaped == []  # no reap_worktree side effect claimed
    assert len(report.errors) == 1


def test_reconcile_mixed_sweep_one_compensated_one_quarantined(tmp_path: Path) -> None:
    """#360: a handled saga and an unhandled saga in one sweep both reach terminal.

    One bad saga must not abort the sweep.
    """
    store = _store(tmp_path)
    _stale_running(store, issue=7)  # handled REMOVE_WORKTREE → COMPENSATED
    _stale_with_compensations(
        store,
        issue=8,
        compensations=(
            Compensation(kind="close-pr", target="pr/8", reason="close pr"),
        ),
    )
    reaped: list[int] = []

    report = reconcile_stale_sagas(store, reap_worktree=reaped.append)

    assert [r.issue for r in report.recovered] == [7]
    assert reaped == [7]  # only the handled saga reaped
    assert store.get("task-7-worker").state == TaskState.COMPENSATED
    assert store.get("task-8-worker").state == TaskState.QUARANTINED
    # Both terminal: nothing left in flight.
    assert store.list_in_flight() == ()
    assert any("close-pr" in e for e in report.errors)


def test_reconcile_quarantined_saga_not_reprocessed_on_resweep(tmp_path: Path) -> None:
    """#360: proves the immortal-saga bug is fixed — a re-sweep is a no-op.

    Once quarantined the saga is terminal, so ``list_stale`` excludes it and a
    second ``reconcile_stale_sagas`` neither re-reports nor re-mutates it.
    """
    store = _store(tmp_path)
    _stale_with_compensations(
        store,
        issue=45,
        compensations=(Compensation(kind="close-pr", target="pr/45", reason="x"),),
    )

    first = reconcile_stale_sagas(store, reap_worktree=lambda _: None)
    assert store.get("task-45-worker").state == TaskState.QUARANTINED
    assert len(first.errors) == 1

    second = reconcile_stale_sagas(store, reap_worktree=lambda _: None)
    # No longer stale (terminal) ⇒ not re-processed, not re-reported.
    assert second.errors == ()
    assert second.recovered == ()


def test_reconcile_quarantines_raw_string_kind_not_in_enum(tmp_path: Path) -> None:
    """#360 (adversarial): a kind written by a newer loop version (not in the enum).

    ``Compensation.kind`` is a plain str, so a saga can carry a kind this
    version has never seen. It must still be driven terminal, never crash, never
    stranded.
    """
    store = _store(tmp_path)
    _stale_with_compensations(
        store,
        issue=46,
        compensations=(
            Compensation(
                kind="some-future-kind-2027",  # not a CompensationKind member at all
                target="whatever",
                reason="written by a newer loop",
            ),
        ),
    )

    report = reconcile_stale_sagas(store, reap_worktree=lambda _: None)

    saga = store.get("task-46-worker")
    assert saga.state == TaskState.QUARANTINED
    assert saga.is_terminal is True
    assert "some-future-kind-2027" in (saga.terminal_reason or "")
    assert any("some-future-kind-2027" in e for e in report.errors)


def test_recovery_handler_keyset_is_exhaustive_over_compensation_kinds() -> None:
    """#360 exhaustiveness contract: every CompensationKind is classified.

    Every member must be either HANDLED (recovery runs its undo) or explicitly
    DEFERRED (enqueued but recovery quarantines it until its handler lands).
    Goes RED the moment a member is added to ``CompensationKind`` without
    registering it in either set — the gap is caught at test time, not in
    production recovery. The two sets are disjoint: a kind is never both.
    """
    from forge_loop.control.recovery import (
        _DEFERRED_COMPENSATION_KINDS,
        _HANDLED_COMPENSATION_KINDS,
    )

    assert not (_HANDLED_COMPENSATION_KINDS & _DEFERRED_COMPENSATION_KINDS)
    assert set(CompensationKind) == _HANDLED_COMPENSATION_KINDS | _DEFERRED_COMPENSATION_KINDS


def test_runner_boot_recovery_reconciles_and_emits_event(tmp_path: Path) -> None:
    from forge_loop.runner import boot as boot_mod

    forge_dir = tmp_path / ".forge"
    store = SqliteTaskSagaStore(forge_dir / "tasks.db")
    _stale_running(store, issue=7)

    cfg: Any = SimpleNamespaceCfg(repo=tmp_path, events_file=tmp_path / "events.jsonl")
    report = boot_mod._run_boot_recovery(cfg)

    assert report is not None
    assert report.recovered_count == 1
    assert SqliteTaskSagaStore(forge_dir / "tasks.db").get("task-7-worker").state == (
        TaskState.COMPENSATED
    )
    events = cfg.events_file.read_text()
    assert "boot_recovery" in events


def test_runner_boot_recovery_noop_without_store(tmp_path: Path) -> None:
    from forge_loop.runner import boot as boot_mod

    cfg: Any = SimpleNamespaceCfg(repo=tmp_path, events_file=tmp_path / "events.jsonl")
    assert boot_mod._run_boot_recovery(cfg) is None
    # No `.forge/tasks.db` should be materialised by a no-op recovery.
    assert not (tmp_path / ".forge" / "tasks.db").exists()


def test_run_tick_recovery_compensates_stale_saga_and_reaps(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Issue #340: a stale saga is compensated when reconciliation runs at tick time.

    Asserts the saga is driven COMPENSATED (the ``mark_compensated`` effect)
    and the worktree reaper fired for its issue — the per-tick equivalent of
    the boot sweep, with no process restart.
    """
    import forge_loop.runner._helpers as helpers_mod
    from forge_loop.runner import boot as boot_mod

    forge_dir = tmp_path / ".forge"
    store = SqliteTaskSagaStore(forge_dir / "tasks.db")
    _stale_running(store, issue=7)

    reaped: list[tuple[Path, int]] = []
    monkeypatch.setattr(
        helpers_mod, "reap_worktree", lambda repo, issue: reaped.append((repo, issue))
    )

    cfg: Any = SimpleNamespaceCfg(repo=tmp_path, events_file=tmp_path / "events.jsonl")
    report = boot_mod._run_tick_recovery(cfg)

    assert report is not None
    assert report.recovered_count == 1
    assert SqliteTaskSagaStore(forge_dir / "tasks.db").get("task-7-worker").state == (
        TaskState.COMPENSATED
    )
    # The worktree reaper fired for the dead worker's issue.
    assert reaped == [(tmp_path, 7)]
    # A non-empty sweep emits the tick_recovery event so an operator tailing
    # the event log can see the mid-session reap.
    assert "tick_recovery" in cfg.events_file.read_text()


def test_run_tick_recovery_leaves_healthy_saga_untouched(tmp_path: Path) -> None:
    """Issue #340: a saga with a live (future-dated) lease is left non-terminal.

    ``list_stale`` excludes it, so no reap/compensate occurs across ticks, and
    an empty sweep emits nothing (no per-tick log spam).
    """
    from forge_loop.runner import boot as boot_mod

    forge_dir = tmp_path / ".forge"
    store = SqliteTaskSagaStore(forge_dir / "tasks.db")
    now = datetime.now(UTC)
    store.create(
        task_id="task-9-worker",
        saga_id="saga-9-worker",
        issue=9,
        branch="loop/9",
        worktree="/tmp/wt-loop-9",
        compensations=(),
    )
    store.acquire_lease(
        "task-9-worker", owner_id="w", expires_at=now + timedelta(hours=1), acquired_at=now
    )

    cfg: Any = SimpleNamespaceCfg(repo=tmp_path, events_file=tmp_path / "events.jsonl")
    report = boot_mod._run_tick_recovery(cfg)

    assert report is not None
    assert report.recovered_count == 0
    # The expired-lease predicate excludes a heart-beated saga.
    assert list(store.list_stale(now=now)) == []
    assert SqliteTaskSagaStore(forge_dir / "tasks.db").get("task-9-worker").state == (
        TaskState.RUNNING
    )
    # Empty sweep ⇒ no event (no per-tick spam).
    assert "tick_recovery" not in cfg.events_file.read_text()


def test_run_tick_recovery_swallows_sweep_failure(tmp_path: Path, monkeypatch: Any) -> None:
    """Issue #340 (adversarial / sad-path): the sweep raises mid-tick.

    The failure is captured as a ``tick_recovery_failed`` event and no
    exception propagates to the tick body — a recovery hiccup must never abort
    or block a tick.
    """
    import forge_loop.control.recovery as recovery_mod
    from forge_loop.runner import boot as boot_mod

    forge_dir = tmp_path / ".forge"
    store = SqliteTaskSagaStore(forge_dir / "tasks.db")
    _stale_running(store, issue=7)

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("store exploded mid-sweep")

    monkeypatch.setattr(recovery_mod, "reconcile_stale_sagas", _boom)

    cfg: Any = SimpleNamespaceCfg(repo=tmp_path, events_file=tmp_path / "events.jsonl")
    # No exception propagates: the helper returns None on a swallowed failure.
    assert boot_mod._run_tick_recovery(cfg) is None
    assert "tick_recovery_failed" in cfg.events_file.read_text()


def test_run_tick_recovery_noop_without_store(tmp_path: Path) -> None:
    """Issue #340: no canonical store on disk ⇒ per-tick recovery is a no-op.

    Same guard as ``_run_boot_recovery``: no event emitted, no store
    materialised.
    """
    from forge_loop.runner import boot as boot_mod

    cfg: Any = SimpleNamespaceCfg(repo=tmp_path, events_file=tmp_path / "events.jsonl")
    assert boot_mod._run_tick_recovery(cfg) is None
    assert not (tmp_path / ".forge" / "tasks.db").exists()
    assert cfg.events_file.read_text() == ""


def test_run_tick_recovery_two_ticks_compensates_without_restart(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Issue #340 (integration): two consecutive ticks against one stale saga.

    The primary falsifiable criterion: a saga whose lease expired mid-session
    is COMPENSATED on the next tick with NO process restart. The second tick
    finds nothing stale and emits nothing (no per-tick spam).
    """
    import forge_loop.runner._helpers as helpers_mod
    from forge_loop.runner import boot as boot_mod

    forge_dir = tmp_path / ".forge"
    store = SqliteTaskSagaStore(forge_dir / "tasks.db")
    _stale_running(store, issue=500)
    monkeypatch.setattr(helpers_mod, "reap_worktree", lambda repo, issue: None)

    cfg: Any = SimpleNamespaceCfg(repo=tmp_path, events_file=tmp_path / "events.jsonl")

    # Tick 1: the dead-worker saga is reconciled — same process, no reboot.
    r1 = boot_mod._run_tick_recovery(cfg)
    assert r1 is not None and r1.recovered_count == 1
    assert SqliteTaskSagaStore(forge_dir / "tasks.db").get("task-500-worker").state == (
        TaskState.COMPENSATED
    )

    # Tick 2: nothing left to reconcile — empty sweep emits no new event.
    events_after_tick1 = cfg.events_file.read_text()
    r2 = boot_mod._run_tick_recovery(cfg)
    assert r2 is not None and r2.recovered_count == 0
    assert cfg.events_file.read_text() == events_after_tick1


class SimpleNamespaceCfg:
    """Minimal Config-shaped stub for the boot recovery helper."""

    def __init__(self, *, repo: Path, events_file: Path) -> None:
        self.repo = repo
        self.events_file = events_file
        events_file.parent.mkdir(parents=True, exist_ok=True)
        events_file.touch()
