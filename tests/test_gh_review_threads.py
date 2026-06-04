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


def _thread(id_: str, *, resolved: bool = False) -> dict[str, Any]:
    return {
        "id": id_,
        "isResolved": resolved,
        "isOutdated": False,
        "path": "src/app.py",
        "line": 12,
        "comments": {"nodes": []},
    }


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
            # Batched query returns one alias (``pr0``) per PR.
            return _completed(
                {
                    "data": {
                        "repository": {"pr0": {"reviewThreads": {"nodes": [_thread("thread-1")]}}}
                    }
                }
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(gh.subprocess, "run", fake_run)

    prs = gh.prs_requiring_repair(5, repo="o/r")

    assert [p["number"] for p in prs] == [7]
    assert prs[0]["repairReasons"] == ["unresolved_review_threads"]
    assert prs[0]["unresolvedReviewThreads"][0]["id"] == "thread-1"


def test_prs_requiring_repair_issues_one_graphql_call_for_many_prs(monkeypatch) -> None:
    """Issue #226: N open PRs ⇒ exactly ONE GraphQL subprocess, not N."""
    pr_list = [
        {
            "number": n,
            "title": f"PR {n}",
            "body": "",
            "headRefName": f"loop/{n}",
            "baseRefName": "trunk",
            "url": f"https://github.com/o/r/pull/{n}",
            "labels": [],
            "updatedAt": f"2026-01-0{n}T00:00:00Z",
            "mergeStateStatus": "CLEAN",
        }
        for n in (1, 2, 3, 4, 5)
    ]
    graphql_calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "list"]:
            return _completed(pr_list)
        if cmd[:3] == ["gh", "api", "graphql"]:
            graphql_calls.append(cmd)
            # All PRs idle: every alias resolves to zero threads.
            return _completed(
                {
                    "data": {
                        "repository": {f"pr{i}": {"reviewThreads": {"nodes": []}} for i in range(5)}
                    }
                }
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(gh.subprocess, "run", fake_run)

    prs = gh.prs_requiring_repair(50, repo="o/r")

    assert prs == []  # idle repo, nothing to repair
    assert len(graphql_calls) == 1  # one batched call, NOT one per PR


def test_prs_requiring_repair_short_circuits_when_no_open_prs(monkeypatch) -> None:
    """Adversarial / idle path: zero open PRs ⇒ no GraphQL subprocess at all."""
    graphql_calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "list"]:
            return _completed([])
        if cmd[:3] == ["gh", "api", "graphql"]:
            graphql_calls.append(cmd)
            return _completed({"data": {"repository": {}}})
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(gh.subprocess, "run", fake_run)

    assert gh.prs_requiring_repair(50, repo="o/r") == []
    assert graphql_calls == []  # short-circuited before any GraphQL


def test_prs_requiring_repair_preserves_decisions_across_reasons(monkeypatch) -> None:
    """Regression: label / merge-state / thread reasons survive batching."""
    pr_list = [
        {
            "number": 1,
            "url": "u1",
            "labels": [{"name": "critic:blocking"}],
            "updatedAt": "2026-01-01T00:00:00Z",
            "mergeStateStatus": "CLEAN",
        },
        {
            "number": 2,
            "url": "u2",
            "labels": [],
            "updatedAt": "2026-01-02T00:00:00Z",
            "mergeStateStatus": "DIRTY",
        },
        {
            "number": 3,
            "url": "u3",
            "labels": [],
            "updatedAt": "2026-01-03T00:00:00Z",
            "mergeStateStatus": "CLEAN",
        },
        {
            "number": 4,
            "url": "u4",
            "labels": [],
            "updatedAt": "2026-01-04T00:00:00Z",
            "mergeStateStatus": "CLEAN",
        },
    ]

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "list"]:
            return _completed(pr_list)
        if cmd[:3] == ["gh", "api", "graphql"]:
            return _completed(
                {
                    "data": {
                        "repository": {
                            "pr0": {"reviewThreads": {"nodes": []}},
                            "pr1": {"reviewThreads": {"nodes": []}},
                            # PR #3 has an UNRESOLVED thread → flagged.
                            "pr2": {"reviewThreads": {"nodes": [_thread("t3")]}},
                            # PR #4 only has a RESOLVED thread → not flagged.
                            "pr3": {"reviewThreads": {"nodes": [_thread("t4", resolved=True)]}},
                        }
                    }
                }
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(gh.subprocess, "run", fake_run)

    prs = gh.prs_requiring_repair(50, repo="o/r")
    by_num = {p["number"]: p for p in prs}

    assert set(by_num) == {1, 2, 3}  # PR #4 (resolved-only) excluded
    assert by_num[1]["repairReasons"] == ["critic:blocking"]
    assert by_num[2]["repairReasons"] == ["merge_state:dirty"]
    assert by_num[3]["repairReasons"] == ["unresolved_review_threads"]
    assert by_num[3]["unresolvedReviewThreads"][0]["id"] == "t3"


def test_review_threads_batch_empty_input_issues_no_subprocess(monkeypatch) -> None:
    """Adversarial: empty PR list ⇒ no GraphQL subprocess, empty map."""

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"should not run: {cmd}")

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    assert gh.review_threads_batch([], repo="o/r") == {}


def test_review_threads_batch_total_failure_falls_back_per_pr(monkeypatch) -> None:
    """Sev2 (#226 review): a batch failure must NOT silently map every PR to [].

    The batched query fails (non-zero return), so the helper falls back to a
    per-PR fetch for each PR in the chunk. Here the per-PR fetches *also* fail,
    so every PR ends up [] — but the count of subprocesses proves the fallback
    actually ran (1 batched attempt + 1 per-PR attempt per PR), not a single
    swallow-and-blank-everything.
    """
    graphql_calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        graphql_calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    out = gh.review_threads_batch([1, 2], repo="o/r")
    assert out == {1: [], 2: []}
    # 1 batched query + 1 per-PR fallback query each for PRs 1 and 2.
    assert len(graphql_calls) == 3


def test_review_threads_batch_fallback_recovers_threads(monkeypatch) -> None:
    """Sev2 (#226 review): when the batch fails but per-PR succeeds, the
    ``unresolved_review_threads`` signal is recovered — not dropped for all PRs.
    """
    batched_calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        # A batched query aliases multiple PRs (pr0/pr1...); a single-PR query
        # passes `number=` as an -F flag. Distinguish them to simulate "batch
        # blows the complexity budget but the smaller per-PR queries succeed".
        is_single = any(arg.startswith("number=") for arg in cmd)
        if not is_single:
            batched_calls.append(cmd)
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="node limit exceeded"
            )
        # Per-PR fallback: PR 1 has an unresolved thread, PR 2 has none.
        number = next(arg.split("=", 1)[1] for arg in cmd if arg.startswith("number="))
        nodes = [_thread("recovered")] if number == "1" else []
        return _completed(
            {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}}
        )

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    out = gh.review_threads_batch([1, 2], repo="o/r")
    assert len(batched_calls) == 1  # the batch was attempted...
    assert out[1][0]["id"] == "recovered"  # ...and the per-PR fallback recovered PR 1
    assert out[2] == []


def test_review_threads_batch_chunks_large_pr_sets(monkeypatch) -> None:
    """Sev2 (#226 review): >chunk PRs are split into multiple bounded queries
    so one giant aliased query can't exceed GraphQL complexity limits.
    """
    chunk = gh._REVIEW_THREADS_BATCH_CHUNK
    pr_numbers = list(range(1, chunk * 2 + 2))  # two full chunks + a remainder
    batched_calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        query = next((arg for arg in cmd if arg.startswith("query=")), "")
        n_aliases = query.count("pullRequest(number:")
        assert n_aliases <= chunk  # no chunk exceeds the bound
        batched_calls.append(cmd)
        repo_obj = {f"pr{i}": {"reviewThreads": {"nodes": []}} for i in range(n_aliases)}
        return _completed({"data": {"repository": repo_obj}})

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    out = gh.review_threads_batch(pr_numbers, repo="o/r")
    assert set(out) == set(pr_numbers)
    assert len(batched_calls) == 3  # ceil((2*chunk+1)/chunk) == 3


def test_review_threads_batch_maps_aliases_back_to_pr_numbers(monkeypatch) -> None:
    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert cmd[:3] == ["gh", "api", "graphql"]
        return _completed(
            {
                "data": {
                    "repository": {
                        "pr0": {"reviewThreads": {"nodes": [_thread("a")]}},
                        "pr1": {"reviewThreads": {"nodes": []}},
                    }
                }
            }
        )

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    out = gh.review_threads_batch([11, 22], repo="o/r")
    assert set(out) == {11, 22}
    assert out[11][0]["id"] == "a"
    assert out[22] == []
