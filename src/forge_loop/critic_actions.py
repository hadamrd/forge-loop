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

MIN_SUSPICIOUS_APPROVE_LINES = 100


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
) -> CriticActionPlan:
    """Pure decision: report + PR size + knobs → plan.

    Rules:
    - ``overall == "block"`` OR any sev1 finding → block merge,
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

    if block_on_sev2 and has_sev2 and not plan.block_merge:
        plan.block_merge = True
        plan.labels_to_add.append("critic:blocking")
        reasons.append("sev2_finding_block_enabled")

    if (
        report.overall == "approve"
        and not report.findings
        and pr_changed_lines > max(min_findings_for_approve, MIN_SUSPICIOUS_APPROVE_LINES)
    ):
        plan.suspicious_approve = True
        plan.block_merge = True
        plan.labels_to_add.append("critic:suspicious")
        reasons.append(f"approve_with_zero_findings_on_{pr_changed_lines}_line_pr")

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
) -> CriticActionPlan:
    """Compute the plan and execute it via ``gh``. Returns the plan so the
    runner can log a summary event."""
    plan = plan_actions(
        report,
        pr_changed_lines,
        block_on_sev2,
        min_findings_for_approve,
    )
    mutation_failed = False

    if plan.block_merge:
        mutation_failed |= _record_mutation_result(
            "disable_pr_auto_merge",
            gh.disable_pr_auto_merge(pr_url, repo=repo),
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
                f"**[{f.severity}/{f.category}]** {f.message}",
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
            f"- **[{f.severity}/{f.category}]** "
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
