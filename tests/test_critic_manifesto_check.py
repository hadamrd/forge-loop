"""Tests for the manifesto-compliance arm of the critic (issue #133).

Covers:
- `load_manifestos_text` reads every file under the canonical manifestos dir.
- `_coerce_report` parses the new `manifesto_violations` field, preserves
  back-compat when absent, drops malformed entries, defaults unknown
  severities to sev3, and forces `overall = "request_changes"` on any
  sev1 violation.
- The brief template, once rendered, contains each manifesto body.
- The auto-merge gate (`plan_actions` in critic_actions) blocks merge on
  a sev1 manifesto violation even when no sev1 finding exists.
"""

from __future__ import annotations

import json
from pathlib import Path

from forge_loop._critic_sdk import load_manifestos_text
from forge_loop.briefs import load_template
from forge_loop.critic import (
    CriticReport,
    ManifestoViolation,
    parse_report_from_text,
)
from forge_loop.critic_actions import plan_actions

# ---------------------------------------------------------------------------
# Manifesto loading + prompt rendering
# ---------------------------------------------------------------------------


def _seed_manifestos(repo: Path) -> None:
    d = repo / "docs" / "manifestos"
    d.mkdir(parents=True)
    (d / "alpha.md").write_text("# Alpha rules\n\nAR-001: never alpha.\n")
    (d / "beta.md").write_text("# Beta rules\n\nBR-001: never beta.\n")
    # Non-markdown file should be ignored.
    (d / "ignore.bin").write_bytes(b"\x00binary\x00")


def test_load_manifestos_text_reads_every_file(tmp_path: Path) -> None:
    _seed_manifestos(tmp_path)
    text = load_manifestos_text(tmp_path)
    assert "## Manifesto: alpha.md" in text
    assert "## Manifesto: beta.md" in text
    assert "AR-001: never alpha." in text
    assert "BR-001: never beta." in text
    assert "binary" not in text


def test_load_manifestos_text_missing_dir_returns_placeholder(tmp_path: Path) -> None:
    text = load_manifestos_text(tmp_path)
    assert "no manifestos configured" in text


def test_manifesto_text_rendered_into_prompt(tmp_path: Path) -> None:
    _seed_manifestos(tmp_path)
    template = load_template("critic")
    rendered = template.format(
        pr_url="https://example.com/pr/1",
        issue_number=1,
        manifestos=load_manifestos_text(tmp_path),
        round_number=0,
        round_guidance="ROUND 1 (first review of this PR).",
    )
    assert "AR-001: never alpha." in rendered
    assert "BR-001: never beta." in rendered
    assert "manifesto_violations" in rendered  # JSON schema instruction present


# ---------------------------------------------------------------------------
# Report parsing
# ---------------------------------------------------------------------------


def test_critic_report_parses_violations_field() -> None:
    blob = json.dumps({
        "overall": "request_changes",
        "findings": [],
        "manifesto_violations": [
            {
                "rule_id": "EH-001",
                "manifesto": "error-handling.md",
                "quote": "except Exception: pass",
                "suggested_fix": "narrow the except and log",
                "severity": "sev1",
            },
        ],
    })
    report, err = parse_report_from_text(blob)
    assert err is None
    assert report is not None
    assert len(report.manifesto_violations) == 1
    v = report.manifesto_violations[0]
    assert isinstance(v, ManifestoViolation)
    assert v.rule_id == "EH-001"
    assert v.manifesto == "error-handling.md"
    assert v.quote == "except Exception: pass"
    assert v.suggested_fix == "narrow the except and log"
    assert v.severity == "sev1"
    assert report.has_sev1_manifesto_violation() is True


def test_critic_report_back_compat_no_field() -> None:
    blob = json.dumps({
        "overall": "approve",
        "findings": [
            {"severity": "sev3", "category": "style", "file": None,
             "line": None, "message": "rename var"},
        ],
    })
    report, err = parse_report_from_text(blob)
    assert err is None
    assert report is not None
    assert report.manifesto_violations == []
    assert report.overall == "approve"


def test_sev1_violation_forces_request_changes() -> None:
    # Model says "approve" but emits a sev1 violation — coercion flips it.
    blob = json.dumps({
        "overall": "approve",
        "findings": [],
        "manifesto_violations": [
            {
                "rule_id": "EH-001",
                "manifesto": "error-handling.md",
                "quote": "except Exception: pass",
                "suggested_fix": "narrow it",
                "severity": "sev1",
            },
        ],
    })
    report, err = parse_report_from_text(blob)
    assert err is None
    assert report is not None
    assert report.overall == "request_changes"


def test_sev2_violation_does_not_block() -> None:
    # sev2 leaves overall untouched and does not flip to request_changes.
    blob = json.dumps({
        "overall": "approve",
        "findings": [],
        "manifesto_violations": [
            {
                "rule_id": "EH-003",
                "manifesto": "error-handling.md",
                "quote": "print('debug')",
                "suggested_fix": "use logger",
                "severity": "sev2",
            },
        ],
    })
    report, err = parse_report_from_text(blob)
    assert err is None
    assert report is not None
    assert report.overall == "approve"
    assert len(report.manifesto_violations) == 1


# ---------------------------------------------------------------------------
# Adversarial / sad-path
# ---------------------------------------------------------------------------


def test_malformed_violation_entry_is_skipped() -> None:
    blob = json.dumps({
        "overall": "approve",
        "findings": [],
        "manifesto_violations": [
            {"manifesto": "x.md", "severity": "sev1"},  # missing rule_id
            {"rule_id": "X-1", "severity": "sev1"},      # missing manifesto
            "not-a-dict",
            {
                "rule_id": "Y-1",
                "manifesto": "y.md",
                "quote": "q",
                "suggested_fix": "f",
                "severity": "sev3",
            },
        ],
    })
    report, err = parse_report_from_text(blob)
    assert err is None
    assert report is not None
    assert [v.rule_id for v in report.manifesto_violations] == ["Y-1"]


def test_unknown_severity_treated_as_sev3() -> None:
    blob = json.dumps({
        "overall": "approve",
        "findings": [],
        "manifesto_violations": [
            {
                "rule_id": "Z-1",
                "manifesto": "z.md",
                "quote": "q",
                "suggested_fix": "f",
                "severity": "critical-omg",
            },
        ],
    })
    report, err = parse_report_from_text(blob)
    assert err is None
    assert report is not None
    assert len(report.manifesto_violations) == 1
    assert report.manifesto_violations[0].severity == "sev3"
    # Defensive default must NOT cascade to a verdict flip.
    assert report.overall == "approve"


def test_violations_field_wrong_type_is_ignored() -> None:
    blob = json.dumps({
        "overall": "approve",
        "findings": [],
        "manifesto_violations": "not a list",
    })
    report, err = parse_report_from_text(blob)
    assert err is None
    assert report is not None
    assert report.manifesto_violations == []


# ---------------------------------------------------------------------------
# Auto-merge gate
# ---------------------------------------------------------------------------


def test_automerge_blocked_on_sev1_violation() -> None:
    report = CriticReport(
        overall="request_changes",  # already flipped by _coerce_report
        findings=[],
        manifesto_violations=[
            ManifestoViolation(
                rule_id="EH-001",
                manifesto="error-handling.md",
                quote="except Exception: pass",
                suggested_fix="narrow it",
                severity="sev1",
            ),
        ],
    )
    plan = plan_actions(
        report,
        pr_changed_lines=10,
        block_on_sev2=False,
        min_findings_for_approve=0,
    )
    assert plan.block_merge is True
    assert "critic:blocking" in plan.labels_to_add
    assert "critic:manifesto-violation" in plan.labels_to_add
    assert "sev1_manifesto_violation" in plan.reason


def test_automerge_not_blocked_on_sev2_violation_only() -> None:
    report = CriticReport(
        overall="approve",
        findings=[],
        manifesto_violations=[
            ManifestoViolation(
                rule_id="EH-003",
                manifesto="error-handling.md",
                quote="print('x')",
                suggested_fix="use logger",
                severity="sev2",
            ),
        ],
    )
    plan = plan_actions(
        report,
        # large enough that an empty-finding approve isn't considered
        # suspicious — we want to verify sev2 manifesto alone doesn't block.
        pr_changed_lines=0,
        block_on_sev2=False,
        min_findings_for_approve=1000,
    )
    assert plan.block_merge is False
    assert "critic:manifesto-violation" not in plan.labels_to_add
