"""Collect a worker worktree's changed files from git output."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

logger = logging.getLogger(__name__)

GitOutputRunner = Callable[[tuple[str, ...], str], str]


def _changed_path(worktree_path: str, name: str) -> str:
    return os.path.normpath(os.path.abspath(os.path.join(worktree_path, name)))


def worker_changed_paths(
    run_git: GitOutputRunner,
    worktree_path: str,
    *,
    base_ref: str,
) -> tuple[str, ...]:
    """Absolute normalized paths changed in ``worktree_path`` relative to ``base_ref``.

    Uses injected git execution only: tracked modifications come from
    ``git diff --name-only <base_ref>`` and untracked, non-ignored files from
    ``git ls-files --others --exclude-standard``. Any git failure returns an
    empty tuple so collection cannot abort the runner tick.
    """
    cwd = os.fspath(worktree_path)
    try:
        diff = run_git(("git", "diff", "--name-only", base_ref), cwd)
        others = run_git(("git", "ls-files", "--others", "--exclude-standard"), cwd)
    except Exception as exc:  # noqa: BLE001 — best-effort diff collection, never crash the tick
        logger.warning(
            "worker changed-path collection failed for worktree=%s base_ref=%s: %s",
            cwd,
            base_ref,
            exc,
            exc_info=True,
        )
        return ()

    seen: set[str] = set()
    changed: list[str] = []
    for output in (diff, others):
        for name in output.splitlines():
            if not name.strip():
                continue
            path = _changed_path(cwd, name)
            if path in seen:
                continue
            seen.add(path)
            changed.append(path)
    return tuple(changed)
