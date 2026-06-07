"""Deterministic worktree garbage-collection (operational-convergence axis).

The loop creates a git worktree per dispatched task under ``worktree_root``. Crashes,
timeouts, and aborted dispatches leave worktrees the per-task reaper misses, so disk
accretes orphaned worktrees (part of the mess the manual cleanup had to clear by hand).

This sweep reconciles worktrees on disk against the AUTHORITATIVE live-lease set
(tasks.db in-flight) — the same primitive used for workers + sagas — and removes any
worktree under ``worktree_root`` that no live lease still owns. It NEVER removes the
main checkout or a worktree backing a live lease. Pure plan (:func:`plan_reap`) +
a thin git remover (:func:`sweep`). Mirrors :mod:`forge_loop.branch_sweep`.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field


def _norm(path: str) -> str:
    return os.path.normpath(path.strip()).rstrip("/")


@dataclass
class WorktreeSweepReport:
    """Result of one :func:`sweep`. Each worktree under root lands in one bucket.

    ``reaped`` — removed (under root, not live, not protected).
    ``kept_live`` — under root but owned by a live in-flight lease (preserved).
    ``errors`` — path → short reason a removal failed.
    Worktrees outside ``worktree_root`` (e.g. the main checkout, dev worktrees
    elsewhere) are never considered and don't appear in any bucket.
    """

    reaped: list[str] = field(default_factory=list)
    kept_live: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def _under_root(path: str, root: str) -> bool:
    p, r = _norm(path), _norm(root)
    return p == r or p.startswith(r + os.sep)


def plan_reap(
    worktree_paths: Iterable[str],
    *,
    live_paths: Iterable[str],
    root: str,
    protected: Iterable[str] = (),
) -> list[str]:
    """Worktrees eligible for reaping: under ``root``, not in ``protected`` (the main
    checkout), and not owned by a live lease. Returns the original path strings."""
    live = {_norm(p) for p in live_paths if p}
    prot = {_norm(p) for p in protected if p}
    out: list[str] = []
    for raw in worktree_paths:
        p = _norm(raw)
        if p in prot or not _under_root(raw, root) or p in live:
            continue
        out.append(raw)
    return out


def sweep(
    remove: Callable[[str], bool],
    worktree_paths: Iterable[str],
    *,
    live_paths: Iterable[str],
    root: str,
    protected: Iterable[str] = (),
) -> WorktreeSweepReport:
    """Reap orphaned worktrees under ``root``. ``remove(path)`` performs the git
    removal and returns True on success. Conservative — see module docstring."""
    paths = list(worktree_paths)
    live = {_norm(p) for p in live_paths if p}
    report = WorktreeSweepReport()
    reapable = set(plan_reap(paths, live_paths=live, root=root, protected=protected))
    for raw in paths:
        if raw in reapable:
            try:
                ok = remove(raw)
            except Exception as ex:  # noqa: BLE001 — best-effort GC, never crash the tick
                report.errors[raw] = str(ex)[:200]
                continue
            if ok:
                report.reaped.append(raw)
            else:
                report.errors[raw] = "remove returned False"
        elif _under_root(raw, root) and _norm(raw) in live:
            report.kept_live.append(raw)
    return report
