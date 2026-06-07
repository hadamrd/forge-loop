"""Periodic stale-lease watchdog (issue #325).

These tests pin the falsifiable acceptance criterion: a worker whose heartbeat
lapses mid-run is detected and its saga reconciled within one watchdog interval
while the loop keeps running — no reboot, no operator ``recover``.

Manifesto coverage:
* T1 (state machine — one test per edge + an adversarial default-branch test):
  the ``maybe_reap`` time gate has three arms — not-due, due, and the
  ``interval <= 0`` disabled/default arm — each exercised below.
* T6 (loop guard fires): after a fire the watchdog rearms and does not fire
  again until another full interval has elapsed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from forge_loop.runner import boot as boot_mod
from forge_loop.runner.lease_watchdog import LeaseWatchdog
from forge_loop.tasks import Compensation, SqliteTaskSagaStore, TaskState


class _FakeClock:
    """Injectable monotonic clock so tests advance time without sleeping."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _counting_sweep() -> tuple[list[int], Any]:
    calls: list[int] = []

    def sweep() -> int:
        calls.append(1)
        return len(calls)

    return calls, sweep


# --------------------------------------------------------------------------- #
# Time-gate edges (T1).
# --------------------------------------------------------------------------- #
def test_does_not_fire_before_one_interval_elapses() -> None:
    """Not-due edge: no elapsed time since boot ⇒ the sweep must not run."""
    clock = _FakeClock()
    calls, sweep = _counting_sweep()
    wd = LeaseWatchdog(interval_s=30.0, sweep=sweep, clock=clock)

    assert wd.maybe_reap() is None
    clock.advance(29.9)  # just shy of the interval
    assert wd.maybe_reap() is None
    assert calls == []


def test_fires_once_after_one_interval() -> None:
    """Due edge: once the interval elapses the sweep runs and its result returns."""
    clock = _FakeClock()
    calls, sweep = _counting_sweep()
    wd = LeaseWatchdog(interval_s=30.0, sweep=sweep, clock=clock)

    clock.advance(30.0)
    result = wd.maybe_reap()

    assert result == 1
    assert calls == [1]


@pytest.mark.parametrize("interval", [0.0, -5.0])
def test_disabled_when_interval_non_positive(interval: float) -> None:
    """Default/disabled arm (T1 adversarial): interval <= 0 never fires, ever.

    Even after an absurd amount of elapsed time the sweep must stay dormant —
    this is the operator opt-out, and a regression that fired anyway would run
    compensations the operator explicitly disabled.
    """
    clock = _FakeClock()
    calls, sweep = _counting_sweep()
    wd = LeaseWatchdog(interval_s=interval, sweep=sweep, clock=clock)

    clock.advance(10_000.0)
    assert wd.maybe_reap() is None
    assert calls == []


def test_rearms_and_fires_again_each_interval() -> None:
    """T6 guard: after a fire the watchdog rearms; it fires once per interval, not on every call."""
    clock = _FakeClock()
    calls, sweep = _counting_sweep()
    wd = LeaseWatchdog(interval_s=30.0, sweep=sweep, clock=clock)

    clock.advance(30.0)
    assert wd.maybe_reap() == 1  # fires
    assert wd.maybe_reap() is None  # immediate re-call: rearmed, not due
    clock.advance(15.0)
    assert wd.maybe_reap() is None  # half an interval: still not due
    clock.advance(15.0)
    assert wd.maybe_reap() == 2  # another full interval: fires again
    assert calls == [1, 1]


# --------------------------------------------------------------------------- #
# Acceptance: a lapsed lease is compensated DURING the run (no reboot/recover).
# --------------------------------------------------------------------------- #
class _Cfg:
    """Minimal Config-shaped stub for the shared sweep helper."""

    def __init__(self, *, repo: Path) -> None:
        self.repo = repo
        self.events_file = repo / "events.jsonl"
        self.events_file.parent.mkdir(parents=True, exist_ok=True)
        self.events_file.touch()


def _seed_dead_worker(store: SqliteTaskSagaStore, *, issue: int) -> None:
    """A worker leased a slot then silently stopped heart-beating: lease lapsed."""
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
                reason="cleanup after dead worker",
            ),
        ),
    )
    acquired = datetime.now(UTC) - timedelta(minutes=10)
    store.acquire_lease(
        f"task-{issue}-worker",
        owner_id=f"worker-{issue}",
        expires_at=acquired + timedelta(minutes=1),  # already long expired
        acquired_at=acquired,
    )


def test_lapsed_lease_reaped_within_one_interval_during_run(tmp_path: Path) -> None:
    """The acceptance test: kill the heartbeat, run the loop's watchdog, assert COMPENSATED.

    No boot, no ``forge-loop recover`` — the running loop's watchdog alone must
    drive the lapsed lease to a terminal/compensated state, and only after one
    watchdog interval has elapsed.
    """
    forge_dir = tmp_path / ".forge"
    store = SqliteTaskSagaStore(forge_dir / "tasks.db")
    _seed_dead_worker(store, issue=7)

    cfg: Any = _Cfg(repo=tmp_path)
    from functools import partial

    clock = _FakeClock()
    wd = LeaseWatchdog(
        interval_s=30.0,
        sweep=partial(boot_mod._lease_reconcile_sweep, cfg, event_prefix="watchdog_recovery"),
        clock=clock,
    )

    # Mid-run, before the interval elapses: the lease is still held.
    assert wd.maybe_reap() is None
    assert store.get("task-7-worker").state == TaskState.RUNNING

    # One watchdog interval later: the running loop reaps the lapsed lease.
    clock.advance(30.0)
    report = wd.maybe_reap()

    assert report is not None
    assert report.recovered_count == 1
    reopened = SqliteTaskSagaStore(forge_dir / "tasks.db")
    assert reopened.get("task-7-worker").state == TaskState.COMPENSATED
    assert reopened.list_in_flight() == ()
    assert "watchdog_recovery" in cfg.events_file.read_text()


def test_healthy_lease_survives_the_watchdog(tmp_path: Path) -> None:
    """Adversarial false-case (T2): a worker still heart-beating must NOT be reaped.

    If the watchdog reaped a live lease it would yank a slot out from under a
    working worker — the opposite of the bug we are fixing.
    """
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
        "task-9-worker",
        owner_id="worker-9",
        expires_at=now + timedelta(minutes=30),  # lease alive well into the future
        acquired_at=now,
    )

    cfg: Any = _Cfg(repo=tmp_path)
    from functools import partial

    clock = _FakeClock()
    wd = LeaseWatchdog(
        interval_s=30.0,
        sweep=partial(boot_mod._lease_reconcile_sweep, cfg, event_prefix="watchdog_recovery"),
        clock=clock,
    )

    clock.advance(30.0)
    report = wd.maybe_reap()

    assert report is not None
    assert report.recovered_count == 0
    assert store.get("task-9-worker").state == TaskState.RUNNING
    assert "watchdog_recovery" not in cfg.events_file.read_text()


def test_sweep_is_noop_when_no_saga_store(tmp_path: Path) -> None:
    """T2 negative branch: no control plane on disk ⇒ sweep is a silent no-op."""
    cfg: Any = _Cfg(repo=tmp_path)
    from functools import partial

    sweep = partial(boot_mod._lease_reconcile_sweep, cfg, event_prefix="watchdog_recovery")
    assert sweep() is None
    # A no-op sweep must not materialise the store, nor emit an event.
    assert not (tmp_path / ".forge" / "tasks.db").exists()
    assert cfg.events_file.read_text() == ""
