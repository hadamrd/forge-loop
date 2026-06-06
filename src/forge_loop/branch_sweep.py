"""Stale-branch sweep — keep the repo's branch list clean (issue #146).

Background
==========

After every merge the loop accumulates branches indefinitely. The auto-merge
path can delete the head branch, but not every merge path does, and a repo
running the loop for a week ends up with hundreds of stale ``loop/*`` /
``feat/*`` / ``fix/*`` branches whose PRs merged or closed long ago. The
operator is left running ``git push origin --delete`` loops by hand — painful
and error-prone (one slip and a still-open branch dies).

This module is the maintenance sweep that closes the gap. It runs on the
``branch_sweep_every_n_ticks`` cadence (and on the manual
``forge-loop sweep branches`` command):

* **Remote sweep** (:func:`sweep_branches`): list remote branches matching the
  loop's naming prefixes, look up each branch's most-recent PR, and delete the
  branch ONLY when its PR is MERGED or CLOSED *and* older than
  ``min_age_days``. A branch with an OPEN PR is NEVER deleted; a branch with no
  PR at all (orphan) is logged and skipped, never deleted.
* **Local sweep** (:func:`sweep_local_branches`): prune local branches in the
  operator's checkout that have no remote tracking ref *and* whose last commit
  is older than ``min_age_days`` (default 30). The current branch and the base
  branch are always protected.

The sweep is conservative on purpose — the cost of leaving a stale branch is
trivial, the cost of deleting a live one is not:

* Every external call is wrapped; a single gh failure records an error and the
  sweep continues with the next branch.
* GitHub rate-limiting (HTTP 403/429) aborts the run early (``rate_limited``)
  rather than hammering the API through the whole branch list.
* Deletion requires an explicit MERGED/CLOSED verdict from the PR lookup —
  "unknown" never deletes.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from forge_loop.gh_client import GhError
from forge_loop.log import get_logger

#: Branch-name prefixes the loop owns. Only branches under one of these are
#: ever considered for deletion — a human's ad-hoc ``wip-foo`` branch is left
#: untouched. Mirrors the conventional-commit areas the worker uses.
DEFAULT_BRANCH_PREFIXES: tuple[str, ...] = (
    "loop/",
    "feat/",
    "fix/",
    "refactor/",
    "chore/",
    "test/",
    "docs/",
)

#: PR states that make a branch eligible for deletion (when old enough).
_DELETABLE_STATES: frozenset[str] = frozenset({"MERGED", "CLOSED"})

#: HTTP statuses that mean "GitHub is rate-limiting us — back off".
_RATE_LIMIT_STATUSES: frozenset[int] = frozenset({403, 429})

RunFn = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]


class GhClientLike(Protocol):
    """Minimal subset of :class:`forge_loop.gh_client.GhClient` the sweep needs.

    Defined locally (mirroring ``stuck_sweep.GhClientLike``) so tests can pass
    a hand-rolled fake. Both production impls (``GithubkitClient`` and the
    in-memory ``MockGhClient``) structurally match.
    """

    def list_branches(self, owner: str, repo: str, *, limit: int = ...) -> list[str]: ...

    def find_pr_by_head(self, owner: str, repo: str, head: str) -> dict[str, Any] | None: ...

    def delete_branch(self, owner: str, repo: str, branch: str) -> bool: ...


@dataclass
class BranchSweepReport:
    """Result of one :func:`sweep_branches` (+ optional local) run.

    ``deleted`` / ``skipped`` list branch names; ``errors`` maps a branch name
    to the failure string so the operator can see what blew up without grepping
    structlog. ``rate_limited`` is True iff the run bailed early on a 403/429.
    ``local_deleted`` lists local branch names pruned by :func:`sweep_local_branches`.
    """

    deleted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    local_deleted: list[str] = field(default_factory=list)
    scanned: int = 0
    rate_limited: bool = False


def _is_rate_limit(exc: Exception) -> bool:
    """True iff ``exc`` is a gh failure that means "rate-limited, back off"."""
    if isinstance(exc, GhError):
        if exc.status in _RATE_LIMIT_STATUSES:
            return True
        return "rate limit" in str(exc).lower()
    return "rate limit" in str(exc).lower()


def _parse_iso(ts: str) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp (``...Z`` or offset). None on junk."""
    if not ts:
        return None
    raw = ts.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _pr_close_age_days(pr: dict[str, Any], now: datetime) -> float | None:
    """Age in days since a MERGED/CLOSED PR was closed. None if unknown.

    Prefers ``mergedAt`` / ``closedAt`` and falls back to ``updatedAt`` so a
    fake/partial PR record still ages out. Returns None when no timestamp
    parses — the caller treats "unknown age" as "do not delete yet".
    """
    for key in ("mergedAt", "closedAt", "updatedAt"):
        dt = _parse_iso(str(pr.get(key) or ""))
        if dt is not None:
            return (now - dt).total_seconds() / 86400.0
    return None


def sweep_branches(
    gh: GhClientLike,
    *,
    owner: str,
    repo: str,
    base_branch: str = "trunk",
    prefixes: tuple[str, ...] = DEFAULT_BRANCH_PREFIXES,
    min_age_days: int = 7,
    now: datetime | None = None,
    limit: int = 200,
) -> BranchSweepReport:
    """Delete remote branches whose PR is merged/closed and old enough.

    Parameters
    ----------
    gh:
        Anything matching :class:`GhClientLike`.
    owner, repo:
        GitHub owner/repo for the API calls.
    base_branch:
        The default branch (``trunk``/``main``) — never deleted even if it
        somehow matches a prefix.
    prefixes:
        Branch-name prefixes to consider. Defaults to :data:`DEFAULT_BRANCH_PREFIXES`.
    min_age_days:
        A merged/closed PR's branch is only deleted once the PR has been
        closed for at least this many days (default 7).
    now:
        Injected "current time" for deterministic tests. Defaults to
        ``datetime.now(timezone.utc)``.
    limit:
        Max branches to fetch from GitHub.

    Returns
    -------
    :class:`BranchSweepReport`. Never raises — gh failures are recorded.
    """
    log = get_logger()
    now = now or datetime.now(UTC)
    report = BranchSweepReport()

    try:
        branches = gh.list_branches(owner, repo, limit=limit)
    except Exception as ex:  # noqa: BLE001 — list failure aborts cleanly, never crashes the tick
        if _is_rate_limit(ex):
            report.rate_limited = True
        report.errors["*"] = f"list_branches: {ex}"[:200]
        log.warning("branch_sweep_list_failed", err=str(ex)[:200])
        return report

    candidates = [
        b for b in branches if b != base_branch and any(b.startswith(p) for p in prefixes)
    ]
    report.scanned = len(candidates)

    for branch in candidates:
        try:
            pr = gh.find_pr_by_head(owner, repo, branch)
        except Exception as ex:  # noqa: BLE001
            if _is_rate_limit(ex):
                # Back off: stop scanning rather than hammering the API.
                report.rate_limited = True
                report.errors[branch] = f"find_pr_by_head: {ex}"[:200]
                log.warning("branch_sweep_rate_limited", branch=branch, err=str(ex)[:200])
                break
            report.errors[branch] = f"find_pr_by_head: {ex}"[:200]
            continue

        if pr is None:
            # Orphan branch — no PR at all. Conservative: never delete here
            # (the local sweep handles truly-dead branches by age). Log + skip.
            report.skipped.append(branch)
            log.info("branch_sweep_orphan_skipped", branch=branch)
            continue

        state = str(pr.get("state") or "").upper()
        if state not in _DELETABLE_STATES:
            # OPEN (or unknown) PR — NEVER delete.
            report.skipped.append(branch)
            continue

        age = _pr_close_age_days(pr, now)
        if age is None or age < min_age_days:
            report.skipped.append(branch)
            continue

        try:
            ok = gh.delete_branch(owner, repo, branch)
        except Exception as ex:  # noqa: BLE001
            if _is_rate_limit(ex):
                report.rate_limited = True
                report.errors[branch] = f"delete_branch: {ex}"[:200]
                log.warning("branch_sweep_rate_limited", branch=branch, err=str(ex)[:200])
                break
            report.errors[branch] = f"delete_branch: {ex}"[:200]
            continue
        if ok:
            report.deleted.append(branch)
        else:
            report.errors[branch] = "delete_branch returned False"

    return report


def _default_run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30)


def sweep_local_branches(
    repo_path: Path,
    *,
    base_branch: str = "trunk",
    prefixes: tuple[str, ...] = DEFAULT_BRANCH_PREFIXES,
    min_age_days: int = 30,
    now: datetime | None = None,
    run: RunFn = _default_run,
) -> list[str]:
    """Delete local branches with no upstream + a stale last commit.

    A branch is pruned when ALL hold: it matches a sweep prefix, it is not the
    current branch or ``base_branch``, it has no remote-tracking ref, and its
    last commit is older than ``min_age_days``. Returns the list of deleted
    branch names. Best-effort: a git failure on one branch never raises.
    """
    log = get_logger()
    now = now or datetime.now(UTC)
    deleted: list[str] = []

    # One cheap call: name, upstream, last-commit-unix per local branch.
    try:
        r = run(
            [
                "git",
                "for-each-ref",
                "--format=%(refname:short)%09%(upstream)%09%(committerdate:unix)",
                "refs/heads/",
            ],
            repo_path,
        )
    except (subprocess.SubprocessError, OSError) as ex:
        log.warning("branch_sweep_local_list_failed", err=str(ex)[:200])
        return deleted
    if r.returncode != 0:
        log.warning("branch_sweep_local_list_failed", err=(r.stderr or "")[:200])
        return deleted

    try:
        head = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo_path)
        current = head.stdout.strip() if head.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError):
        current = ""

    cutoff = now.timestamp() - min_age_days * 86400.0
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name, upstream, committed = parts[0], parts[1], parts[2]
        if not name or name in {current, base_branch}:
            continue
        if not any(name.startswith(p) for p in prefixes):
            continue
        if upstream:  # still tracks a remote ref — leave it for the remote sweep
            continue
        try:
            committed_unix = float(committed)
        except ValueError:
            continue
        if committed_unix >= cutoff:
            continue
        try:
            d = run(["git", "branch", "-D", name], repo_path)
        except (subprocess.SubprocessError, OSError) as ex:
            log.warning("branch_sweep_local_delete_failed", branch=name, err=str(ex)[:200])
            continue
        if d.returncode == 0:
            deleted.append(name)
        else:
            log.warning(
                "branch_sweep_local_delete_failed", branch=name, err=(d.stderr or "")[:200]
            )
    return deleted


__all__ = [
    "DEFAULT_BRANCH_PREFIXES",
    "BranchSweepReport",
    "GhClientLike",
    "sweep_branches",
    "sweep_local_branches",
]
