"""Critic verdict handling for persistent worker dispatch."""

from __future__ import annotations

import contextlib
from typing import Any

from forge_loop import gh_issues as _gh
from forge_loop.worker_sessions import WorkerSessionStore
from forge_loop.worker_state import InvalidTransition, WorkerState

NEEDS_HUMAN_LABEL = "loop:needs-human"
NEEDS_REVIEW_LABEL = "loop:needs-review"


def enforce_critic_iteration_cap(
    *,
    store: WorkerSessionStore,
    session_id: str,
    pr_url: str | None,
    max_critic_iterations: int,
    findings_summary: str = "",
    gh: Any = _gh,
    repo: str | None = None,
    emit: Any = None,
) -> bool:
    """Handle a REQUEST_CHANGES verdict against the iteration cap."""
    sess = store.get(session_id)
    if sess is None:
        raise KeyError(f"unknown session_id: {session_id}")
    if sess.state != WorkerState.AWAITING_CRITIC:
        raise InvalidTransition(sess.state, WorkerState.REVISING)

    current = sess.critic_iterations
    if current >= max_critic_iterations:
        reason = f"max_critic_iterations reached: {current}"
        store.transition_to(session_id, WorkerState.ABANDONED, reason=reason)
        if pr_url:
            _label_pr_best_effort(
                gh,
                pr_url,
                NEEDS_HUMAN_LABEL,
                repo=repo,
                emit=emit,
                event="critic_cap_label_failed",
                issue=sess.issue,
            )
            body = (
                f"Persistent-worker session abandoned after {current} critic "
                f"iteration(s) (cap = {max_critic_iterations}).\n\n"
                "Last critic findings:\n\n"
                f"{findings_summary or '(no findings summary provided)'}"
            )
            _comment_pr_best_effort(
                gh,
                pr_url,
                body,
                repo=repo,
                emit=emit,
                event="critic_cap_comment_failed",
                issue=sess.issue,
            )
        _emit_best_effort(
            emit,
            "critic_iteration_cap_abandoned",
            issue=sess.issue,
            session_id=session_id,
            iterations=current,
            cap=max_critic_iterations,
            pr=pr_url,
        )
        return True

    new_count = store.increment_iterations(session_id)
    store.transition_to(session_id, WorkerState.REVISING, reason="critic requested changes")
    _emit_best_effort(
        emit,
        "critic_iteration_revising",
        issue=sess.issue,
        session_id=session_id,
        iterations=new_count,
        cap=max_critic_iterations,
    )
    return False


def format_critic_followup_prompt(report: Any) -> str:
    """Serialise critic findings into the next worker prompt verbatim."""
    findings = list(getattr(report, "findings", []) or [])
    header = (
        "The critic returned REQUEST_CHANGES on your PR. "
        "Address every finding below verbatim, then push a follow-up commit."
    )
    if not findings:
        raw = str(getattr(report, "raw", "")).strip()
        body = raw or "(critic provided no findings text)"
        return f"{header}\n\nCritic report:\n{body}"

    lines: list[str] = [header, "", "Critic findings:"]
    for finding in findings:
        loc = ""
        if getattr(finding, "file", None):
            loc = finding.file
            if getattr(finding, "line", None):
                loc = f"{loc}:{finding.line}"
            loc = f" ({loc})"
        lines.append(f"- [{finding.severity}/{finding.category}]{loc} {finding.message}")
    return "\n".join(lines)


def handle_critic_verdict(
    *,
    store: WorkerSessionStore,
    session_id: str,
    report: Any,
    pr_url: str | None,
    gh: Any = _gh,
    repo: str | None = None,
    emit: Any = None,
    dispatch_revision: Any = None,
) -> str:
    """Apply a critic verdict to the persistent-worker FSM."""
    sess = store.get(session_id)
    if sess is None:
        raise KeyError(f"unknown session_id: {session_id}")
    if sess.state != WorkerState.AWAITING_CRITIC:
        raise InvalidTransition(sess.state, WorkerState.REVISING)

    overall = str(getattr(report, "overall", "")).lower()

    if overall == "approve":
        store.transition_to(session_id, WorkerState.MERGED, reason="critic approved")
        _emit_best_effort(
            emit,
            "critic_verdict_merged",
            issue=sess.issue,
            session_id=session_id,
            pr=pr_url,
        )
        return "merged"

    if overall == "block":
        store.transition_to(session_id, WorkerState.ABANDONED, reason="critic blocked (sev1)")
        if pr_url:
            _label_pr_best_effort(
                gh,
                pr_url,
                NEEDS_REVIEW_LABEL,
                repo=repo,
                emit=emit,
                event="critic_block_label_failed",
                issue=sess.issue,
            )
        _emit_best_effort(
            emit,
            "critic_verdict_blocked",
            issue=sess.issue,
            session_id=session_id,
            pr=pr_url,
        )
        return "abandoned"

    if overall == "request_changes":
        new_count = store.increment_iterations(session_id)
        store.transition_to(session_id, WorkerState.REVISING, reason="critic requested changes")
        followup_prompt = format_critic_followup_prompt(report)
        resume_kw = resume_kwargs_for(store, session_id)
        _emit_best_effort(
            emit,
            "critic_verdict_revising",
            issue=sess.issue,
            session_id=session_id,
            iterations=new_count,
            pr=pr_url,
            resumed=bool(resume_kw),
        )
        if dispatch_revision is not None:
            try:
                dispatch_revision(
                    session=store.get(session_id),
                    prompt=followup_prompt,
                    resume_kwargs=resume_kw,
                )
            except Exception as ex_:  # noqa: BLE001
                _emit_best_effort(
                    emit,
                    "critic_revision_dispatch_failed",
                    issue=sess.issue,
                    session_id=session_id,
                    err=str(ex_)[:300],
                )
        return "revising"

    _emit_best_effort(
        emit,
        "critic_verdict_unknown",
        issue=sess.issue,
        session_id=session_id,
        overall=overall,
    )
    return "noop"


def resume_kwargs_for(
    store: WorkerSessionStore,
    session_id: str,
) -> dict[str, str]:
    """Return ``{"resume": <sdk_id>}`` if this session can warm-resume."""
    sess = store.get(session_id)
    if sess is None or not sess.sdk_session_id:
        return {}
    if sess.state not in {WorkerState.RUNNING, WorkerState.REVISING}:
        return {}
    return {"resume": sess.sdk_session_id}


def sev_counts(outcome: Any) -> dict[str, int]:
    """Tally sev1/sev2/sev3 from a CriticOutcome.report. Safe on None."""
    report = getattr(outcome, "report", None)
    counts = {"sev1": 0, "sev2": 0, "sev3": 0}
    if report is None:
        return counts
    for finding in report.findings:
        if finding.severity in counts:
            counts[finding.severity] += 1
    return counts


def _label_pr_best_effort(
    gh: Any,
    pr_url: str,
    label: str,
    *,
    repo: str | None,
    emit: Any,
    event: str,
    issue: int,
) -> None:
    try:
        gh.add_pr_label(pr_url, [label], repo=repo)
    except Exception as ex_:  # noqa: BLE001
        _emit_best_effort(emit, event, issue=issue, err=str(ex_)[:200])


def _comment_pr_best_effort(
    gh: Any,
    pr_url: str,
    body: str,
    *,
    repo: str | None,
    emit: Any,
    event: str,
    issue: int,
) -> None:
    try:
        gh.pr_comment(pr_url, body, repo=repo)
    except Exception as ex_:  # noqa: BLE001
        _emit_best_effort(emit, event, issue=issue, err=str(ex_)[:200])


def _emit_best_effort(emit: Any, kind: str, **payload: Any) -> None:
    if emit is not None:
        with contextlib.suppress(Exception):
            emit(kind, **payload)
