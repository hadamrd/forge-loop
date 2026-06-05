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


def _thread(
    id_: str,
    *,
    resolved: bool = False,
    body: str = "**[sev3/correctness]** leftover critic finding",
    author: str = "critic-bot",
) -> dict[str, Any]:
    """A review thread node. Defaults to a CRITIC-authored sev3 thread (the
    ``**[sevN/...]**`` body signature ``critic_actions`` emits) so existing
    approved-mergeable tests model the real #230 case. Pass a non-critic
    ``body`` to model a human request-changes thread (#230 AC3)."""
    return {
        "id": id_,
        "isResolved": resolved,
        "isOutdated": False,
        "path": "src/app.py",
        "line": 12,
        "comments": {
            "nodes": [
                {
                    "author": {"login": author},
                    "body": body,
                    "url": "https://github.com/o/r/pull/7#discussion",
                    "path": "src/app.py",
                    "line": 12,
                    "createdAt": "2026-01-01T00:00:00Z",
                }
            ]
        },
    }


def test_prs_requiring_repair_excludes_approved_mergeable_with_only_sev3_threads(
    monkeypatch,
) -> None:
    """#230: a critic-approved (no block label) + CLEAN PR whose only open
    threads are leftover sev3 critic inline comments is NOT returned as a
    repair — it is terminal and belongs on the merge conveyor. The exclusion is
    surfaced via ``on_skip`` (no silent drop), never as a repair worker."""

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

    skipped: list[dict[str, Any]] = []
    prs = gh.prs_requiring_repair(5, repo="o/r", on_skip=skipped.append)

    # NOT eligible for a repair worker.
    assert prs == []
    # But surfaced, not silently dropped.
    assert [p["number"] for p in skipped] == [7]
    assert skipped[0]["approvedMergeableSkip"] is True
    assert skipped[0]["unresolvedReviewThreads"][0]["id"] == "thread-1"


def test_prs_requiring_repair_no_on_skip_is_silent_but_still_excludes(monkeypatch) -> None:
    """Adversarial: with no ``on_skip`` callback the approved-mergeable PR is
    still excluded from the repair set (the default path must not raise)."""

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "list"]:
            return _completed(
                [
                    {
                        "number": 7,
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
                        "repository": {"pr0": {"reviewThreads": {"nodes": [_thread("thread-1")]}}}
                    }
                }
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    assert gh.prs_requiring_repair(5, repo="o/r") == []


def test_prs_requiring_repair_blocking_label_still_selected_despite_clean(monkeypatch) -> None:
    """#230 regression guard: a ``critic:blocking`` PR is STILL selected for
    repair even when CLEAN with sev3 threads (it is NOT approved-mergeable)."""

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "list"]:
            return _completed(
                [
                    {
                        "number": 8,
                        "url": "https://github.com/o/r/pull/8",
                        "headRefName": "loop/43-blocked",
                        "labels": [{"name": "critic:blocking"}],
                        "updatedAt": "2026-01-01T00:00:00Z",
                        "mergeStateStatus": "CLEAN",
                    }
                ]
            )
        if cmd[:3] == ["gh", "api", "graphql"]:
            return _completed(
                {
                    "data": {
                        "repository": {"pr0": {"reviewThreads": {"nodes": [_thread("thread-2")]}}}
                    }
                }
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    skipped: list[dict[str, Any]] = []
    prs = gh.prs_requiring_repair(5, repo="o/r", on_skip=skipped.append)

    assert [p["number"] for p in prs] == [8]
    assert prs[0]["repairReasons"] == ["critic:blocking", "unresolved_review_threads"]
    assert skipped == []  # not skipped — genuinely blocked


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
            # #230: an unresolved thread is a repair reason ONLY when the PR is
            # not approved-mergeable. CONFLICTING keeps PR #3 out of the
            # approved-mergeable set, so the thread reason survives batching.
            "number": 3,
            "url": "u3",
            "labels": [],
            "updatedAt": "2026-01-03T00:00:00Z",
            "mergeStateStatus": "CONFLICTING",
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

    assert set(by_num) == {1, 2, 3}  # PR #4 (resolved-only, approved+CLEAN) excluded
    assert by_num[1]["repairReasons"] == ["critic:blocking"]
    assert by_num[2]["repairReasons"] == ["merge_state:dirty"]
    # CONFLICTING + unresolved thread → both reasons survive batching.
    assert by_num[3]["repairReasons"] == ["merge_state:conflicting", "unresolved_review_threads"]
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


# ---------------------------------------------------------------------------
# is_approved_mergeable helper (issue #230)
# ---------------------------------------------------------------------------


def test_is_approved_mergeable_true_when_clean_and_no_block_labels() -> None:
    """verdict=approved (no block label) + CLEAN → True."""
    pr = {"labels": [{"name": "loop:adopted"}], "mergeStateStatus": "CLEAN"}
    assert gh.is_approved_mergeable(pr) is True
    # No labels at all is also "no block label".
    assert gh.is_approved_mergeable({"labels": [], "mergeStateStatus": "CLEAN"}) is True


def test_is_approved_mergeable_false_when_blocking_label_present() -> None:
    """A block label means NOT approved, regardless of merge state."""
    for label in ("critic:blocking", "critic:suspicious"):
        pr = {"labels": [{"name": label}], "mergeStateStatus": "CLEAN"}
        assert gh.is_approved_mergeable(pr) is False, label


def test_is_approved_mergeable_false_when_not_clean() -> None:
    """Any non-CLEAN merge state means NOT mergeable, so NOT terminal."""
    for state in ("DIRTY", "CONFLICTING", "BEHIND", "BLOCKED", "UNKNOWN", "", None):
        pr = {"labels": [], "mergeStateStatus": state}
        assert gh.is_approved_mergeable(pr) is False, state


def test_is_approved_mergeable_missing_fields_is_false() -> None:
    """Adversarial: empty dict (no labels, no merge state) → not mergeable."""
    assert gh.is_approved_mergeable({}) is False


# ---------------------------------------------------------------------------
# critic-vs-human thread classification + AC3 human-thread gating (issue #230)
# ---------------------------------------------------------------------------


def _norm(thread: dict[str, Any]) -> dict[str, Any]:
    """Normalise a raw GraphQL thread node the way the fetchers do."""
    return gh._normalise_review_thread(thread)


def test_human_unresolved_threads_keeps_human_drops_critic() -> None:
    """The critic's ``**[sevN/...]**`` threads are filtered out; a human
    request-changes thread (free prose) is kept (#230 AC3)."""
    critic = _norm(_thread("c1", body="**[sev3/performance]** redundant I/O"))
    human = _norm(_thread("h1", body="Please handle the error path here.", author="alice"))
    resolved_human = _norm(_thread("h2", body="nit: rename this", author="alice", resolved=True))
    out = gh.human_unresolved_threads([critic, human, resolved_human])
    assert [t["id"] for t in out] == ["h1"]  # only the unresolved human thread


def test_human_unresolved_threads_treats_empty_thread_as_human() -> None:
    """Conservative direction: a thread we cannot prove is the critic's (no
    comments / unknown signature) is treated as human so we never auto-merge
    over it."""
    empty = {"id": "e", "isResolved": False, "comments": []}
    assert [t["id"] for t in gh.human_unresolved_threads([empty])] == ["e"]


def test_is_approved_mergeable_false_with_unresolved_human_thread() -> None:
    """AC3: a CLEAN PR with no block label is NOT terminal while a human
    request-changes thread is open — even though merge state is CLEAN."""
    pr = {"labels": [], "mergeStateStatus": "CLEAN"}
    human = _norm(_thread("h", body="This needs a different approach.", author="bob"))
    assert gh.is_approved_mergeable(pr, unresolved_threads=[human]) is False


def test_is_approved_mergeable_true_with_only_critic_threads() -> None:
    """A CLEAN PR whose only open threads are the critic's leftover sev3 notes
    IS terminal (the leftover threads do not hold it back)."""
    pr = {"labels": [], "mergeStateStatus": "CLEAN"}
    critic = _norm(_thread("c", body="**[sev3/style]** rename field"))
    assert gh.is_approved_mergeable(pr, unresolved_threads=[critic]) is True


def test_prs_requiring_repair_selects_approved_clean_with_human_thread(monkeypatch) -> None:
    """#230 AC3 through the REAL selector: an approved + CLEAN PR carrying an
    unresolved *human* request-changes thread is STILL selected for repair (it
    is not approved-mergeable), and is NOT surfaced as an approved skip."""

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "pr", "list"]:
            return _completed(
                [
                    {
                        "number": 9,
                        "url": "https://github.com/o/r/pull/9",
                        "headRefName": "loop/9-x",
                        "labels": [],
                        "updatedAt": "2026-01-01T00:00:00Z",
                        "mergeStateStatus": "CLEAN",
                    }
                ]
            )
        if cmd[:3] == ["gh", "api", "graphql"]:
            human = _thread("h", body="Please rework this design.", author="alice")
            return _completed(
                {"data": {"repository": {"pr0": {"reviewThreads": {"nodes": [human]}}}}}
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    skipped: list[dict[str, Any]] = []
    prs = gh.prs_requiring_repair(5, repo="o/r", on_skip=skipped.append)

    assert [p["number"] for p in prs] == [9]  # selected for repair
    assert prs[0]["repairReasons"] == ["unresolved_review_threads"]
    assert skipped == []  # NOT treated as approved-mergeable
