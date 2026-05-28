"""Tests for critic.py — verdict + typed CriticReport parsing."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from forge_loop import critic as critic_mod
from forge_loop.critic import (
    CriticReport,
    Finding,
    _extract_verdict,
    parse_report_from_log,
    parse_report_from_text,
    review_pr,
)


def _stream_log(path: Path, result_text: str) -> None:
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": "..."},
        {"type": "result", "subtype": "success", "result": result_text},
    ]
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def test_extract_verdict_approved_from_trailing_json(tmp_path: Path) -> None:
    log = tmp_path / "c.log"
    _stream_log(log, '{"verdict": "approved", "reasons": ["LGTM"], "issue": 942}')
    verdict, reasons = _extract_verdict(log)
    assert verdict == "approved"
    assert reasons == ["LGTM"]


def test_extract_verdict_changes_requested_with_reasons(tmp_path: Path) -> None:
    log = tmp_path / "c.log"
    _stream_log(
        log,
        '{"verdict": "changes_requested", '
        '"reasons": ["test missing", "scope creep"], "issue": 100}',
    )
    verdict, reasons = _extract_verdict(log)
    assert verdict == "changes_requested"
    assert "test missing" in reasons
    assert "scope creep" in reasons


def test_extract_verdict_text_fallback_approved(tmp_path: Path) -> None:
    log = tmp_path / "c.log"
    _stream_log(log, "Looking at the diff, the changes look approved to me.")
    verdict, reasons = _extract_verdict(log)
    assert verdict == "approved"
    assert reasons == []


def test_extract_verdict_text_fallback_changes_requested(tmp_path: Path) -> None:
    log = tmp_path / "c.log"
    _stream_log(log, "I'd say changes requested — the test doesn't actually exercise the fix.")
    verdict, reasons = _extract_verdict(log)
    assert verdict == "changes_requested"


def test_extract_verdict_empty_log_returns_error(tmp_path: Path) -> None:
    log = tmp_path / "c.log"
    _stream_log(log, "")
    verdict, reasons = _extract_verdict(log)
    assert verdict == "error"
    assert reasons == []


# ---------------------------------------------------------------------------
# Typed CriticReport — happy path
# ---------------------------------------------------------------------------

VALID_REPORT_JSON = json.dumps({
    "overall": "request_changes",
    "findings": [
        {"severity": "sev1", "category": "correctness",
         "file": "src/foo.py", "line": 42,
         "message": "off-by-one in pagination"},
        {"severity": "sev3", "category": "style",
         "file": None, "line": None, "message": "rename var"},
    ],
    "issue": 5,
})


def test_parse_report_valid_json_from_log(tmp_path: Path) -> None:
    log = tmp_path / "c.log"
    _stream_log(log, VALID_REPORT_JSON)
    report, err = parse_report_from_log(log)
    assert err is None
    assert isinstance(report, CriticReport)
    assert report.overall == "request_changes"
    assert len(report.findings) == 2
    f1 = report.findings[0]
    assert f1.severity == "sev1"
    assert f1.category == "correctness"
    assert f1.file == "src/foo.py"
    assert f1.line == 42
    assert report.has_sev1() is True


def test_parse_report_from_text_with_prose() -> None:
    text = (
        "Looking at the diff:\n"
        "- ran tests, all green\n"
        "Here is the report:\n"
        + VALID_REPORT_JSON
    )
    report, err = parse_report_from_text(text)
    assert err is None
    assert report is not None
    assert report.overall == "request_changes"


def test_parse_report_drops_invalid_findings() -> None:
    blob = json.dumps({
        "overall": "approve",
        "findings": [
            {"severity": "sev9", "category": "correctness", "message": "bad sev"},
            {"severity": "sev2", "category": "made-up", "message": "bad cat"},
            {"severity": "sev3", "category": "tests",
             "file": None, "line": None, "message": "ok finding"},
        ],
    })
    report, err = parse_report_from_text(blob)
    assert err is None
    assert report is not None
    assert len(report.findings) == 1
    assert report.findings[0].message == "ok finding"


def test_parse_report_invalid_overall_rejected() -> None:
    blob = json.dumps({"overall": "looks-ok", "findings": []})
    report, err = parse_report_from_text(blob)
    assert report is None
    assert err == "no_valid_report"


def test_parse_report_malformed_json() -> None:
    text = "not json at all, just words and { half-open"
    report, err = parse_report_from_text(text)
    assert report is None
    assert err == "no_valid_report"


# ---------------------------------------------------------------------------
# Retry-on-malformed + critic_parse_failed event
# ---------------------------------------------------------------------------

# NOTE: The legacy subprocess-driven retry tests (`_FakeRun` + the two
# retry tests) were deleted in issue #85 when critic.py migrated to the
# Claude Agent SDK. Equivalent retry coverage now lives in
# tests/test_critic_sdk.py::test_review_pr_unparseable_text_retries_then_errors
# which mocks the SDK boundary instead of subprocess.


def test_finding_is_valid_rejects_blank_message() -> None:
    f = Finding(severity="sev1", category="correctness", file=None, line=None, message="   ")
    assert f.is_valid() is False


def test_finding_is_valid_accepts_minimal_ok_finding() -> None:
    f = Finding(severity="sev3", category="docs", file=None, line=None, message="add docstring")
    assert f.is_valid() is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
