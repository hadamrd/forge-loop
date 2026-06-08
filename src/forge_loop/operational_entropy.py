"""Pure operational-entropy snapshot (operational-convergence axis).

The forge-loop generates its own exhaust: ``loop/<n>`` branches, leased
worktrees, open epics, and an aging ``loop:ready`` backlog. Those signals are
scattered across :mod:`forge_loop.branch_sweep`, :mod:`forge_loop.worktree_sweep`,
:mod:`forge_loop.epic_sweep`, and the status command, with no single, testable
computation that reduces them to one comparable shape. This module is that
authoritative reduction: a *future convergence gate* (or an operator) reads
:class:`OperationalEntropy` to judge how far the loop has drifted from "clean".

It is pure / no-LLM by construction (issue #413). Like the injectable cores of
:func:`forge_loop.branch_sweep.sweep` and ``worktree_sweep.sweep``, every input
is **injected**: branch names, worktree paths, epic issues, backlog timestamps,
and the ``now`` reference. The function performs **no I/O** — no ``gh``, no git,
no ``time.time()`` internally — so identical inputs always yield an equal
snapshot. The runner/CLI wiring that *feeds* this core is out of scope here and
tracked under the parent epic #412.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from forge_loop.branch_sweep import loop_issue_number


@dataclass(frozen=True)
class OperationalEntropy:
    """One comparable reduction of the loop's self-generated exhaust.

    Frozen + ``eq`` so two snapshots from identical inputs compare equal and a
    snapshot can be used as a dict key / set member. All four fields are simple
    scalars; ``oldest_backlog_age_s`` is ``None`` exactly when the backlog is
    empty (NOT ``0`` — an empty backlog is distinct from a zero-age one).
    """

    open_loop_branches: int
    live_worktrees: int
    open_epics: int
    oldest_backlog_age_s: float | None


def snapshot(
    *,
    branch_names: Iterable[str],
    worktree_paths: Iterable[object],
    open_epics: Iterable[object],
    backlog_created_ts: Iterable[float],
    now: float,
) -> OperationalEntropy:
    """Reduce injected loop-exhaust signals to a frozen :class:`OperationalEntropy`.

    Pure: no git/``gh``/clock reads — ``now`` is the only time reference and it
    is injected. Parameters (all caller-supplied):

    * ``branch_names`` — candidate branch names; only those matching the
      ``loop/<n>`` pattern (via :func:`forge_loop.branch_sweep.loop_issue_number`,
      single-sourcing the definition of a loop branch) are counted, so
      ``feat/x`` / ``main`` / ``trunk`` are excluded.
    * ``worktree_paths`` — in-flight leased worktree paths the caller deems
      live; the function counts what it is given and does NOT re-derive liveness.
    * ``open_epics`` — open epic-labelled issues the caller has already filtered
      by label + state; the function counts them.
    * ``backlog_created_ts`` — created timestamps (same unit as ``now``) of the
      open ``loop:ready`` backlog; ``oldest_backlog_age_s`` is ``now - min(...)``
      over them, or ``None`` when the backlog is empty.
    * ``now`` — reference time in the same unit as ``backlog_created_ts``.
    """
    open_loop_branches = sum(1 for b in branch_names if loop_issue_number(b) is not None)
    live_worktrees = sum(1 for _ in worktree_paths)
    open_epic_count = sum(1 for _ in open_epics)

    timestamps = list(backlog_created_ts)
    oldest_backlog_age_s = (now - min(timestamps)) if timestamps else None

    return OperationalEntropy(
        open_loop_branches=open_loop_branches,
        live_worktrees=live_worktrees,
        open_epics=open_epic_count,
        oldest_backlog_age_s=oldest_backlog_age_s,
    )
