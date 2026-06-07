"""Periodic stale-lease reaper for the run loop (issue #325).

Boot recovery (``runner.boot._run_boot_recovery``) reconciles dead-worker
sagas exactly once, at startup. But a worker that silently stops heart-beating
*mid-run* keeps its lease — and therefore its dispatch slot — held until the
next boot or until an operator runs ``forge-loop recover`` by hand. For a long
autonomous session that freezes a slot indefinitely.

This watchdog closes the gap by re-running the SAME stale-lease sweep
(``reconcile_stale_sagas`` via ``runner.boot._lease_reconcile_sweep``) on a
bounded interval from inside the running loop. There is deliberately no new
compensation logic here: the lease TTL and the worktree-reaping compensation
path are reused verbatim; the only addition is *continuous* invocation so a
lapsed lease is detected within one watchdog interval — with no reboot and no
operator ``recover``.

State is per-instance (a ``_last_run`` monotonic stamp), never module-level, so
the loop stays restartable in-process (quality manifesto Q1).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

# The bound sweep: takes no args, runs one reconcile pass, returns its report
# (or ``None`` when there is no saga store, or the pass failed). Kept opaque on
# purpose — the watchdog only decides *when* to fire, never inspects the result.
Sweep = Callable[[], object]
# Monotonic clock, injected so tests drive elapsed time without sleeping.
Clock = Callable[[], float]


@dataclass
class LeaseWatchdog:
    """Fire a stale-lease sweep at most once per ``interval_s`` monotonic seconds.

    ``interval_s <= 0`` disables the watchdog (operator opt-out): it never
    fires. ``clock`` defaults to ``time.monotonic`` but is injectable so tests
    advance time deterministically instead of sleeping.
    """

    interval_s: float
    sweep: Sweep
    clock: Clock = time.monotonic
    _last_run: float = field(init=False)

    def __post_init__(self) -> None:
        # Seed from "now" so the first fire happens one interval AFTER boot.
        # Boot recovery already ran a sweep, so an immediate re-sweep on the
        # first tick would be redundant work on every startup.
        self._last_run = self.clock()

    def maybe_reap(self) -> object | None:
        """Run the sweep iff one interval has elapsed since the last run.

        Returns the sweep result when it fires, else ``None``. Idempotent
        within an interval: repeated calls between fires are cheap no-ops, so
        the run loop can call this every tick regardless of tick cadence.
        """
        if self.interval_s <= 0:
            # Disabled (default/opt-out branch): never fires, never advances.
            return None
        now = self.clock()
        if (now - self._last_run) < self.interval_s:
            return None
        self._last_run = now
        return self.sweep()
