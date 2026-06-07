"""Translate a CriticReport into runner actions on a PR.

Separated from runner.py so the gating logic is unit-testable without
spinning up subprocesses or threads. The runner imports
``apply_critic_report`` and the gh helpers do the actual side effects.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from forge_loop.critic import CriticReport, Finding
from forge_loop.critic_format import finding_tag

MIN_SUSPICIOUS_APPROVE_LINES = 600  # calibrated 2026-06-05 from live data:
# scope-capped clean PRs run ~350-500 changed lines and legitimately have 0
# findings; only a 0-findings approval on a VERY large diff (e.g. the ~1050-line
# gamed rubber-stamp this guard correctly caught) is implausible. The old floor
# of 100 false-positived EVERY clean approval and blocked all auto-merges.

#: Heading the teaching critic stamps on its minimal-path-to-green comment.
#: Stable so the repair worker (and a human) can find the acceptance predicate
#: at a glance in the review thread it reads back via ``pr_review_context``.
MINIMAL_PATH_HEADING = "## Minimal path to green (must-fix to merge)"
FOLLOW_UPS_HEADING = "### Optional follow-ups (do NOT block merge)"


def render_minimal_path_comment(report: CriticReport) -> str:
    """Render the teaching critic's must-fix path + follow-ups as a PR comment.

    Ch9 §9.5.1: the worker must never have to GUESS the acceptance predicate.
    This renders the critic's ordered, minimal must-fix set (clearly separated
    from optional polish) into one comment that leads the review thread, so the
    next repair round's brief carries it verbatim.

    Returns "" when there is nothing to teach — no must-fix steps and no
    follow-ups — so a clean approval does not spam the PR.
    """
    must_fix = [s for s in report.minimal_path_to_green if s.strip()]
    follow_ups = report.follow_ups
    if not must_fix and not follow_ups:
        return ""

    parts: list[str] = [MINIMAL_PATH_HEADING]
    if must_fix:
        parts.extend(f"{i}. {step.strip()}" for i, step in enumerate(must_fix, start=1))
    else:
        # overall == approve (or all blockers demoted): nothing blocks merge.
        parts.append("_Nothing blocks merge._")

    if follow_ups:
        parts.append("")
        parts.append(FOLLOW_UPS_HEADING)
        parts.extend(
            f"- {finding_tag(f.severity, f.category)} "
            f"{f.file or ''}{':' + str(f.line) if f.line else ''}"
            f"{' — ' if (f.file or f.line) else ''}{f.message}"
            for f in follow_ups
        )
    return "\n".join(parts)


class GhClient(Protocol):
    """The slice of forge_loop.gh that we need. Allows tests to inject a
    spy without monkey-patching the global module."""

    def add_pr_label(self, pr: int | str, labels: list[str], repo: str | None = None) -> bool: ...

    def disable_pr_auto_merge(self, pr: int | str, repo: str | None = None) -> bool: ...

    def post_review_comment(
        self,
        pr: int | str,
        body: str,
        file: str | None = None,
        line: int | None = None,
        repo: str | None = None,
    ) -> bool: ...

    @property
    def auth_source(self) -> str: ...


@dataclass
class CriticActionPlan:
    """What the runner should do given a CriticReport. Returned by the pure
    decision function and then executed by ``apply_critic_report``."""

    block_merge: bool = False
    labels_to_add: list[str] = field(default_factory=list)
    inline_comments: list[Finding] = field(default_factory=list)
    summary_comments: list[Finding] = field(default_factory=list)
    suspicious_approve: bool = False
    reason: str = ""


def plan_actions(
    report: CriticReport,
    pr_changed_lines: int,
    block_on_sev2: bool,
    min_findings_for_approve: int,
    pr_touches_tests: bool = False,
) -> CriticActionPlan:
    """Pure decision: report + PR size + knobs → plan.

    Rules:
    - ``overall == "block"`` OR any sev1 finding → block merge,
      label ``critic:blocking``.
    - ``overall == "request_changes"`` with any sev2 finding → block merge,
      label ``critic:blocking``.
    - ``block_on_sev2`` AND any sev2 finding → also block + label
      ``critic:blocking``.
    - sev2/sev3 findings → inline comment if file+line, else summary.
    - ``overall == "approve"`` with zero findings AND a large diff →
      suspicious: block merge + label ``critic:suspicious``. The size floor
      prevents tiny/docs-style PRs from being blocked only because the critic
      had no findings.
    """
    plan = CriticActionPlan()
    reasons: list[str] = []

    has_sev1 = report.has_sev1()
    has_sev2 = report.has_sev2()
    has_sev1_manifesto = report.has_sev1_manifesto_violation()

    if report.overall == "block" or has_sev1 or has_sev1_manifesto:
        plan.block_merge = True
        plan.labels_to_add.append("critic:blocking")
        if has_sev1_manifesto:
            plan.labels_to_add.append("critic:manifesto-violation")
            reasons.append("sev1_manifesto_violation")
        if has_sev1:
            reasons.append("sev1_finding")
        if report.overall == "block":
            reasons.append("overall_block")

    if report.overall == "request_changes" and has_sev2 and not plan.block_merge:
        plan.block_merge = True
        plan.labels_to_add.append("critic:blocking")
        reasons.append("request_changes_sev2")

    if block_on_sev2 and has_sev2 and not plan.block_merge:
        plan.block_merge = True
        plan.labels_to_add.append("critic:blocking")
        reasons.append("sev2_finding_block_enabled")

    if (
        report.overall == "approve"
        and not report.findings
        and pr_changed_lines > max(min_findings_for_approve, MIN_SUSPICIOUS_APPROVE_LINES)
        # Self-clear (#311): a large zero-finding "approve" is far less likely a
        # rubber-stamp when the PR ADDED TESTS — rubber-stamps don't write tests.
        # So only hold-as-suspicious when the diff is large, clean, AND test-free.
        # A clean, well-tested large PR (e.g. a +665 change with 37 new tests)
        # proceeds via the normal merge path with zero human action.
        and not pr_touches_tests
    ):
        plan.suspicious_approve = True
        plan.block_merge = True
        plan.labels_to_add.append("critic:suspicious")
        reasons.append(f"approve_with_zero_findings_on_{pr_changed_lines}_line_pr_no_tests")

    for f in report.findings:
        if f.severity == "sev1":
            # sev1 already escalates via the label + summary; still post inline
            # when location is known so reviewers see it next to the code.
            (plan.inline_comments if (f.file and f.line) else plan.summary_comments).append(f)
        elif f.severity in {"sev2", "sev3"}:
            (plan.inline_comments if (f.file and f.line) else plan.summary_comments).append(f)

    # Dedupe labels while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for lab in plan.labels_to_add:
        if lab in seen:
            continue
        seen.add(lab)
        deduped.append(lab)
    plan.labels_to_add = deduped
    plan.reason = ",".join(reasons) or "no_action"
    return plan


def apply_critic_report(
    report: CriticReport,
    pr_url: str,
    pr_changed_lines: int,
    block_on_sev2: bool,
    min_findings_for_approve: int,
    gh: GhClient,
    repo: str | None,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
    pr_touches_tests: bool = False,
) -> CriticActionPlan:
    """Compute the plan and execute it via ``gh``. Returns the plan so the
    runner can log a summary event."""
    plan = plan_actions(
        report,
        pr_changed_lines,
        block_on_sev2,
        min_findings_for_approve,
        pr_touches_tests=pr_touches_tests,
    )
    mutation_failed = False

    if plan.block_merge:
        gh.disable_pr_auto_merge(pr_url, repo=repo)

    # Teaching critic (Ch9): post the explicit minimal-path-to-green FIRST so it
    # leads the review thread the repair worker reads back via
    # ``gh.pr_review_context`` — the acceptance predicate, stated, not guessed.
    mptg_body = render_minimal_path_comment(report)
    if mptg_body:
        mutation_failed |= _record_mutation_result(
            "post_review_comment",
            gh.post_review_comment(pr_url, mptg_body, repo=repo),
            gh=gh,
            pr_url=pr_url,
            emit=emit,
        )

    if plan.labels_to_add:
        mutation_failed |= _record_mutation_result(
            "add_pr_label",
            gh.add_pr_label(pr_url, plan.labels_to_add, repo=repo),
            gh=gh,
            pr_url=pr_url,
            emit=emit,
        )

    for f in plan.inline_comments:
        mutation_failed |= _record_mutation_result(
            "post_review_comment",
            gh.post_review_comment(
                pr_url,
                f"{finding_tag(f.severity, f.category)} {f.message}",
                file=f.file,
                line=f.line,
                repo=repo,
            ),
            gh=gh,
            pr_url=pr_url,
            emit=emit,
        )

    if plan.summary_comments:
        summary = "\n".join(
            f"- {finding_tag(f.severity, f.category)} "
            f"{f.file or ''}{':' + str(f.line) if f.line else ''}"
            f"{' — ' if (f.file or f.line) else ''}{f.message}"
            for f in plan.summary_comments
        )
        mutation_failed |= _record_mutation_result(
            "post_review_comment",
            gh.post_review_comment(pr_url, f"Critic findings:\n{summary}", repo=repo),
            gh=gh,
            pr_url=pr_url,
            emit=emit,
        )

    if report.manifesto_violations:
        viol_summary = "\n".join(
            f"- **[{v.severity}] {v.manifesto}#{v.rule_id}** — "
            f"`{v.quote.strip()[:120]}` → {v.suggested_fix}"
            for v in report.manifesto_violations
        )
        mutation_failed |= _record_mutation_result(
            "post_review_comment",
            gh.post_review_comment(
                pr_url,
                f"Manifesto violations:\n{viol_summary}",
                repo=repo,
            ),
            gh=gh,
            pr_url=pr_url,
            emit=emit,
        )

    if emit is not None and not mutation_failed:
        emit(
            "critic_actions_applied",
            {
                "pr": pr_url,
                "block_merge": plan.block_merge,
                "labels": plan.labels_to_add,
                "inline_count": len(plan.inline_comments),
                "summary_count": len(plan.summary_comments),
                "suspicious_approve": plan.suspicious_approve,
                "reason": plan.reason,
            },
        )

    return plan


def _record_mutation_result(
    method: str,
    ok: bool,
    *,
    gh: GhClient,
    pr_url: str,
    emit: Callable[[str, dict[str, Any]], None] | None,
) -> bool:
    if ok:
        return False
    if emit is None:
        return True
    emit(
        "critic_actions_failed",
        {
            "pr": pr_url,
            "method": method,
            "auth_source": getattr(gh, "auth_source", "github-client"),
        },
    )
    return True
