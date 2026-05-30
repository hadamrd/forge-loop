"""Regression tests for the legacy gh.py surface backed by GhClient."""

from __future__ import annotations

import pytest

from forge_loop import gh, gh_issues
from forge_loop.gh_client import GhError, Issue, MockGhClient


@pytest.fixture(autouse=True)
def _reset_client() -> None:
    gh_issues.set_client(None)
    yield
    gh_issues.set_client(None)


def test_top_issues_uses_typed_client_not_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    client = MockGhClient(
        issues_by_label_response=[
            Issue(number=1, title="one", body="body", labels=["loop:ready", "axis:cli"])
        ]
    )
    gh_issues.set_client(client)
    monkeypatch.setattr(
        gh.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("subprocess must not run")),
    )

    issues = gh.top_issues("loop:ready", 10, repo="owner/repo")

    assert issues == [
        {
            "number": 1,
            "title": "one",
            "body": "body",
            "state": "open",
            "labels": [{"name": "loop:ready"}, {"name": "axis:cli"}],
            "createdAt": "",
            "updatedAt": "",
        }
    ]
    assert client.calls == [
        ("issues_by_label", {"owner": "owner", "repo": "repo", "label": "loop:ready", "limit": 10})
    ]


def test_fetch_issue_uses_typed_client_and_preserves_none() -> None:
    client = MockGhClient(
        issues={("owner", "repo", 2): Issue(number=2, title="two", body="b", state="closed")}
    )
    gh_issues.set_client(client)

    issue = gh.fetch_issue(2, repo="owner/repo")
    missing = gh.fetch_issue(3, repo="owner/repo")

    assert issue is not None
    assert issue["number"] == 2
    assert issue["state"] == "closed"
    assert missing is None


def test_issue_mutations_use_typed_client_and_keep_best_effort_semantics() -> None:
    client = MockGhClient(raise_on={"add_comment": GhError("add_comment", 500, "boom")})
    gh_issues.set_client(client)

    gh.comment(4, "hello", repo="owner/repo")
    gh.label(4, ["a", "b"], repo="owner/repo")
    gh.unlabel(4, "a", repo="owner/repo")

    assert [call[0] for call in client.calls] == ["add_comment", "add_labels", "remove_label"]
    assert client.calls[1][1]["labels"] == ["a", "b"]
    assert client.calls[2][1]["label"] == "a"


def test_create_issue_returns_number_and_none_on_failure() -> None:
    client = MockGhClient(create_issue_responses=[44])
    gh_issues.set_client(client)

    assert gh.create_issue("title", "body", ["x"], repo="owner/repo") == 44

    client.raise_on_create_titles["bad"] = GhError("create_issue", 422, "bad")
    assert gh.create_issue("bad", "body", repo="owner/repo") is None
