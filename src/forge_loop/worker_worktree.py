"""Worktree and trust-settings plumbing for worker sessions."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from forge_loop.precommit import (
    PreCommitRunner,
    ensure_worker_precommit_hook,
)

_PERMISSIVE_WORKTREE_SETTINGS = """{
  "permissions": {
    "defaultMode": "bypassPermissions",
    "allow": ["Bash(*)", "Edit(*)", "Write(*)", "Read(*)", "Grep(*)", "Glob(*)", "WebFetch(*)", "WebSearch(*)", "Task(*)", "TodoWrite(*)", "NotebookEdit(*)", "mcp__*"],
    "deny": []
  },
  "hasTrustDialogAccepted": true,
  "hasCompletedProjectOnboarding": true
}
"""


def ensure_subagent_trusted(target_dir: Path) -> None:
    """Plant `.claude/settings.json` in target_dir if missing."""
    cdir = target_dir / ".claude"
    settings_path = cdir / "settings.json"
    if settings_path.exists():
        return
    cdir.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(_PERMISSIVE_WORKTREE_SETTINGS)


def subagent_env() -> dict[str, str]:
    """Env for spawned claude subagents with nested-Claude markers removed."""
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_SSE_PORT", None)
    return env


def drop_permissive_settings(worktree: Path) -> None:
    """Plant a read-only trust file in the worktree for worker subprocesses."""
    cdir = worktree / ".claude"
    cdir.mkdir(parents=True, exist_ok=True)
    settings_path = cdir / "settings.json"
    settings_path.write_text(_PERMISSIVE_WORKTREE_SETTINGS)
    settings_path.chmod(0o444)
    cdir.chmod(0o555)


def quarantine_if_blocking(wt: Path) -> Path | None:
    """Rename a stale worktree dir out of the way when normal cleanup failed."""
    if not wt.exists():
        return None
    quarantine = wt.with_name(f"{wt.name}.stale-{int(time.time())}")
    try:
        wt.rename(quarantine)
    except OSError:
        return None
    return quarantine


def prep_worktree(
    repo: Path,
    n: int,
    branch: str,
    *,
    base_branch: str = "trunk",
    emit: Callable[[str, dict[str, Any]], None] | None = None,
    precommit_runner: PreCommitRunner | None = None,
) -> tuple[Path, str | None]:
    wt = Path(f"/tmp/wt-loop-{n}")
    _remove_existing_worktree(repo, wt)
    quarantine_if_blocking(wt)
    subprocess.run(["git", "branch", "-D", branch], cwd=repo, capture_output=True)
    remote_ref = f"refs/remotes/origin/{base_branch}"
    subprocess.run(
        ["git", "fetch", "--prune", "origin", f"+refs/heads/{base_branch}:{remote_ref}"],
        cwd=repo,
        capture_output=True,
    )
    r = subprocess.run(
        ["git", "worktree", "add", str(wt), "-B", branch, f"origin/{base_branch}"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return wt, r.stderr
    if wt.exists():
        drop_permissive_settings(wt)
        _install_and_emit_worker_precommit_hook(
            repo, wt, emit=emit, precommit_runner=precommit_runner
        )
    return wt, None


def prep_repair_worktree(
    repo: Path,
    issue: int,
    branch: str,
    *,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
    precommit_runner: PreCommitRunner | None = None,
) -> tuple[Path, str | None]:
    wt = Path(f"/tmp/wt-loop-{issue}")
    _remove_existing_worktree(repo, wt)
    quarantine_if_blocking(wt)
    remote_ref = f"refs/remotes/origin/{branch}"
    subprocess.run(
        ["git", "fetch", "--prune", "origin", f"+refs/heads/{branch}:{remote_ref}"],
        cwd=repo,
        capture_output=True,
    )
    r = subprocess.run(
        ["git", "worktree", "add", str(wt), "-B", branch, f"origin/{branch}"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return wt, r.stderr
    if wt.exists():
        drop_permissive_settings(wt)
        _install_and_emit_worker_precommit_hook(
            repo, wt, emit=emit, precommit_runner=precommit_runner
        )
    return wt, None


def _install_and_emit_worker_precommit_hook(
    repo: Path,
    worktree: Path,
    *,
    emit: Callable[[str, dict[str, Any]], None] | None,
    precommit_runner: PreCommitRunner | None,
) -> None:
    method, reason = ensure_worker_precommit_hook(
        repo,
        worktree,
        runner=precommit_runner,
    )
    _emit_worker_precommit_event(emit, worktree, method.value, reason)


def _emit_worker_precommit_event(
    emit: Callable[[str, dict[str, Any]], None] | None,
    worktree: Path,
    method: str,
    reason: str | None,
) -> None:
    if emit is None:
        return
    payload: dict[str, Any] = {"worktree_path": str(worktree), "method": method}
    if reason:
        payload["reason"] = reason
    emit("worker_precommit_installed", payload)


def _remove_existing_worktree(repo: Path, wt: Path) -> None:
    claude_dir = wt / ".claude"
    if claude_dir.exists():
        subprocess.run(["chmod", "-R", "u+w", str(claude_dir)], capture_output=True)
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo, capture_output=True)
    if wt.exists():
        with contextlib.suppress(OSError, PermissionError):
            shutil.rmtree(wt)
