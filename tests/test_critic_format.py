"""Lock the critic inline-finding tag contract (#230 sev2/architecture).

The producer (``critic_actions``) and the thread classifier
(``gh_issues._thread_is_critic`` → ``critic_format.is_finding_body``) must agree
on ONE spelling of the ``**[<sev>/<category>]**`` tag, forever. If they drift,
every leftover critic thread is misclassified as *human*, the auto-merge gate
never fires, and a critic-approved PR stalls in the repair loop (the #229
multi-hour stall). These tests fail the moment producer and classifier
disagree — so the contract cannot silently desync.
"""

from __future__ import annotations

from typing import Any

import pytest

from forge_loop import critic, critic_format
from forge_loop.critic import CriticReport, Finding
from forge_loop.critic_actions import apply_critic_report
from forge_loop.critic_format import finding_tag, is_finding_body
from forge_loop.gh_issues import _thread_is_critic


@pytest.mark.parametrize("severity", critic_format.SEVERITIES)
@pytest.mark.parametrize("category", critic_format.CATEGORIES)
def test_finding_tag_roundtrips_through_classifier(severity: str, category: str) -> None:
    """Every tag the producer can emit is recognised by the classifier."""
    body = f"{finding_tag(severity, category)} some message"
    assert is_finding_body(body) is True


def test_vocabulary_is_single_sourced_with_critic_module() -> None:
    """``critic.VALID_*`` are aliases of the ``critic_format`` vocabulary — add a
    severity/category in one place and both the validator and the classifier
    regex pick it up. Guards against a second, drifting copy."""
    assert set(critic_format.SEVERITIES) == critic.VALID_SEVERITY
    assert set(critic_format.CATEGORIES) == critic.VALID_CATEGORY


def test_classifier_rejects_human_prose_and_pastes() -> None:
    """Negatives — none of these are the critic's own opening finding."""
    assert is_finding_body("Please rework this design.") is False
    # Human quoting the tag mid-sentence (text precedes it) → NOT critic.
    assert is_finding_body("you flagged **[sev3/style]** but I disagree") is False
    # Unknown category / severity → NOT critic (full tag must match the vocab).
    assert is_finding_body("**[sev3/bogus]** x") is False
    assert is_finding_body("**[sev9/style]** x") is False
    # A bare opening that is not the full tag → NOT critic.
    assert is_finding_body("**[sev") is False
    assert is_finding_body("**bold** but not a finding") is False
    assert is_finding_body("") is False
    assert is_finding_body(None) is False


def test_classifier_accepts_leading_whitespace() -> None:
    assert is_finding_body(f"  {finding_tag('sev1', 'correctness')} msg") is True


class _SpyGh:
    """Captures every inline review-comment body the producer posts."""

    auth_source = "test"

    def __init__(self) -> None:
        self.inline_bodies: list[str] = []

    def add_pr_label(self, pr: Any, labels: list[str], repo: str | None = None) -> bool:
        return True

    def disable_pr_auto_merge(self, pr: Any, repo: str | None = None) -> bool:
        return True

    def post_review_comment(
        self,
        pr: Any,
        body: str,
        file: str | None = None,
        line: int | None = None,
        repo: str | None = None,
    ) -> bool:
        # Only the per-finding inline comments carry a file+line; the summary
        # comment (no line) is a roll-up we don't classify as a thread.
        if line is not None:
            self.inline_bodies.append(body)
        return True


def test_real_producer_inline_bodies_classify_as_critic() -> None:
    """End-to-end drift guard: drive the REAL ``apply_critic_report`` producer
    and assert every inline comment body it posts is recognised by the REAL
    classifier as the critic's own. This binds the two sides through their
    actual code paths, not just the shared helper — if either stops going
    through ``critic_format``, this fails."""
    report = CriticReport(
        overall="request_changes",
        findings=[
            Finding("sev2", "correctness", "src/a.py", 10, "off-by-one"),
            Finding("sev3", "performance", "src/b.py", 20, "n+1 query"),
            Finding("sev3", "architecture", "src/c.py", 5, "reinvents httpx"),
        ],
    )
    gh = _SpyGh()
    apply_critic_report(
        report,
        pr_url="https://github.com/o/r/pull/1",
        pr_changed_lines=200,
        block_on_sev2=True,
        min_findings_for_approve=1,
        gh=gh,
        repo="o/r",
    )

    assert len(gh.inline_bodies) == 3
    for body in gh.inline_bodies:
        # As it would arrive on a review thread's opening comment.
        thread = {"comments": [{"author": {"login": "x"}, "body": body}]}
        assert is_finding_body(body) is True, body
        assert _thread_is_critic(thread) is True, body
