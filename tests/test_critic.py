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

class _FakeRun:
    """Stub of subprocess.run that writes a canned log per call."""

    def __init__(self, payloads: list[str]):
        self.payloads = list(payloads)
        self.calls = 0

    def __call__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        # The 1st positional arg is the argv list; stdout kwarg is the file.
        log_fh = kwargs["stdout"]
        payload = self.payloads[min(self.calls, len(self.payloads) - 1)]
        events = [
            {"type": "system"},
            {"type": "result", "subtype": "success", "result": payload},
        ]
        for e in events:
            log_fh.write((json.dumps(e) + "\n").encode("utf-8"))
        log_fh.flush()
        self.calls += 1
        # Return a minimal CompletedProcess-shaped object.
        import subprocess as _sp
        return _sp.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")


def test_review_pr_retries_on_malformed_then_succeeds(tmp_path: Path) -> None:
    fake = _FakeRun([
        "this is not json — malformed first try",
        VALID_REPORT_JSON,
    ])
    emits: list[tuple[str, dict]] = []

    def emit(kind: str, payload: dict) -> None:
        emits.append((kind, payload))

    with patch.object(critic_mod, "subprocess") as sp_mod, \
            patch.object(critic_mod, "ensure_subagent_trusted", lambda _r: None):
        sp_mod.run = fake
        sp_mod.TimeoutExpired = __import__("subprocess").TimeoutExpired
        out = review_pr(
            "https://github.com/o/r/pull/1", 5,
            repo=tmp_path, logs_dir=tmp_path / "logs",
            timeout_s=10, emit=emit,
        )
    assert fake.calls == 2  # one retry consumed
    assert out.report is not None
    assert out.report.overall == "request_changes"
    assert out.verdict == "changes_requested"
    assert out.parse_retries == 1
    # No parse-failed event because retry succeeded.
    assert not any(k == "critic_parse_failed" for k, _ in emits)


def test_review_pr_emits_critic_parse_failed_after_retry(tmp_path: Path) -> None:
    fake = _FakeRun(["garbage one", "garbage two"])
    emits: list[tuple[str, dict]] = []

    with patch.object(critic_mod, "subprocess") as sp_mod, \
            patch.object(critic_mod, "ensure_subagent_trusted", lambda _r: None):
        sp_mod.run = fake
        sp_mod.TimeoutExpired = __import__("subprocess").TimeoutExpired
        out = review_pr(
            "https://github.com/o/r/pull/1", 5,
            repo=tmp_path, logs_dir=tmp_path / "logs",
            timeout_s=10, emit=lambda k, p: emits.append((k, p)),
        )
    assert fake.calls == 2
    assert out.report is None
    assert out.verdict == "error"
    assert out.parse_retries == 2
    kinds = [k for k, _ in emits]
    assert "critic_parse_failed" in kinds
    payload = next(p for k, p in emits if k == "critic_parse_failed")
    assert payload["issue"] == 5
    assert payload["retries"] == 2


def test_finding_is_valid_rejects_blank_message() -> None:
    f = Finding(severity="sev1", category="correctness", file=None, line=None, message="   ")
    assert f.is_valid() is False


def test_finding_is_valid_accepts_minimal_ok_finding() -> None:
    f = Finding(severity="sev3", category="docs", file=None, line=None, message="add docstring")
    assert f.is_valid() is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
