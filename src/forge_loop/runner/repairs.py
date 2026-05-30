"""Repair PR selection and post-repair automerge handling."""

from __future__ import annotations

import re
from typing import Any

from forge_loop.config import Config
from forge_loop.gh import fetch_issue, pr_review_context, prs_requiring_repair
from forge_loop.state import append_event
from forge_loop.worker import WorkerOutcome


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


def enable_automerge_for_repaired_prs(
    cfg: Config,
    outcomes: list[WorkerOutcome],
    emit: Any,
) -> None:
    """After repair + critic, put fixed PRs back on the merge conveyor."""
    from forge_loop import gh as _gh
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
