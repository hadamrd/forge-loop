"""Integration: critic -> store -> repair brief with GitHub mocked OUT (#242).

This is the key #234 regression guard. It proves the repair worker's data path
has ZERO dependency on GitHub posting: with the GitHub poster patched to fail
(returning ``False`` like a 422, or raising), the findings still land in the
durable store AND surface in the repair brief.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_loop.config import Config
from forge_loop.critic import CriticReport, Finding
from forge_loop.critic_actions import apply_critic_report
from forge_loop.critic_findings import SqliteCriticFindingsStore
from forge_loop.runner.repairs import blocking_pr_repairs
from forge_loop.worker_brief import make_repair_brief

PR = "https://github.com/acme/widgets/pull/10"


class _FailingGh:
    """A GhClient whose every mutation fails — simulating the lossy medium."""

    auth_source = "test"

    def add_pr_label(self, pr, labels, repo=None) -> bool:  # noqa: ANN001
        return False

    def disable_pr_auto_merge(self, pr, repo=None) -> bool:  # noqa: ANN001
        return False

    def pr_head_branch(self, pr, repo=None) -> str | None:  # noqa: ANN001
        # Default: no recoverable head branch (e.g. API returned None). Subclasses
        # override to model a loop branch, a non-loop branch, or a raised error.
        return None

    def post_review_comment(self, pr, body, file=None, line=None, repo=None) -> bool:  # noqa: ANN001
        # The original bug: inline comment on an out-of-diff line 422s. We model
        # the worst case — posting reports failure for every comment.
        return False


def _report() -> CriticReport:
    return CriticReport(
        overall="request_changes",
        findings=[
            Finding(
                severity="sev2",
                category="correctness",
                file="src/x.py",
                line=999,  # out-of-diff line → would 422 on inline post
                message="off-by-one in the retry loop",
            ),
            Finding(
                severity="sev1",
                category="security",
                file=None,
                line=None,
                message="secret logged in plaintext",
            ),
        ],
    )


def test_findings_land_in_store_even_when_posting_fails() -> None:
    store = SqliteCriticFindingsStore(":memory:")
    plan = apply_critic_report(
        _report(),
        PR,
        500,
        block_on_sev2=False,
        min_findings_for_approve=0,
        gh=_FailingGh(),
        repo="acme/widgets",
        findings_store=store,
        issue=242,
    )
    # Posting failed for everything, yet the durable store has both findings.
    assert plan.block_merge is True
    open_rows = store.open_findings(PR)
    assert {r.message for r in open_rows} == {
        "off-by-one in the retry loop",
        "secret logged in plaintext",
    }
    assert store.open_count(PR) == 2


def test_repair_brief_renders_store_findings_with_empty_github_context(tmp_path) -> None:
    """The #234 guard: GitHub review-context is EMPTY (comments dropped), but
    the store-backed findings still reach ``make_repair_brief``."""
    store = SqliteCriticFindingsStore(":memory:")
    apply_critic_report(
        _report(),
        PR,
        500,
        block_on_sev2=False,
        min_findings_for_approve=0,
        gh=_FailingGh(),
        repo="acme/widgets",
        findings_store=store,
        issue=242,
    )

    cfg = Config(repo=tmp_path, github_repo="acme/widgets")
    pr_dict = {
        "number": 10,
        "url": PR,
        "headRefName": "loop/242-fix",
        "repairReasons": ["critic_blocked"],
    }
    repairs = blocking_pr_repairs(
        cfg,
        prs_requiring_repair_fn=lambda *_a, **_k: [pr_dict],
        fetch_issue_fn=lambda *_a, **_k: {"number": 242, "title": "fix it", "state": "OPEN"},
        # GitHub read-model returns NOTHING — the dropped-comments scenario.
        pr_review_context_fn=lambda *_a, **_k: "",
        findings_store=store,
    )
    assert len(repairs) == 1
    issue, pr, review_context = repairs[0]
    assert "off-by-one in the retry loop" in review_context
    assert "secret logged in plaintext" in review_context

    brief = make_repair_brief(issue, Path(tmp_path), pr=pr, review_context=review_context)
    assert "off-by-one in the retry loop" in brief
    assert "secret logged in plaintext" in brief
    assert "DURABLE CRITIC FINDINGS" in brief


def test_missing_issue_is_loud_not_a_silent_drop() -> None:
    """sev2/correctness regression guard: a store supplied with ``issue=None``
    must NOT silently no-op the durable data path (which would shove the worker
    back onto the lossy GitHub round-trip — the #242/Q10 failure). It emits a
    ``critic_findings_persist_skipped`` warning event instead."""
    store = SqliteCriticFindingsStore(":memory:")
    events: list[tuple[str, dict]] = []

    apply_critic_report(
        _report(),
        PR,
        500,
        block_on_sev2=False,
        min_findings_for_approve=0,
        gh=_FailingGh(),
        repo="acme/widgets",
        emit=lambda name, payload: events.append((name, payload)),
        findings_store=store,
        issue=None,
    )

    # Nothing persisted (no issue key), but the drop is OBSERVABLE.
    assert store.open_count(PR) == 0
    skipped = [p for n, p in events if n == "critic_findings_persist_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "issue_missing"
    assert skipped[0]["dropped_findings"] == 2
    # And no false "persisted" event was emitted.
    assert not any(n == "critic_findings_persisted" for n, _ in events)


def test_missing_issue_recovered_from_loop_branch() -> None:
    """sev3/correctness review fix: a missing ``issue`` is RECOVERED from the
    PR's canonical ``loop/<n>-`` head branch before any drop — so findings still
    persist instead of shoving the worker onto the GitHub round-trip."""

    class _BranchGh(_FailingGh):
        def pr_head_branch(self, pr, repo=None):  # noqa: ANN001
            return "loop/242-fix-the-thing"

    store = SqliteCriticFindingsStore(":memory:")
    events: list[tuple[str, dict]] = []

    apply_critic_report(
        _report(),
        PR,
        500,
        block_on_sev2=False,
        min_findings_for_approve=0,
        gh=_BranchGh(),
        repo="acme/widgets",
        emit=lambda name, payload: events.append((name, payload)),
        findings_store=store,
        issue=None,
    )

    # The branch carried issue 242 → findings persisted, NOT dropped.
    assert store.open_count(PR) == 2
    recovered = [p for n, p in events if n == "critic_findings_issue_recovered"]
    assert len(recovered) == 1
    assert recovered[0]["issue"] == 242
    assert recovered[0]["source"] == "head_branch"
    # The persist succeeded → no loud skip.
    assert not any(n == "critic_findings_persist_skipped" for n, _ in events)
    persisted = [p for n, p in events if n == "critic_findings_persisted"]
    assert persisted and persisted[0]["issue"] == 242


def test_unrecoverable_issue_still_loud_drops() -> None:
    """If the head branch is NOT a loop branch (no issue derivable), the drop is
    still loud — the fallback never masks a genuinely-missing issue."""

    class _NonLoopGh(_FailingGh):
        def pr_head_branch(self, pr, repo=None):  # noqa: ANN001
            return "feature/manual-work"

    store = SqliteCriticFindingsStore(":memory:")
    events: list[tuple[str, dict]] = []

    apply_critic_report(
        _report(),
        PR,
        500,
        block_on_sev2=False,
        min_findings_for_approve=0,
        gh=_NonLoopGh(),
        repo="acme/widgets",
        emit=lambda name, payload: events.append((name, payload)),
        findings_store=store,
        issue=None,
    )

    assert store.open_count(PR) == 0
    assert not any(n == "critic_findings_issue_recovered" for n, _ in events)
    skipped = [p for n, p in events if n == "critic_findings_persist_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "issue_missing"


def test_posting_raise_does_not_lose_findings() -> None:
    """Even if the poster RAISES (not just returns False), findings written
    before the posting step are already durable."""

    class _RaisingGh(_FailingGh):
        def post_review_comment(self, pr, body, file=None, line=None, repo=None):  # noqa: ANN001
            raise RuntimeError("HTTP 422 Unprocessable Entity")

    store = SqliteCriticFindingsStore(":memory:")
    with pytest.raises(RuntimeError):
        apply_critic_report(
            _report(),
            PR,
            500,
            block_on_sev2=False,
            min_findings_for_approve=0,
            gh=_RaisingGh(),
            repo="acme/widgets",
            findings_store=store,
            issue=242,
        )
    # Findings were persisted BEFORE the posting that raised.
    assert store.open_count(PR) == 2


def test_recovery_api_failure_is_loud_not_silently_swallowed() -> None:
    """sev1/correctness (EH-001): an EXPECTED failure of the head-branch lookup
    (a GitHub ``GhError`` / malformed-ref ``ValueError``) is no longer swallowed
    by a broad ``suppress(Exception)``. It emits
    ``critic_findings_issue_recovery_failed`` with context BEFORE degrading to a
    loud ``issue_missing`` drop — the fallback's failure is observable."""
    from forge_loop.gh_client import GhError

    class _ApiFailGh(_FailingGh):
        def pr_head_branch(self, pr, repo=None):  # noqa: ANN001
            raise GhError("get_pull(10)", 500, "boom")

    store = SqliteCriticFindingsStore(":memory:")
    events: list[tuple[str, dict]] = []

    apply_critic_report(
        _report(),
        PR,
        500,
        block_on_sev2=False,
        min_findings_for_approve=0,
        gh=_ApiFailGh(),
        repo="acme/widgets",
        emit=lambda name, payload: events.append((name, payload)),
        findings_store=store,
        issue=None,
    )

    # The recovery failure was made loud with context...
    failed = [p for n, p in events if n == "critic_findings_issue_recovery_failed"]
    assert len(failed) == 1
    assert failed[0]["error_type"] == "GhError"
    assert failed[0]["pr"] == PR
    # ...and the genuinely-missing issue still produces a loud drop, never silence.
    skipped = [p for n, p in events if n == "critic_findings_persist_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "issue_missing"
    assert store.open_count(PR) == 0


def test_recovery_programming_bug_is_not_hidden() -> None:
    """sev1/correctness (EH-001): a genuine programming bug in the head-branch
    lookup (e.g. ``TypeError``) must PROPAGATE, not be masked by the recovery's
    expected-error catch — preserving the observability discipline this PR adds."""

    class _BuggyGh(_FailingGh):
        def pr_head_branch(self, pr, repo=None):  # noqa: ANN001
            raise TypeError("unexpected programming bug")

    store = SqliteCriticFindingsStore(":memory:")
    with pytest.raises(TypeError):
        apply_critic_report(
            _report(),
            PR,
            500,
            block_on_sev2=False,
            min_findings_for_approve=0,
            gh=_BuggyGh(),
            repo="acme/widgets",
            findings_store=store,
            issue=None,
        )
