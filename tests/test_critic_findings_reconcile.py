"""Closed-loop reconciliation for first-class critic findings (#242, AC6).

Covers convergence (open-count drains to 0), the resolved->close path, and the
two REQUIRED adversarial sad-paths:

* an inline post returns HTTP 422 (out-of-diff line) yet the worker brief STILL
  contains the finding and convergence is unaffected;
* a worker marks a finding ``addressed`` but it is STILL present on re-review →
  the critic reopens it (no false convergence).
"""

from __future__ import annotations

from forge_loop.critic import CriticReport, Finding
from forge_loop.critic_actions import apply_critic_report
from forge_loop.critic_findings import SqliteCriticFindingsStore
from forge_loop.critic_findings.store import FindingStatus, render_findings_block

PR = "https://github.com/acme/widgets/pull/42"


def _f(message: str, *, line: int) -> Finding:
    return Finding(severity="sev2", category="correctness", file="m.py", line=line, message=message)


def test_reconcile_inserts_then_converges_to_zero() -> None:
    store = SqliteCriticFindingsStore(":memory:")
    f1, f2 = _f("a", line=1), _f("b", line=2)

    r1 = store.reconcile(PR, 42, 1, [f1, f2])
    assert (r1.inserted, r1.open_count) == (2, 2)

    # f1 fixed (gone), f2 still present.
    r2 = store.reconcile(PR, 42, 2, [f2])
    assert r2.kept_open == 1
    assert r2.closed == 1  # f1 resolved
    assert r2.open_count == 1

    # f2 fixed too → drained.
    r3 = store.reconcile(PR, 42, 3, [])
    assert r3.closed == 1
    assert r3.open_count == 0


def test_reconcile_reopens_worker_addressed_but_still_present() -> None:
    """ADVERSARIAL: worker claims a finding addressed, but it is STILL present
    on re-review → critic reopens it. No false convergence."""
    store = SqliteCriticFindingsStore(":memory:")
    f1 = _f("still-here", line=5)
    stored = store.reconcile(PR, 42, 1, [f1])
    assert stored.open_count == 1

    fid = store.open_findings(PR)[0].finding_id
    store.set_status(fid, FindingStatus.ADDRESSED, note="claims fixed")
    assert store.open_count(PR) == 0  # worker's claim, pre re-review

    # Re-review still sees f1 → reopen.
    result = store.reconcile(PR, 42, 2, [f1])
    assert result.reopened == 1
    assert result.open_count == 1
    assert store.get(fid) is not None and store.get(fid).status is FindingStatus.OPEN


def test_reconcile_respects_wontfix() -> None:
    """A ``wontfix`` finding is never auto-reopened even if still present."""
    store = SqliteCriticFindingsStore(":memory:")
    f1 = _f("acceptable", line=7)
    store.reconcile(PR, 42, 1, [f1])
    fid = store.open_findings(PR)[0].finding_id
    store.set_status(fid, FindingStatus.WONTFIX)

    result = store.reconcile(PR, 42, 2, [f1])
    assert result.reopened == 0
    assert result.open_count == 0
    assert store.get(fid).status is FindingStatus.WONTFIX


class _Gh422:
    """GhClient whose inline post 422s (out-of-diff line), like the original bug."""

    auth_source = "test"

    def add_pr_label(self, pr, labels, repo=None) -> bool:  # noqa: ANN001
        return True

    def disable_pr_auto_merge(self, pr, repo=None) -> bool:  # noqa: ANN001
        return True

    def post_review_comment(self, pr, body, file=None, line=None, repo=None) -> bool:  # noqa: ANN001
        # Inline (file+line) on an out-of-diff line → 422. Summary posts ok.
        return file is None


def test_422_inline_post_does_not_break_store_or_convergence() -> None:
    """End-to-end adversarial: the 422 inline post drops the GitHub comment, but
    the store has the finding, the brief renders it, and convergence is intact."""
    store = SqliteCriticFindingsStore(":memory:")
    report = CriticReport(
        overall="request_changes",
        findings=[_f("out-of-diff finding", line=12345)],
    )
    apply_critic_report(
        report,
        PR,
        300,
        block_on_sev2=False,
        min_findings_for_approve=0,
        gh=_Gh422(),
        repo="acme/widgets",
        findings_store=store,
        issue=42,
    )
    # The inline comment 422'd, but the finding is durable + renderable.
    assert store.open_count(PR) == 1
    block = render_findings_block(store.open_findings(PR))
    assert "out-of-diff finding" in block

    # Worker fixes it; next re-review no longer reports it → convergence.
    empty = CriticReport(overall="approve", findings=[])
    apply_critic_report(
        empty,
        PR,
        300,
        block_on_sev2=False,
        min_findings_for_approve=0,
        gh=_Gh422(),
        repo="acme/widgets",
        findings_store=store,
        issue=42,
    )
    assert store.open_count(PR) == 0
