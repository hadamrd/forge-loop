"""Tests for critic.py — verdict parsing."""

from __future__ import annotations

import json
from pathlib import Path

from forge_loop.critic import _extract_verdict


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
