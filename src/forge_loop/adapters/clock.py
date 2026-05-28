"""Clock adapter — wall + monotonic time + sleep.

Wraps the stdlib ``time`` module so tests can substitute a deterministic
clock without ``monkeypatch.setattr(time, "time", ...)`` gymnastics.

The Protocol is intentionally minimal — three operations cover every
call site found in production code (cooldown checks, drift timestamps,
tick interval sleep). Anything more (datetime, timezone math) stays in
Python's stdlib because it doesn't need test substitution.
"""

from __future__ import annotations

import time as _time
from collections import deque
from typing import Protocol


class Clock(Protocol):
    """Time + sleep contract — injected so tests can fast-forward."""

    def now(self) -> float:
        """Wall-clock seconds since epoch — ``time.time()``."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds — for timing deltas immune to wall clock jumps."""
        ...

    def sleep(self, seconds: float) -> None:
        """Block for ``seconds``. Fake implementations may no-op or advance
        a virtual clock instead of actually sleeping."""
        ...


class SystemClock:
    """Real clock — delegates straight to :mod:`time`."""

    def now(self) -> float:
        return _time.time()

    def monotonic(self) -> float:
        return _time.monotonic()

    def sleep(self, seconds: float) -> None:
        _time.sleep(seconds)


class FakeClock:
    """Deterministic clock for tests.

    Maintains a virtual ``now`` value that ``sleep()`` advances instead
    of actually sleeping. Useful for testing cooldown / drift / retry
    logic that branches on elapsed-time without making the test suite
    slow.

    Usage::

        clock = FakeClock(start=1_700_000_000.0)
        assert clock.now() == 1_700_000_000.0
        clock.sleep(30)
        assert clock.now() == 1_700_000_030.0
        # sleeps are also recorded for assertions:
        assert clock.sleeps == [30]
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        self._monotonic = float(start)
        self.sleeps: deque[float] = deque()

    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError(f"sleep duration must be non-negative, got {seconds}")
        self._now += seconds
        self._monotonic += seconds
        self.sleeps.append(seconds)

    def advance(self, seconds: float) -> None:
        """Advance the virtual clock WITHOUT recording a sleep — useful
        for simulating events that happened "outside" the tested code."""
        if seconds < 0:
            raise ValueError(f"advance must be non-negative, got {seconds}")
        self._now += seconds
        self._monotonic += seconds


__all__ = ["Clock", "FakeClock", "SystemClock"]
