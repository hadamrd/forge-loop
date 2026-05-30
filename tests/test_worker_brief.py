"""Tests for worker brief rendering."""

from __future__ import annotations

from pathlib import Path

from forge_loop.worker import make_brief, make_repair_brief


def test_make_brief_includes_issue_number_and_body(tmp_path: Path) -> None:
    issue = {"number": 947, "title": "feat(pdl): onFailure", "body": "Some body text"}
    brief = make_brief(issue, tmp_path / "wt-947")
    assert "#947" in brief
    assert "feat(pdl): onFailure" in brief
    assert "Some body text" in brief
    assert str(tmp_path / "wt-947") in brief
    assert "CONTRACT" in brief


def test_make_brief_caps_body_at_6000_chars(tmp_path: Path) -> None:
    long_body = "a" * 10000
    brief = make_brief({"number": 1, "title": "x", "body": long_body}, tmp_path / "w")
    assert brief.count("a") <= 6500


def test_make_brief_includes_history_section_when_past_attempts(tmp_path: Path) -> None:
    issue = {"number": 942, "title": "fix x", "body": "Do the thing."}
    past = [
        {"ts": "2026-05-26T10:00:00Z", "status": "failed", "note": "test missing", "pr_url": None},
        {
            "ts": "2026-05-26T11:00:00Z",
            "status": "merged",
            "note": "shipped",
            "pr_url": "https://github.com/h/r/pull/9",
        },
    ]
    brief = make_brief(issue, tmp_path / "w", past_attempts=past)
    assert "PREVIOUS ATTEMPTS" in brief
    assert "test missing" in brief
    assert "https://github.com/h/r/pull/9" in brief


def test_make_brief_promotes_critic_blockers_to_hard_contract(tmp_path: Path) -> None:
    issue = {"number": 23, "title": "AR4 proof", "body": "Do AR4."}
    brief = make_brief(
        issue,
        tmp_path / "w",
        blocking_comments=[
            "Post-merge critic found AR4 is not actually complete.\n"
            "Required repair: add a test with at least two candidate summaries."
        ],
    )

    assert "CRITIC / OPERATOR BLOCKERS" in brief
    assert "HARD ACCEPTANCE CONTRACT" in brief
    assert "two candidate summaries" in brief
    assert "Do not satisfy this issue with adjacent cleanup" in brief


def test_make_brief_no_history_section_when_empty(tmp_path: Path) -> None:
    issue = {"number": 942, "title": "fix x", "body": ""}
    brief = make_brief(issue, tmp_path / "w", past_attempts=[])
    assert "PREVIOUS ATTEMPTS" not in brief


def test_make_brief_risk_gated_disables_automerge(tmp_path: Path) -> None:
    issue = {"number": 942, "title": "fix x", "body": ""}
    brief = make_brief(issue, tmp_path / "w", risk_gated=True)
    assert "DO NOT enable auto-merge" in brief
    assert "ready for human review" in brief
    assert '"status": "open"' in brief


def test_make_brief_default_stops_before_automerge(tmp_path: Path) -> None:
    issue = {"number": 942, "title": "fix x", "body": ""}
    brief = make_brief(issue, tmp_path / "w")
    assert "DO NOT enable auto-merge" in brief
    assert "DO NOT merge the PR" in brief
    assert "owns merge after critic approval" in brief
    assert '"status": "open|failed"' in brief


def test_make_repair_brief_keeps_same_pr_contract(tmp_path: Path) -> None:
    issue = {"number": 42, "title": "fix blocked pr", "body": "Acceptance"}
    pr = {
        "number": 7,
        "url": "https://github.com/o/r/pull/7",
        "headRefName": "loop/42-fix-blocked-pr",
    }
    brief = make_repair_brief(
        issue,
        tmp_path / "wt",
        pr=pr,
        review_context="[sev1] fix the real consumer",
    )
    assert "Repair the EXISTING PR branch" in brief
    assert "Do not create a new branch" in brief
    assert "https://github.com/o/r/pull/7" in brief
    assert "[sev1] fix the real consumer" in brief
    assert '"pr": "https://github.com/o/r/pull/7"' in brief
