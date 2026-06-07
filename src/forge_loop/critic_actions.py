"""Translate a CriticReport into runner actions on a PR.

Separated from runner.py so the gating logic is unit-testable without
spinning up subprocesses or threads. The runner imports
``apply_critic_report`` and the gh helpers do the actual side effects.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from forge_loop.critic import CriticReport, Finding
from forge_loop.critic_format import finding_tag


class SuspiciousResolution(StrEnum):
    """Verdict of the issue #311 self-clearing second critic pass.

    A ``str`` Enum (not a string literal) so the discriminator is shared across
    the decision function here and the orchestration in ``runner.dispatch`` —
    the manifesto's "no stringly-typed cross-module event boundaries" rule.

    - ``CLEARED``: the independent second pass found no real sev1/sev2, so the
      first pass's zero-findings approve was genuine → auto-merge, no human.
    - ``CORROBORATED``: the second pass found a real sev1/sev2 the rubber-stamp
      hid → HOLD as a normal critic block (repair loop), NEVER auto-merge.
    """

    CLEARED = "cleared"
    CORROBORATED = "corroborated"


def reconcile_suspicious(second: CriticReport) -> SuspiciousResolution:
    """Adjudicate a ``critic:suspicious`` flag from an independent second pass.

    Issue #311: the suspicious guard must be self-clearing, never human-terminal.
    The first pass flagged "approve with ZERO findings on a huge diff" — an
    implausible rubber-stamp. The second INDEPENDENT pass is the tie-breaker:

    - it surfaces a real sev1/sev2 (finding OR manifesto violation) → the
      rubber-stamp was hiding genuine problems → ``CORROBORATED`` (held).
    - it surfaces no real sev1/sev2 → the clean approve was genuine →
      ``CLEARED`` (auto-merge with zero human action).

    Note a second zero-findings approve is ``CLEARED``: two independent passes
    agreeing the diff is clean is the strongest possible evidence it is, so the
    PR must not stay frozen pending only a human.
    """
    if second.has_sev1() or second.has_sev2():
        return SuspiciousResolution.CORROBORATED
    return SuspiciousResolution.CLEARED


def resolve_suspicious_timeout(
    verdicts: list[str],
    *,
    has_real_sev: bool,
) -> SuspiciousResolution:
    """Resolve a still-held ``critic:suspicious`` PR once it ages past the
    configured timeout — so it NEVER sits frozen pending only a human (AC4).

    The resolution is the *majority* recorded verdict across the passes:
    ``"approved"`` votes vs everything else. A strict approve majority →
    ``CLEARED`` (default to the agreed-clean verdict). A tie, an empty record,
    or a non-approve majority → ``CORROBORATED`` (the conservative direction —
    a held flag only ever auto-resolves DOWN to merge on a clear approve
    majority).

    AC5 safety invariant (never violated): if ANY recorded pass carried a real
    sev1/sev2, the resolution is ALWAYS ``CORROBORATED`` regardless of the
    vote — a corroborated real severity is never auto-merged by the timeout.
    """
    if has_real_sev:
        return SuspiciousResolution.CORROBORATED
    approve_votes = sum(1 for v in verdicts if v == "approved")
    if approve_votes * 2 > len(verdicts):
        return SuspiciousResolution.CLEARED
    return SuspiciousResolution.CORROBORATED


@dataclass(frozen=True)
class SuspiciousCalibration:
    """Result of the Part-B (issue #311) self-calibration stub."""

    effective_min_lines: int
    loosened: bool = False


def suspicious_precision(window: list[SuspiciousResolution]) -> float | None:
    """Precision of the ``critic:suspicious`` flag over a rolling window (AC7).

    Precision = true-positives / total = the fraction of suspicious PRs the
    second pass (or a human) CORROBORATED. A low value means the flag is
    firing mostly on clean PRs (false positives). ``None`` for an empty
    window (no signal yet).
    """
    if not window:
        return None
    corroborated = sum(1 for r in window if r is SuspiciousResolution.CORROBORATED)
    return corroborated / len(window)


def calibrate_suspicious_threshold(
    *,
    precision: float | None,
    base_min_lines: int = 600,
    enabled: bool = False,
    precision_floor: float = 0.5,
) -> SuspiciousCalibration:
    """Part B (issue #311), GATED on the Scorecard projection (#307).

    When the suspicious flag's precision is low (too many false positives), the
    heuristic is loosened by raising the effective ``MIN_SUSPICIOUS_APPROVE_LINES``
    floor so fewer clean PRs are flagged. Ships DISABLED (``enabled=False``):
    a no-op returning the base threshold until #307 lands the precision signal.

    TODO(#307): feed ``precision`` from the Scorecard projection's rolling
    suspicious-precision trend and flip ``enabled`` on via config.
    """
    if not enabled or precision is None:
        return SuspiciousCalibration(effective_min_lines=base_min_lines, loosened=False)
    if precision < precision_floor:
        return SuspiciousCalibration(effective_min_lines=base_min_lines * 2, loosened=True)
    return SuspiciousCalibration(effective_min_lines=base_min_lines, loosened=False)


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
