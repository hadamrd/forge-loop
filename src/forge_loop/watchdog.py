"""Worker liveness watchdog — detect + kill stuck claude-code subagents.

Runs as a thread alongside each spawned worker. Polls the worker's
``sprint-events.jsonl`` and the worker subprocess' log file every
``poll_interval_s`` seconds. If neither has progressed in ``stuck_after_s``
seconds, kills the worker subprocess (SIGTERM, then SIGKILL after 10s).

Emits events so the master log shows what happened:
- ``watchdog_started``
- ``watchdog_worker_stuck`` (soft warning)
- ``watchdog_worker_killed`` (hard kill)
- ``watchdog_stopped``
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class WatchdogConfig:
    poll_interval_s: float = 15.0
    # Content-grade work needs room to breathe. A worker running a real
    # integration test or thinking-through a multi-file feature can sit
    # quietly for several minutes between log appends. 10/15min strikes
    # the balance: catches genuine hangs without killing legitimate work.
    stuck_after_s: float = 600.0   # 10 min of no progress → warn
    kill_after_s: float = 900.0    # 15 min of no progress → SIGTERM + SIGKILL


def _last_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class WorkerWatchdog:
    """Threaded watchdog for one worker subprocess."""

    def __init__(
        self,
        *,
        proc: subprocess.Popen[bytes],
        worktree: Path,
        log_path: Path,
        emit: Callable[[str, dict[str, Any]], None],
        issue: int,
        cfg: WatchdogConfig | None = None,
    ) -> None:
        self._proc = proc
        self._worktree = worktree
        self._log_path = log_path
        self._emit = emit
        self._issue = issue
        self._cfg = cfg or WatchdogConfig()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._stop = threading.Event()
        self.killed = False
        self.warnings = 0

    def start(self) -> None:
        self._emit("watchdog_started", {
            "issue": self._issue,
            "worktree": str(self._worktree),
            "poll_interval_s": self._cfg.poll_interval_s,
            "stuck_after_s": self._cfg.stuck_after_s,
            "kill_after_s": self._cfg.kill_after_s,
        })
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)
        self._emit("watchdog_stopped", {"issue": self._issue, "killed": self.killed})

    def _progress_marker(self) -> float:
        """Most recent activity timestamp across worker outputs."""
        events_path = self._worktree / "sprint-events.jsonl"
        return max(_last_mtime(events_path), _last_mtime(self._log_path))

    def _run(self) -> None:
        last_activity = self._progress_marker() or time.time()
        warned = False

        while not self._stop.is_set():
            if self._proc.poll() is not None:
                return  # worker exited on its own

            now = time.time()
            mark = self._progress_marker()
            if mark > last_activity:
                last_activity = mark
                warned = False

            idle = now - last_activity

            if idle > self._cfg.kill_after_s and not self.killed:
                self.killed = True
                self._emit("watchdog_worker_killed", {
                    "issue": self._issue, "idle_s": round(idle, 1),
                    "kill_after_s": self._cfg.kill_after_s,
                })
                self._kill_worker()
                return

            if idle > self._cfg.stuck_after_s and not warned:
                warned = True
                self.warnings += 1
                self._emit("watchdog_worker_stuck", {
                    "issue": self._issue, "idle_s": round(idle, 1),
                    "stuck_after_s": self._cfg.stuck_after_s,
                })

            if self._stop.wait(self._cfg.poll_interval_s):
                return

    def _kill_worker(self) -> None:
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        except Exception:
            # Process may already be dead.
            pass
