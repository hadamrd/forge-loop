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

import json
import subprocess
from pathlib import Path

import pytest

from forge_loop.runner.iteration import (
    WorkerState,
    escalate_to_human,
    probe_worker_state,
)


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


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
    calls: list[list[str]] = []

    def fake_run(args, _cwd):
        calls.append(list(args))
        if args[:3] == ["gh", "pr", "list"]:
            return _completed("[]")  # no PR
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
    def fake_run(args, _cwd):
        if args[:3] == ["gh", "pr", "list"]:
            return _completed("[]")
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
    def fake_run(args, _cwd):
        if args[:3] == ["gh", "pr", "list"]:
            return _completed("[]")
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
    not reliable.' This pin asserts the labels move together."""
    invocations: list[list[str]] = []

    def fake_run(args, _cwd):
        invocations.append(list(args))
        return _completed("ok", returncode=0)

    ok = escalate_to_human(
        issue_n=42,
        repo="o/r",
        state=WorkerState.COMMITTED_NOT_PUSHED,
        worktree=worktree,
        pr_url=None,
        run=fake_run,
    )
    assert ok

    # The gh issue edit invocation must carry BOTH --add-label
    # loop:needs-human AND --remove-label loop:ready in the same call,
    # so the dispatcher sees a consistent state on the next tick.
    edit_call = next(
        (c for c in invocations if c[:3] == ["gh", "issue", "edit"]),
        None,
    )
    assert edit_call is not None, "escalate_to_human must call gh issue edit"
    assert "--add-label" in edit_call
    assert "loop:needs-human" in edit_call
    assert "--remove-label" in edit_call, (
        "escalate_to_human MUST remove loop:ready — without this the "
        "dispatcher re-picks the issue every tick."
    )
    assert "loop:ready" in edit_call


def test_escalate_returns_false_on_subprocess_error(worktree: Path) -> None:
    """Adversarial: a network/gh failure during escalation must not
    crash the iteration loop. Returns False so the caller knows the
    operator label wasn't applied."""
    def fake_run(args, _cwd):
        raise subprocess.SubprocessError("network down")

    ok = escalate_to_human(
        issue_n=42,
        repo="o/r",
        state=WorkerState.COMMITTED_NOT_PUSHED,
        worktree=worktree,
        pr_url=None,
        run=fake_run,
    )
    assert ok is False
