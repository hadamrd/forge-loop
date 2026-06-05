"""Repair PR selection and post-repair automerge handling."""

from __future__ import annotations

import re
from typing import Any

from forge_loop.config import Config
from forge_loop.gh_issues import (
    fetch_issue,
    open_prs,
    pr_review_context,
    prs_by_label,
    prs_requiring_repair,
)
from forge_loop.state import append_event
from forge_loop.worker import WorkerOutcome

#: Marker label the #213 adoption scan stamps on a PR once it has been
#: re-critic'd + put back on the merge conveyor. The selector excludes PRs
#: carrying it so re-running a tick is a no-op (no duplicate critic runs,
#: no double auto-merge).
LOOP_ADOPTED_LABEL = "loop:adopted"

#: Critic verdict labels that mean "do NOT auto-adopt this PR". A blocked or
#: suspicious PR is the repair loop's job, not the adoption scan's.
_CRITIC_BLOCK_LABELS = frozenset({"critic:blocking", "critic:suspicious"})

#: A loop-authored PR head branch is exactly ``loop/<issue>-<slug>``. The
#: adoption scan keys off this so a human PR (any other branch) is never
#: adopted/critic'd/merged.
_LOOP_BRANCH_RE = re.compile(r"^loop/(\d+)-")


def loop_issue_from_branch(pr: dict[str, Any]) -> int | None:
    """Return the issue number iff the PR's head branch is ``loop/<n>-...``.

    Stricter than :func:`issue_number_from_pr` (which also matches body /
    ``fixes #n`` text): adoption only ever touches PRs the loop itself
    authored, identified by their canonical head branch.
    """
    head = pr.get("headRefName")
    if not isinstance(head, str):
        return None
    match = _LOOP_BRANCH_RE.match(head)
    return int(match.group(1)) if match else None


def issue_number_from_pr(pr: dict[str, Any]) -> int | None:
    for value in (pr.get("headRefName"), pr.get("body"), pr.get("title")):
        if not isinstance(value, str):
            continue
        match = re.search(r"(?:^|/)loop/(\d+)-", value)
        if match:
            return int(match.group(1))
        match = re.search(r"(?:refs?|closes|fixes|resolves)\s+#(\d+)", value, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def blocking_pr_repairs(
    cfg: Config,
    *,
    prs_requiring_repair_fn: Any = prs_requiring_repair,
    fetch_issue_fn: Any = fetch_issue,
    pr_review_context_fn: Any = pr_review_context,
) -> list[tuple[dict[str, Any], dict[str, Any], str]]:
    from forge_loop.axis import matches_axes, parse_filter_env

    axis_filter = parse_filter_env()
    repairs: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for pr in prs_requiring_repair_fn(cfg.parallel, repo=cfg.github_repo):
        issue_num = issue_number_from_pr(pr)
        if issue_num is None:
            append_event(
                cfg.events_file,
                "repair_pr_skipped",
                pr=pr.get("url"),
                reason="source_issue_not_found",
            )
            continue
        issue = fetch_issue_fn(issue_num, repo=cfg.github_repo)
        if not issue:
            append_event(
                cfg.events_file,
                "repair_pr_skipped",
                pr=pr.get("url"),
                issue=issue_num,
                reason="issue_fetch_failed",
            )
            continue
        if axis_filter and not matches_axes(issue.get("labels") or [], axis_filter):
            append_event(
                cfg.events_file,
                "repair_pr_skipped",
                pr=pr.get("url"),
                issue=issue_num,
                reason="axis_filter_mismatch",
                axes=axis_filter,
            )
            continue
        append_event(
            cfg.events_file,
            "repair_pr_selected",
            pr=pr.get("url"),
            issue=issue_num,
            reasons=pr.get("repairReasons") or [],
        )
        repairs.append((issue, pr, pr_review_context_fn(pr["number"], repo=cfg.github_repo)))
    return repairs


def ready_issue_open_pr_repairs(
    cfg: Config,
    ready_issues: list[dict[str, Any]],
    *,
    open_prs_fn: Any = prs_by_label,
    pr_review_context_fn: Any = pr_review_context,
) -> list[tuple[dict[str, Any], dict[str, Any], str]]:
    """Select existing open PRs for ready issues before new dispatch.

    ``loop:ready`` means "the operator wants the loop to act". If the issue
    already has an open ``loop/<issue>-...`` PR, acting means repairing that
    PR branch, not spawning a second worker from ``main``. This catches the
    common dogfood path where an operator edits an issue body to retarget the
    work while the first PR is still open.
    """
    ready_by_number = {int(issue["number"]): issue for issue in ready_issues}
    if not ready_by_number:
        return []

    repairs: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    seen_issues: set[int] = set()
    limit = max(cfg.parallel, 50)
    for pr in open_prs_fn("", limit, repo=cfg.github_repo):
        issue_num = issue_number_from_pr(pr)
        if issue_num is None or issue_num not in ready_by_number:
            continue
        if issue_num in seen_issues:
            append_event(
                cfg.events_file,
                "ready_issue_open_pr_skipped",
                issue=issue_num,
                pr=pr.get("url"),
                reason="issue_already_selected",
            )
            continue

        seen_issues.add(issue_num)
        enriched = dict(pr)
        reasons = list(enriched.get("repairReasons") or [])
        if "ready_issue_has_open_pr" not in reasons:
            reasons.append("ready_issue_has_open_pr")
        enriched["repairReasons"] = reasons
        append_event(
            cfg.events_file,
            "ready_issue_open_pr_selected",
            issue=issue_num,
            pr=enriched.get("url"),
            reasons=reasons,
        )
        repairs.append(
            (
                ready_by_number[issue_num],
                enriched,
                pr_review_context_fn(enriched["number"], repo=cfg.github_repo),
            )
        )
    return repairs


def _pr_labels(pr: dict[str, Any]) -> set[str]:
    return {str(lab.get("name") or "") for lab in pr.get("labels") or []}


def orphaned_clean_pr_adoptions(
    cfg: Config,
    *,
    open_prs_fn: Any = open_prs,
    fetch_issue_fn: Any = fetch_issue,
) -> list[tuple[WorkerOutcome, dict[str, Any]]]:
    """Select open loop PRs orphaned by deadline-cancellation for adoption.

    Issue #213. A worker can open its PR and then trip ``worker_timeout_s``
    before the tick's post-critic merge step runs, leaving a CLEAN (or
    never-critic'd) PR open forever — it matches neither
    :func:`blocking_pr_repairs` (only critic-blocked / conflicted /
    unresolved-thread PRs) nor :func:`ready_issue_open_pr_repairs` (only
    ``loop:ready`` issues).

    Returns ``(synthetic_outcome, pr)`` tuples for PRs that are safe to
    re-critic + merge: head branch ``loop/<n>-``, source issue OPEN and not
    risk-gated, NOT critic-blocked/suspicious, NOT already adopted. Every
    excluded PR emits an ``orphan_pr_skipped`` event carrying the ``reason``
    — no silent drops (acceptance criterion 4).
    """
    risk_gate_label = cfg.labels.risk_gate
    limit = max(cfg.parallel, 50)
    adoptions: list[tuple[WorkerOutcome, dict[str, Any]]] = []
    seen_issues: set[int] = set()
    for pr in open_prs_fn(limit, repo=cfg.github_repo):
        url = pr.get("url")
        issue_num = loop_issue_from_branch(pr)
        if issue_num is None:
            # Not a loop-authored branch — a human PR. Never adopt it.
            continue

        labels = _pr_labels(pr)
        if labels & _CRITIC_BLOCK_LABELS:
            append_event(
                cfg.events_file,
                "orphan_pr_skipped",
                pr=url,
                issue=issue_num,
                reason="critic_blocked",
            )
            continue
        if LOOP_ADOPTED_LABEL in labels:
            append_event(
                cfg.events_file,
                "orphan_pr_skipped",
                pr=url,
                issue=issue_num,
                reason="already_adopted",
            )
            continue
        if issue_num in seen_issues:
            append_event(
                cfg.events_file,
                "orphan_pr_skipped",
                pr=url,
                issue=issue_num,
                reason="issue_already_selected",
            )
            continue

        issue = fetch_issue_fn(issue_num, repo=cfg.github_repo)
        if not issue:
            append_event(
                cfg.events_file,
                "orphan_pr_skipped",
                pr=url,
                issue=issue_num,
                reason="issue_fetch_failed",
            )
            continue
        # Conservative on unknown state (None / missing) — treat as closed,
        # mirroring the issue-closed merge gate: never adopt a PR whose
        # source issue the operator may have intentionally closed.
        state = str(issue.get("state") or "").upper()
        if state != "OPEN":
            append_event(
                cfg.events_file,
                "orphan_pr_skipped",
                pr=url,
                issue=issue_num,
                reason="issue_closed",
            )
            continue
        issue_labels = {str(lab.get("name") or "") for lab in issue.get("labels") or []}
        if risk_gate_label and risk_gate_label in issue_labels:
            append_event(
                cfg.events_file,
                "orphan_pr_skipped",
                pr=url,
                issue=issue_num,
                reason="risk_gated",
            )
            continue

        seen_issues.add(issue_num)
        outcome = WorkerOutcome(
            issue=issue_num,
            title=str(issue.get("title") or pr.get("title") or ""),
            pr_url=url if isinstance(url, str) else None,
            status="open",
            duration_s=0.0,
            stdout_tail="",
            events=[],
        )
        append_event(
            cfg.events_file,
            "orphan_pr_selected",
            pr=url,
            issue=issue_num,
            merge_state=str(pr.get("mergeStateStatus") or "").upper() or None,
        )
        adoptions.append((outcome, pr))
    return adoptions


def enable_automerge_for_repaired_prs(
    cfg: Config,
    outcomes: list[WorkerOutcome],
    emit: Any,
) -> None:
    """After repair + critic, put fixed PRs back on the merge conveyor."""
    from forge_loop import gh_issues as _gh
    from forge_loop.runner.merge_gate import apply_issue_closed_gate

    apply_issue_closed_gate(
        outcomes,
        gh=_gh,
        repo=cfg.github_repo,
        events_file=cfg.events_file,
        emit=emit,
    )
    for outcome in outcomes:
        if outcome.status not in {"open", "merged"} or not outcome.pr_url:
            continue
        threads = _gh.unresolved_review_threads(outcome.pr_url, repo=cfg.github_repo)
        if threads:
            append_event(
                cfg.events_file,
                "repair_automerge_skipped",
                issue=outcome.issue,
                pr=outcome.pr_url,
                reason="unresolved_review_threads",
                unresolved=len(threads),
            )
            continue
        if _gh.enable_pr_auto_merge(outcome.pr_url, repo=cfg.github_repo):
            outcome.status = "merged"
            append_event(
                cfg.events_file,
                "repair_automerge_enabled",
                issue=outcome.issue,
                pr=outcome.pr_url,
            )
        else:
            append_event(
                cfg.events_file,
                "repair_automerge_failed",
                issue=outcome.issue,
                pr=outcome.pr_url,
            )
