"""Worktree and trust-settings plumbing for worker sessions."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from forge_loop.precommit import (
    PreCommitInstallMethod,
    PreCommitRunner,
    ensure_worker_precommit_hook,
)
from forge_loop.sandbox import CapabilityPolicy, policy_hash

# Operator-trusted contexts (critic/PO subagents running against the operator's
# OWN checkout via ``ensure_subagent_trusted``) keep the historical permissive
# blob — they are not leased workers and run with the operator's full surface.
# Worker WORKTREES no longer use this; they get a deny-by-default settings file
# rendered from their CapabilityPolicy lease (see ``render_worker_settings``).
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

# Read-capable tools scoped to ``read_roots`` and write-capable tools scoped to
# ``write_roots``. Bash is intentionally NOT path-templated: Claude's Bash
# permission grammar matches command strings, not paths, so a path glob would
# silently match nothing. We instead gate Bash on the presence of any write
# grant (a worker with write access must run git/tests) and rely on the SDK
# sandbox profile (see ``worker_permissions``) for host-level Bash confinement.
_READ_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob")
_WRITE_TOOLS: tuple[str, ...] = ("Write", "Edit")


def _mcp_allow_entries(policy: CapabilityPolicy) -> list[str]:
    """Allow-list entries for the granted MCP servers/tools.

    A grant with no tools (or an explicit ``*``) yields ``mcp__<server>__*``;
    a tool allowlist yields one ``mcp__<server>__<tool>`` per tool. A server
    that is not in ``policy.mcp`` produces NO entry — never a blanket
    ``mcp__*``.
    """
    entries: list[str] = []
    for grant in policy.mcp:
        server = grant.server
        if not server:
            continue
        tools = tuple(tool for tool in grant.tools if tool)
        if not tools or "*" in tools:
            entries.append(f"mcp__{server}__*")
            continue
        entries.extend(f"mcp__{server}__{tool}" for tool in tools)
    return entries


def _fs_allow_entries(policy: CapabilityPolicy) -> list[str]:
    """Allow-list entries scoping Read/Grep/Glob and Write/Edit to roots."""
    entries: list[str] = []
    for root in policy.filesystem.write_roots:
        if not root:
            continue
        glob = f"{root.rstrip('/')}/**"
        entries.extend(f"{tool}({glob})" for tool in _WRITE_TOOLS)
    if any(root for root in policy.filesystem.write_roots):
        # See ``_WRITE_TOOLS`` note: Bash cannot be path-scoped in settings, so
        # we grant it only when the lease includes write access at all.
        entries.append("Bash(*)")
    for root in policy.filesystem.read_roots:
        if not root:
            continue
        glob = f"{root.rstrip('/')}/**"
        entries.extend(f"{tool}({glob})" for tool in _READ_TOOLS)
    return entries


def render_worker_settings(policy: CapabilityPolicy) -> str:
    """Render a deny-by-default ``.claude/settings.json`` from ``policy``.

    The effective tool/path/server surface EQUALS the lease (#200):

    * ``defaultMode`` is ``"default"`` — never ``bypassPermissions``.
    * ``allow`` lists only what ``policy`` grants — MCP servers/tools and
      filesystem roots. An empty :class:`CapabilityPolicy` renders an empty
      ``allow`` (fail safe, not open).
    * ``deny`` stays empty: confinement comes from the absence of grants in
      ``allow`` plus ``defaultMode`` requiring approval for anything else.
    """
    allow = _mcp_allow_entries(policy) + _fs_allow_entries(policy)
    settings = {
        "permissions": {
            "defaultMode": "default",
            "allow": allow,
            "deny": [],
        },
        "hasTrustDialogAccepted": True,
        "hasCompletedProjectOnboarding": True,
    }
    return json.dumps(settings, indent=2, sort_keys=True) + "\n"


def worktree_base(repo: Path) -> Path:
    """Per-repo worktree root: ``/tmp/forge-<repo-dir-name>/``.

    Worker worktrees are namespaced by repo so two loops running against
    different checkouts never share a ``wt-loop-<issue>`` path or reap each
    other's in-flight worktrees at boot. The issue number alone is NOT
    unique across repositories (repo A's #155 and repo B's #155 would have
    collided under the old flat ``/tmp/wt-loop-<issue>`` scheme), and the
    boot-time orphan reaper globbed ``/tmp/wt-loop-*`` indiscriminately —
    so starting one loop wiped another loop's live worktrees.
    """
    return Path("/tmp") / f"forge-{repo.name}"


def worktree_path(repo: Path, issue: int | str) -> Path:
    """Canonical worktree path for ``issue`` under ``repo``'s namespace."""
    return worktree_base(repo) / f"wt-loop-{issue}"


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


def plant_worker_settings(
    worktree: Path,
    capability_policy: CapabilityPolicy | None,
    *,
    events_file: Path | None = None,
) -> None:
    """Plant the read-only deny-by-default worker trust file (#200).

    Supersedes the old ``drop_permissive_settings`` blob. The settings are
    rendered from ``capability_policy`` so the worktree's effective surface
    equals the lease. A ``None`` policy falls back to an empty
    :class:`CapabilityPolicy` — a CLOSED file, never the old permissive blob
    (fail safe, not open).

    The file stays read-only (``0o444``) and its ``.claude`` dir ``0o555`` so
    the worker can't widen its own grant. When ``events_file`` is provided, a
    typed :class:`~forge_loop.events.WorkerPolicyEnforcedEvent` is appended so
    boot/replay can confirm the worker ran within its grant.
    """
    policy = capability_policy or CapabilityPolicy()
    cdir = worktree / ".claude"
    cdir.mkdir(parents=True, exist_ok=True)
    settings_path = cdir / "settings.json"
    settings_path.write_text(render_worker_settings(policy))
    settings_path.chmod(0o444)
    cdir.chmod(0o555)
    _emit_worker_policy_event(events_file, worktree, policy)


def _emit_worker_policy_event(
    events_file: Path | None,
    worktree: Path,
    policy: CapabilityPolicy,
) -> None:
    if events_file is None:
        return
    from forge_loop.events import WorkerPolicyEnforcedEvent, emit

    emit(
        events_file,
        WorkerPolicyEnforcedEvent(
            worktree_path=str(worktree),
            policy_hash=policy_hash(policy),
        ),
    )


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
    capability_policy: CapabilityPolicy | None = None,
    events_file: Path | None = None,
) -> tuple[Path, str | None]:
    wt = worktree_path(repo, n)
    wt.parent.mkdir(parents=True, exist_ok=True)
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
        plant_worker_settings(wt, capability_policy, events_file=events_file)
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
    capability_policy: CapabilityPolicy | None = None,
    events_file: Path | None = None,
) -> tuple[Path, str | None]:
    wt = worktree_path(repo, issue)
    wt.parent.mkdir(parents=True, exist_ok=True)
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
        plant_worker_settings(wt, capability_policy, events_file=events_file)
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
    _emit_worker_precommit_event(emit, worktree, method, reason)


def _emit_worker_precommit_event(
    emit: Callable[[str, dict[str, Any]], None] | None,
    worktree: Path,
    method: PreCommitInstallMethod,
    reason: str | None,
) -> None:
    if emit is None:
        return
    from forge_loop.events import WorkerPreCommitInstalledEvent

    event = WorkerPreCommitInstalledEvent.model_validate(
        {
            "worktree_path": str(worktree),
            "method": method,
            "reason": reason,
        }
    )
    payload = event.model_dump(mode="json", exclude_none=True)
    emit("worker_precommit_installed", payload)


def _remove_existing_worktree(repo: Path, wt: Path) -> None:
    claude_dir = wt / ".claude"
    if claude_dir.exists():
        subprocess.run(["chmod", "-R", "u+w", str(claude_dir)], capture_output=True)
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo, capture_output=True)
    if wt.exists():
        with contextlib.suppress(OSError, PermissionError):
            shutil.rmtree(wt)
