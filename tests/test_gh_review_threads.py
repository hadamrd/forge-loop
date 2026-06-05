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


# ---------------------------------------------------------------------------
# issue #230 — critic-approved + CLEAN PRs must NOT re-enter the repair loop;
# critic-vs-human thread classification + AC3 human-thread gating.
# ---------------------------------------------------------------------------


def _comment_thread(
    id_: str, *, body: str, author: str = "critic-bot", resolved: bool = False
) -> dict[str, Any]:
    """A normalised review-thread node (as ``review_threads_batch`` returns it:
    ``comments`` is a flat list). The *opening* comment's ``body`` is what
    classifies the thread as critic vs human (#230)."""
    return {
        "id": id_,
        "isResolved": resolved,
        "isOutdated": False,
        "path": "src/app.py",
        "line": 12,
        "comments": [
            {
                "author": {"login": author},
                "body": body,
                "path": "src/app.py",
                "line": 12,
            }
        ],
    }


def _critic_thread(id_: str, *, sev: str = "sev3", resolved: bool = False) -> dict[str, Any]:
    return _comment_thread(
        id_, body=f"**[{sev}/correctness]** leftover critic finding", resolved=resolved
    )


def _human_thread(id_: str, *, resolved: bool = False) -> dict[str, Any]:
    return _comment_thread(
        id_, body="Please rework this design.", author="alice", resolved=resolved
    )


# --- is_approved_mergeable (pure predicate) --------------------------------


def test_is_approved_mergeable_true_when_clean_and_no_block_labels() -> None:
    """verdict=approved (no block label) + CLEAN → True."""
    pr = {"labels": [{"name": "loop:adopted"}], "mergeStateStatus": "CLEAN"}
    assert gh_issues.is_approved_mergeable(pr) is True
    assert gh_issues.is_approved_mergeable({"labels": [], "mergeStateStatus": "CLEAN"}) is True


def test_is_approved_mergeable_false_when_blocking_label_present() -> None:
    """A block label means NOT approved, regardless of merge state."""
    for label in ("critic:blocking", "critic:suspicious"):
        pr = {"labels": [{"name": label}], "mergeStateStatus": "CLEAN"}
        assert gh_issues.is_approved_mergeable(pr) is False, label


def test_is_approved_mergeable_false_when_not_clean() -> None:
    """Any non-CLEAN merge state means NOT mergeable, so NOT terminal."""
    for state in ("DIRTY", "CONFLICTING", "BEHIND", "BLOCKED", "UNKNOWN", "", None):
        pr = {"labels": [], "mergeStateStatus": state}
        assert gh_issues.is_approved_mergeable(pr) is False, state


def test_is_approved_mergeable_missing_fields_is_false() -> None:
    """Adversarial: empty dict (no labels, no merge state) → not mergeable."""
    assert gh_issues.is_approved_mergeable({}) is False


# --- critic-vs-human thread classification (AC3) ---------------------------


def test_human_unresolved_threads_keeps_human_drops_critic() -> None:
    """The critic's ``**[sevN/...]**`` threads are filtered out; an unresolved
    human request-changes thread is kept (#230 AC3)."""
    critic = _critic_thread("c1", sev="sev3")
    human = _human_thread("h1")
    resolved_human = _human_thread("h2", resolved=True)
    out = gh_issues.human_unresolved_threads([critic, human, resolved_human])
    assert [t["id"] for t in out] == ["h1"]


def test_human_unresolved_threads_treats_empty_thread_as_human() -> None:
    """Conservative direction: a thread we cannot prove is the critic's (no
    comments / unknown signature) is treated as human so we never auto-merge
    over it."""
    empty = {"id": "e", "isResolved": False, "comments": []}
    assert [t["id"] for t in gh_issues.human_unresolved_threads([empty])] == ["e"]


def test_is_approved_mergeable_false_with_unresolved_human_thread() -> None:
    """AC3: a CLEAN PR with no block label is NOT terminal while a human
    request-changes thread is open — even though merge state is CLEAN."""
    pr = {"labels": [], "mergeStateStatus": "CLEAN"}
    assert gh_issues.is_approved_mergeable(pr, unresolved_threads=[_human_thread("h")]) is False


def test_is_approved_mergeable_true_with_only_critic_threads() -> None:
    """A CLEAN PR whose only open threads are the critic's leftover sev3 notes
    IS terminal (the leftover threads do not hold it back)."""
    pr = {"labels": [], "mergeStateStatus": "CLEAN"}
    assert gh_issues.is_approved_mergeable(pr, unresolved_threads=[_critic_thread("c")]) is True


# --- prs_requiring_repair (the real selector) ------------------------------


def test_prs_requiring_repair_excludes_approved_mergeable_with_only_sev3_threads() -> None:
    """#230: a critic-approved (no block label) + CLEAN PR whose only open
    threads are leftover sev3 critic inline comments is NOT returned as a
    repair — it is terminal. The exclusion is surfaced via ``on_skip`` (no
    silent drop)."""
    client = MockGhClient(
        open_prs_response=[_open_pr(7, body="closes #42", headRefName="loop/42-fix-thing")],
        review_threads_by_pr={7: [_critic_thread("c1")]},
    )
    gh_issues.set_client(client)

    skipped: list[dict[str, Any]] = []
    prs = gh_issues.prs_requiring_repair(5, repo="o/r", on_skip=skipped.append)

    assert prs == []
    assert [p["number"] for p in skipped] == [7]
    assert skipped[0]["approvedMergeableSkip"] is True


def test_prs_requiring_repair_no_on_skip_is_silent_but_still_excludes() -> None:
    """Without an ``on_skip`` callback the approved-mergeable PR is still
    excluded (the exclusion does not depend on the callback)."""
    client = MockGhClient(
        open_prs_response=[_open_pr(7, body="closes #42", headRefName="loop/42-fix-thing")],
        review_threads_by_pr={7: [_critic_thread("c1")]},
    )
    gh_issues.set_client(client)

    assert gh_issues.prs_requiring_repair(5, repo="o/r") == []


def test_prs_requiring_repair_blocking_label_still_selected_despite_clean() -> None:
    """Regression guard: a ``critic:blocking`` PR is STILL selected even when
    CLEAN and carrying only critic threads — the fix must not disable repair."""
    client = MockGhClient(
        open_prs_response=[
            _open_pr(8, labels=[{"name": "critic:blocking"}], headRefName="loop/8-x"),
        ],
        review_threads_by_pr={8: [_critic_thread("c1")]},
    )
    gh_issues.set_client(client)

    skipped: list[dict[str, Any]] = []
    prs = gh_issues.prs_requiring_repair(5, repo="o/r", on_skip=skipped.append)

    assert [p["number"] for p in prs] == [8]
    assert "critic:blocking" in prs[0]["repairReasons"]
    assert skipped == []


def test_prs_requiring_repair_selects_approved_clean_with_human_thread() -> None:
    """#230 AC3 through the REAL selector: an approved + CLEAN PR carrying an
    unresolved *human* request-changes thread is STILL selected for repair (it
    is not approved-mergeable), and is NOT surfaced as an approved skip."""
    client = MockGhClient(
        open_prs_response=[_open_pr(9, headRefName="loop/9-x")],
        review_threads_by_pr={9: [_human_thread("h")]},
    )
    gh_issues.set_client(client)

    skipped: list[dict[str, Any]] = []
    prs = gh_issues.prs_requiring_repair(5, repo="o/r", on_skip=skipped.append)

    assert [p["number"] for p in prs] == [9]
    assert prs[0]["repairReasons"] == ["unresolved_review_threads"]
    assert skipped == []


def test_prs_requiring_repair_conflicting_with_critic_thread_still_selected() -> None:
    """A CONFLICTING PR is NOT approved-mergeable, so its (critic) threads
    still contribute — the merge-state path is unaffected by the #230 fix."""
    client = MockGhClient(
        open_prs_response=[_open_pr(5, mergeStateStatus="CONFLICTING", headRefName="loop/5-x")],
        review_threads_by_pr={5: [_critic_thread("c1")]},
    )
    gh_issues.set_client(client)

    prs = gh_issues.prs_requiring_repair(5, repo="o/r")
    assert [p["number"] for p in prs] == [5]
    assert "merge_state:conflicting" in prs[0]["repairReasons"]
    assert "unresolved_review_threads" in prs[0]["repairReasons"]
