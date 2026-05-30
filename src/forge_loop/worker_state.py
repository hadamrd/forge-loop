"""Worker lifecycle state machine (issue #95).

Foundation for the persistent-worker-session epic. Before #95, each
dispatch is fire-and-forget: spawn SDK → opens PR → exit → critic
runs in a separate session → on REQUEST_CHANGES a *fresh* worker is
dispatched (cold prompt cache, re-reads CONSTITUTION/CLAUDE.md,
re-greps the codebase).

Persistent-worker design routes every worker through an explicit
state machine. The session is preserved across critic round-trips
via the SDK's ``session_id`` resumption, so iteration 2+ benefits
from a warm prompt cache + an in-context understanding of the prior
attempt.

This module defines the FSM only — the SQLite-backed session store
(:mod:`forge_loop.worker_sessions`) persists transitions, and the
runner integration (gated by ``settings.iteration.persistent_worker``)
ships in a follow-up PR.

State diagram::

    DISPATCHED -> RUNNING
    RUNNING    -> AWAITING_CRITIC  (worker opened PR, exited cleanly)
    RUNNING    -> ABANDONED        (worker failed; budget exhausted)
    AWAITING_CRITIC -> REVISING    (critic returned REQUEST_CHANGES)
    AWAITING_CRITIC -> MERGED      (critic approved + CI green + merge)
    AWAITING_CRITIC -> ABANDONED   (critic BLOCK on sev1, or operator abort)
    REVISING        -> AWAITING_CRITIC  (revision opened, exited cleanly)
    REVISING        -> ABANDONED   (revision failed; max-iterations cap)
    MERGED, ABANDONED -> (terminal)
"""

from __future__ import annotations

from enum import Enum


class WorkerState(str, Enum):
    """Discrete states a worker session can occupy.

    :class:`str` inheritance keeps the value JSON-serialisable + lets
    SQLite store the state as a ``TEXT`` column (no enum-to-int
    translation needed at the DB boundary).
    """

    DISPATCHED = "dispatched"
    RUNNING = "running"
    AWAITING_CRITIC = "awaiting_critic"
    REVISING = "revising"
    MERGED = "merged"
    ABANDONED = "abandoned"

    @property
    def is_terminal(self) -> bool:
        """``MERGED`` + ``ABANDONED`` are sinks — no transitions leave them."""
        return self in (WorkerState.MERGED, WorkerState.ABANDONED)

    @property
    def is_active(self) -> bool:
        """``RUNNING`` + ``REVISING`` are the slots that count against
        ``Settings.scheduling.parallel``. ``AWAITING_CRITIC`` is paused —
        no token cost while it waits for the critic verdict, so it
        doesn't consume a parallel slot.
        """
        return self in (WorkerState.RUNNING, WorkerState.REVISING)


# ---------------------------------------------------------------------------
# Transition table. Each (from_state, to_state) pair lives here once;
# attempts to follow a transition not in the table raise an error so
# illegal state graph mutations are caught at the FSM boundary, not by
# the downstream DB or critic logic.
# ---------------------------------------------------------------------------


_ALLOWED: frozenset[tuple[WorkerState, WorkerState]] = frozenset({
    (WorkerState.DISPATCHED, WorkerState.RUNNING),
    (WorkerState.DISPATCHED, WorkerState.ABANDONED),
    (WorkerState.RUNNING, WorkerState.AWAITING_CRITIC),
    (WorkerState.RUNNING, WorkerState.ABANDONED),
    (WorkerState.AWAITING_CRITIC, WorkerState.REVISING),
    (WorkerState.AWAITING_CRITIC, WorkerState.MERGED),
    (WorkerState.AWAITING_CRITIC, WorkerState.ABANDONED),
    (WorkerState.REVISING, WorkerState.AWAITING_CRITIC),
    (WorkerState.REVISING, WorkerState.ABANDONED),
})


class InvalidTransition(ValueError):
    """Raised when ``transition(from_state, to_state)`` is not in :data:`_ALLOWED`.

    Includes the source + target in the message so caller bugs surface
    with full diagnostics, not just a generic ValueError.
    """

    def __init__(self, src: WorkerState, dst: WorkerState) -> None:
        super().__init__(
            f"illegal worker state transition: {src.value} -> {dst.value}"
        )
        self.src = src
        self.dst = dst


def is_allowed(src: WorkerState, dst: WorkerState) -> bool:
    """Pure predicate — useful for guarding transitions in callsite
    branches without try/except."""
    return (src, dst) in _ALLOWED


def transition(src: WorkerState, dst: WorkerState) -> WorkerState:
    """Validate the (src, dst) edge; return ``dst`` on success.

    Identity transitions (e.g. RUNNING -> RUNNING, a "heartbeat") are
    rejected so callers don't accidentally hide a missing state-update
    bug behind a no-op. Use ``is_allowed`` to probe without raising.
    """
    if not is_allowed(src, dst):
        raise InvalidTransition(src, dst)
    return dst


def allowed_next(src: WorkerState) -> frozenset[WorkerState]:
    """Return every state ``src`` can legally transition into.

    Useful for runner code that wants to decide where to go next based
    on outcome — branch on the outcome, then call ``transition(src, choice)``.
    """
    return frozenset(d for (s, d) in _ALLOWED if s == src)


__all__ = [
    "InvalidTransition",
    "WorkerState",
    "allowed_next",
    "is_allowed",
    "transition",
]
