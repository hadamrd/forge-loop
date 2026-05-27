"""Tests for maintenance.py — outcome parsing (subprocess not exercised)."""

from __future__ import annotations

import json
from pathlib import Path

from forge_loop.maintenance import _parse_outcome, _tail


def _write_stream_log(path: Path, result_text: str) -> None:
    """Synthesise a claude stream-json log with a single `result` event."""
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": "thinking"},
        {"type": "result", "subtype": "success", "result": result_text},
    ]
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def test_parse_outcome_extracts_trailing_json(tmp_path: Path) -> None:
    log = tmp_path / "m.log"
    _write_stream_log(
        log,
        "Summary of work:\n"
        '{"acted_on": 7, "added_ready": [962, 967], "closed_dupes": [905], "retitled": [880]}',
    )
    out = _parse_outcome(log)
    assert out["acted_on"] == 7
    assert out["added_ready"] == [962, 967]
    assert out["closed_dupes"] == [905]
    assert out["retitled"] == [880]


def test_parse_outcome_falls_back_to_regex_for_acted_on(tmp_path: Path) -> None:
    log = tmp_path / "m.log"
    _write_stream_log(log, 'I acted on 5 things. "acted_on": 5')
    out = _parse_outcome(log)
    assert out["acted_on"] == 5


def test_parse_outcome_empty_log_returns_zero(tmp_path: Path) -> None:
    log = tmp_path / "m.log"
    _write_stream_log(log, "")
    out = _parse_outcome(log)
    assert out["acted_on"] == 0


def test_tail_reads_last_n_bytes(tmp_path: Path) -> None:
    p = tmp_path / "log"
    p.write_text("a" * 1000 + "END")
    assert _tail(p, 3) == "END"


def test_tail_missing_file_returns_empty(tmp_path: Path) -> None:
    assert _tail(tmp_path / "nope", 10) == ""
