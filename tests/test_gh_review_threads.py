"""Review-thread / repair-selection tests, ported to the GhClient (#223).

Review threads + PR enrichment used to be ``gh api graphql`` subprocesses;
they now go through ``GithubkitClient`` (REST + ``githubkit.GitHub.graphql``).
These tests drive a :class:`MockGhClient` through the ``gh_issues`` facade for
the selection logic, and a real :class:`GithubkitClient` with a stubbed
``.graphql`` for the batching / chunking / per-PR-fallback behaviour — no ``gh``
CLI, no network.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest

from forge_loop import gh_issues
from forge_loop.gh_client import GithubkitClient, MockGhClient, PullRequest


@pytest.fixture(autouse=True)
def _reset_client() -> Generator[None, None, None]:
    gh_issues.set_client(None)
    yield
    gh_issues.set_client(None)


def _thread(id_: str, *, resolved: bool = False) -> dict[str, Any]:
    return {
        "id": id_,
        "isResolved": resolved,
        "isOutdated": False,
        "path": "src/app.py",
        "line": 12,
        "comments": [],
    }


# ---------------------------------------------------------------------------
# pr_review_context — composes PR body + comments + review threads
# ---------------------------------------------------------------------------


def test_pr_review_context_includes_unresolved_inline_threads() -> None:
    client = MockGhClient(
        pulls={
            ("o", "r", 7): PullRequest(
                number=7, title="Fix thing", body="body", head_ref="loop/42-fix-thing"
            )
        },
        review_threads_by_pr={
            7: [
                {
                    "id": "thread-1",
                    "isResolved": False,
                    "isOutdated": False,
                    "path": "src/app.py",
                    "line": 12,
                    "comments": [
                        {
                            "author": {"login": "reviewer"},
                            "body": "Please handle the error path.",
                            "path": "src/app.py",
                            "line": 12,
                        }
                    ],
                }
            ]
        },
    )
    gh_issues.set_client(client)

    context = gh_issues.pr_review_context(7, repo="o/r")

    assert "REVIEW THREADS" in context
    assert "src/app.py:12 resolved=False reviewer: Please handle the error path." in context


# ---------------------------------------------------------------------------
# prs_requiring_repair — enriches open PRs with repair reasons
# ---------------------------------------------------------------------------


def _open_pr(number: int, **over: Any) -> dict[str, Any]:
    base = {
        "number": number,
        "title": f"PR {number}",
        "body": "",
        "headRefName": f"loop/{number}",
        "baseRefName": "trunk",
        "url": f"https://github.com/o/r/pull/{number}",
        "labels": [],
        "updatedAt": f"2026-01-0{number}T00:00:00Z",
        "mergeStateStatus": "CLEAN",
    }
    base.update(over)
    return base


def test_prs_requiring_repair_detects_threads_without_critic_label() -> None:
    client = MockGhClient(
        open_prs_response=[_open_pr(7, body="closes #42", headRefName="loop/42-fix-thing")],
        review_threads_by_pr={7: [_thread("thread-1")]},
    )
    gh_issues.set_client(client)

    prs = gh_issues.prs_requiring_repair(5, repo="o/r")

    assert [p["number"] for p in prs] == [7]
    assert prs[0]["repairReasons"] == ["unresolved_review_threads"]
    assert prs[0]["unresolvedReviewThreads"][0]["id"] == "thread-1"


def test_prs_requiring_repair_short_circuits_when_no_open_prs() -> None:
    client = MockGhClient(open_prs_response=[])
    gh_issues.set_client(client)

    assert gh_issues.prs_requiring_repair(50, repo="o/r") == []
    # No PRs -> the batched review-threads call must not fire.
    assert "review_threads_batch" not in [c[0] for c in client.calls]


def test_prs_requiring_repair_preserves_decisions_across_reasons() -> None:
    client = MockGhClient(
        open_prs_response=[
            _open_pr(1, labels=[{"name": "critic:blocking"}]),
            _open_pr(2, mergeStateStatus="DIRTY"),
            _open_pr(3),
            _open_pr(4),
        ],
        review_threads_by_pr={
            3: [_thread("t3")],  # unresolved -> flagged
            4: [_thread("t4", resolved=True)],  # resolved-only -> not flagged
        },
    )
    gh_issues.set_client(client)

    prs = gh_issues.prs_requiring_repair(50, repo="o/r")
    by_num = {p["number"]: p for p in prs}

    assert set(by_num) == {1, 2, 3}
    assert by_num[1]["repairReasons"] == ["critic:blocking"]
    assert by_num[2]["repairReasons"] == ["merge_state:dirty"]
    assert by_num[3]["repairReasons"] == ["unresolved_review_threads"]
    assert by_num[3]["unresolvedReviewThreads"][0]["id"] == "t3"


# ---------------------------------------------------------------------------
# review_threads_batch — chunking + per-PR fallback (real client, stub graphql)
# ---------------------------------------------------------------------------


class _StubGraphQL:
    """A githubkit-like object whose ``.graphql`` is scripted per call."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._handler(query, variables or {})


def _real_client(handler: Any) -> GithubkitClient:
    client = GithubkitClient.__new__(GithubkitClient)
    client._auth_source = "test"  # type: ignore[attr-defined]
    client._gh = _StubGraphQL(handler)  # type: ignore[attr-defined]
    return client


def test_review_threads_batch_empty_input_issues_no_call() -> None:
    calls: list[Any] = []
    client = _real_client(lambda q, v: calls.append(q) or {})
    assert client.review_threads_batch("o", "r", []) == {}
    assert calls == []


def test_review_threads_batch_maps_aliases_back_to_pr_numbers() -> None:
    def handler(query: str, _vars: dict[str, Any]) -> dict[str, Any]:
        return {
            "repository": {
                "pr0": {"reviewThreads": {"nodes": [_gql_thread("a")]}},
                "pr1": {"reviewThreads": {"nodes": []}},
            }
        }

    client = _real_client(handler)
    out = client.review_threads_batch("o", "r", [11, 22])
    assert set(out) == {11, 22}
    assert out[11][0]["id"] == "a"
    assert out[22] == []


def test_review_threads_batch_total_failure_falls_back_per_pr() -> None:
    """A batch failure must NOT silently map every PR to [] — it falls back."""
    calls: list[str] = []

    def handler(query: str, _vars: dict[str, Any]) -> dict[str, Any]:
        calls.append(query)
        raise RuntimeError("node limit exceeded")  # both batch + per-PR fail

    client = _real_client(handler)
    out = client.review_threads_batch("o", "r", [1, 2])
    assert out == {1: [], 2: []}
    # 1 batched attempt + 1 per-PR fallback each for PRs 1 and 2.
    assert len(calls) == 3


def test_review_threads_batch_fallback_recovers_threads() -> None:
    """Batch fails but per-PR succeeds -> the signal is recovered, not dropped."""
    batched: list[str] = []

    def handler(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        # Batched query aliases many PRs (no ``number`` var); per-PR passes one.
        if "number" not in variables:
            batched.append(query)
            raise RuntimeError("node limit exceeded")
        nodes = [_gql_thread("recovered")] if variables["number"] == 1 else []
        return {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}

    client = _real_client(handler)
    out = client.review_threads_batch("o", "r", [1, 2])
    assert len(batched) == 1
    assert out[1][0]["id"] == "recovered"
    assert out[2] == []


def test_review_threads_batch_chunks_large_pr_sets() -> None:
    """>chunk PRs split into multiple bounded queries (GraphQL complexity)."""
    from forge_loop.gh_client import _REVIEW_THREADS_BATCH_CHUNK as chunk

    pr_numbers = list(range(1, chunk * 2 + 2))  # two full chunks + remainder
    batched: list[str] = []

    def handler(query: str, _vars: dict[str, Any]) -> dict[str, Any]:
        n_aliases = query.count("pullRequest(number:")
        assert n_aliases <= chunk
        batched.append(query)
        return {
            "repository": {f"pr{i}": {"reviewThreads": {"nodes": []}} for i in range(n_aliases)}
        }

    client = _real_client(handler)
    out = client.review_threads_batch("o", "r", pr_numbers)
    assert set(out) == set(pr_numbers)
    assert len(batched) == 3  # ceil((2*chunk+1)/chunk) == 3


def _gql_thread(id_: str, *, resolved: bool = False) -> dict[str, Any]:
    return {
        "id": id_,
        "isResolved": resolved,
        "isOutdated": False,
        "path": "src/app.py",
        "line": 12,
        "comments": {"nodes": []},
    }
