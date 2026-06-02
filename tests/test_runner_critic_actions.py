"""Tests for critic_actions.plan_actions + apply_critic_report."""

from __future__ import annotations

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
    plan = plan_actions(rep, pr_changed_lines=200, block_on_sev2=False, min_findings_for_approve=50)
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
        pr_changed_lines=200,
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
        lambda *_a, **_kw: SimpleNamespace(block_merge=True),
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
        lambda *_a, **_kw: SimpleNamespace(block_merge=False),
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
