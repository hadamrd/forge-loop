"""Tests for the critic ping-pong protocol (issue #110).

Acceptance matrix (from the issue body):

* APPROVE   -> AWAITING_CRITIC -> MERGED.
* REQUEST_CHANGES -> AWAITING_CRITIC -> REVISING (loop) +
  ``critic_iterations`` bumps via ``store.increment_iterations``.
* BLOCK (sev1) -> AWAITING_CRITIC -> ABANDONED + PR labelled
  ``loop:needs-review``.
* The next dispatch prompt contains the critic comments VERBATIM —
  no summarisation, no truncation.

Adversarial coverage:

* unknown/error verdict is a no-op (caller retries the critic).
* an exception from ``gh.add_pr_label`` MUST NOT undo the ABANDONED
  transition (FSM is the source of truth).
* an exception from ``dispatch_revision`` MUST NOT undo the REVISING
  transition or the counter bump.
* calling the handler with a session that is not in AWAITING_CRITIC
  raises ``InvalidTransition`` so caller bugs surface loudly.
"""

from __future__ import annotations

from typing import Any

import pytest

from forge_loop.critic import CriticReport, Finding
from forge_loop.runner.dispatch import (
    NEEDS_REVIEW_LABEL,
    format_critic_followup_prompt,
    handle_critic_verdict,
)
from forge_loop.worker_sessions import WorkerSessionStore
from forge_loop.worker_state import InvalidTransition, WorkerState

# ---------------------------------------------------------------------------
# Stubs / helpers
# ---------------------------------------------------------------------------


class _StubGh:
    def __init__(self, *, raise_on_label: bool = False) -> None:
        self.labels: list[tuple[str, list[str], str | None]] = []
        self._raise_on_label = raise_on_label

    def add_pr_label(
        self,
        pr: str,
        labels: list[str],
        repo: str | None = None,
    ) -> bool:
        if self._raise_on_label:
            raise RuntimeError("simulated gh failure")
        self.labels.append((pr, list(labels), repo))
        return True


def _events() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    sink: list[tuple[str, dict[str, Any]]] = []

    def emit(kind: str, **kw: Any) -> None:
        sink.append((kind, kw))

    return sink, emit


def _seed_awaiting(
    store: WorkerSessionStore,
    *,
    sdk_session_id: str | None = "sdk-abc-123",
) -> str:
    sess = store.create(issue=110, branch="loop/110", worktree_path="/tmp/wt-loop-110")
    store.transition_to(sess.session_id, WorkerState.RUNNING)
    store.transition_to(sess.session_id, WorkerState.AWAITING_CRITIC, reason="pr opened")
    if sdk_session_id:
        store.set_sdk_session_id(sess.session_id, sdk_session_id)
    store.set_pr_url(sess.session_id, "https://github.com/o/r/pull/110")
    return sess.session_id


def _report(
    overall: str,
    *,
    findings: list[Finding] | None = None,
    raw: str = "",
) -> CriticReport:
    return CriticReport(overall=overall, findings=findings or [], raw=raw)


# ---------------------------------------------------------------------------
# APPROVE -> MERGED
# ---------------------------------------------------------------------------


def test_approve_transitions_to_merged() -> None:
    store = WorkerSessionStore(":memory:")
    sid = _seed_awaiting(store)
    events, emit = _events()

    result = handle_critic_verdict(
        store=store,
        session_id=sid,
        report=_report("approve"),
        pr_url="https://github.com/o/r/pull/110",
        gh=_StubGh(),
        emit=emit,
    )

    assert result == "merged"
    sess = store.get(sid)
    assert sess is not None
    assert sess.state == WorkerState.MERGED
    assert sess.last_transition_reason == "critic approved"
    assert any(k == "critic_verdict_merged" for k, _ in events)


# ---------------------------------------------------------------------------
# REQUEST_CHANGES -> REVISING (+ bump + dispatch w/ verbatim prompt)
# ---------------------------------------------------------------------------


def test_request_changes_transitions_to_revising_and_bumps_counter() -> None:
    store = WorkerSessionStore(":memory:")
    sid = _seed_awaiting(store)
    events, emit = _events()

    result = handle_critic_verdict(
        store=store,
        session_id=sid,
        report=_report(
            "request_changes",
            findings=[
                Finding(
                    severity="sev2",
                    category="correctness",
                    file="src/foo.py",
                    line=42,
                    message="off-by-one in loop bound",
                ),
            ],
        ),
        pr_url="https://github.com/o/r/pull/110",
        gh=_StubGh(),
        emit=emit,
    )

    assert result == "revising"
    sess = store.get(sid)
    assert sess is not None
    assert sess.state == WorkerState.REVISING
    assert sess.critic_iterations == 1
    assert sess.last_transition_reason == "critic requested changes"
    kinds = [k for k, _ in events]
    assert "critic_verdict_revising" in kinds


def test_request_changes_dispatches_followup_with_verbatim_critic_comments() -> None:
    """The acceptance contract: the next dispatch prompt MUST contain
    the critic comments verbatim — no summarisation."""
    store = WorkerSessionStore(":memory:")
    sid = _seed_awaiting(store, sdk_session_id="sdk-xyz")
    captured: dict[str, Any] = {}

    findings = [
        Finding(
            severity="sev2",
            category="correctness",
            file="src/foo.py",
            line=42,
            message="precise verbatim message ALPHA",
        ),
        Finding(
            severity="sev3",
            category="style",
            file=None,
            line=None,
            message="precise verbatim message BETA",
        ),
    ]

    def _dispatch(*, session: Any, prompt: str, resume_kwargs: dict[str, str]) -> None:
        captured["session"] = session
        captured["prompt"] = prompt
        captured["resume_kwargs"] = resume_kwargs

    handle_critic_verdict(
        store=store,
        session_id=sid,
        report=_report("request_changes", findings=findings),
        pr_url="https://github.com/o/r/pull/110",
        gh=_StubGh(),
        dispatch_revision=_dispatch,
    )

    # Every finding's message MUST appear verbatim in the follow-up prompt.
    assert "precise verbatim message ALPHA" in captured["prompt"]
    assert "precise verbatim message BETA" in captured["prompt"]
    # File/line and severity tags are also present.
    assert "src/foo.py:42" in captured["prompt"]
    assert "[sev2/correctness]" in captured["prompt"]
    assert "[sev3/style]" in captured["prompt"]
    # The session resumes the SDK session id so the prompt cache is warm.
    assert captured["resume_kwargs"] == {"resume": "sdk-xyz"}
    # The session handed to the dispatcher is the REVISING row.
    assert captured["session"].state == WorkerState.REVISING


def test_format_followup_prompt_preserves_findings_verbatim() -> None:
    """Pure prompt-builder unit test — no FSM involvement."""
    report = _report(
        "request_changes",
        findings=[
            Finding(
                severity="sev1",
                category="security",
                file="x.py",
                line=7,
                message="CWE-89 raw SQL concat — fix before merge",
            ),
        ],
    )
    out = format_critic_followup_prompt(report)
    assert "CWE-89 raw SQL concat — fix before merge" in out
    assert "[sev1/security]" in out
    assert "x.py:7" in out


def test_format_followup_prompt_handles_empty_findings_with_raw() -> None:
    """Adversarial: REQUEST_CHANGES with no findings — fall back to raw."""
    report = _report("request_changes", findings=[], raw="the critic said this")
    out = format_critic_followup_prompt(report)
    assert "the critic said this" in out


# ---------------------------------------------------------------------------
# BLOCK -> ABANDONED + label loop:needs-review
# ---------------------------------------------------------------------------


def test_block_transitions_to_abandoned_and_labels_pr() -> None:
    store = WorkerSessionStore(":memory:")
    sid = _seed_awaiting(store)
    gh = _StubGh()
    events, emit = _events()

    result = handle_critic_verdict(
        store=store,
        session_id=sid,
        report=_report(
            "block",
            findings=[
                Finding(
                    severity="sev1",
                    category="security",
                    file="x.py",
                    line=1,
                    message="SQL injection",
                ),
            ],
        ),
        pr_url="https://github.com/o/r/pull/110",
        gh=gh,
        repo="o/r",
        emit=emit,
    )

    assert result == "abandoned"
    sess = store.get(sid)
    assert sess is not None
    assert sess.state == WorkerState.ABANDONED
    assert sess.last_transition_reason == "critic blocked (sev1)"
    # PR was labelled per spec.
    assert gh.labels == [
        ("https://github.com/o/r/pull/110", [NEEDS_REVIEW_LABEL], "o/r"),
    ]
    assert any(k == "critic_verdict_blocked" for k, _ in events)


def test_block_label_failure_does_not_undo_abandoned_transition() -> None:
    """Robustness — a transient gh failure MUST NOT corrupt the FSM."""
    store = WorkerSessionStore(":memory:")
    sid = _seed_awaiting(store)
    gh = _StubGh(raise_on_label=True)
    events, emit = _events()

    result = handle_critic_verdict(
        store=store,
        session_id=sid,
        report=_report("block"),
        pr_url="https://github.com/o/r/pull/110",
        gh=gh,
        emit=emit,
    )

    assert result == "abandoned"
    assert store.get(sid).state == WorkerState.ABANDONED
    assert any(k == "critic_block_label_failed" for k, _ in events)


def test_block_without_pr_url_skips_label_call() -> None:
    """Adversarial: BLOCK on a session whose PR URL was never set."""
    store = WorkerSessionStore(":memory:")
    sid = _seed_awaiting(store)
    gh = _StubGh()
    result = handle_critic_verdict(
        store=store,
        session_id=sid,
        report=_report("block"),
        pr_url=None,
        gh=gh,
    )
    assert result == "abandoned"
    assert gh.labels == []


# ---------------------------------------------------------------------------
# Adversarial / sad paths
# ---------------------------------------------------------------------------


def test_unknown_verdict_is_a_noop() -> None:
    store = WorkerSessionStore(":memory:")
    sid = _seed_awaiting(store)
    events, emit = _events()
    result = handle_critic_verdict(
        store=store,
        session_id=sid,
        report=_report("error"),
        pr_url=None,
        gh=_StubGh(),
        emit=emit,
    )
    assert result == "noop"
    # Session must remain in AWAITING_CRITIC so the next tick can retry.
    assert store.get(sid).state == WorkerState.AWAITING_CRITIC
    assert any(k == "critic_verdict_unknown" for k, _ in events)


def test_wrong_starting_state_raises_invalid_transition() -> None:
    """Calling the handler outside AWAITING_CRITIC is a caller bug."""
    store = WorkerSessionStore(":memory:")
    sess = store.create(issue=110, branch="loop/110")
    # session is in DISPATCHED, not AWAITING_CRITIC
    with pytest.raises(InvalidTransition):
        handle_critic_verdict(
            store=store,
            session_id=sess.session_id,
            report=_report("approve"),
            pr_url=None,
            gh=_StubGh(),
        )


def test_unknown_session_raises_keyerror() -> None:
    store = WorkerSessionStore(":memory:")
    with pytest.raises(KeyError):
        handle_critic_verdict(
            store=store,
            session_id="does-not-exist",
            report=_report("approve"),
            pr_url=None,
            gh=_StubGh(),
        )


def test_dispatch_failure_does_not_undo_revising_transition() -> None:
    """Robustness — a dispatch_revision crash MUST leave the FSM in
    REVISING with the counter bumped. The next tick can retry."""
    store = WorkerSessionStore(":memory:")
    sid = _seed_awaiting(store)
    events, emit = _events()

    def _boom(**kw: Any) -> None:
        raise RuntimeError("simulated dispatch crash")

    result = handle_critic_verdict(
        store=store,
        session_id=sid,
        report=_report(
            "request_changes",
            findings=[
                Finding(
                    severity="sev2",
                    category="tests",
                    file=None,
                    line=None,
                    message="add a regression test",
                ),
            ],
        ),
        pr_url="https://github.com/o/r/pull/110",
        gh=_StubGh(),
        emit=emit,
        dispatch_revision=_boom,
    )

    assert result == "revising"
    sess = store.get(sid)
    assert sess.state == WorkerState.REVISING
    assert sess.critic_iterations == 1
    assert any(k == "critic_revision_dispatch_failed" for k, _ in events)
