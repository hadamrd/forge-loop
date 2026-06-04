"""Tests for attempts.py — render + parse round-trip + fingerprint guards."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from forge_loop import attempts as _attempts
from forge_loop.attempts import (
    MARKER,
    AttemptRecord,
    classify_skip,
    compute_fingerprint,
    cooldown_from_env,
    fetch_issue_attempts,
    parse_blocking_comments,
    parse_history,
    parse_history_strict,
    render_comment,
)


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
    rec1 = AttemptRecord(
        ts="2026-05-26T10:00:00Z",
        status="failed",
        pr_url=None,
        duration_s=42.0,
        note="brief unclear",
        event_count=1,
    )
    rec2 = AttemptRecord(
        ts="2026-05-26T11:00:00Z",
        status="merged",
        pr_url="https://x/p/1",
        duration_s=58.0,
        note="",
        event_count=2,
    )
    comments = [render_comment(rec1), "some other comment without marker", render_comment(rec2)]
    history = parse_history(comments)
    assert len(history) == 2
    assert history[0]["status"] == "failed"
    assert history[0]["note"] == "brief unclear"
    assert history[1]["status"] == "merged"
    assert history[1]["pr_url"] == "https://x/p/1"


def test_parse_history_ignores_non_marker_comments() -> None:
    assert parse_history(["just a comment", "another one"]) == []


def test_parse_blocking_comments_extracts_critic_repair_contract() -> None:
    comments = [
        "nice work",
        "Post-merge critic found AR5 is not actually complete.\n\n"
        "Remaining blocker:\n"
        "- native proof is still entry-point-only.\n\n"
        "Required repair:\n"
        "1. Add a native stock-Git test.",
        "ordinary follow-up without critic keywords",
    ]

    blockers = parse_blocking_comments(comments)

    assert len(blockers) == 1
    assert "Remaining blocker" in blockers[0]
    assert "native stock-Git test" in blockers[0]


def test_parse_history_skips_malformed_json_blocks() -> None:
    bad = MARKER + "\n```json\n{not valid json\n```"
    assert parse_history([bad]) == []


def test_render_skips_pr_line_when_no_pr() -> None:
    rec = AttemptRecord(
        ts="t", status="failed", pr_url=None, duration_s=10.0, note="", event_count=0
    )
    body = render_comment(rec)
    assert "PR:" not in body


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_stable_for_identical_inputs() -> None:
    fp1 = compute_fingerprint(42, "body", "tmpl-hash")
    fp2 = compute_fingerprint(42, "body", "tmpl-hash")
    assert fp1 == fp2
    # 64-char hex
    assert len(fp1) == 64


def test_fingerprint_changes_when_body_changes() -> None:
    fp1 = compute_fingerprint(42, "body v1", "tmpl-hash")
    fp2 = compute_fingerprint(42, "body v2", "tmpl-hash")
    assert fp1 != fp2


def test_fingerprint_changes_when_template_changes() -> None:
    fp1 = compute_fingerprint(42, "body", "tmpl-v1")
    fp2 = compute_fingerprint(42, "body", "tmpl-v2")
    assert fp1 != fp2


def test_fingerprint_changes_when_issue_changes() -> None:
    fp1 = compute_fingerprint(42, "body", "tmpl")
    fp2 = compute_fingerprint(43, "body", "tmpl")
    assert fp1 != fp2


def test_record_persists_fingerprint_in_json_block() -> None:
    rec = AttemptRecord(
        ts="2026-05-26T10:00:00Z",
        status="open",
        pr_url="https://x/p/9",
        duration_s=30.0,
        note="",
        event_count=0,
        brief_fingerprint="deadbeef" * 8,
    )
    body = render_comment(rec)
    parsed = parse_history([body])
    assert parsed[0]["brief_fingerprint"] == "deadbeef" * 8


# ---------------------------------------------------------------------------
# classify_skip: in-flight, cooldown, expiry
# ---------------------------------------------------------------------------

FP = "a" * 64
OTHER_FP = "b" * 64
NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)


def _rec(
    status: str, *, ts: datetime, fp: str = FP, pr_url: str | None = None
) -> dict[str, object]:
    return {
        "ts": ts.isoformat(timespec="seconds"),
        "status": status,
        "pr_url": pr_url,
        "brief_fingerprint": fp,
    }


def test_skip_in_flight_fires_when_open_pr_for_same_fingerprint() -> None:
    history = [_rec("open", ts=NOW - timedelta(minutes=5), pr_url="https://github.com/o/r/pull/7")]
    d = classify_skip(history, FP, cooldown_s=3600, now=NOW)
    assert d.kind == "in_flight"
    assert d.pr_url == "https://github.com/o/r/pull/7"


def test_skip_cooldown_fires_within_window() -> None:
    history = [_rec("failed", ts=NOW - timedelta(minutes=10))]
    d = classify_skip(history, FP, cooldown_s=3600, now=NOW)
    assert d.kind == "cooldown"
    assert 0 < d.cooldown_remaining_s <= 3600


def test_skip_cooldown_releases_after_window() -> None:
    history = [_rec("failed", ts=NOW - timedelta(hours=2))]
    d = classify_skip(history, FP, cooldown_s=3600, now=NOW)
    assert d.kind == ""


def test_skip_ignores_attempts_with_different_fingerprint() -> None:
    history = [_rec("open", ts=NOW - timedelta(minutes=1), fp=OTHER_FP, pr_url="https://x/p/1")]
    d = classify_skip(history, FP, cooldown_s=3600, now=NOW)
    assert d.kind == ""


def test_skip_uses_latest_matching_attempt_not_oldest() -> None:
    # An older failure followed by a recent merged-but-now-open dispatch
    # should classify based on the most recent matching record.
    history = [
        _rec("failed", ts=NOW - timedelta(hours=5)),
        _rec("open", ts=NOW - timedelta(minutes=2), pr_url="https://x/p/9"),
    ]
    d = classify_skip(history, FP, cooldown_s=3600, now=NOW)
    assert d.kind == "in_flight"
    assert d.pr_url == "https://x/p/9"


def test_skip_no_history_returns_empty() -> None:
    assert classify_skip([], FP, cooldown_s=3600, now=NOW).kind == ""


def test_skip_empty_fingerprint_never_skips() -> None:
    # Defensive: if the runner couldn't compute a fingerprint, don't skip.
    history = [_rec("open", ts=NOW, pr_url="https://x/p/1")]
    assert classify_skip(history, "", cooldown_s=3600, now=NOW).kind == ""


def test_skip_merged_status_does_not_fire() -> None:
    history = [_rec("merged", ts=NOW - timedelta(minutes=5))]
    assert classify_skip(history, FP, cooldown_s=3600, now=NOW).kind == ""


def test_skip_timeout_treated_as_failure_for_cooldown() -> None:
    history = [_rec("timeout", ts=NOW - timedelta(minutes=5))]
    d = classify_skip(history, FP, cooldown_s=3600, now=NOW)
    assert d.kind == "cooldown"


# ---------------------------------------------------------------------------
# Corruption surface
# ---------------------------------------------------------------------------


def test_parse_history_strict_counts_corrupt_rows() -> None:
    good = render_comment(
        AttemptRecord(
            ts="2026-05-26T10:00:00Z",
            status="merged",
            pr_url="https://x/p/1",
            duration_s=1.0,
            note="",
            event_count=0,
            brief_fingerprint=FP,
        )
    )
    bad = MARKER + "\n```json\n{not valid json\n```"
    no_block = MARKER + " marker but no fenced block at all"
    records, corrupt = parse_history_strict([good, bad, no_block, "untagged"])
    assert len(records) == 1
    assert corrupt == 2


# ---------------------------------------------------------------------------
# fetch_issue_attempts: single fetch feeds both parsers (issue #226)
# ---------------------------------------------------------------------------


def test_fetch_issue_attempts_fetches_payload_once(monkeypatch) -> None:
    """The comment payload is fetched ONCE and fed to both the history parser
    and the blocking-comment parser — not two ``gh issue view`` round-trips."""
    rec = AttemptRecord(
        ts="2026-05-26T10:00:00Z",
        status="failed",
        pr_url=None,
        duration_s=1.0,
        note="brief unclear",
        event_count=1,
        brief_fingerprint="f" * 64,
    )
    bodies = [
        render_comment(rec),
        "Critic found AR5 incomplete.\nRemaining blocker: add a native test.",
        "ordinary chatter",
    ]
    calls: list[tuple[int, str | None]] = []

    def fake_bodies(issue: int, repo: str | None = None) -> list[str]:
        calls.append((issue, repo))
        return bodies

    monkeypatch.setattr(_attempts._gh, "issue_comment_bodies", fake_bodies)

    view = fetch_issue_attempts(99, repo="o/r")

    # Exactly one fetch — the core acceptance criterion of #226.
    assert calls == [(99, "o/r")]
    # Both views derived from that single payload.
    assert len(view.history) == 1
    assert view.history[0]["status"] == "failed"
    assert view.corrupt == 0
    assert len(view.blocking_comments) == 1
    assert "Remaining blocker" in view.blocking_comments[0]


def test_fetch_issue_attempts_requires_repo() -> None:
    """Adversarial: missing repo raises rather than silently shelling out."""
    import pytest

    with pytest.raises(RuntimeError):
        fetch_issue_attempts(1, repo=None)


def test_fetch_issue_attempts_empty_payload(monkeypatch) -> None:
    """Adversarial: empty comment list ⇒ empty views, still one fetch."""
    calls: list[int] = []

    def fake_bodies(issue: int, repo: str | None = None) -> list[str]:
        calls.append(issue)
        return []

    monkeypatch.setattr(_attempts._gh, "issue_comment_bodies", fake_bodies)

    view = fetch_issue_attempts(7, repo="o/r")
    assert view.history == []
    assert view.corrupt == 0
    assert view.blocking_comments == []
    assert calls == [7]


def test_cooldown_from_env_default(monkeypatch) -> None:
    monkeypatch.delenv("LOOP_RETRY_COOLDOWN_S", raising=False)
    assert cooldown_from_env() == 3600


def test_cooldown_from_env_override(monkeypatch) -> None:
    monkeypatch.setenv("LOOP_RETRY_COOLDOWN_S", "7")
    assert cooldown_from_env() == 7


def test_cooldown_from_env_garbage_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("LOOP_RETRY_COOLDOWN_S", "not-a-number")
    assert cooldown_from_env(default_s=42) == 42
