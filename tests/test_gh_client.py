"""Tests for the typed GitHub client (issue #83).

Pins the MockGhClient contract + the auth resolution + the GhError
shape. The githubkit-backed real impl is exercised only at the
boundary — full integration testing against api.github.com belongs
in a recorded-fixture pass, deferred to follow-up.
"""

from __future__ import annotations

import pytest

from forge_loop.gh_client import (
    GhError,
    Issue,
    MockGhClient,
    PullRequest,
    resolve_token,
)


# ---------------------------------------------------------------------------
# Auth resolution
# ---------------------------------------------------------------------------


def test_resolve_token_prefers_gh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "from-gh")
    monkeypatch.setenv("GITHUB_TOKEN", "from-actions")
    assert resolve_token() == "from-gh"


def test_resolve_token_falls_back_to_github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "from-actions")
    assert resolve_token() == "from-actions"


def test_resolve_token_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert resolve_token() is None


# ---------------------------------------------------------------------------
# MockGhClient — call recording + canned responses
# ---------------------------------------------------------------------------


def test_mock_records_calls_with_kwargs() -> None:
    gh = MockGhClient()
    gh.add_comment(owner="o", repo="r", number=1, body="hi")
    gh.add_labels(owner="o", repo="r", number=1, labels=["x", "y"])
    assert [c[0] for c in gh.calls] == ["add_comment", "add_labels"]
    assert gh.calls[0][1] == {"owner": "o", "repo": "r", "number": 1, "body": "hi"}
    assert gh.calls[1][1] == {"owner": "o", "repo": "r", "number": 1, "labels": ["x", "y"]}


def test_mock_get_issue_returns_preloaded() -> None:
    gh = MockGhClient(issues={
        ("o", "r", 42): Issue(number=42, title="t", body="b", labels=["a"]),
    })
    issue = gh.get_issue("o", "r", 42)
    assert issue is not None
    assert issue.title == "t"
    assert issue.labels == ["a"]


def test_mock_get_issue_returns_none_when_missing() -> None:
    gh = MockGhClient()
    assert gh.get_issue("o", "r", 999) is None


def test_mock_get_pull_returns_preloaded() -> None:
    gh = MockGhClient(pulls={
        ("o", "r", 5): PullRequest(number=5, title="pr", state="open", draft=True,
                                    head_ref="feat/x", additions=10, deletions=2),
    })
    pr = gh.get_pull("o", "r", 5)
    assert pr is not None
    assert pr.draft is True
    assert pr.head_ref == "feat/x"
    assert pr.additions == 10


def test_mock_issues_by_label_respects_limit() -> None:
    gh = MockGhClient(issues_by_label_response=[
        Issue(number=i, title=f"i{i}") for i in range(10)
    ])
    out = gh.issues_by_label("o", "r", "ready", limit=3)
    assert len(out) == 3
    assert out[0].number == 0


def test_mock_raise_on_fires_typed_error() -> None:
    err = GhError("add_comment", 403, "rate limited")
    gh = MockGhClient(raise_on={"add_comment": err})
    with pytest.raises(GhError) as excinfo:
        gh.add_comment(owner="o", repo="r", number=1, body="x")
    assert excinfo.value.status == 403
    assert "rate limited" in excinfo.value.body_tail


# ---------------------------------------------------------------------------
# GhError — message shape carries diagnostics
# ---------------------------------------------------------------------------


def test_gh_error_message_includes_method_status_body() -> None:
    e = GhError("create_issue", 422, "validation failed: title too long")
    msg = str(e)
    assert "create_issue" in msg
    assert "422" in msg
    assert "validation failed" in msg


def test_gh_error_truncates_long_body() -> None:
    long_body = "x" * 1000
    e = GhError("get_pull", 500, long_body)
    msg = str(e)
    # Body is truncated to <= 300 chars in the message
    assert len(msg) < 500


# ---------------------------------------------------------------------------
# Issue / PullRequest dataclasses — defaults sane
# ---------------------------------------------------------------------------


def test_issue_defaults() -> None:
    i = Issue(number=1, title="t")
    assert i.body == ""
    assert i.state == "open"
    assert i.labels == []


def test_pull_request_defaults() -> None:
    p = PullRequest(number=1, title="t")
    assert p.draft is False
    assert p.changed_files == 0
    assert p.labels == []
