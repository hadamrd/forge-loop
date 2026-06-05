"""Unit tests for ``forge_loop.runner.iteration.probe_worker_state`` (issue #78).

The probe is read-only: ``git`` state via the injectable ``run`` shim, GitHub
state (the PR for a head branch, CI status, the critic report) via the
GhClient. We seed a ``MockGhClient`` for the GitHub side and a fake ``run`` for
git, so every state branch is testable without forking subprocesses or hitting
the network (issue #223).
"""

from __future__ import annotations

import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from forge_loop import gh_issues
from forge_loop.gh_client import MockGhClient
from forge_loop.runner.iteration import (
    NON_LLM_STATES,
    TERMINAL_STATES,
    WorkerState,
    brief_kind_for,
    is_terminal,
    probe_worker_state,
)


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _make_fake_run(
    plan: dict[tuple[str, ...], subprocess.CompletedProcess[str]],
    default: subprocess.CompletedProcess[str] | None = None,
):
    """git-only ``run`` shim keyed by argv prefix (gh state is on the client)."""
    default = default if default is not None else _completed("", 0)

    def _run(args, _cwd):
        for prefix, out in plan.items():
            if tuple(args[: len(prefix)]) == prefix:
                return out
        return default

    return _run


def _seed_pr(
    head: str,
    pr: dict[str, Any] | None,
    *,
    status_failed: bool = False,
    critic_report: str = "",
) -> MockGhClient:
    client = MockGhClient(
        pr_by_head={head: pr} if pr else {},
        pr_status_failed_by_pr={int(pr["number"]): status_failed} if pr else {},
        critic_report_by_pr={int(pr["number"]): critic_report} if pr else {},
    )
    gh_issues.set_client(client)
    return client


@pytest.fixture(autouse=True)
def _reset_client() -> Generator[None, None, None]:
    gh_issues.set_client(None)
    yield
    gh_issues.set_client(None)


@pytest.fixture()
def worktree(tmp_path: Path) -> Path:
    """Create a tmp dir that *looks* like a worktree (exists, has .git)."""
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").mkdir()
    return wt


# ---------------------------------------------------------------------------
# Happy / terminal states
# ---------------------------------------------------------------------------


def test_done_merged_when_pr_state_merged(worktree: Path) -> None:
    _seed_pr(
        "b",
        {
            "url": "https://x/1",
            "number": 1,
            "state": "MERGED",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "isDraft": False,
        },
    )
    state, ctx = probe_worker_state(worktree, "b", "owner/r", 1, run=_make_fake_run({}))
    assert state == WorkerState.DONE_MERGED
    assert ctx.pr_url == "https://x/1"
    assert is_terminal(state)


def test_pr_open_healthy_when_clean_no_critic(worktree: Path) -> None:
    _seed_pr(
        "b",
        {
            "url": "https://x/2",
            "number": 2,
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "isDraft": False,
        },
        status_failed=False,
        critic_report="",
    )
    run = _make_fake_run({("git", "status", "--porcelain"): _completed("")})
    state, _ = probe_worker_state(worktree, "b", "owner/r", 2, run=run)
    assert state == WorkerState.PR_OPEN_HEALTHY
    assert state in NON_LLM_STATES


# ---------------------------------------------------------------------------
# Sad-path / non-terminal states
# ---------------------------------------------------------------------------


def test_pr_open_ci_failed(worktree: Path) -> None:
    _seed_pr(
        "b",
        {
            "url": "https://x/3",
            "number": 3,
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "UNSTABLE",
            "isDraft": False,
        },
        status_failed=True,
    )
    run = _make_fake_run({("git", "status", "--porcelain"): _completed("")})
    state, _ = probe_worker_state(worktree, "b", "owner/r", 3, run=run)
    assert state == WorkerState.PR_OPEN_CI_FAILED


def test_pr_open_blocked_when_critic_report_present(worktree: Path) -> None:
    _seed_pr(
        "b",
        {
            "url": "https://x/4",
            "number": 4,
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "BLOCKED",
            "isDraft": False,
        },
        status_failed=False,
        critic_report="critic-report: sev1: bad regex",
    )
    run = _make_fake_run({("git", "status", "--porcelain"): _completed("")})
    state, ctx = probe_worker_state(worktree, "b", "owner/r", 4, run=run)
    assert state == WorkerState.PR_OPEN_BLOCKED
    assert "sev1" in ctx.critic_report


def test_pr_open_conflict(worktree: Path) -> None:
    _seed_pr(
        "b",
        {
            "url": "https://x/5",
            "number": 5,
            "state": "OPEN",
            "mergeable": "CONFLICTING",
            "mergeStateStatus": "DIRTY",
            "isDraft": False,
        },
    )
    run = _make_fake_run({("git", "status", "--porcelain"): _completed("")})
    state, _ = probe_worker_state(worktree, "b", "owner/r", 5, run=run)
    assert state == WorkerState.PR_OPEN_CONFLICT


def test_dirty_no_commit_when_no_pr_and_worktree_dirty(worktree: Path) -> None:
    _seed_pr("b", None)
    run = _make_fake_run({("git", "status", "--porcelain"): _completed(" M file.py\n")})
    state, _ = probe_worker_state(worktree, "b", "owner/r", 6, run=run)
    assert state == WorkerState.DIRTY_NO_COMMIT


def test_committed_not_pushed(worktree: Path) -> None:
    _seed_pr("b", None)
    run = _make_fake_run(
        {
            # origin/<branch> exists (new probe added by the pushed_no_pr
            # misclassification hot-fix); rev-list returns 2 ahead.
            ("git", "rev-parse", "--verify"): _completed("abc123\n"),
            ("git", "status", "--porcelain"): _completed(""),
            ("git", "rev-list", "--count"): _completed("2\n"),
        }
    )
    state, _ = probe_worker_state(worktree, "b", "owner/r", 7, run=run)
    assert state == WorkerState.COMMITTED_NOT_PUSHED


def test_pushed_no_pr(worktree: Path) -> None:
    _seed_pr("b", None)
    run = _make_fake_run(
        {
            # origin/<branch> exists AND local matches it (ahead=0).
            ("git", "rev-parse", "--verify"): _completed("abc123\n"),
            ("git", "status", "--porcelain"): _completed(""),
            ("git", "rev-list", "--count"): _completed("0\n"),
        }
    )
    state, _ = probe_worker_state(worktree, "b", "owner/r", 8, run=run)
    assert state == WorkerState.PUSHED_NO_PR


def test_clean_nothing_when_worktree_missing(tmp_path: Path) -> None:
    """Adversarial: worktree directory doesn't exist (got reaped or never created)."""
    missing = tmp_path / "does-not-exist"
    state, ctx = probe_worker_state(missing, "b", "owner/r", 9)
    assert state == WorkerState.CLEAN_NOTHING
    assert ctx.pr_url is None


def test_gh_failure_degrades_gracefully(worktree: Path) -> None:
    """Adversarial: gh subprocess errors → fall through, don't crash.

    After the pushed_no_pr hot-fix, the probe is more conservative when
    origin/<branch> can't be verified. With no PR seen AND no proof of
    a remote ref AND no local commits, we return CLEAN_NOTHING. This
    is SAFER than the old "guess PUSHED_NO_PR" verdict — the worker
    won't try to open a PR on a branch that doesn't exist remotely."""

    from forge_loop.gh_client import GhError

    # Simulate the GitHub API being down: find_pr_by_head raises -> the probe
    # degrades to "no PR seen" without crashing.
    gh_issues.set_client(
        MockGhClient(raise_on={"find_pr_by_head": GhError("find_pr_by_head", 503, "down")})
    )

    def _run(args, _cwd):
        if args[0] == "git" and args[1] == "status":
            return _completed("")
        if args[0] == "git" and args[1] == "rev-list":
            return _completed("0\n")
        return _completed("")

    state, _ = probe_worker_state(worktree, "b", "owner/r", 10, run=_run)
    assert state == WorkerState.CLEAN_NOTHING


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_brief_kind_for_maps_every_non_terminal_state() -> None:
    for s in WorkerState:
        if s in TERMINAL_STATES:
            assert brief_kind_for(s) is None
        elif s in NON_LLM_STATES:
            assert brief_kind_for(s) == "enable_automerge"
        else:
            kind = brief_kind_for(s)
            assert kind is not None and kind, f"missing brief kind for {s}"


def test_terminal_set_includes_done_merged_and_closed_pr_abandoned() -> None:
    """Terminal members: DONE_MERGED (happy path) + CLOSED_PR_ABANDONED
    (prior attempt thrown away — added with the iteration push-forever
    fix). CLEAN_NOTHING is NOT terminal because it must allow re-attempt
    when the worker exited without producing any state."""
    assert WorkerState.DONE_MERGED in TERMINAL_STATES
    assert WorkerState.CLOSED_PR_ABANDONED in TERMINAL_STATES
    assert WorkerState.CLEAN_NOTHING not in TERMINAL_STATES
