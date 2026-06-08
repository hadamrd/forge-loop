"""Unit coverage for the pure operational-entropy snapshot (issue #413).

The snapshot reduces four injected loop-exhaust signals — ``loop/<n>`` branch
names, leased worktree paths, open epic issues, and ``loop:ready`` backlog
timestamps — to a frozen :class:`OperationalEntropy`. It performs no I/O, so
these tests are plain functions over hand-rolled inputs (mirroring
``tests/test_branch_sweep.py``): no network, no git, no clock read except the
injected ``now``.

Note on file name: ``tests/test_operational_entropy.py`` is already taken by the
I/O-bound control-plane metric (issue #402, different field names); this pure
core lives here to avoid clobbering that suite.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from forge_loop.operational_entropy import OperationalEntropy, snapshot


def test_happy_path_all_four_fields() -> None:
    """Primary AC: 3 loop branches, 2 worktrees, 1 epic, oldest 3600s ago."""
    now = 10_000.0
    oe = snapshot(
        branch_names=["loop/156", "loop/241-slug", "loop/9-x"],
        worktree_paths=["/tmp/wt-loop-1", "/tmp/wt-loop-2"],
        open_epics=[{"number": 412}],
        backlog_created_ts=[now - 3600.0, now - 100.0],
        now=now,
    )
    assert oe == OperationalEntropy(
        open_loop_branches=3,
        live_worktrees=2,
        open_epics=1,
        oldest_backlog_age_s=3600.0,
    )


def test_branch_filtering_counts_only_loop_names() -> None:
    """Only ``loop/<n>`` names count; feat/main/trunk are excluded."""
    oe = snapshot(
        branch_names=["loop/156", "loop/241-slug", "feat/x", "main", "trunk"],
        worktree_paths=[],
        open_epics=[],
        backlog_created_ts=[],
        now=0.0,
    )
    assert oe.open_loop_branches == 2


def test_empty_backlog_age_is_none_not_zero() -> None:
    """Adversarial/sad path: empty backlog → ``None`` (not ``0``, not a crash)."""
    oe = snapshot(
        branch_names=["loop/1"],
        worktree_paths=["/tmp/wt"],
        open_epics=[object()],
        backlog_created_ts=[],
        now=123.0,
    )
    assert oe.oldest_backlog_age_s is None


def test_oldest_of_many_uses_min_created_ts() -> None:
    """Age is computed from the oldest (min) timestamp, not first/newest."""
    now = 5000.0
    # Oldest is 1000.0 (age 4000), regardless of list order.
    oe = snapshot(
        branch_names=[],
        worktree_paths=[],
        open_epics=[],
        backlog_created_ts=[4900.0, 1000.0, 3000.0],
        now=now,
    )
    assert oe.oldest_backlog_age_s == 4000.0


def test_determinism_identical_inputs_compare_equal() -> None:
    kwargs = dict(
        branch_names=["loop/1", "feat/x"],
        worktree_paths=["/a", "/b"],
        open_epics=[1, 2, 3],
        backlog_created_ts=[100.0, 200.0],
        now=500.0,
    )
    assert snapshot(**kwargs) == snapshot(**kwargs)


def test_dataclass_is_frozen() -> None:
    oe = snapshot(
        branch_names=[],
        worktree_paths=[],
        open_epics=[],
        backlog_created_ts=[],
        now=0.0,
    )
    with pytest.raises(FrozenInstanceError):
        oe.open_loop_branches = 99  # type: ignore[misc]


def test_all_zero_inputs_yield_zeros_and_none() -> None:
    oe = snapshot(
        branch_names=[],
        worktree_paths=[],
        open_epics=[],
        backlog_created_ts=[],
        now=0.0,
    )
    assert oe == OperationalEntropy(
        open_loop_branches=0,
        live_worktrees=0,
        open_epics=0,
        oldest_backlog_age_s=None,
    )
