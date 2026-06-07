"""Issue #311: the ``critic:suspicious`` guard must be self-clearing, never
human-terminal.

Covers the pure decision functions in ``critic_actions`` (second-pass
reconciliation, timeout resolution, Part-B calibration stub) and the
``runner.dispatch`` orchestration that runs ONE independent second pass and
resolves the flag autonomously.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from forge_loop.config import Config, CriticConfig
from forge_loop.critic import CriticOutcome, CriticReport, Finding, ManifestoViolation
from forge_loop.critic_actions import (
    SuspiciousResolution,
    calibrate_suspicious_threshold,
    reconcile_suspicious,
    resolve_suspicious_timeout,
    suspicious_precision,
)
from forge_loop.runner import dispatch as dispatch_mod
from forge_loop.worker import WorkerOutcome


def _report(overall: str, findings: list[Finding] | None = None, **kw) -> CriticReport:
    return CriticReport(overall=overall, findings=findings or [], **kw)


# ---------------------------------------------------------------------------
# reconcile_suspicious — second-pass adjudication (AC2/AC3/AC5)
# ---------------------------------------------------------------------------


def test_reconcile_real_sev2_corroborates() -> None:
    second = _report("request_changes", [Finding("sev2", "correctness", "a.py", 3, "bug")])
    assert reconcile_suspicious(second) is SuspiciousResolution.CORROBORATED


def test_reconcile_real_sev1_manifesto_corroborates() -> None:
    second = _report(
        "block",
        manifesto_violations=[
            ManifestoViolation("Q1", "quality", "globals", "use Container", "sev1")
        ],
    )
    assert reconcile_suspicious(second) is SuspiciousResolution.CORROBORATED


def test_reconcile_clean_second_approve_clears() -> None:
    # Two independent passes find nothing real → the flag was a false positive.
    assert reconcile_suspicious(_report("approve", [])) is SuspiciousResolution.CLEARED


def test_reconcile_only_sev3_clears() -> None:
    # A cosmetic nit is not a real sev1/sev2 → not corroborated (AC2).
    second = _report("request_changes", [Finding("sev3", "style", None, None, "nit")])
    assert reconcile_suspicious(second) is SuspiciousResolution.CLEARED


# ---------------------------------------------------------------------------
# resolve_suspicious_timeout — majority verdict, never frozen (AC4/AC5)
# ---------------------------------------------------------------------------


def test_timeout_majority_approve_clears() -> None:
    res = resolve_suspicious_timeout(["approved", "approved"], has_real_sev=False)
    assert res is SuspiciousResolution.CLEARED


def test_timeout_tie_defaults_to_hold() -> None:
    # AC4: a tie resolves to HOLD (the conservative direction), never auto-merge.
    res = resolve_suspicious_timeout(["approved", "error"], has_real_sev=False)
    assert res is SuspiciousResolution.CORROBORATED


def test_timeout_real_sev_always_holds_even_when_majority_approves() -> None:
    # AC5 safety invariant: a recorded real sev1/sev2 holds regardless of votes.
    res = resolve_suspicious_timeout(
        ["approved", "approved", "approved"], has_real_sev=True
    )
    assert res is SuspiciousResolution.CORROBORATED


def test_timeout_empty_votes_holds() -> None:
    assert resolve_suspicious_timeout([], has_real_sev=False) is SuspiciousResolution.CORROBORATED


# ---------------------------------------------------------------------------
# Part B (gated): precision + calibration stub (AC7/AC8)
# ---------------------------------------------------------------------------


def test_precision_none_on_empty_window() -> None:
    assert suspicious_precision([]) is None


def test_precision_is_fraction_corroborated() -> None:
    window = [
        SuspiciousResolution.CORROBORATED,
        SuspiciousResolution.CLEARED,
        SuspiciousResolution.CLEARED,
        SuspiciousResolution.CORROBORATED,
    ]
    assert suspicious_precision(window) == pytest.approx(0.5)


def test_calibration_disabled_is_noop() -> None:
    # AC8: flag off → heuristic unchanged regardless of precision.
    cal = calibrate_suspicious_threshold(precision=0.0, base_min_lines=600, enabled=False)
    assert cal.effective_min_lines == 600
    assert cal.loosened is False


def test_calibration_loosens_only_below_threshold() -> None:
    low = calibrate_suspicious_threshold(precision=0.1, base_min_lines=600, enabled=True)
    assert low.loosened is True
    assert low.effective_min_lines > 600

    high = calibrate_suspicious_threshold(precision=0.9, base_min_lines=600, enabled=True)
    assert high.loosened is False
    assert high.effective_min_lines == 600


def test_calibration_missing_precision_is_noop() -> None:
    cal = calibrate_suspicious_threshold(precision=None, base_min_lines=600, enabled=True)
    assert cal.effective_min_lines == 600
    assert cal.loosened is False


# ---------------------------------------------------------------------------
# dispatch orchestration — one independent second pass, resolved autonomously
# ---------------------------------------------------------------------------


def _event_kinds(events_file: Path) -> list[str]:
    if not events_file.exists():
        return []
    kinds: list[str] = []
    for ln in events_file.read_text().splitlines():
        if not ln.strip():
            continue
        obj = json.loads(ln)
        kind = obj.get("event") or obj.get("kind")
        if kind:
            kinds.append(kind)
    return kinds


def _cfg(tmp_path) -> Config:
    return Config(
        repo=tmp_path,
        github_repo="o/r",
        critic=CriticConfig(enabled=True, timeout_s=10),
    )


def _outcome() -> WorkerOutcome:
    return WorkerOutcome(
        issue=311,
        title="big clean refactor",
        pr_url="https://github.com/o/r/pull/7",
        status="open",
        duration_s=1.0,
        stdout_tail="",
    )


def _wire_first_pass_suspicious(monkeypatch, second_outcomes: list[CriticOutcome]) -> list:
    """Make the FIRST critic pass flag suspicious, then feed ``second_outcomes``
    to the inline second pass. Returns the list of ``_critic_review`` call args
    so a test can assert the second pass fires exactly once."""
    calls: list = []
    queue = [
        CriticOutcome(
            verdict="approved",
            reasons=[],
            duration_s=1.0,
            stdout_tail="",
            report=_report("approve", []),
        ),
        *second_outcomes,
    ]

    def fake_review(*a, **kw):  # type: ignore[no-untyped-def]
        calls.append((a, kw))
        return queue.pop(0)

    monkeypatch.setattr(dispatch_mod, "_critic_review", fake_review)
    monkeypatch.setattr(dispatch_mod._gh, "pr_changed_lines", lambda *_a, **_kw: 900)
    # First pass: a suspicious-approve plan that routes into the second pass.
    monkeypatch.setattr(
        dispatch_mod,
        "apply_critic_report",
        lambda *_a, **_kw: SimpleNamespace(suspicious_approve=True, block_merge=True),
    )
    return calls


def test_second_pass_clears_removes_labels_and_emits(monkeypatch, tmp_path) -> None:
    cfg = _cfg(tmp_path)
    o = _outcome()
    removed: list = []
    monkeypatch.setattr(dispatch_mod._gh, "auth_source", "gh cli", raising=False)
    monkeypatch.setattr(
        dispatch_mod._gh,
        "remove_pr_label",
        lambda pr, label, repo=None: (removed.append(label), True)[1],
    )
    calls = _wire_first_pass_suspicious(
        monkeypatch,
        [
            CriticOutcome(
                verdict="approved",
                reasons=[],
                duration_s=1.0,
                stdout_tail="",
                report=_report("approve", []),
            )
        ],
    )
    dispatch_mod._run_critic_for_outcomes(cfg, [o], lambda *_a, **_kw: None)

    # Exactly ONE second pass fired (AC1): first pass + one second pass.
    assert len(calls) == 2
    assert "critic:suspicious" in removed
    assert "critic_suspicious_cleared" in _event_kinds(cfg.events_file)
    # Cleared → NOT held: no error withholding auto-merge.
    assert not o.error


def test_second_pass_corroborates_real_sev_holds_and_blocks(monkeypatch, tmp_path) -> None:
    cfg = _cfg(tmp_path)
    o = _outcome()
    labels_added: list = []
    monkeypatch.setattr(dispatch_mod._gh, "auth_source", "gh cli", raising=False)
    monkeypatch.setattr(dispatch_mod._gh, "remove_pr_label", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        dispatch_mod._gh,
        "add_pr_label",
        lambda pr, labels, repo=None: (labels_added.extend(labels), True)[1],
    )
    _wire_first_pass_suspicious(
        monkeypatch,
        [
            CriticOutcome(
                verdict="changes_requested",
                reasons=["real bug"],
                duration_s=1.0,
                stdout_tail="",
                report=_report(
                    "request_changes", [Finding("sev2", "correctness", "a.py", 9, "real bug")]
                ),
            )
        ],
    )
    dispatch_mod._run_critic_for_outcomes(cfg, [o], lambda *_a, **_kw: None)

    kinds = _event_kinds(cfg.events_file)
    assert "critic_suspicious_corroborated" in kinds
    assert "critic_suspicious_cleared" not in kinds
    # AC5 safety: a corroborated real sev is BLOCKED (never auto-merged).
    assert "critic:blocking" in labels_added
    assert o.status == "open"
    assert "corroborated" in (o.error or "")


def test_second_pass_error_does_not_clear_or_merge(monkeypatch, tmp_path) -> None:
    """#267 hole: a crashed second pass (report is None) must NOT read as cleared
    — never auto-merge an unreviewed PR; hold and retry."""
    cfg = _cfg(tmp_path)
    o = _outcome()
    removed: list = []
    monkeypatch.setattr(dispatch_mod._gh, "auth_source", "gh cli", raising=False)
    monkeypatch.setattr(
        dispatch_mod._gh,
        "remove_pr_label",
        lambda pr, label, repo=None: (removed.append(label), True)[1],
    )
    _wire_first_pass_suspicious(
        monkeypatch,
        [
            CriticOutcome(
                verdict="error",
                reasons=[],
                duration_s=1.0,
                stdout_tail="(timeout)",
                report=None,
                error="critic exceeded 10s",
            )
        ],
    )
    dispatch_mod._run_critic_for_outcomes(cfg, [o], lambda *_a, **_kw: None)

    kinds = _event_kinds(cfg.events_file)
    assert "critic_suspicious_second_pass" in kinds
    # NOT cleared and the suspicious label is NOT dropped → no auto-merge.
    assert "critic_suspicious_cleared" not in kinds
    assert "critic:suspicious" not in removed
    assert o.status == "open"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
