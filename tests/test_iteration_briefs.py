"""Unit tests for ``next_brief`` — the state→brief router (issue #78).

Each non-terminal state must produce a focused, IMPERATIVE brief. Terminal /
non-LLM states must return ``None`` so the caller doesn't dispatch.
"""

from __future__ import annotations

from forge_loop.runner.iteration import (
    NON_LLM_STATES,
    STATE_TO_BRIEF_KIND,
    TERMINAL_STATES,
    WorkerState,
    next_brief,
)

_ISSUE = {"number": 78, "title": "feat: do the thing", "body": "Build X to do Y."}


def test_done_merged_returns_none() -> None:
    """Terminal: no follow-up dispatch."""
    assert next_brief(WorkerState.DONE_MERGED, None, "", _ISSUE) is None


def test_pr_open_healthy_returns_none() -> None:
    """Non-LLM state: caller calls ``gh pr merge --auto`` directly."""
    assert next_brief(WorkerState.PR_OPEN_HEALTHY, None, "", _ISSUE) is None


def test_dirty_no_commit_brief_says_commit() -> None:
    brief = next_brief(WorkerState.DIRTY_NO_COMMIT, None, "", _ISSUE, attempt=2)
    assert brief is not None
    assert "git commit" in brief.lower() or "commit" in brief.lower()
    assert "#78" in brief
    assert "attempt 2" in brief.lower()


def test_committed_not_pushed_brief_says_push() -> None:
    brief = next_brief(WorkerState.COMMITTED_NOT_PUSHED, None, "", _ISSUE)
    assert brief is not None
    assert "push" in brief.lower()


def test_pushed_no_pr_brief_says_pr_create() -> None:
    brief = next_brief(WorkerState.PUSHED_NO_PR, None, "", _ISSUE)
    assert brief is not None
    assert "gh pr create" in brief.lower()


def test_pr_open_blocked_brief_includes_critic_report() -> None:
    critic = "sev1: missing null check at foo.py:42"
    brief = next_brief(
        WorkerState.PR_OPEN_BLOCKED, None, critic, _ISSUE, pr_url="https://github.com/o/r/pull/78"
    )
    assert brief is not None
    assert "critic" in brief.lower()
    assert "sev1" in brief


def test_pr_open_ci_failed_brief_says_green_ci() -> None:
    brief = next_brief(
        WorkerState.PR_OPEN_CI_FAILED, None, "", _ISSUE, pr_url="https://github.com/o/r/pull/78"
    )
    assert brief is not None
    assert "ci" in brief.lower()
    assert "pr checks" in brief.lower()


def test_pr_open_conflict_brief_says_resolve() -> None:
    brief = next_brief(
        WorkerState.PR_OPEN_CONFLICT,
        None,
        "",
        _ISSUE,
        base_branch="main",
        pr_url="https://github.com/o/r/pull/78",
    )
    assert brief is not None
    assert "merge" in brief.lower() or "rebase" in brief.lower()
    assert "main" in brief


def test_clean_nothing_brief_restates_issue_body() -> None:
    """If the worker did nothing, the follow-up brief must include the issue body."""
    brief = next_brief(WorkerState.CLEAN_NOTHING, None, "", _ISSUE)
    assert brief is not None
    assert "Build X to do Y." in brief
    assert "ship" in brief.lower()


def test_every_non_terminal_state_renders() -> None:
    """Adversarial: defend against new states being added without a template."""
    for s in WorkerState:
        if s in TERMINAL_STATES or s in NON_LLM_STATES:
            continue
        kind = STATE_TO_BRIEF_KIND[s]
        brief = next_brief(s, None, "x", _ISSUE)
        assert brief is not None, f"{s} → {kind} returned None"
        assert len(brief) < 4000, f"{s} brief too long ({len(brief)} chars) — must be focused"


def test_brief_is_short_and_imperative() -> None:
    """Spec: per-state briefs are ~10 lines, IMPERATIVE 'Your ONLY job is X'."""
    brief = next_brief(WorkerState.PUSHED_NO_PR, None, "", _ISSUE)
    assert brief is not None
    # Imperative cue.
    assert "only job" in brief.lower()
    # Short: <20 non-empty lines.
    non_empty = [ln for ln in brief.splitlines() if ln.strip()]
    assert len(non_empty) < 20


def test_attempt_count_propagates_into_brief() -> None:
    brief = next_brief(WorkerState.DIRTY_NO_COMMIT, None, "", _ISSUE, attempt=3, max_attempts=3)
    assert brief is not None
    assert "attempt 3 of 3" in brief.lower()
