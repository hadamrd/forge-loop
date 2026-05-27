"""Tests for po.py — outcome parsing + substantive-body heuristic + idempotency."""

from __future__ import annotations

import json
from pathlib import Path

from forge_loop.po import (
    _extract_outcome,
    _has_expansion_marker,
    _looks_substantive,
)


def _stream_log(path: Path, result_text: str) -> None:
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": "..."},
        {"type": "result", "subtype": "success", "result": result_text},
    ]
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def test_extract_outcome_skipped_true(tmp_path: Path) -> None:
    log = tmp_path / "p.log"
    _stream_log(
        log,
        '{"issue": 942, "skipped": true, "reason": "already substantive",'
        ' "sections_added": []}',
    )
    out = _extract_outcome(log)
    assert out["skipped"] is True
    assert out["reason"] == "already substantive"


def test_extract_outcome_expanded_with_sections(tmp_path: Path) -> None:
    log = tmp_path / "p.log"
    _stream_log(
        log,
        '{"issue": 947, "skipped": false, "reason": "expanded",'
        ' "sections_added": ["acceptance", "test matrix"]}',
    )
    out = _extract_outcome(log)
    assert out["skipped"] is False
    assert "acceptance" in out["sections_added"]


def test_extract_outcome_no_final_json_falls_back(tmp_path: Path) -> None:
    log = tmp_path / "p.log"
    _stream_log(log, "I did some work but forgot the final JSON line.")
    out = _extract_outcome(log)
    assert out["skipped"] is False
    assert "no-final-json" in out["reason"]


def test_has_expansion_marker_detects() -> None:
    body = "## Problem\nfoo\n<!-- po-spec-expanded -->\n"
    assert _has_expansion_marker(body) is True


def test_has_expansion_marker_negative_on_thin_body() -> None:
    assert _has_expansion_marker("fix this thing pls") is False
    assert _has_expansion_marker("") is False
    assert _has_expansion_marker(None) is False  # type: ignore[arg-type]


def test_looks_substantive_too_short_is_not_substantive() -> None:
    # Under 800 chars never counts even with all headers
    body = "## Acceptance criteria\n## Test plan\n## Out of scope"
    assert _looks_substantive(body) is False


def test_looks_substantive_long_with_two_headers_passes() -> None:
    body = (
        "## Acceptance criteria\n"
        + "- one\n- two\n- three\n" * 50
        + "\n## Test matrix\n"
        + "- unit test\n- integration test\n" * 10
    )
    assert len(body) >= 800
    assert _looks_substantive(body) is True


def test_looks_substantive_long_with_only_one_header_fails() -> None:
    body = "## Acceptance criteria\n" + "stuff " * 200
    assert len(body) >= 800
    # Only one of the 3 headers present → not substantive
    assert _looks_substantive(body) is False


def test_looks_substantive_empty_or_none() -> None:
    assert _looks_substantive("") is False
    assert _looks_substantive(None) is False  # type: ignore[arg-type]
