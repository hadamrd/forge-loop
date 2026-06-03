"""Tests for the SDK-driven critic + PO paths (issue #85).

Pins the public contract of :mod:`forge_loop.critic` and :mod:`forge_loop.po`
after migrating from ``subprocess.run(['claude', '-p', ...])`` to the
Claude Agent SDK.

The SDK boundary is mocked: ``run_critic_sdk`` / ``run_po_sdk`` are
monkeypatched to return canned :class:`CriticSdkResult` instances, so
unit tests don't need the SDK client importable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge_loop import critic as critic_mod
from forge_loop import po as po_mod
from forge_loop._critic_sdk import CriticSdkResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_logs(tmp_path: Path) -> Path:
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs


def _approve_payload(reason: str = "looks good") -> str:
    """Build a minimal valid CriticReport JSON payload."""
    return json.dumps(
        {
            "overall": "approve",
            "findings": [
                {
                    "severity": "sev3",
                    "category": "style",
                    "file": None,
                    "line": None,
                    "message": reason,
                },
            ],
        }
    )


# ---------------------------------------------------------------------------
# critic.review_pr — SDK path is now the default for the claude provider
# ---------------------------------------------------------------------------


def test_review_pr_uses_sdk_path_no_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claude-provider critic path goes through run_critic_sdk, not
    subprocess. The regression we're pinning: a future contributor must
    not reintroduce subprocess.run('claude', ...) under any branch."""
    called: dict[str, object] = {}

    def fake_sdk(**kwargs: object) -> CriticSdkResult:
        called.update(kwargs)
        return CriticSdkResult(
            last_message=_approve_payload(),
            duration_s=1.5,
        )

    monkeypatch.setattr("forge_loop.critic.run_critic_sdk", fake_sdk, raising=False)
    # Direct import path used inside critic.review_pr is a from-import; we
    # set the function on the module via the helper above. Belt-and-braces:
    monkeypatch.setattr("forge_loop._critic_sdk.run_critic_sdk", fake_sdk)
    # ensure_subagent_trusted touches the filesystem; bypass for tests
    monkeypatch.setattr("forge_loop.critic.ensure_subagent_trusted", lambda _p: None)

    outcome = critic_mod.review_pr(
        pr_url="https://github.com/owner/repo/pull/1",
        issue_number=1,
        repo=tmp_path,
        logs_dir=_mk_logs(tmp_path),
        timeout_s=60,
        model="claude-sonnet-4-6",
    )

    assert outcome.verdict == "approved"
    assert outcome.report is not None
    assert outcome.report.overall == "approve"
    assert called["model"] == "claude-sonnet-4-6"
    assert called["timeout_s"] == 60


def test_review_pr_enforces_precommit_bypass_detector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "forge_loop._critic_sdk.run_critic_sdk",
        lambda **kw: CriticSdkResult(
            last_message=json.dumps({"overall": "approve", "findings": []}),
            duration_s=0.1,
        ),
    )
    monkeypatch.setattr("forge_loop.critic.ensure_subagent_trusted", lambda _p: None)
    monkeypatch.setattr(
        "forge_loop.critic._fetch_pr_precommit_context",
        lambda _pr_url, _repo: ("ordinary PR body", "git commit --no-verify -m bad"),
        raising=False,
    )

    outcome = critic_mod.review_pr(
        pr_url="https://github.com/owner/repo/pull/1",
        issue_number=1,
        repo=tmp_path,
        logs_dir=_mk_logs(tmp_path),
        timeout_s=60,
    )

    assert outcome.verdict == "changes_requested"
    assert outcome.report is not None
    assert outcome.report.has_sev1()
    assert any("precommit_bypass" in reason for reason in outcome.reasons)


def test_review_pr_enforces_precommit_bypass_from_worker_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "forge_loop._critic_sdk.run_critic_sdk",
        lambda **kw: CriticSdkResult(
            last_message=json.dumps({"overall": "approve", "findings": []}),
            duration_s=0.1,
        ),
    )
    monkeypatch.setattr("forge_loop.critic.ensure_subagent_trusted", lambda _p: None)
    monkeypatch.setattr(
        "forge_loop.critic._fetch_pr_precommit_context",
        lambda _pr_url, _repo: ("ordinary PR body", "ordinary commit"),
        raising=False,
    )
    logs = _mk_logs(tmp_path)
    (logs / "worker-1-123.log").write_text(
        json.dumps(
            {
                "type": "item.started",
                "item": {
                    "type": "command_execution",
                    "command": "git commit --no-verify -m bypass",
                },
            }
        )
        + "\n"
    )

    outcome = critic_mod.review_pr(
        pr_url="https://github.com/owner/repo/pull/1",
        issue_number=1,
        repo=tmp_path,
        logs_dir=logs,
        timeout_s=60,
    )

    assert outcome.verdict == "changes_requested"
    assert outcome.report is not None
    assert outcome.report.has_sev1()
    assert any("precommit_bypass" in reason for reason in outcome.reasons)


def test_review_pr_sdk_timeout_returns_error_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An SDK-side timeout must surface as verdict=error so the runner
    doesn't auto-approve nor auto-block a PR the critic never reviewed."""
    monkeypatch.setattr(
        "forge_loop._critic_sdk.run_critic_sdk",
        lambda **kw: CriticSdkResult(last_message="", duration_s=60.0, timed_out=True, error=None),
    )
    monkeypatch.setattr("forge_loop.critic.ensure_subagent_trusted", lambda _p: None)

    outcome = critic_mod.review_pr(
        pr_url="https://github.com/owner/repo/pull/1",
        issue_number=1,
        repo=tmp_path,
        logs_dir=_mk_logs(tmp_path),
        timeout_s=60,
    )
    assert outcome.verdict == "error"
    assert "timeout" in (outcome.error or "").lower() or "exceeded" in (outcome.error or "").lower()


def test_review_pr_sdk_error_returns_error_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An SDK transport / auth error must surface as verdict=error with
    the error string propagated for operator triage."""
    monkeypatch.setattr(
        "forge_loop._critic_sdk.run_critic_sdk",
        lambda **kw: CriticSdkResult(
            last_message="",
            duration_s=0.5,
            timed_out=False,
            error="sdk_auth_failed: 401",
        ),
    )
    monkeypatch.setattr("forge_loop.critic.ensure_subagent_trusted", lambda _p: None)

    outcome = critic_mod.review_pr(
        pr_url="https://github.com/owner/repo/pull/1",
        issue_number=1,
        repo=tmp_path,
        logs_dir=_mk_logs(tmp_path),
        timeout_s=60,
    )
    assert outcome.verdict == "error"
    assert outcome.error is not None
    assert "401" in outcome.error or "sdk" in outcome.error.lower()


def test_review_pr_unparseable_text_retries_then_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the SDK returns text that isn't a CriticReport, the critic
    retries once. Two consecutive unparseable responses → verdict=error
    and emit('critic_parse_failed') fires."""
    monkeypatch.setattr(
        "forge_loop._critic_sdk.run_critic_sdk",
        lambda **kw: CriticSdkResult(
            last_message="this is not json",
            duration_s=0.1,
        ),
    )
    monkeypatch.setattr("forge_loop.critic.ensure_subagent_trusted", lambda _p: None)
    emitted: list[tuple[str, dict[str, object]]] = []
    outcome = critic_mod.review_pr(
        pr_url="https://github.com/owner/repo/pull/1",
        issue_number=1,
        repo=tmp_path,
        logs_dir=_mk_logs(tmp_path),
        timeout_s=60,
        emit=lambda kind, payload: emitted.append((kind, payload)),
    )
    assert outcome.verdict == "error"
    assert any(k == "critic_parse_failed" for k, _ in emitted)
    assert outcome.parse_retries >= 1


# ---------------------------------------------------------------------------
# po._run_one — same migration
# ---------------------------------------------------------------------------


def test_po_uses_sdk_path_no_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_sdk(**kwargs: object) -> CriticSdkResult:
        captured.update(kwargs)
        return CriticSdkResult(
            last_message=json.dumps(
                {
                    "skipped": False,
                    "reason": "expanded",
                    "sections_added": ["Acceptance criteria"],
                }
            ),
            duration_s=2.0,
        )

    monkeypatch.setattr("forge_loop._critic_sdk.run_po_sdk", fake_sdk)
    monkeypatch.setattr("forge_loop.po.ensure_subagent_trusted", lambda _p: None)

    outcome = po_mod._run_one(
        issue_number=42,
        brief="prompt",
        repo=tmp_path,
        logs_dir=_mk_logs(tmp_path),
        timeout_s=120,
        model="claude-opus-4-7",
    )

    assert outcome.skipped is False
    assert outcome.reason == "expanded"
    assert "Acceptance criteria" in outcome.sections_added
    assert captured["model"] == "claude-opus-4-7"


def test_po_sdk_timeout_returns_po_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "forge_loop._critic_sdk.run_po_sdk",
        lambda **kw: CriticSdkResult(
            last_message="",
            duration_s=120.0,
            timed_out=True,
        ),
    )
    monkeypatch.setattr("forge_loop.po.ensure_subagent_trusted", lambda _p: None)

    outcome = po_mod._run_one(
        issue_number=42,
        brief="prompt",
        repo=tmp_path,
        logs_dir=_mk_logs(tmp_path),
        timeout_s=120,
    )
    assert outcome.reason == "po-timeout"
    assert outcome.error is not None


# ---------------------------------------------------------------------------
# Architectural regression — no subprocess.run('claude', ...) in critic.py /
# po.py after #85.
# ---------------------------------------------------------------------------


def test_no_subprocess_claude_in_critic_or_po() -> None:
    """Catches a future contributor reintroducing the legacy subprocess
    path. The AC for #85 was 'grep returns 0 hits' — this test pins it
    via reading the source files."""
    repo_root = Path(__file__).resolve().parent.parent
    for fname in ("src/forge_loop/critic.py", "src/forge_loop/po.py"):
        text = (repo_root / fname).read_text()
        # Tolerate doc/comment mentions; reject actual subprocess calls.
        assert "subprocess.run" not in text, (
            f"{fname} still calls subprocess.run — migrate via _critic_sdk"
        )
        assert "import subprocess" not in text, f"{fname} still imports subprocess directly"
