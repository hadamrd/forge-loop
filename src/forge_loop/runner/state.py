"""Runtime state container for the runner (issue #87).

Before: ``runner/boot.py`` and ``runner/drift.py`` shared mutable state via
module globals (``_RUN`` bool, ``_RECENT_OUTCOMES`` deque). Two Runner
instances in the same process trampled each other; signal handlers leaked
state across re-execs; tests couldn't drive concurrent dispatch loops
without monkey-patching the module.

This module ships a tiny container — :class:`RunnerState` — that holds
the shared mutable state per Runner instance. The legacy module
functions read the **default** instance unless an explicit state is
passed, which keeps every existing call site working with zero changes
while opening the door to per-instance state for tests and concurrent
runs.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field

# Drift detector entry shape — (had_workers, all_failed, error_signature).
# Kept here (not in drift.py) so the dataclass field type lives next to
# the data it describes.
DriftOutcome = tuple[bool, bool, str]


@dataclass
class RunnerState:
    """Per-Runner mutable runtime state.

    Owned by exactly one :class:`forge_loop.runner.Runner` instance. The
    module-level legacy globals (``boot._RUN``, ``drift._RECENT_OUTCOMES``)
    now read/write through a default instance for back-compat — but tests
    and any future concurrent-Runner code create their own instance and
    pass it explicitly.

    All fields are mutable on purpose; the dataclass is a state holder,
    not a value object. Thread-safe writes use ``stop_event`` (a
    :class:`threading.Event`) and Python's GIL-backed atomic deque
    operations.
    """

    # Stop flag. A ``threading.Event`` (not a bare bool) so a thread
    # blocking on ``wait()`` wakes up immediately on SIGTERM — important
    # for the ``_short_sleep`` loop that polls between ticks.
    stop_event: threading.Event = field(default_factory=threading.Event)

    # Last N tick outcomes used by the drift detector. Keep maxlen=3 in
    # sync with ``drift._check_drift_and_maybe_halt`` which requires
    # exactly 3 entries before checking.
    recent_outcomes: deque[DriftOutcome] = field(
        default_factory=lambda: deque(maxlen=3)
    )

    @property
    def should_run(self) -> bool:
        """Inverse of stop_event — preserves the legacy ``while _RUN:`` shape."""
        return not self.stop_event.is_set()

    def request_stop(self) -> None:
        """Signal the dispatch loop to exit on the next iteration."""
        self.stop_event.set()

    def clear_for_test(self) -> None:
        """Reset to a fresh state (used by tests that share the default instance)."""
        self.stop_event.clear()
        self.recent_outcomes.clear()


# ---------------------------------------------------------------------------
# Default singleton — the legacy module-level access path. New code should
# construct its own RunnerState and pass it explicitly.
# ---------------------------------------------------------------------------


_default_state = RunnerState()


def get_default_state() -> RunnerState:
    """Return the legacy module-level state singleton.

    Used by the back-compat shims in ``boot.py`` and ``drift.py`` so
    existing module-level calls keep working without thread-through.
    """
    return _default_state


__all__ = ["DriftOutcome", "RunnerState", "get_default_state"]
