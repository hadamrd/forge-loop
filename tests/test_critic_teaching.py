"""Teaching-critic tests (Ch9 — the convergence problem).

Covers the four teaching behaviours added on top of the existing filter:
  1. minimal-path-to-green is always rendered and reaches the repair worker;
  2. round-aware escalating specificity (terse round 1, patch-sketch round >=2);
  3. severity triage — sev3 demoted after N rounds, sev1/sev2 NEVER demoted;
  4. failure-mode diagnosis prompting on a large pure-addition diff.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge_loop.briefs import render_brief
from forge_loop.critic import (
    CriticReport,
    Finding,
    ManifestoViolation,
    _round_guidance,
    count_prior_critic_rounds,
    demote_sev3_if_stalled,
    parse_report_from_text,
)
from forge_loop.critic_actions import (
    FOLLOW_UPS_HEADING,
    MINIMAL_PATH_HEADING,
    render_minimal_path_comment,
)

# ---------------------------------------------------------------------------
# Round source — count prior critic reviews from the on-disk logs
# ---------------------------------------------------------------------------


def _write_critic_log(logs_dir: Path, issue: int, stamp: int, attempt: str = "0") -> None:
    (logs_dir / f"critic-{issue}-{stamp}-{attempt}.log").write_text("{}")


def test_round_count_zero_when_no_prior_logs(tmp_path: Path) -> None:
    assert count_prior_critic_rounds(42, tmp_path) == 0


def test_round_count_missing_dir_is_zero(tmp_path: Path) -> None:
    assert count_prior_critic_rounds(42, tmp_path / "nope") == 0


def test_round_count_counts_distinct_reviews_not_retry_attempts(tmp_path: Path) -> None:
    # One review that retried (two attempt files, same timestamp) == ONE round.
    _write_critic_log(tmp_path, 7, 1000, "0")
    _write_critic_log(tmp_path, 7, 1000, "1")
    # Two further distinct reviews at different timestamps.
    _write_critic_log(tmp_path, 7, 2000, "0")
    (tmp_path / "critic-7-3000-codex.log").write_text("{}")
    # A different issue's logs must not leak in.
    _write_critic_log(tmp_path, 99, 4000, "0")
    assert count_prior_critic_rounds(7, tmp_path) == 3


# ---------------------------------------------------------------------------
# 1. Minimal path to green — always rendered, reaches the repair worker
# ---------------------------------------------------------------------------


def test_brief_always_carries_minimal_path_contract() -> None:
    out = render_brief(
        "critic",
        pr_url="https://github.com/o/r/pull/1",
        issue_number=1,
        manifestos="(none)",
        round_number=0,
        round_guidance=_round_guidance(0, 3),
    )
    assert "minimal_path_to_green" in out
    # The contract is stated as mandatory, separated from optional follow-ups.
    assert "follow_ups" in out
    assert "minimal" in out.lower()


def test_minimal_path_comment_renders_ordered_must_fix_and_follow_ups() -> None:
    report = CriticReport(
        overall="request_changes",
        findings=[Finding("sev1", "correctness", "a.py", 3, "off-by-one")],
        minimal_path_to_green=[
            "Fix the off-by-one in a.py:paginate (use < not <=)",
            "Add a test that fails before the fix",
        ],
        follow_ups=[Finding("sev3", "style", None, None, "rename var")],
    )
    body = render_minimal_path_comment(report)
    assert MINIMAL_PATH_HEADING in body
    assert "1. Fix the off-by-one" in body
    assert "2. Add a test" in body
    # ordering preserved
    assert body.index("1. Fix") < body.index("2. Add")
    # follow-ups clearly separated and marked non-blocking
    assert FOLLOW_UPS_HEADING in body
    assert "rename var" in body
    assert body.index(MINIMAL_PATH_HEADING) < body.index(FOLLOW_UPS_HEADING)


def test_minimal_path_comment_empty_when_nothing_to_teach() -> None:
    report = CriticReport(overall="approve", findings=[])
    assert render_minimal_path_comment(report) == ""


def test_minimal_path_comment_reaches_pr_via_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    """apply_critic_report posts the must-fix path so the repair brief carries it."""
    from dataclasses import dataclass, field

    from forge_loop.critic_actions import apply_critic_report

    @dataclass
    class _Gh:
        comments: list[str] = field(default_factory=list)

        def add_pr_label(self, pr, labels, repo=None):  # type: ignore[no-untyped-def]
            return True

        def disable_pr_auto_merge(self, pr, repo=None):  # type: ignore[no-untyped-def]
            return True

        def post_review_comment(self, pr, body, file=None, line=None, repo=None):  # type: ignore[no-untyped-def]
            self.comments.append(body)
            return True

        @property
        def auth_source(self) -> str:
            return "test"

    gh = _Gh()
    report = CriticReport(
        overall="request_changes",
        findings=[Finding("sev1", "correctness", "a.py", 3, "off-by-one")],
        minimal_path_to_green=["Fix off-by-one in a.py:paginate"],
    )
    apply_critic_report(
        report,
        "https://github.com/o/r/pull/1",
        pr_changed_lines=120,
        block_on_sev2=False,
        min_findings_for_approve=50,
        gh=gh,
        repo="o/r",
    )
    assert any(MINIMAL_PATH_HEADING in c for c in gh.comments)
    # and it leads the thread (posted before the per-finding summary/inline).
    mptg_idx = next(i for i, c in enumerate(gh.comments) if MINIMAL_PATH_HEADING in c)
    assert mptg_idx == 0


# ---------------------------------------------------------------------------
# 2. Round-aware escalating specificity
# ---------------------------------------------------------------------------


def test_round1_guidance_is_terse_no_patch_sketch() -> None:
    g = _round_guidance(0, 3)
    assert "ROUND 1" in g
    assert "terse" in g.lower()
    # No demand for a patch sketch on the first round.
    assert "patch sketch" not in g.lower()


def test_round2_guidance_demands_why_how_patch_sketch() -> None:
    g = _round_guidance(1, 3)
    assert "ROUND 2" in g
    low = g.lower()
    assert "why" in low
    assert "how" in low
    assert "patch sketch" in low
    # and it diagnoses the meta-cause, not just symptoms.
    assert "scope" in low


def test_round_guidance_escalation_grows_with_round() -> None:
    assert len(_round_guidance(2, 3)) > len(_round_guidance(0, 3))


# ---------------------------------------------------------------------------
# 3. Severity triage — demote sev3 after N rounds; NEVER sev1/sev2
# ---------------------------------------------------------------------------


def test_sev3_demoted_after_threshold_rounds() -> None:
    report = CriticReport(
        overall="request_changes",
        findings=[
            Finding("sev2", "tests", "a.py", 1, "real defect"),
            Finding("sev3", "style", None, None, "nit one"),
            Finding("sev3", "docs", None, None, "nit two"),
        ],
    )
    out = demote_sev3_if_stalled(report, round_number=3, threshold=3)
    sevs = sorted(f.severity for f in out.findings)
    assert sevs == ["sev2"]  # sev3s gone from the blocking set
    assert sorted(f.severity for f in out.follow_ups) == ["sev3", "sev3"]
    # The real defect still blocks.
    assert out.overall == "request_changes"


def test_sev3_not_demoted_before_threshold() -> None:
    report = CriticReport(
        overall="request_changes",
        findings=[Finding("sev3", "style", None, None, "nit")],
    )
    out = demote_sev3_if_stalled(report, round_number=2, threshold=3)
    assert [f.severity for f in out.findings] == ["sev3"]
    assert out.follow_ups == []


def test_sev1_and_sev2_never_demoted_even_at_high_round() -> None:
    report = CriticReport(
        overall="block",
        findings=[
            Finding("sev1", "security", "a.py", 1, "auth bypass"),
            Finding("sev2", "correctness", "b.py", 2, "untested error path"),
        ],
    )
    out = demote_sev3_if_stalled(report, round_number=99, threshold=3)
    sevs = sorted(f.severity for f in out.findings)
    assert sevs == ["sev1", "sev2"]
    assert out.follow_ups == []  # nothing demoted
    assert out.overall == "block"


def test_demotion_relaxes_to_approve_when_only_nits_remained() -> None:
    report = CriticReport(
        overall="request_changes",
        findings=[Finding("sev3", "style", None, None, "nit")],
    )
    out = demote_sev3_if_stalled(report, round_number=4, threshold=3)
    assert out.findings == []
    assert out.overall == "approve"  # nothing blocks anymore
    assert [f.severity for f in out.follow_ups] == ["sev3"]


def test_demotion_keeps_block_when_sev1_manifesto_violation_present() -> None:
    report = CriticReport(
        overall="request_changes",
        findings=[Finding("sev3", "style", None, None, "nit")],
        manifesto_violations=[
            ManifestoViolation("EH-001", "eh.md", "except: pass", "be specific", "sev1")
        ],
    )
    out = demote_sev3_if_stalled(report, round_number=4, threshold=3)
    assert out.findings == []
    # A sev1 manifesto violation still blocks — never relaxed to approve.
    assert out.overall == "request_changes"


def test_demotion_disabled_when_threshold_zero() -> None:
    report = CriticReport(
        overall="request_changes",
        findings=[Finding("sev3", "style", None, None, "nit")],
    )
    out = demote_sev3_if_stalled(report, round_number=99, threshold=0)
    assert [f.severity for f in out.findings] == ["sev3"]


# ---------------------------------------------------------------------------
# 4. Failure-mode diagnosis — large pure-addition diff prompt
# ---------------------------------------------------------------------------


def test_brief_instructs_scope_inflation_diagnosis_on_pure_addition() -> None:
    out = render_brief(
        "critic",
        pr_url="https://github.com/o/r/pull/1",
        issue_number=1,
        manifestos="(none)",
        round_number=1,
        round_guidance=_round_guidance(1, 3),
    )
    low = out.lower()
    # The brief must teach the critic to diagnose +N/-0 scope inflation.
    assert "+n/-0" in low or "pure-addition" in low
    assert "scope" in low
    assert "cut scope" in low or "split" in low


def test_round_guidance_flags_recurring_finding_class_as_wrong_approach() -> None:
    g = _round_guidance(2, 3).lower()
    assert "recur" in g or "same class" in g


# ---------------------------------------------------------------------------
# Parsing — the new fields survive coercion (and tolerate absence)
# ---------------------------------------------------------------------------


def test_parse_extracts_minimal_path_and_follow_ups() -> None:
    blob = json.dumps(
        {
            "overall": "request_changes",
            "minimal_path_to_green": ["step one", "step two"],
            "findings": [
                {"severity": "sev1", "category": "correctness", "message": "bug"}
            ],
            "follow_ups": [
                {"severity": "sev3", "category": "style", "message": "nit"}
            ],
        }
    )
    report, err = parse_report_from_text(blob)
    assert err is None
    assert report is not None
    assert report.minimal_path_to_green == ["step one", "step two"]
    assert [f.severity for f in report.follow_ups] == ["sev3"]


def test_parse_tolerates_missing_teaching_fields() -> None:
    blob = json.dumps({"overall": "approve", "findings": []})
    report, err = parse_report_from_text(blob)
    assert err is None
    assert report is not None
    assert report.minimal_path_to_green == []
    assert report.follow_ups == []


def test_parse_coerces_single_string_minimal_path_to_list() -> None:
    blob = json.dumps(
        {"overall": "request_changes", "minimal_path_to_green": "do the one thing", "findings": []}
    )
    report, _ = parse_report_from_text(blob)
    assert report is not None
    assert report.minimal_path_to_green == ["do the one thing"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
