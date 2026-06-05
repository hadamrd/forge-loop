"""Tests for the pushed_no_pr misclassification fix + the escalate
loop:ready-removal fix (hot-fix).

Both bugs surfaced dogfooding the brainstormer epic on forge-loop's
own backlog. The CTO observed "loop is not reliable" — the iteration
probe was claiming branches were pushed when they weren't, AND the
escalate_to_human path added loop:needs-human without removing
loop:ready, so the dispatcher kept re-picking the same broken issue
every tick.
"""

from __future__ import annotations

import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest

from forge_loop import gh_issues
from forge_loop.gh_client import MockGhClient
from forge_loop.runner.iteration import (
    WorkerState,
    escalate_to_human,
    probe_worker_state,
)


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


@pytest.fixture(autouse=True)
def _reset_client() -> Generator[None, None, None]:
    gh_issues.set_client(None)
    yield
    gh_issues.set_client(None)


@pytest.fixture()
def worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").mkdir()
    return wt


# ---------------------------------------------------------------------------
# Probe — origin/<branch> existence check
# ---------------------------------------------------------------------------


def test_no_origin_branch_returns_committed_not_pushed_not_pushed_no_pr(worktree: Path) -> None:
    """Headline regression pin — the probe must NOT return PUSHED_NO_PR
    when origin/<branch> doesn't exist. Pre-fix: rev-list failed
    silently, ahead defaulted to 0, returned PUSHED_NO_PR → open_pr
    brief → worker tried to open PR on a branch GitHub couldn't see →
    iteration loop spun forever."""
    gh_issues.set_client(MockGhClient())  # no PR for this branch
    calls: list[list[str]] = []

    def fake_run(args, _cwd):
        calls.append(list(args))
        if args[:2] == ["git", "fetch"]:
            # Branch doesn't exist on origin — fetch fails.
            return _completed("", returncode=128)
        if args[:1] == ["git"] and "rev-parse" in args:
            # The new ref-existence probe — origin ref missing.
            return _completed("", returncode=128)
        if args[:1] == ["git"] and "status" in args:
            return _completed("")  # clean worktree
        if args[:1] == ["git"] and "log" in args:
            return _completed("abc123def456")  # has local commits
        if args[:1] == ["git"] and "rev-list" in args:
            # Defensive: if this were called (it shouldn't, because
            # origin doesn't exist), it'd fail.
            return _completed("", returncode=128)
        return _completed("")

    state, _ = probe_worker_state(worktree, "loop/999-nonexistent", "o/r", 999, run=fake_run)
    # Must be COMMITTED_NOT_PUSHED (the fallback for "local commits but
    # no upstream"), NOT PUSHED_NO_PR.
    assert state == WorkerState.COMMITTED_NOT_PUSHED, (
        f"got {state.value!r}; this is the exact regression that wedged the "
        f"iteration loop on forge-loop #125/#126."
    )


def test_origin_branch_exists_with_local_match_returns_pushed_no_pr(worktree: Path) -> None:
    """Sanity: when origin/<branch> DOES exist and local matches it
    (ahead=0), we still return PUSHED_NO_PR. The fix only narrows
    the misclassification — it doesn't break the legitimate path."""
    gh_issues.set_client(MockGhClient())  # no PR for this branch

    def fake_run(args, _cwd):
        if args[:2] == ["git", "fetch"]:
            return _completed("")
        if args[:1] == ["git"] and "rev-parse" in args:
            # origin/<branch> exists
            return _completed("abc123\n", returncode=0)
        if args[:1] == ["git"] and "status" in args:
            return _completed("")
        if args[:1] == ["git"] and "rev-list" in args:
            return _completed("0")  # local matches origin
        return _completed("")

    state, _ = probe_worker_state(worktree, "loop/1-real", "o/r", 1, run=fake_run)
    assert state == WorkerState.PUSHED_NO_PR


def test_origin_branch_exists_with_local_ahead_returns_committed_not_pushed(worktree: Path) -> None:
    """Sanity: real "ahead by N commits" still routes to push brief."""
    gh_issues.set_client(MockGhClient())  # no PR for this branch

    def fake_run(args, _cwd):
        if args[:2] == ["git", "fetch"]:
            return _completed("")
        if args[:1] == ["git"] and "rev-parse" in args:
            return _completed("abc123\n", returncode=0)
        if args[:1] == ["git"] and "status" in args:
            return _completed("")
        if args[:1] == ["git"] and "rev-list" in args:
            return _completed("3")  # 3 commits ahead
        return _completed("")

    state, _ = probe_worker_state(worktree, "loop/1-real", "o/r", 1, run=fake_run)
    assert state == WorkerState.COMMITTED_NOT_PUSHED


# ---------------------------------------------------------------------------
# escalate_to_human — must REMOVE loop:ready, not just add loop:needs-human
# ---------------------------------------------------------------------------


def test_escalate_removes_loop_ready_in_same_call(worktree: Path) -> None:
    """The headline reliability fix — adding loop:needs-human without
    removing loop:ready meant the dispatcher kept picking the same
    broken issue every tick, forever. The CTO noticed: 'the loop is
    not reliable.' This pin asserts the labels move together via the
    client's ``update_issue`` (add loop:needs-human + remove loop:ready)."""
    client = MockGhClient()
    gh_issues.set_client(client)

    ok = escalate_to_human(
        issue_n=42,
        repo="o/r",
        state=WorkerState.COMMITTED_NOT_PUSHED,
        worktree=worktree,
        pr_url=None,
    )
    assert ok

    update_calls = [c for c in client.calls if c[0] == "update_issue"]
    assert update_calls, "escalate_to_human must call update_issue"
    kwargs = update_calls[0][1]
    assert kwargs["number"] == 42
    assert kwargs["add_labels"] == ["loop:needs-human"]
    assert kwargs["remove_labels"] == ["loop:ready"], (
        "escalate_to_human MUST remove loop:ready — without this the "
        "dispatcher re-picks the issue every tick."
    )


def test_escalate_returns_false_on_client_error(worktree: Path) -> None:
    """Adversarial: a network/API failure during escalation must not
    crash the iteration loop. Returns False so the caller knows the
    operator label wasn't applied."""
    from forge_loop.gh_client import GhError

    client = MockGhClient(raise_on={"update_issue": GhError("update_issue", 503, "network down")})
    gh_issues.set_client(client)

    ok = escalate_to_human(
        issue_n=42,
        repo="o/r",
        state=WorkerState.COMMITTED_NOT_PUSHED,
        worktree=worktree,
        pr_url=None,
    )
    assert ok is False
