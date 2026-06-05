"""Regression: a critic finding must ALWAYS land on the PR.

Root cause it guards (found 2026-06-05 while supervising a 44-min non-converging
repair loop): the critic posts findings as INLINE review comments pinned to
file:line; GitHub 422-rejects an inline comment whose line is not in the PR's
diff. The old code returned False and dropped the finding. Because the repair
worker rebuilds its brief from the *posted* review context, a dropped finding
blinds the repair loop and it never converges. The fix falls back to a plain
``--comment`` review (with the location preserved in text) so the finding always
reaches the worker.
"""

from __future__ import annotations

import subprocess
from typing import Any

from forge_loop import gh


def _cp(cmd: Any, rc: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=cmd, returncode=rc, stdout="", stderr=stderr)


def _is_inline(cmd: list[str]) -> bool:
    return "api" in cmd and any("/reviews" in str(p) for p in cmd)


def _is_plain_review(cmd: list[str]) -> bool:
    return "review" in cmd and "--comment" in cmd


def test_inline_success_does_not_fall_back(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_k: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _cp(cmd, 0)

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    assert gh.post_review_comment(231, "msg", file="src/x.py", line=10, repo="o/r") is True
    assert any(_is_inline(c) for c in calls)
    assert not any(_is_plain_review(c) for c in calls)  # no needless fallback


def test_inline_422_falls_back_to_plain_comment_with_location(monkeypatch) -> None:
    """THE regression: inline rejected (line not in diff) -> finding still lands."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_k: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if _is_inline(cmd):
            return _cp(cmd, 1, stderr="HTTP 422: line must be part of the diff")
        return _cp(cmd, 0)

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    ok = gh.post_review_comment(
        231, "broad except swallows error", file="src/x.py", line=999, repo="o/r"
    )

    assert ok is True, "a finding on an out-of-diff line must still be posted, not dropped"
    assert any(_is_inline(c) for c in calls), "inline should be attempted first"
    review = next(c for c in calls if _is_plain_review(c))
    body = review[review.index("--body") + 1]
    assert "src/x.py:999" in body, "fallback must preserve the location"
    assert "broad except swallows error" in body, "fallback must preserve the finding text"


def test_both_paths_fail_returns_false(monkeypatch) -> None:
    monkeypatch.setattr(gh.subprocess, "run", lambda cmd, **_k: _cp(cmd, 1, "boom"))
    assert gh.post_review_comment(231, "msg", file="src/x.py", line=999, repo="o/r") is False


def test_summary_comment_uses_plain_path(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_k: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _cp(cmd, 0)

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    assert gh.post_review_comment(231, "summary", repo="o/r") is True
    assert any(_is_plain_review(c) for c in calls)
    assert not any(_is_inline(c) for c in calls)  # no file/line -> no inline attempt
