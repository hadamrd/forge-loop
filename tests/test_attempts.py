"""Tests for attempts.py — render + parse round-trip."""

from __future__ import annotations

from forge_loop.attempts import MARKER, AttemptRecord, parse_history, render_comment


def test_render_includes_marker_and_payload() -> None:
    rec = AttemptRecord(
        ts="2026-05-26T16:00:00Z",
        status="merged",
        pr_url="https://github.com/h/r/pull/942",
        duration_s=83.4,
        note="shipped clean",
        event_count=3,
    )
    body = render_comment(rec)
    assert MARKER in body
    assert "merged" in body.lower()
    assert "942" in body
    assert "shipped clean" in body
    # Embedded JSON block
    assert "```json" in body
    assert '"status": "merged"' in body


def test_parse_history_extracts_attempts_in_order() -> None:
    rec1 = AttemptRecord(ts="2026-05-26T10:00:00Z", status="failed",
                         pr_url=None, duration_s=42.0, note="brief unclear",
                         event_count=1)
    rec2 = AttemptRecord(ts="2026-05-26T11:00:00Z", status="merged",
                         pr_url="https://x/p/1", duration_s=58.0, note="",
                         event_count=2)
    comments = [render_comment(rec1), "some other comment without marker", render_comment(rec2)]
    history = parse_history(comments)
    assert len(history) == 2
    assert history[0]["status"] == "failed"
    assert history[0]["note"] == "brief unclear"
    assert history[1]["status"] == "merged"
    assert history[1]["pr_url"] == "https://x/p/1"


def test_parse_history_ignores_non_marker_comments() -> None:
    assert parse_history(["just a comment", "another one"]) == []


def test_parse_history_skips_malformed_json_blocks() -> None:
    bad = MARKER + "\n```json\n{not valid json\n```"
    assert parse_history([bad]) == []


def test_render_skips_pr_line_when_no_pr() -> None:
    rec = AttemptRecord(ts="t", status="failed", pr_url=None,
                        duration_s=10.0, note="", event_count=0)
    body = render_comment(rec)
    assert "PR:" not in body
