"""Parallel-slot accounting tests for issue #112.

The dispatcher's slot-free count is the per-tick decision: "can I
spawn another worker right now, or am I at the parallel cap?". The
contract under issue #112 is that this decision uses
``WorkerSessionStore.active_count()`` — which excludes paused
``AWAITING_CRITIC`` sessions — instead of an in-memory counter that
would block on those paused sessions and stall forward progress.

These tests pin the matrix from the issue body:
- 5 AWAITING_CRITIC sessions → free slots = parallel.
- parallel=3 with 2 RUNNING + 1 REVISING → free slots = 0.

Plus adversarial cases (negative-budget guard, empty store, terminal
states ignored) so a future refactor of slot accounting fails loudly
instead of silently regressing.
"""

from __future__ import annotations

import pytest

from forge_loop.runner.dispatch import free_dispatch_slots
from forge_loop.worker_sessions import WorkerSessionStore
from forge_loop.worker_state import WorkerState


def _make_store() -> WorkerSessionStore:
    return WorkerSessionStore(":memory:")


# ---------------------------------------------------------------------------
# Issue #112 test matrix — explicitly called out in the acceptance.
# ---------------------------------------------------------------------------


def test_awaiting_critic_sessions_do_not_consume_slots() -> None:
    """5 AWAITING_CRITIC sessions in the store → free count = parallel.

    This is the headline win: paused sessions waiting on the critic
    must not block fresh dispatches.
    """
    store = _make_store()
    for i in range(5):
        sess = store.create(issue=100 + i, branch=f"b{i}")
        store.transition_to(sess.session_id, WorkerState.RUNNING)
        store.transition_to(
            sess.session_id,
            WorkerState.AWAITING_CRITIC,
            reason="pr opened",
        )

    assert free_dispatch_slots(store, parallel=3) == 3
    assert free_dispatch_slots(store, parallel=10) == 10


def test_running_plus_revising_at_cap_yields_zero_slots() -> None:
    """parallel=3, 2 RUNNING + 1 REVISING → 0 free slots."""
    store = _make_store()
    s1 = store.create(issue=1, branch="b1")
    s2 = store.create(issue=2, branch="b2")
    s3 = store.create(issue=3, branch="b3")

    store.transition_to(s1.session_id, WorkerState.RUNNING)
    store.transition_to(s2.session_id, WorkerState.RUNNING)
    # s3: DISPATCHED -> RUNNING -> AWAITING_CRITIC -> REVISING
    store.transition_to(s3.session_id, WorkerState.RUNNING)
    store.transition_to(s3.session_id, WorkerState.AWAITING_CRITIC)
    store.transition_to(s3.session_id, WorkerState.REVISING)

    assert free_dispatch_slots(store, parallel=3) == 0


def test_mixed_states_only_running_and_revising_count() -> None:
    """parallel=4, 1 RUNNING + 1 REVISING + 2 AWAITING_CRITIC + 1
    DISPATCHED → 2 free slots (only RUNNING + REVISING count)."""
    store = _make_store()
    running = store.create(issue=1, branch="b1")
    store.transition_to(running.session_id, WorkerState.RUNNING)

    revising = store.create(issue=2, branch="b2")
    store.transition_to(revising.session_id, WorkerState.RUNNING)
    store.transition_to(revising.session_id, WorkerState.AWAITING_CRITIC)
    store.transition_to(revising.session_id, WorkerState.REVISING)

    for i in (3, 4):
        s = store.create(issue=i, branch=f"b{i}")
        store.transition_to(s.session_id, WorkerState.RUNNING)
        store.transition_to(s.session_id, WorkerState.AWAITING_CRITIC)

    # one untouched DISPATCHED session — also doesn't consume a slot
    store.create(issue=5, branch="b5")

    assert free_dispatch_slots(store, parallel=4) == 2


# ---------------------------------------------------------------------------
# Adversarial / sad-path coverage.
# ---------------------------------------------------------------------------


def test_empty_store_returns_full_capacity() -> None:
    store = _make_store()
    assert free_dispatch_slots(store, parallel=3) == 3


def test_zero_parallel_returns_zero_even_when_idle() -> None:
    """Operator pinning parallel=0 (drain mode) must not return positive
    capacity, even on an empty store."""
    store = _make_store()
    assert free_dispatch_slots(store, parallel=0) == 0


def test_oversubscription_clamps_to_zero_never_negative() -> None:
    """If active_count exceeds the configured parallel (e.g. operator
    lowered the cap mid-run with live workers in flight), the helper
    must not return a negative number — the dispatcher would treat
    that as "infinite free slots" via truthy-int comparisons.
    """
    store = _make_store()
    for i in range(5):
        s = store.create(issue=i, branch=f"b{i}")
        store.transition_to(s.session_id, WorkerState.RUNNING)

    assert free_dispatch_slots(store, parallel=2) == 0


def test_terminal_states_do_not_consume_slots() -> None:
    """MERGED / ABANDONED sessions linger in the store for audit but
    must not pin slots."""
    store = _make_store()
    merged = store.create(issue=1, branch="b1")
    store.transition_to(merged.session_id, WorkerState.RUNNING)
    store.transition_to(merged.session_id, WorkerState.AWAITING_CRITIC)
    store.transition_to(merged.session_id, WorkerState.MERGED)

    abandoned = store.create(issue=2, branch="b2")
    store.transition_to(abandoned.session_id, WorkerState.ABANDONED)

    assert free_dispatch_slots(store, parallel=3) == 3


@pytest.mark.parametrize("parallel", [1, 2, 5, 8])
def test_capacity_scales_with_parallel_setting(parallel: int) -> None:
    """Sanity: with one RUNNING session the free count tracks `parallel - 1`."""
    store = _make_store()
    s = store.create(issue=1, branch="b1")
    store.transition_to(s.session_id, WorkerState.RUNNING)

    assert free_dispatch_slots(store, parallel=parallel) == parallel - 1
