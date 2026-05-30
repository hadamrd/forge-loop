"""Tests for ping-pong cap enforcement (issue #113).

Covers the AWAITING_CRITIC -> (REVISING | ABANDONED) edge that the
persistent-worker FSM uses to bound critic round-trips. The acceptance
contract is: cap = N means N revisions allowed; the (N+1)-th
REQUEST_CHANGES is the abandon signal.

These tests pin three things:
  1. Happy path — under the cap, the session revises and the counter
     bumps.
  2. Sad path / boundary — at the cap, the session abandons, the PR
     gets ``loop:needs-human``, and the last critic findings are
     posted as a comment.
  3. Robustness — gh failures don't undo the FSM transition.
"""

from __future__ import annotations

from typing import Any

import pytest

from forge_loop.runner.dispatch import (
    NEEDS_HUMAN_LABEL,
    enforce_critic_iteration_cap,
)
from forge_loop.worker_sessions import WorkerSessionStore
from forge_loop.worker_state import InvalidTransition, WorkerState

# ---------------------------------------------------------------------------
# Stub gh — records every call so tests can assert exactly what was
# attempted, and lets us simulate transient gh failures by toggling
# ``raise_on``.
# ---------------------------------------------------------------------------


class _StubGh:
    def __init__(self, *, raise_on: set[str] | None = None) -> None:
        self.labels: list[tuple[str, list[str], str | None]] = []
        self.comments: list[tuple[str, str, str | None]] = []
        self._raise_on = raise_on or set()

    def add_pr_label(self, pr: str, labels: list[str], repo: str | None = None) -> bool:
        if "label" in self._raise_on:
            raise RuntimeError("simulated gh label failure")
        self.labels.append((pr, list(labels), repo))
        return True

    def pr_comment(self, pr: str, body: str, repo: str | None = None) -> bool:
        if "comment" in self._raise_on:
            raise RuntimeError("simulated gh comment failure")
        self.comments.append((pr, body, repo))
        return True


def _events() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    sink: list[tuple[str, dict[str, Any]]] = []

    def emit(kind: str, **kw: Any) -> None:
        sink.append((kind, kw))

    return sink, emit


def _seed_awaiting(store: WorkerSessionStore, *, iterations: int) -> str:
    sess = store.create(issue=113, branch="loop/113", worktree_path="/tmp/wt-loop-113")
    store.transition_to(sess.session_id, WorkerState.RUNNING)
    store.transition_to(sess.session_id, WorkerState.AWAITING_CRITIC, reason="pr opened")
    for _ in range(iterations):
        store.increment_iterations(sess.session_id)
    return sess.session_id


# ---------------------------------------------------------------------------
# Happy path — under the cap.
# ---------------------------------------------------------------------------


def test_third_request_changes_with_cap_three_still_revises() -> None:
    """cap=3 means three revisions are allowed. The 3rd REQUEST_CHANGES
    arrives with counter=2 (two prior revisions); it must transition to
    REVISING and bump the counter to 3."""
    store = WorkerSessionStore(":memory:")
    sid = _seed_awaiting(store, iterations=2)
    gh = _StubGh()
    events, emit = _events()

    abandoned = enforce_critic_iteration_cap(
        store=store,
        session_id=sid,
        pr_url="https://github.com/o/r/pull/9",
        max_critic_iterations=3,
        findings_summary="(unused)",
        gh=gh,
        repo="o/r",
        emit=emit,
    )

    assert abandoned is False
    sess = store.get(sid)
    assert sess is not None
    assert sess.state == WorkerState.REVISING
    assert sess.critic_iterations == 3
    assert sess.last_transition_reason == "critic requested changes"
    # No GitHub side effects on the revise path.
    assert gh.labels == []
    assert gh.comments == []
    # An observability event was emitted.
    assert any(k == "critic_iteration_revising" for k, _ in events)


def test_first_request_changes_revises_from_fresh_session() -> None:
    """Boundary on the low end — counter=0 with cap=3 revises cleanly."""
    store = WorkerSessionStore(":memory:")
    sid = _seed_awaiting(store, iterations=0)
    abandoned = enforce_critic_iteration_cap(
        store=store,
        session_id=sid,
        pr_url=None,
        max_critic_iterations=3,
        gh=_StubGh(),
    )
    assert abandoned is False
    sess = store.get(sid)
    assert sess is not None
    assert sess.state == WorkerState.REVISING
    assert sess.critic_iterations == 1


# ---------------------------------------------------------------------------
# Sad path — at the cap, abandon + label + comment.
# ---------------------------------------------------------------------------


def test_fourth_request_changes_with_cap_three_abandons() -> None:
    """The acceptance contract: cap=3 + 4th REQUEST_CHANGES => abandon.

    The session arrives in AWAITING_CRITIC with counter=3 (three prior
    revisions). The check ``counter >= cap`` fires; the session moves
    to ABANDONED with the reason string the issue spec mandates.
    """
    store = WorkerSessionStore(":memory:")
    sid = _seed_awaiting(store, iterations=3)
    gh = _StubGh()
    events, emit = _events()

    abandoned = enforce_critic_iteration_cap(
        store=store,
        session_id=sid,
        pr_url="https://github.com/o/r/pull/42",
        max_critic_iterations=3,
        findings_summary="- sev2 in foo.py:10\n- sev3 in bar.py:55",
        gh=gh,
        repo="o/r",
        emit=emit,
    )

    assert abandoned is True
    sess = store.get(sid)
    assert sess is not None
    assert sess.state == WorkerState.ABANDONED
    # Reason text format is part of the spec — operators / dashboards
    # parse it. Pin it.
    assert sess.last_transition_reason == "max_critic_iterations reached: 3"
    # Counter is preserved (not re-bumped) on the abandon path.
    assert sess.critic_iterations == 3

    # PR labelling — exactly one add_pr_label call with the spec label.
    assert gh.labels == [
        (
            "https://github.com/o/r/pull/42",
            [NEEDS_HUMAN_LABEL],
            "o/r",
        )
    ]
    # PR comment — exactly one, carrying the last critic findings.
    assert len(gh.comments) == 1
    pr_arg, body, repo_arg = gh.comments[0]
    assert pr_arg == "https://github.com/o/r/pull/42"
    assert repo_arg == "o/r"
    assert "sev2 in foo.py:10" in body
    assert "sev3 in bar.py:55" in body
    assert "abandoned" in body.lower()

    # Observability event with the cap context.
    kinds = [k for k, _ in events]
    assert "critic_iteration_cap_abandoned" in kinds


def test_abandon_path_skips_label_and_comment_when_pr_url_missing() -> None:
    """A session that never opened a PR can still hit the cap (e.g. the
    worker pushed a branch but the gh-pr-create call failed). We must
    still abandon the FSM but cannot call GH without a PR url."""
    store = WorkerSessionStore(":memory:")
    sid = _seed_awaiting(store, iterations=3)
    gh = _StubGh()

    abandoned = enforce_critic_iteration_cap(
        store=store,
        session_id=sid,
        pr_url=None,
        max_critic_iterations=3,
        findings_summary="ignored",
        gh=gh,
        repo="o/r",
    )

    assert abandoned is True
    assert store.get(sid).state == WorkerState.ABANDONED
    assert gh.labels == []
    assert gh.comments == []


def test_abandon_path_survives_gh_label_failure() -> None:
    """Transient gh failures must not undo the FSM transition — the
    session is still ABANDONED even if labelling fails. The failure is
    surfaced through ``emit`` for operator visibility."""
    store = WorkerSessionStore(":memory:")
    sid = _seed_awaiting(store, iterations=3)
    gh = _StubGh(raise_on={"label"})
    events, emit = _events()

    abandoned = enforce_critic_iteration_cap(
        store=store,
        session_id=sid,
        pr_url="https://github.com/o/r/pull/1",
        max_critic_iterations=3,
        findings_summary="sev1: data loss",
        gh=gh,
        repo="o/r",
        emit=emit,
    )

    assert abandoned is True
    sess = store.get(sid)
    assert sess is not None
    assert sess.state == WorkerState.ABANDONED
    # Comment still posted even though labelling failed.
    assert len(gh.comments) == 1
    # Failure observable.
    kinds = [k for k, _ in events]
    assert "critic_cap_label_failed" in kinds


# ---------------------------------------------------------------------------
# Adversarial — caller bugs.
# ---------------------------------------------------------------------------


def test_unknown_session_raises_keyerror() -> None:
    store = WorkerSessionStore(":memory:")
    with pytest.raises(KeyError):
        enforce_critic_iteration_cap(
            store=store,
            session_id="ghost",
            pr_url=None,
            max_critic_iterations=3,
            gh=_StubGh(),
        )


def test_wrong_state_raises_invalid_transition() -> None:
    """Calling the cap check on a RUNNING session (i.e. not on the
    AWAITING_CRITIC edge) is a caller bug. Surface it loudly rather
    than silently mutate the FSM into an inconsistent place."""
    store = WorkerSessionStore(":memory:")
    sess = store.create(issue=1, branch="b")
    store.transition_to(sess.session_id, WorkerState.RUNNING)
    with pytest.raises(InvalidTransition):
        enforce_critic_iteration_cap(
            store=store,
            session_id=sess.session_id,
            pr_url=None,
            max_critic_iterations=3,
            gh=_StubGh(),
        )


def test_cap_zero_abandons_immediately_on_first_request_changes() -> None:
    """Operator override path — setting cap=0 means 'no revisions
    allowed; the first REQUEST_CHANGES is fatal.' Pin this so a
    careless ``>`` vs ``>=`` regression is caught."""
    store = WorkerSessionStore(":memory:")
    sid = _seed_awaiting(store, iterations=0)
    gh = _StubGh()
    abandoned = enforce_critic_iteration_cap(
        store=store,
        session_id=sid,
        pr_url="https://github.com/o/r/pull/7",
        max_critic_iterations=0,
        findings_summary="-",
        gh=gh,
        repo="o/r",
    )
    assert abandoned is True
    assert store.get(sid).state == WorkerState.ABANDONED
    assert gh.labels and gh.labels[0][1] == [NEEDS_HUMAN_LABEL]
