"""Tests for watchdog.py — stuck detection + kill behavior, using a fake Popen."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from forge_loop.watchdog import WatchdogConfig, WorkerWatchdog


class FakePopen:
    """Stand-in for subprocess.Popen — implements just poll/terminate/wait/kill."""

    def __init__(self) -> None:
        self._alive = True
        self._terminate_called = False
        self._kill_called = False
        self._returncode: int | None = None

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self._terminate_called = True
        self._alive = False
        self._returncode = -15  # SIGTERM convention

    def wait(self, timeout: float = 0.0) -> int:
        # Pretend to wait; immediately return.
        return self._returncode if self._returncode is not None else 0

    def kill(self) -> None:
        self._kill_called = True
        self._returncode = -9


def _collect_emitter() -> tuple[list[tuple[str, dict]], callable]:
    events: list[tuple[str, dict]] = []
    lock = threading.Lock()

    def emit(kind: str, payload: dict) -> None:
        with lock:
            events.append((kind, payload))

    return events, emit


def test_watchdog_does_not_kill_active_worker(tmp_path: Path) -> None:
    """If activity files are fresh, watchdog should NOT escalate."""
    worktree = tmp_path
    log = tmp_path / "worker.log"
    log.write_text("starting")
    (worktree / "sprint-events.jsonl").write_text("")

    events, emit = _collect_emitter()
    proc = FakePopen()
    wd = WorkerWatchdog(
        proc=proc, worktree=worktree, log_path=log, emit=emit, issue=1,
        cfg=WatchdogConfig(poll_interval_s=0.05, stuck_after_s=10, kill_after_s=20),
    )
    wd.start()

    # Make activity happen continuously
    for _ in range(5):
        log.write_text(f"progress {time.time()}")
        time.sleep(0.06)

    proc._returncode = 0  # worker "finished"
    wd.stop()

    kinds = [k for k, _ in events]
    assert "watchdog_started" in kinds
    assert "watchdog_stopped" in kinds
    assert "watchdog_worker_killed" not in kinds
    assert wd.killed is False


def test_watchdog_emits_stuck_warning(tmp_path: Path) -> None:
    """If activity is stale beyond stuck_after_s, watchdog should WARN once."""
    worktree = tmp_path
    log = tmp_path / "worker.log"
    log.write_text("started")

    events, emit = _collect_emitter()
    proc = FakePopen()
    # Very short stuck threshold for fast test
    wd = WorkerWatchdog(
        proc=proc, worktree=worktree, log_path=log, emit=emit, issue=42,
        cfg=WatchdogConfig(poll_interval_s=0.05, stuck_after_s=0.2, kill_after_s=5.0),
    )
    wd.start()
    time.sleep(0.5)  # exceed stuck_after_s without activity

    proc._returncode = 0  # let it exit cleanly
    wd.stop()

    kinds = [k for k, _ in events]
    assert "watchdog_worker_stuck" in kinds
    assert wd.warnings >= 1
    assert wd.killed is False


def test_watchdog_kills_dead_worker(tmp_path: Path) -> None:
    """If activity stays stale past kill_after_s, watchdog should KILL."""
    worktree = tmp_path
    log = tmp_path / "worker.log"
    log.write_text("started")

    events, emit = _collect_emitter()
    proc = FakePopen()
    wd = WorkerWatchdog(
        proc=proc, worktree=worktree, log_path=log, emit=emit, issue=99,
        cfg=WatchdogConfig(poll_interval_s=0.05, stuck_after_s=0.1, kill_after_s=0.4),
    )
    wd.start()
    time.sleep(0.7)  # exceed kill_after_s
    wd.stop()

    kinds = [k for k, _ in events]
    assert "watchdog_worker_killed" in kinds
    assert wd.killed is True
    assert proc._terminate_called or proc._kill_called


def test_watchdog_stops_cleanly_when_worker_exits_normally(tmp_path: Path) -> None:
    worktree = tmp_path
    log = tmp_path / "worker.log"
    log.write_text("started")

    events, emit = _collect_emitter()
    proc = FakePopen()
    proc._returncode = 0  # already finished
    wd = WorkerWatchdog(
        proc=proc, worktree=worktree, log_path=log, emit=emit, issue=7,
        cfg=WatchdogConfig(poll_interval_s=0.05, stuck_after_s=5, kill_after_s=10),
    )
    wd.start()
    time.sleep(0.2)
    wd.stop()

    kinds = [k for k, _ in events]
    assert "watchdog_worker_killed" not in kinds
    assert wd.killed is False
