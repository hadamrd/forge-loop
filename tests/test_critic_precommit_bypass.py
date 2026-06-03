"""Tests for critic enforcement of unjustified `git commit --no-verify`."""

from __future__ import annotations

from forge_loop.critic import detect_precommit_bypass


def test_unjustified_no_verify_is_sev1_precommit_bypass() -> None:
    report = detect_precommit_bypass(
        "ran git commit --no-verify because the hook was annoying\n",
        pr_body="## Summary\nShip it\n",
    )

    assert report.has_sev1()
    assert report.findings[0].severity == "sev1"
    assert report.findings[0].category == "correctness"
    assert "precommit_bypass" in report.findings[0].message


def test_no_verify_with_pr_body_justification_is_clean() -> None:
    report = detect_precommit_bypass(
        "git commit --no-verify\n",
        pr_body=(
            "## Summary\nShip it\n\n"
            "## Pre-commit bypass justification\n"
            "The generated fixture intentionally violates formatter output.\n"
        ),
    )

    assert not report.has_sev1()
    assert report.findings == []
