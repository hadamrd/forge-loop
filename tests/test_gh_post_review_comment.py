"""Regression: a critic finding must ALWAYS land on the PR (#234).

Root cause it guards (found 2026-06-05 while supervising a 44-min non-converging
repair loop): the critic posts findings as INLINE review comments pinned to
file:line; GitHub 422-rejects an inline comment whose line is not in the PR's
diff. The old code returned False and dropped the finding. Because the repair
worker rebuilds its brief from the *posted* review context, a dropped finding
blinds the repair loop and it never converges. The fix falls back to a plain
review comment (with the location preserved in text) so the finding always
reaches the worker.

Ported in #223 from subprocess-level mocking to the GhClient: the inline
attempt and the plain-comment fallback are now driven through
``GithubkitClient.post_review_comment`` (REST ``pulls.create_review``), so we
exercise the real fallback code path with a stubbed githubkit REST surface —
no ``gh`` CLI, no network.
"""

from __future__ import annotations

from typing import Any

import pytest

from forge_loop.gh_client import GhError, GithubkitClient


class _FakeReviews:
    """Records ``pulls.create_review`` calls and simulates inline 422s."""

    def __init__(self, *, fail_inline: bool = False, fail_all: bool = False) -> None:
        self.fail_inline = fail_inline
        self.fail_all = fail_all
        self.calls: list[dict[str, Any]] = []

    def create_review(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        is_inline = bool(kwargs.get("comments"))
        if self.fail_all or (is_inline and self.fail_inline):
            raise GhError("create_review", 422, "line must be part of the diff")
        return object()  # truthy response; _raise_if_error tolerates non-Response


def _client_with(reviews: _FakeReviews) -> GithubkitClient:
    client = GithubkitClient.__new__(GithubkitClient)
    client._auth_source = "test"  # type: ignore[attr-defined]
    client._gh = type("GH", (), {"rest": type("R", (), {"pulls": reviews})()})()  # type: ignore[attr-defined]
    # _raise_if_error reads status_code/text off the response; our dummy has
    # neither, so it is treated as success (status None). Failures are raised
    # by _FakeReviews directly.
    return client


def _post(client: GithubkitClient, **kw: Any) -> bool:
    return client.post_review_comment("o", "r", 231, **kw)


def test_inline_success_does_not_fall_back() -> None:
    reviews = _FakeReviews()
    client = _client_with(reviews)
    assert _post(client, body="msg", file="src/x.py", line=10) is True
    assert len(reviews.calls) == 1
    assert reviews.calls[0]["comments"][0]["path"] == "src/x.py"


def test_inline_422_falls_back_to_plain_comment_with_location() -> None:
    """THE regression: inline rejected (line not in diff) -> finding still lands."""
    reviews = _FakeReviews(fail_inline=True)
    client = _client_with(reviews)

    ok = _post(client, body="broad except swallows error", file="src/x.py", line=999)

    assert ok is True, "a finding on an out-of-diff line must still be posted, not dropped"
    assert len(reviews.calls) == 2, "inline attempt + plain fallback"
    assert reviews.calls[0]["comments"], "inline attempted first"
    fallback = reviews.calls[1]
    assert "comments" not in fallback, "fallback is a plain (non-inline) review"
    assert "src/x.py:999" in fallback["body"], "fallback must preserve the location"
    assert "broad except swallows error" in fallback["body"], "fallback preserves the finding"


def test_both_paths_fail_returns_false() -> None:
    client = _client_with(_FakeReviews(fail_all=True))
    assert _post(client, body="msg", file="src/x.py", line=999) is False


def test_summary_comment_uses_plain_path() -> None:
    reviews = _FakeReviews()
    client = _client_with(reviews)
    assert _post(client, body="summary") is True
    assert len(reviews.calls) == 1
    assert "comments" not in reviews.calls[0], "no file/line -> no inline attempt"


@pytest.mark.parametrize("fail_inline", [True, False])
def test_facade_routes_through_client(fail_inline: bool) -> None:
    """The gh_issues facade delegates to the client's post_review_comment."""
    from forge_loop import gh_issues
    from forge_loop.gh_client import MockGhClient

    client = MockGhClient(inline_review_fails=fail_inline)
    gh_issues.set_client(client)
    try:
        ok = gh_issues.post_review_comment(231, "msg", file="src/x.py", line=10, repo="o/r")
    finally:
        gh_issues.set_client(None)
    assert ok is True
    methods = [c[0] for c in client.calls]
    assert "post_review_comment_inline" in methods
    if fail_inline:
        assert "post_review_comment_plain" in methods
