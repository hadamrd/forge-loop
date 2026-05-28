from __future__ import annotations

import json
import subprocess
from typing import Any

from forge_loop import gh


def _completed(stdout: dict[str, Any] | list[Any]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["gh"],
        returncode=0,
        stdout=json.dumps(stdout),
        stderr="",
    )


def test_pr_review_context_includes_unresolved_inline_threads(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "view"]:
            return _completed(
                {
                    "number": 7,
                    "title": "Fix thing",
                    "body": "body",
                    "url": "https://github.com/o/r/pull/7",
                    "headRefName": "loop/42-fix-thing",
                    "comments": [],
                    "reviews": [],
                }
            )
        if cmd[:3] == ["gh", "api", "graphql"]:
            return _completed(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [
                                        {
                                            "id": "thread-1",
                                            "isResolved": False,
                                            "isOutdated": False,
                                            "path": "src/app.py",
                                            "line": 12,
                                            "comments": {
                                                "nodes": [
                                                    {
                                                        "author": {"login": "reviewer"},
                                                        "body": "Please handle the error path.",
                                                        "url": "https://github.com/o/r/pull/7#discussion",
                                                        "path": "src/app.py",
                                                        "line": 12,
                                                        "createdAt": "2026-01-01T00:00:00Z",
                                                    }
                                                ]
                                            },
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(gh.subprocess, "run", fake_run)

    context = gh.pr_review_context(7, repo="o/r")

    assert "REVIEW THREADS" in context
    assert "src/app.py:12 resolved=False reviewer: Please handle the error path." in context
    assert any(c[:3] == ["gh", "api", "graphql"] for c in calls)


def test_prs_requiring_repair_detects_threads_without_critic_label(monkeypatch) -> None:
    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "list"]:
            return _completed(
                [
                    {
                        "number": 7,
                        "title": "Fix thing",
                        "body": "closes #42",
                        "headRefName": "loop/42-fix-thing",
                        "baseRefName": "trunk",
                        "url": "https://github.com/o/r/pull/7",
                        "labels": [],
                        "updatedAt": "2026-01-01T00:00:00Z",
                        "mergeStateStatus": "CLEAN",
                    }
                ]
            )
        if cmd[:3] == ["gh", "api", "graphql"]:
            return _completed(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "nodes": [
                                        {
                                            "id": "thread-1",
                                            "isResolved": False,
                                            "isOutdated": False,
                                            "path": "src/app.py",
                                            "line": 12,
                                            "comments": {"nodes": []},
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(gh.subprocess, "run", fake_run)

    prs = gh.prs_requiring_repair(5, repo="o/r")

    assert [p["number"] for p in prs] == [7]
    assert prs[0]["repairReasons"] == ["unresolved_review_threads"]
    assert prs[0]["unresolvedReviewThreads"][0]["id"] == "thread-1"
