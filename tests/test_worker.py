"""Tests for worker.py — outcome parsing + branch naming + brief shape.

The subprocess call to `claude -p` is NOT exercised here (that's an
integration / smoke concern). Focus on the deterministic parsing logic.
"""

from __future__ import annotations

import json
from pathlib import Path

from forge_loop.worker import (
    _branch_name,
    _extract_outcome,
    _read_subagent_events,
    _tail,
    make_brief,
)


def test_branch_name_slugifies_title() -> None:
    b = _branch_name(942, "fix(api): webhook HMAC fail-closed on blank secret")
    assert b.startswith("loop/942-")
    assert "fix" in b
    assert "api" in b
    assert " " not in b
    # ≤40-char suffix after the issue number
    suffix = b.split("-", 1)[1]
    assert len(suffix) <= 50


def test_branch_name_empty_title_falls_back() -> None:
    assert _branch_name(1, "") == "loop/1-fix"
    assert _branch_name(2, "!!!") == "loop/2-fix"


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
    assert brief.count("a") <= 6500  # body truncation in effect


def _write_stream_log(path: Path, result_text: str) -> None:
    """Synthesise a claude stream-json log with a single `result` event."""
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": "thinking"},
        {"type": "result", "subtype": "success", "result": result_text},
    ]
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def test_extract_outcome_parses_trailing_json_object(tmp_path: Path) -> None:
    log = tmp_path / "w.log"
    _write_stream_log(
        log,
        'Some narrative...\n{"issue": 933, "pr": "https://github.com/h/r/pull/942", '
        '"status": "merged", "note": "shipped"}',
    )
    pr, status = _extract_outcome(log)
    assert pr == "https://github.com/h/r/pull/942"
    assert status == "merged"


def test_extract_outcome_regex_fallback_when_no_trailing_json(tmp_path: Path) -> None:
    log = tmp_path / "w.log"
    _write_stream_log(log, "PR opened: https://github.com/foo/bar/pull/123 ready for review")
    pr, status = _extract_outcome(log)
    assert pr == "https://github.com/foo/bar/pull/123"
    assert status == "open"


def test_extract_outcome_empty_result_returns_no_pr(tmp_path: Path) -> None:
    log = tmp_path / "w.log"
    _write_stream_log(log, "")
    pr, status = _extract_outcome(log)
    assert pr is None
    assert status == "no_pr"


def test_extract_outcome_picks_last_json_when_multiple_present(tmp_path: Path) -> None:
    log = tmp_path / "w.log"
    _write_stream_log(
        log,
        '{"issue": 1, "pr": "https://github.com/a/b/pull/1", "status": "open"}\n'
        '{"issue": 1, "pr": "https://github.com/a/b/pull/2", "status": "merged"}',
    )
    pr, status = _extract_outcome(log)
    assert pr == "https://github.com/a/b/pull/2"
    assert status == "merged"


def test_extract_outcome_handles_bad_json_gracefully(tmp_path: Path) -> None:
    log = tmp_path / "w.log"
    _write_stream_log(log, "{this isn't json}\n{also broken")
    pr, status = _extract_outcome(log)
    assert pr is None
    assert status == "no_pr"


def test_tail_reads_last_n_bytes(tmp_path: Path) -> None:
    p = tmp_path / "log"
    p.write_text("a" * 1000 + "Z")
    assert _tail(p, 5).endswith("Z")
    assert len(_tail(p, 5)) == 5


def test_tail_missing_file_returns_empty(tmp_path: Path) -> None:
    assert _tail(tmp_path / "nope", 100) == ""


def test_read_subagent_events_parses_jsonl(tmp_path: Path) -> None:
    (tmp_path / "sprint-events.jsonl").write_text(
        '{"ts":"2026-01-01T00:00:00Z","kind":"bug_found","detail":"X"}\n'
        '{"ts":"2026-01-01T00:00:05Z","kind":"pr_opened","url":"https://github.com/h/r/pull/1"}\n'
        "not-json-line\n"
        '{"ts":"2026-01-01T00:00:10Z","kind":"merged"}\n'
    )
    events = _read_subagent_events(tmp_path)
    assert len(events) == 3  # bad line skipped
    assert events[0]["kind"] == "bug_found"
    assert events[1]["url"] == "https://github.com/h/r/pull/1"
    assert events[2]["kind"] == "merged"


def test_read_subagent_events_no_file_returns_empty(tmp_path: Path) -> None:
    assert _read_subagent_events(tmp_path) == []


def test_make_brief_includes_history_section_when_past_attempts(tmp_path: Path) -> None:
    issue = {"number": 942, "title": "fix x", "body": "Do the thing."}
    past = [
        {"ts": "2026-05-26T10:00:00Z", "status": "failed", "note": "test missing", "pr_url": None},
        {"ts": "2026-05-26T11:00:00Z", "status": "merged", "note": "shipped",
         "pr_url": "https://github.com/h/r/pull/9"},
    ]
    brief = make_brief(issue, tmp_path / "w", past_attempts=past)
    assert "PREVIOUS ATTEMPTS" in brief
    assert "test missing" in brief
    assert "https://github.com/h/r/pull/9" in brief


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


def test_make_brief_default_keeps_automerge(tmp_path: Path) -> None:
    issue = {"number": 942, "title": "fix x", "body": ""}
    brief = make_brief(issue, tmp_path / "w")
    assert "gh pr merge" in brief
    assert "--auto" in brief
    assert "DO NOT enable auto-merge" not in brief


# Gradle/WSL-OOM guard tests removed: forge-loop is stack-agnostic; the
# generic brief now says "avoid full-suite runs" without project-specific
# JVM flag pinning. Operators add their own gates via project tooling.
