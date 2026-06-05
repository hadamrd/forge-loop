"""Regression tests for the gh_issues facade backed by the GhClient.

These guard the module-level facade (``forge_loop.gh_issues``) — the single
stringly-shaped surface the runner/CLI call. Every test drives a
:class:`MockGhClient` via ``gh_issues.set_client(...)``; nothing shells out to
the ``gh`` CLI (it no longer exists — issue #223 deleted ``forge_loop.gh``).
"""

from __future__ import annotations

from collections.abc import Generator

import pytest

from forge_loop import gh_issues
from forge_loop.gh_client import GhError, Issue, MockGhClient


@pytest.fixture(autouse=True)
def _reset_client() -> Generator[None, None, None]:
    gh_issues.set_client(None)
    yield
    gh_issues.set_client(None)


def test_top_issues_uses_typed_client() -> None:
    client = MockGhClient(
        issues_by_label_response=[
            Issue(number=1, title="one", body="body", labels=["loop:ready", "axis:cli"])
        ]
    )
    gh_issues.set_client(client)

    issues = gh_issues.top_issues("loop:ready", 10, repo="owner/repo")

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

    issue = gh_issues.fetch_issue(2, repo="owner/repo")
    missing = gh_issues.fetch_issue(3, repo="owner/repo")

    assert issue is not None
    assert issue["number"] == 2
    assert issue["state"] == "closed"
    assert missing is None


def test_issue_mutations_use_typed_client_and_keep_best_effort_semantics() -> None:
    client = MockGhClient(raise_on={"add_comment": GhError("add_comment", 500, "boom")})
    gh_issues.set_client(client)

    # comment() swallows the add_comment error (best-effort, caller logs).
    gh_issues.comment(4, "hello", repo="owner/repo")
    gh_issues.label(4, ["a", "b"], repo="owner/repo")
    gh_issues.unlabel(4, "a", repo="owner/repo")

    assert [call[0] for call in client.calls] == ["add_comment", "add_labels", "remove_label"]
    assert client.calls[1][1]["labels"] == ["a", "b"]
    assert client.calls[2][1]["label"] == "a"


def test_create_issue_returns_number_and_none_on_failure() -> None:
    client = MockGhClient(create_issue_responses=[44])
    gh_issues.set_client(client)

    assert gh_issues.create_issue("title", "body", ["x"], repo="owner/repo") == 44

    client.raise_on_create_titles["bad"] = GhError("create_issue", 422, "bad")
    assert gh_issues.create_issue("bad", "body", repo="owner/repo") is None


def test_pr_precommit_context_excludes_body_from_commit_text() -> None:
    client = MockGhClient(
        precommit_by_pr={
            1: (
                "Rule text may mention `git commit --no-verify`.",
                "real commit\nbody text",
            )
        }
    )
    gh_issues.set_client(client)

    body, commit_text = gh_issues.pr_precommit_context("https://github.com/o/r/pull/1", "o/r")

    assert "git commit --no-verify" in body
    assert "git commit --no-verify" not in commit_text
    assert "real commit" in commit_text
    assert "body text" in commit_text
    assert client.calls[-1] == (
        "pr_precommit_context",
        {"owner": "o", "repo": "r", "number": 1},
    )


def test_pr_precommit_context_returns_empty_when_client_has_nothing() -> None:
    gh_issues.set_client(MockGhClient())
    assert gh_issues.pr_precommit_context("https://github.com/o/r/pull/1", "o/r") == ("", "")
