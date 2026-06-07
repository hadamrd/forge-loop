"""#311 — the suspicious-merge guard self-clears when the PR added tests.

A large, zero-finding "approve" used to be held as ``critic:suspicious`` purely
on size, which false-positived clean *well-tested* large PRs and froze them for a
human. The self-clear keeps the rubber-stamp defense (large + clean + NO tests
still holds) while letting a large, clean, test-bearing PR proceed autonomously.
"""

from __future__ import annotations

from forge_loop.critic import CriticReport
from forge_loop.critic_actions import plan_actions

_BIG = 900  # > MIN_SUSPICIOUS_APPROVE_LINES (600)


def _clean_approve() -> CriticReport:
    return CriticReport(overall="approve", findings=[])


def test_large_clean_approve_without_tests_is_held_suspicious() -> None:
    plan = plan_actions(
        _clean_approve(),
        pr_changed_lines=_BIG,
        block_on_sev2=True,
        min_findings_for_approve=1,
        pr_touches_tests=False,
    )
    assert plan.suspicious_approve is True
    assert plan.block_merge is True
    assert "critic:suspicious" in plan.labels_to_add


def test_large_clean_approve_with_tests_self_clears() -> None:
    plan = plan_actions(
        _clean_approve(),
        pr_changed_lines=_BIG,
        block_on_sev2=True,
        min_findings_for_approve=1,
        pr_touches_tests=True,
    )
    assert plan.suspicious_approve is False
    assert plan.block_merge is False
    assert "critic:suspicious" not in plan.labels_to_add


def test_default_preserves_suspicious_behavior() -> None:
    # No pr_touches_tests arg → defaults False → existing guard unchanged.
    plan = plan_actions(
        _clean_approve(),
        pr_changed_lines=_BIG,
        block_on_sev2=True,
        min_findings_for_approve=1,
    )
    assert plan.suspicious_approve is True


def test_small_clean_approve_with_tests_flag_is_not_suspicious() -> None:
    # Below the size floor it was never suspicious; the flag must not change that.
    plan = plan_actions(
        _clean_approve(),
        pr_changed_lines=10,
        block_on_sev2=True,
        min_findings_for_approve=1,
        pr_touches_tests=True,
    )
    assert plan.suspicious_approve is False
    assert plan.block_merge is False
