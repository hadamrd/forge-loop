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
from forge_loop.critic_findings.store import CriticFindingsStore
from forge_loop.critic_format import finding_tag

MIN_SUSPICIOUS_APPROVE_LINES = 100


class GhClient(Protocol):
    """The slice of forge_loop.gh that we need. Allows tests to inject a
    spy without monkey-patching the global module."""

    def add_pr_label(self, pr: int | str, labels: list[str], repo: str | None = None) -> bool: ...

    def disable_pr_auto_merge(self, pr: int | str, repo: str | None = None) -> bool: ...

    def pr_head_branch(self, pr: int | str, repo: str | None = None) -> str | None: ...

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


def _recover_issue_from_branch(
    gh: GhClient,
    pr_url: str,
    repo: str | None,
    emit: Callable[[str, dict[str, Any]], None] | None,
) -> int | None:
    """Best-effort: derive the loop issue from the PR's head branch (#242 fix).

    A degenerate-case fallback for the durable persist path: when the caller
    didn't supply ``issue``, recover it from the PR's canonical ``loop/<n>-``
    head branch instead of dropping every finding (which would blind the
    worker). Network/parse failures degrade to ``None`` so the loud-skip path
    still fires; on success a ``critic_findings_issue_recovered`` event records
    that the fallback was exercised.
    """

    # Lazy import: avoids a module-load cycle (repairs imports worker_brief /
    # gh_issues; this module is imported by the runner that also imports repairs).
    from forge_loop.gh_client import GhError
    from forge_loop.runner.repairs import issue_from_loop_branch

    branch: str | None = None
    # EH-001: only the EXPECTED degradation modes are swallowed — a GitHub API
    # failure (``GhError``) or a malformed pr/repo reference (``ValueError`` from
    # the pr-number / owner-name parse). A programming bug (AttributeError,
    # TypeError, …) must NOT be hidden: it propagates so the observability
    # discipline this PR adds isn't defeated by a silent broad catch. On the
    # expected failures we emit ``critic_findings_issue_recovery_failed`` with
    # context BEFORE degrading to ``None`` so the fallback's failure is visible.
    try:
        branch = gh.pr_head_branch(pr_url, repo=repo)
    except (GhError, ValueError) as exc:
        if emit is not None:
            emit(
                "critic_findings_issue_recovery_failed",
                {
                    "pr": pr_url,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
    issue = issue_from_loop_branch(branch)
    if issue is not None and emit is not None:
        emit(
            "critic_findings_issue_recovered",
            {"pr": pr_url, "issue": issue, "source": "head_branch"},
        )
    return issue


def apply_critic_report(
    report: CriticReport,
    pr_url: str,
    pr_changed_lines: int,
    block_on_sev2: bool,
    min_findings_for_approve: int,
    gh: GhClient,
    repo: str | None,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
    *,
    findings_store: CriticFindingsStore | None = None,
    issue: int | None = None,
) -> CriticActionPlan:
    """Compute the plan and execute it via ``gh``. Returns the plan so the
    runner can log a summary event.

    When ``findings_store`` + ``issue`` are supplied, every :class:`Finding`
    is persisted to the durable control plane (status ``open``) and reconciled
    against prior findings (#242) BEFORE any GitHub posting. The store — not the
    re-fetched GitHub comments — is the repair worker's data path, so a posting
    failure (e.g. an inline-comment 422 on an out-of-diff line) no longer
    silently starves the worker. GitHub posting is demoted to a derived,
    humans-only read-model.
    """
    plan = plan_actions(
        report,
        pr_changed_lines,
        block_on_sev2,
        min_findings_for_approve,
    )

    # AC2/AC6: durable findings FIRST — load-bearing data is handed to the
    # repair worker via the store, never round-tripped through GitHub (Q10).
    # ``issue`` is REQUIRED for the persist branch: dropping it silently would
    # no-op the entire durable data path and shove the worker back onto the
    # lossy GitHub round-trip (the exact #242/Q10 blind-repair failure). Before
    # dropping, we try to RECOVER a missing issue from the PR's canonical
    # ``loop/<n>-`` head branch (#242 review fix); only if that also fails do we
    # make the drop LOUD via a warning event instead of vanishing the findings.
    if findings_store is not None:
        if issue is None:
            issue = _recover_issue_from_branch(gh, pr_url, repo, emit)
        if issue is None:
            if emit is not None:
                emit(
                    "critic_findings_persist_skipped",
                    {
                        "pr": pr_url,
                        "reason": "issue_missing",
                        "dropped_findings": len(report.findings),
                    },
                )
        else:
            result = findings_store.reconcile(pr_url, issue, list(report.findings))
            if emit is not None:
                emit(
                    "critic_findings_persisted",
                    {
                        "pr": pr_url,
                        "issue": issue,
                        "inserted": result.inserted,
                        "kept_open": result.kept_open,
                        "reopened": result.reopened,
                        "closed": result.closed,
                        "open_count": result.open_count,
                    },
                )

    mutation_failed = False

    if plan.block_merge:
        gh.disable_pr_auto_merge(pr_url, repo=repo)
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
