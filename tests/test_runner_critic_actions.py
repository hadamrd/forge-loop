"""Tests for critic_actions.plan_actions + apply_critic_report."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from forge_loop.config import Config, CriticConfig
from forge_loop.critic import CriticOutcome, CriticReport, Finding
from forge_loop.critic_actions import apply_critic_report, plan_actions
from forge_loop.runner import dispatch as dispatch_mod
from forge_loop.worker import WorkerOutcome


@dataclass
class _FakeGh:
    label_calls: list[tuple] = field(default_factory=list)
    disable_calls: list[tuple] = field(default_factory=list)
    comment_calls: list[dict] = field(default_factory=list)
    remove_label_calls: list[tuple] = field(default_factory=list)
    disable_result: bool = True
    label_result: bool = True
    comment_result: bool = True
    remove_label_result: bool = True
    changed_lines: int = 1000
    auth_source: str = "github-client"

    def add_pr_label(self, pr, labels, repo=None):  # type: ignore[no-untyped-def]
        self.label_calls.append((pr, tuple(labels), repo))
        return self.label_result

    def disable_pr_auto_merge(self, pr, repo=None):  # type: ignore[no-untyped-def]
        self.disable_calls.append((pr, repo))
        return self.disable_result

    def post_review_comment(self, pr, body, file=None, line=None, repo=None):  # type: ignore[no-untyped-def]
        self.comment_calls.append(
            {"pr": pr, "body": body, "file": file, "line": line, "repo": repo}
        )
        return self.comment_result

    def remove_pr_label(self, pr, label, repo=None):  # type: ignore[no-untyped-def]
        self.remove_label_calls.append((pr, label, repo))
        return self.remove_label_result

    def pr_changed_lines(self, pr, repo=None):  # type: ignore[no-untyped-def]
        return self.changed_lines


def _report(overall: str, findings: list[Finding]) -> CriticReport:
    return CriticReport(overall=overall, findings=findings)


# ---------------------------------------------------------------------------
# plan_actions — pure logic
# ---------------------------------------------------------------------------


def test_sev1_blocks_and_labels() -> None:
    rep = _report(
        "request_changes",
        [
            Finding("sev1", "correctness", "src/foo.py", 10, "bad math"),
        ],
    )
    plan = plan_actions(rep, pr_changed_lines=100, block_on_sev2=False, min_findings_for_approve=50)
    assert plan.block_merge is True
    assert "critic:blocking" in plan.labels_to_add
    assert plan.inline_comments and plan.inline_comments[0].severity == "sev1"


def test_block_overall_without_sev1_still_blocks() -> None:
    rep = _report(
        "block",
        [
            Finding("sev3", "style", None, None, "trivial"),
        ],
    )
    plan = plan_actions(rep, 10, False, 50)
    assert plan.block_merge is True
    assert plan.labels_to_add == ["critic:blocking"]


def test_request_changes_blocks_even_when_sev2_knob_is_off() -> None:
    rep = _report(
        "request_changes",
        [
            Finding("sev2", "tests", "t.py", 1, "weak"),
        ],
    )
    plan = plan_actions(rep, 100, block_on_sev2=False, min_findings_for_approve=50)
    assert plan.block_merge is True
    assert "critic:blocking" in plan.labels_to_add
    assert plan.inline_comments


def test_sev2_blocks_when_knob_set() -> None:
    rep = _report(
        "request_changes",
        [
            Finding("sev2", "tests", "t.py", 1, "weak"),
        ],
    )
    plan = plan_actions(rep, 100, block_on_sev2=True, min_findings_for_approve=50)
    assert plan.block_merge is True
    assert plan.labels_to_add == ["critic:blocking"]


def test_zero_findings_large_pr_is_suspicious() -> None:
    rep = _report("approve", [])
    plan = plan_actions(rep, pr_changed_lines=1000, block_on_sev2=False, min_findings_for_approve=50)
    assert plan.suspicious_approve is True
    assert plan.block_merge is True
    assert "critic:suspicious" in plan.labels_to_add


def test_zero_findings_small_pr_is_not_suspicious() -> None:
    rep = _report("approve", [])
    plan = plan_actions(rep, pr_changed_lines=10, block_on_sev2=False, min_findings_for_approve=50)
    assert plan.suspicious_approve is False
    assert plan.block_merge is False
    assert plan.labels_to_add == []


def test_zero_findings_tiny_pr_does_not_block_even_if_config_threshold_is_low() -> None:
    rep = _report("approve", [])
    plan = plan_actions(rep, pr_changed_lines=37, block_on_sev2=False, min_findings_for_approve=30)
    assert plan.suspicious_approve is False
    assert plan.block_merge is False
    assert plan.labels_to_add == []


def test_summary_vs_inline_split() -> None:
    rep = _report(
        "request_changes",
        [
            Finding("sev2", "correctness", "a.py", 5, "with loc"),
            Finding("sev3", "docs", None, None, "no loc"),
            Finding("sev2", "tests", "b.py", None, "missing line"),
        ],
    )
    plan = plan_actions(rep, 100, False, 50)
    assert len(plan.inline_comments) == 1
    assert len(plan.summary_comments) == 2


# ---------------------------------------------------------------------------
# apply_critic_report — wires plan → gh calls
# ---------------------------------------------------------------------------


def test_apply_sev1_disables_auto_merge_and_labels_pr() -> None:
    gh = _FakeGh()
    rep = _report(
        "request_changes",
        [
            Finding("sev1", "correctness", "src/foo.py", 7, "uh oh"),
        ],
    )
    emits: list[tuple[str, dict]] = []
    apply_critic_report(
        rep,
        "https://gh.com/o/r/pull/9",
        pr_changed_lines=120,
        block_on_sev2=False,
        min_findings_for_approve=50,
        gh=gh,
        repo="o/r",
        emit=lambda k, p: emits.append((k, p)),
    )
    assert gh.disable_calls == [("https://gh.com/o/r/pull/9", "o/r")]
    assert gh.label_calls and "critic:blocking" in gh.label_calls[0][1]
    assert gh.comment_calls and gh.comment_calls[0]["file"] == "src/foo.py"
    assert any(k == "critic_actions_applied" for k, _ in emits)


def test_apply_reports_failed_label_mutation_without_success_event() -> None:
    gh = _FakeGh(label_result=False)
    rep = _report(
        "request_changes",
        [
            Finding("sev1", "correctness", "src/foo.py", 7, "uh oh"),
        ],
    )
    emits: list[tuple[str, dict]] = []

    apply_critic_report(
        rep,
        "https://gh.com/o/r/pull/9",
        pr_changed_lines=120,
        block_on_sev2=False,
        min_findings_for_approve=50,
        gh=gh,
        repo="o/r",
        emit=lambda k, p: emits.append((k, p)),
    )

    assert not any(k == "critic_actions_applied" for k, _ in emits)
    failed = [payload for kind, payload in emits if kind == "critic_actions_failed"]
    assert failed
    assert failed[0]["method"] == "add_pr_label"
    assert failed[0]["auth_source"] == "github-client"


def test_apply_ignores_false_disable_auto_merge_when_other_mutations_succeed() -> None:
    gh = _FakeGh(disable_result=False)
    rep = _report(
        "request_changes",
        [
            Finding("sev1", "correctness", "src/foo.py", 7, "uh oh"),
        ],
    )
    emits: list[tuple[str, dict]] = []

    apply_critic_report(
        rep,
        "https://gh.com/o/r/pull/9",
        pr_changed_lines=120,
        block_on_sev2=False,
        min_findings_for_approve=50,
        gh=gh,
        repo="o/r",
        emit=lambda k, p: emits.append((k, p)),
    )

    assert gh.disable_calls == [("https://gh.com/o/r/pull/9", "o/r")]
    assert gh.label_calls
    assert gh.comment_calls
    assert not any(k == "critic_actions_failed" for k, _ in emits)
    assert any(k == "critic_actions_applied" for k, _ in emits)


def test_apply_approve_zero_findings_large_pr_blocks_and_labels_suspicious() -> None:
    gh = _FakeGh()
    rep = _report("approve", [])
    plan = apply_critic_report(
        rep,
        "https://gh.com/o/r/pull/9",
        pr_changed_lines=1000,
        block_on_sev2=False,
        min_findings_for_approve=50,
        gh=gh,
        repo="o/r",
    )
    assert plan.suspicious_approve is True
    assert gh.disable_calls == [("https://gh.com/o/r/pull/9", "o/r")]
    assert any("critic:suspicious" in lab for _, lab, _ in gh.label_calls)


def test_apply_clean_small_pr_does_nothing() -> None:
    gh = _FakeGh()
    rep = _report("approve", [])
    plan = apply_critic_report(
        rep,
        "url",
        pr_changed_lines=5,
        block_on_sev2=False,
        min_findings_for_approve=50,
        gh=gh,
        repo="o/r",
    )
    assert plan.block_merge is False
    assert gh.disable_calls == []
    assert gh.label_calls == []
    assert gh.comment_calls == []


def test_apply_sev2_with_block_knob_blocks_merge() -> None:
    gh = _FakeGh()
    rep = _report(
        "request_changes",
        [
            Finding("sev2", "security", "x.py", 1, "input not validated"),
        ],
    )
    apply_critic_report(
        rep,
        "url",
        pr_changed_lines=30,
        block_on_sev2=True,
        min_findings_for_approve=50,
        gh=gh,
        repo="o/r",
    )
    assert gh.disable_calls and gh.disable_calls[0][0] == "url"
    assert any("critic:blocking" in lab for _, lab, _ in gh.label_calls)


def test_apply_only_sev3_posts_comment_no_block() -> None:
    gh = _FakeGh()
    rep = _report(
        "request_changes",
        [
            Finding("sev3", "style", None, None, "nit"),
        ],
    )
    plan = apply_critic_report(
        rep,
        "url",
        pr_changed_lines=30,
        block_on_sev2=False,
        min_findings_for_approve=50,
        gh=gh,
        repo="o/r",
    )
    assert plan.block_merge is False
    assert gh.disable_calls == []
    assert gh.label_calls == []
    # sev3 with no file/line lands as a summary comment
    assert len(gh.comment_calls) == 1
    assert gh.comment_calls[0]["file"] is None


def test_run_critic_block_reopens_optimistic_merged_outcome(monkeypatch, tmp_path) -> None:
    cfg = Config(
        repo=tmp_path,
        github_repo="o/r",
        critic=CriticConfig(enabled=True, timeout_s=10),
    )
    outcome = WorkerOutcome(
        issue=99,
        title="fix thing",
        pr_url="https://github.com/o/r/pull/123",
        status="merged",
        duration_s=1.0,
        stdout_tail="",
    )

    monkeypatch.setattr(
        dispatch_mod,
        "_critic_review",
        lambda *_a, **_kw: CriticOutcome(
            verdict="changes_requested",
            reasons=["missing required proof"],
            duration_s=1.0,
            stdout_tail="",
            report=_report(
                "request_changes",
                [Finding("sev1", "tests", "tests/test_x.py", 7, "missing required proof")],
            ),
        ),
    )
    monkeypatch.setattr(dispatch_mod._gh, "pr_changed_lines", lambda *_a, **_kw: 12)
    monkeypatch.setattr(
        dispatch_mod,
        "apply_critic_report",
        lambda *_a, **_kw: SimpleNamespace(block_merge=True, suspicious_approve=False),
    )

    dispatch_mod._run_critic_for_outcomes(cfg, [outcome], lambda *_a, **_kw: None)

    assert outcome.status == "open"
    assert outcome.error == "critic blocked merge: missing required proof"


def test_run_critic_reports_failed_cleanup_label_removal(monkeypatch, tmp_path) -> None:
    cfg = Config(
        repo=tmp_path,
        github_repo="o/r",
        critic=CriticConfig(enabled=True, timeout_s=10),
    )
    outcome = WorkerOutcome(
        issue=99,
        title="fix thing",
        pr_url="https://github.com/o/r/pull/123",
        status="open",
        duration_s=1.0,
        stdout_tail="",
    )
    emitted: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        dispatch_mod,
        "_critic_review",
        lambda *_a, **_kw: CriticOutcome(
            verdict="approved",
            reasons=[],
            duration_s=1.0,
            stdout_tail="",
            report=_report("approve", []),
        ),
    )
    monkeypatch.setattr(dispatch_mod._gh, "pr_changed_lines", lambda *_a, **_kw: 12)
    monkeypatch.setattr(
        dispatch_mod,
        "apply_critic_report",
        lambda *_a, **_kw: SimpleNamespace(block_merge=False, suspicious_approve=False),
    )
    monkeypatch.setattr(dispatch_mod._gh, "auth_source", "gh cli", raising=False)
    monkeypatch.setattr(dispatch_mod._gh, "remove_pr_label", lambda *_a, **_kw: False)

    dispatch_mod._run_critic_for_outcomes(cfg, [outcome], lambda k, p: emitted.append((k, p)))

    failures = [payload for kind, payload in emitted if kind == "critic_actions_failed"]
    assert [failure["label"] for failure in failures] == [
        "critic:blocking",
        "critic:suspicious",
    ]
    assert all(failure["method"] == "remove_pr_label" for failure in failures)
    assert all(failure["auth_source"] == "gh cli" for failure in failures)


def test_run_critic_error_verdict_clears_stale_block_and_emits(monkeypatch, tmp_path) -> None:
    """Issue #245: a critic verdict=error (report is None) on a previously
    blocked PR must NOT leave the stale critic:blocking label, must emit the
    typed ``critic_review_errored`` event, and must leave the PR open for the
    next tick to re-review from scratch — never a silent stale block, never an
    auto-approve."""
    cfg = Config(
        repo=tmp_path,
        github_repo="o/r",
        critic=CriticConfig(enabled=True, timeout_s=10),
    )
    outcome = WorkerOutcome(
        issue=230,
        title="addressed every finding",
        pr_url="https://github.com/o/r/pull/231",
        status="open",
        duration_s=1.0,
        stdout_tail="",
    )

    monkeypatch.setattr(
        dispatch_mod,
        "_critic_review",
        lambda *_a, **_kw: CriticOutcome(
            verdict="error",
            reasons=[],
            duration_s=1.0,
            stdout_tail="(timeout)",
            report=None,
            error="critic exceeded 10s",
        ),
    )
    removed: list[tuple] = []
    monkeypatch.setattr(
        dispatch_mod._gh,
        "remove_pr_label",
        lambda pr, label, repo=None: (removed.append((pr, label, repo)), True)[1],
    )
    # apply_critic_report / pr_changed_lines must NOT be reached on the error
    # branch (report is None) — make them explode if they are.
    monkeypatch.setattr(
        dispatch_mod._gh,
        "pr_changed_lines",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not be called")),
    )

    dispatch_mod._run_critic_for_outcomes(cfg, [outcome], lambda *_a, **_kw: None)

    # PR is left OPEN (needs re-review), never auto-approved/merged.
    assert outcome.status == "open"
    # Stale block labels are re-derived (removed), not carried forward.
    assert {label for _, label, _ in removed} == {"critic:blocking", "critic:suspicious"}
    # The typed error event landed on disk.
    lines = cfg.events_file.read_text().splitlines()
    errored = [json.loads(ln) for ln in lines if '"critic_review_errored"' in ln]
    assert len(errored) == 1
    assert errored[0]["kind"] == "critic_review_errored"
    assert errored[0]["issue"] == 230
    assert errored[0]["pr"] == "https://github.com/o/r/pull/231"
    assert errored[0]["verdict"] == "error"
    assert "10s" in errored[0]["error"]


def test_run_critic_error_label_clear_failure_is_reported(monkeypatch, tmp_path) -> None:
    """Adversarial: a gh remove failure on the error branch is surfaced via
    critic_actions_failed and does not raise or freeze the PR."""
    cfg = Config(
        repo=tmp_path,
        github_repo="o/r",
        critic=CriticConfig(enabled=True, timeout_s=10),
    )
    outcome = WorkerOutcome(
        issue=230,
        title="x",
        pr_url="https://github.com/o/r/pull/231",
        status="open",
        duration_s=1.0,
        stdout_tail="",
    )
    emitted: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        dispatch_mod,
        "_critic_review",
        lambda *_a, **_kw: CriticOutcome(
            verdict="error",
            reasons=[],
            duration_s=1.0,
            stdout_tail="",
            report=None,
            error="parse failed",
        ),
    )
    monkeypatch.setattr(dispatch_mod._gh, "auth_source", "gh cli", raising=False)
    monkeypatch.setattr(dispatch_mod._gh, "remove_pr_label", lambda *_a, **_kw: False)

    dispatch_mod._run_critic_for_outcomes(cfg, [outcome], lambda k, p: emitted.append((k, p)))

    assert outcome.status == "open"
    failures = [p for k, p in emitted if k == "critic_actions_failed"]
    assert [f["label"] for f in failures] == ["critic:blocking", "critic:suspicious"]
    assert all(f["method"] == "remove_pr_label" for f in failures)


# ---------------------------------------------------------------------------
# Issue #311 — self-clearing critic:suspicious guard
# ---------------------------------------------------------------------------


def _seq_review(outcomes: list[CriticOutcome]):  # type: ignore[no-untyped-def]
    """Return a _critic_review stub that yields ``outcomes`` in order — so the
    FIRST (suspicious) pass and the SECOND (adjudicating) pass differ."""
    it = iter(outcomes)

    def _review(*_a, **_kw):  # type: ignore[no-untyped-def]
        return next(it)

    return _review


def _suspicious_first() -> CriticOutcome:
    """A first pass that trips the suspicious guard: approve + ZERO findings."""
    return CriticOutcome(
        verdict="approved",
        reasons=[],
        duration_s=1.0,
        stdout_tail="",
        report=_report("approve", []),
    )


# reconcile_suspicious — pure adjudication (one test per edge, T1)


def test_reconcile_clears_on_zero_findings_second_pass() -> None:
    from forge_loop.critic_actions import SuspiciousResolution, reconcile_suspicious

    assert reconcile_suspicious(_report("approve", [])) is SuspiciousResolution.CLEARED


def test_reconcile_clears_when_only_sev3() -> None:
    from forge_loop.critic_actions import SuspiciousResolution, reconcile_suspicious

    rep = _report("approve", [Finding("sev3", "style", "a.py", 1, "nit")])
    assert reconcile_suspicious(rep) is SuspiciousResolution.CLEARED


def test_reconcile_corroborates_on_sev1() -> None:
    from forge_loop.critic_actions import SuspiciousResolution, reconcile_suspicious

    rep = _report("block", [Finding("sev1", "correctness", "a.py", 1, "real bug")])
    assert reconcile_suspicious(rep) is SuspiciousResolution.CORROBORATED


def test_reconcile_corroborates_on_sev2() -> None:
    from forge_loop.critic_actions import SuspiciousResolution, reconcile_suspicious

    rep = _report("request_changes", [Finding("sev2", "reuse", "a.py", 1, "dup")])
    assert reconcile_suspicious(rep) is SuspiciousResolution.CORROBORATED


# _run_critic_for_outcomes — end-to-end suspicious resolution


def _suspicious_cfg(tmp_path) -> Config:  # type: ignore[no-untyped-def]
    return Config(
        repo=tmp_path,
        github_repo="o/r",
        critic=CriticConfig(enabled=True, timeout_s=10),
    )


def _suspicious_outcome() -> WorkerOutcome:
    return WorkerOutcome(
        issue=108,
        title="clean PR mislabeled suspicious",
        pr_url="https://github.com/o/r/pull/108",
        status="open",
        duration_s=1.0,
        stdout_tail="",
    )


def test_suspicious_cleared_by_second_pass_merges_with_zero_human_action(
    monkeypatch, tmp_path
) -> None:
    """AC: a clean PR mislabeled suspicious gets a second pass and proceeds to
    merge with ZERO human action — no error set, verdict approved, the
    suspicious label dropped, and the real merge gate no longer withholds."""
    from forge_loop.runner.tick import _automerge_withheld_reason

    cfg = _suspicious_cfg(tmp_path)
    outcome = _suspicious_outcome()
    gh = _FakeGh()
    monkeypatch.setattr(dispatch_mod, "_gh", gh)
    monkeypatch.setattr(
        dispatch_mod,
        "_critic_review",
        _seq_review([_suspicious_first(), _suspicious_first()]),
    )

    dispatch_mod._run_critic_for_outcomes(cfg, [outcome], lambda *_a, **_kw: None)

    # First pass stamped critic:suspicious; the second pass un-froze it.
    assert ("critic:suspicious",) in [labs for _, labs, _ in gh.label_calls]
    assert "critic:suspicious" in {label for _, label, _ in gh.remove_label_calls}
    # No human gate left: open, no blocking error, verdict approved.
    assert outcome.status == "open"
    assert not outcome.error
    assert outcome.critic_verdict == "approved"
    # The real merge gate would now PROCEED (no withhold reason).
    assert _automerge_withheld_reason(outcome) is None
    # Resolution events landed for observability.
    events = cfg.events_file.read_text()
    assert "critic_suspicious_second_pass" in events
    assert "critic_suspicious_cleared" in events


def test_suspicious_corroborated_is_held_never_merges(monkeypatch, tmp_path) -> None:
    """AC: a real rubber-stamp (0 findings on a huge diff) hiding a sev1 is
    corroborated by the independent second pass and HELD as a normal critic
    block for the repair loop — NEVER auto-merged."""
    from forge_loop.runner.tick import _automerge_withheld_reason

    cfg = _suspicious_cfg(tmp_path)
    outcome = _suspicious_outcome()
    gh = _FakeGh()
    monkeypatch.setattr(dispatch_mod, "_gh", gh)
    second = CriticOutcome(
        verdict="blocked",
        reasons=["[sev1/correctness] silent data loss"],
        duration_s=1.0,
        stdout_tail="",
        report=_report("block", [Finding("sev1", "correctness", "x.py", 9, "silent data loss")]),
    )
    monkeypatch.setattr(
        dispatch_mod,
        "_critic_review",
        _seq_review([_suspicious_first(), second]),
    )

    dispatch_mod._run_critic_for_outcomes(cfg, [outcome], lambda *_a, **_kw: None)

    # Suspicious flag demoted to a normal block carrying the real finding.
    assert "critic:suspicious" in {label for _, label, _ in gh.remove_label_calls}
    assert ("critic:blocking",) in [labs for _, labs, _ in gh.label_calls]
    # Held: open + blocking error, and the merge gate refuses (verdict != approved).
    assert outcome.status == "open"
    assert outcome.error and "corroborated" in outcome.error
    assert _automerge_withheld_reason(outcome) is not None
    events = cfg.events_file.read_text()
    assert "critic_suspicious_corroborated" in events


def test_suspicious_second_pass_error_holds_and_retries(monkeypatch, tmp_path) -> None:
    """Adversarial: when the second pass itself errors (no report), the PR is
    held (not merged) with an inconclusive note so the NEXT tick retries — it is
    never frozen pending only a human, and never auto-merged on a non-verdict."""
    from forge_loop.runner.tick import _automerge_withheld_reason

    cfg = _suspicious_cfg(tmp_path)
    outcome = _suspicious_outcome()
    gh = _FakeGh()
    monkeypatch.setattr(dispatch_mod, "_gh", gh)
    errored = CriticOutcome(
        verdict="error",
        reasons=[],
        duration_s=1.0,
        stdout_tail="(timeout)",
        report=None,
        error="critic exceeded 10s",
    )
    monkeypatch.setattr(
        dispatch_mod,
        "_critic_review",
        _seq_review([_suspicious_first(), errored]),
    )

    dispatch_mod._run_critic_for_outcomes(cfg, [outcome], lambda *_a, **_kw: None)

    assert outcome.status == "open"
    assert outcome.error and "inconclusive" in outcome.error
    # verdict stays the first pass's "approved", but the blocking error withholds
    # merge — and the second_pass event proves the retry path was taken.
    assert _automerge_withheld_reason(outcome) is None  # verdict-based gate
    assert outcome.error  # ...but error-based gate in tick withholds the merge
    assert "critic_suspicious_second_pass" in cfg.events_file.read_text()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
