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


# ---------------------------------------------------------------------------
# Teaching critic — round derivation + brief threading + sev3 demotion E2E
# ---------------------------------------------------------------------------


def test_review_pr_threads_round_number_into_brief_and_demotes_sev3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: prior critic logs => round count => brief carries the round
    and stalled-round guidance => the parsed report's sev3 nits are demoted."""
    logs = _mk_logs(tmp_path)
    # Seed THREE prior reviews so this call is round 4 (>= demotion threshold 3).
    for stamp in (1000, 2000, 3000):
        (logs / f"critic-1-{stamp}-0.log").write_text("{}")

    seen_brief: dict[str, str] = {}

    def fake_sdk(**kwargs: object) -> CriticSdkResult:
        seen_brief["prompt"] = str(kwargs.get("prompt", ""))
        return CriticSdkResult(
            last_message=json.dumps(
                {
                    "overall": "request_changes",
                    "minimal_path_to_green": ["fix the real defect in a.py"],
                    "findings": [
                        {"severity": "sev2", "category": "tests", "message": "weak assert"},
                        {"severity": "sev3", "category": "style", "message": "rename var"},
                    ],
                }
            ),
            duration_s=0.1,
        )

    monkeypatch.setattr("forge_loop._critic_sdk.run_critic_sdk", fake_sdk)
    monkeypatch.setattr("forge_loop.critic.ensure_subagent_trusted", lambda _p: None)

    outcome = critic_mod.review_pr(
        pr_url="https://github.com/owner/repo/pull/1",
        issue_number=1,
        repo=tmp_path,
        logs_dir=logs,
        timeout_s=60,
        sev3_demotion_round_threshold=3,
    )

    assert "3 prior review(s)" in seen_brief["prompt"]
    assert "ROUND 4" in seen_brief["prompt"]
    assert outcome.report is not None
    assert outcome.report.round_number == 3
    assert sorted(f.severity for f in outcome.report.findings) == ["sev2"]
    assert sorted(f.severity for f in outcome.report.follow_ups) == ["sev3"]
    assert outcome.report.minimal_path_to_green == ["fix the real defect in a.py"]


def test_review_pr_round1_keeps_sev3_blocking_and_terse_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First review (no prior logs): sev3 is NOT demoted and the brief is terse."""
    logs = _mk_logs(tmp_path)
    seen_brief: dict[str, str] = {}

    def fake_sdk(**kwargs: object) -> CriticSdkResult:
        seen_brief["prompt"] = str(kwargs.get("prompt", ""))
        return CriticSdkResult(
            last_message=json.dumps(
                {
                    "overall": "request_changes",
                    "minimal_path_to_green": ["address the nit"],
                    "findings": [
                        {"severity": "sev3", "category": "style", "message": "rename var"},
                    ],
                }
            ),
            duration_s=0.1,
        )

    monkeypatch.setattr("forge_loop._critic_sdk.run_critic_sdk", fake_sdk)
    monkeypatch.setattr("forge_loop.critic.ensure_subagent_trusted", lambda _p: None)

    outcome = critic_mod.review_pr(
        pr_url="https://github.com/owner/repo/pull/1",
        issue_number=1,
        repo=tmp_path,
        logs_dir=logs,
        timeout_s=60,
        sev3_demotion_round_threshold=3,
    )

    assert "ROUND 1" in seen_brief["prompt"]
    assert outcome.report is not None
    assert outcome.report.round_number == 0
    assert [f.severity for f in outcome.report.findings] == ["sev3"]
    assert outcome.report.follow_ups == []


# ---------------------------------------------------------------------------
# #270 — root-cause critic verdict=error: capture, classify, retry-recover.
# ---------------------------------------------------------------------------

from forge_loop._critic_sdk import (  # noqa: E402
    CriticErrorClass,
    classify_critic_error,
    classify_critic_error_text,
    is_transient_critic_error,
)


def test_classify_event_loop_closed_from_exception() -> None:
    assert (
        classify_critic_error(RuntimeError("Event loop is closed"))
        is CriticErrorClass.EVENT_LOOP_CLOSED
    )


def test_classify_timeout_from_exception() -> None:
    assert classify_critic_error(TimeoutError()) is CriticErrorClass.TIMEOUT


def test_classify_transport_and_unknown_from_text() -> None:
    assert (
        classify_critic_error_text("httpx.ConnectError: connection refused")
        is CriticErrorClass.SDK_TRANSPORT
    )
    # Regression (#274 sev2): "read timed out" is a transient transport blip
    # (retryable SDK_TRANSPORT), NOT the terminal TIMEOUT — transport markers
    # must be checked before the generic timeout branch.
    assert (
        classify_critic_error_text("httpx.ReadTimeout: read timed out")
        is CriticErrorClass.SDK_TRANSPORT
    )
    assert classify_critic_error_text("critic session timed out after 600s") is CriticErrorClass.TIMEOUT
    assert classify_critic_error_text("totally novel boom") is CriticErrorClass.UNKNOWN


def test_only_eventloop_and_transport_are_transient() -> None:
    assert is_transient_critic_error(CriticErrorClass.EVENT_LOOP_CLOSED)
    assert is_transient_critic_error(CriticErrorClass.SDK_TRANSPORT)
    assert not is_transient_critic_error(CriticErrorClass.TIMEOUT)
    assert not is_transient_critic_error(CriticErrorClass.PARSE_FAILURE)
    assert not is_transient_critic_error(CriticErrorClass.UNKNOWN)


def test_transient_sdk_error_retries_then_recovers_to_real_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient event-loop error on attempt 0 that succeeds on attempt 1
    returns the REAL verdict (not verdict=error), and parse_retries reflects
    the retry. Proves a transient blip recovers instead of burning a round."""
    monkeypatch.setattr(critic_mod, "_TRANSIENT_BACKOFF_S", 0.0)
    monkeypatch.setattr("forge_loop.critic.ensure_subagent_trusted", lambda _p: None)
    calls = {"n": 0}

    def flaky_sdk(**kw: object) -> CriticSdkResult:
        calls["n"] += 1
        if calls["n"] == 1:
            return CriticSdkResult(
                last_message="",
                duration_s=0.1,
                error="RuntimeError: Event loop is closed",
                error_class=CriticErrorClass.EVENT_LOOP_CLOSED,
                error_detail="RuntimeError: Event loop is closed\n<traceback>",
            )
        return CriticSdkResult(last_message=_approve_payload(), duration_s=0.2)

    monkeypatch.setattr("forge_loop._critic_sdk.run_critic_sdk", flaky_sdk)

    outcome = critic_mod.review_pr(
        pr_url="https://github.com/owner/repo/pull/1",
        issue_number=7,
        repo=tmp_path,
        logs_dir=_mk_logs(tmp_path),
        timeout_s=60,
    )

    assert calls["n"] == 2
    assert outcome.verdict == "approved"
    assert outcome.parse_retries == 1
    assert outcome.error_class is None


def test_persistent_event_loop_closed_exhausts_budget_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adversarial: a PERSISTENT event-loop failure exhausts the retry+backoff
    budget and still returns verdict=error with error_class=event_loop_closed —
    retry can't mask a real outage and the #264/#269 safety contract holds."""
    monkeypatch.setattr(critic_mod, "_TRANSIENT_BACKOFF_S", 0.0)
    monkeypatch.setattr("forge_loop.critic.ensure_subagent_trusted", lambda _p: None)
    calls = {"n": 0}

    def always_loop_closed(**kw: object) -> CriticSdkResult:
        calls["n"] += 1
        return CriticSdkResult(
            last_message="",
            duration_s=0.1,
            error="RuntimeError: Event loop is closed",
            error_class=CriticErrorClass.EVENT_LOOP_CLOSED,
            error_detail="RuntimeError: Event loop is closed\n" + ("x" * 3000),
        )

    monkeypatch.setattr("forge_loop._critic_sdk.run_critic_sdk", always_loop_closed)
    emitted: list[tuple[str, dict[str, object]]] = []

    outcome = critic_mod.review_pr(
        pr_url="https://github.com/owner/repo/pull/2",
        issue_number=8,
        repo=tmp_path,
        logs_dir=_mk_logs(tmp_path),
        timeout_s=60,
        emit=lambda kind, payload: emitted.append((kind, payload)),
    )

    # Bounded: exactly the 2-attempt budget, never an unbounded storm.
    assert calls["n"] == 2
    assert outcome.verdict == "error"
    assert outcome.error_class is CriticErrorClass.EVENT_LOOP_CLOSED
    assert outcome.parse_retries == 1
    payload = next(p for k, p in emitted if k == "critic_parse_failed")
    assert payload["error_class"] == "event_loop_closed"
    assert payload["error_log"]


def test_error_log_written_with_full_class_and_long_excerpt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-critic error log holds the class name + an excerpt LONGER than
    the 200-char event field (full capture goes to disk, #270)."""
    monkeypatch.setattr("forge_loop.critic.ensure_subagent_trusted", lambda _p: None)
    long_detail = "RuntimeError: Event loop is closed\n" + ("y" * 2500)
    monkeypatch.setattr(
        "forge_loop._critic_sdk.run_critic_sdk",
        lambda **kw: CriticSdkResult(
            last_message="",
            duration_s=0.1,
            error="sdk_session_failed: RuntimeError: Event loop is closed",
            error_class=CriticErrorClass.UNKNOWN,  # non-transient → single attempt
            error_detail=long_detail,
        ),
    )
    logs = _mk_logs(tmp_path)
    outcome = critic_mod.review_pr(
        pr_url="https://github.com/owner/repo/pull/3",
        issue_number=9,
        repo=tmp_path,
        logs_dir=logs,
        timeout_s=60,
    )
    assert outcome.verdict == "error"
    assert outcome.error_log is not None
    log_text = Path(outcome.error_log).read_text(encoding="utf-8")
    assert "error_class=unknown" in log_text
    assert len(log_text) > 200
    assert "y" * 2000 in log_text


def test_timeout_is_classified_and_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout on a large diff is NOT retried into a multiplied hang: single
    attempt, classified error_class=timeout, verdict=error."""
    monkeypatch.setattr(critic_mod, "_TRANSIENT_BACKOFF_S", 0.0)
    monkeypatch.setattr("forge_loop.critic.ensure_subagent_trusted", lambda _p: None)
    calls = {"n": 0}

    def slow_sdk(**kw: object) -> CriticSdkResult:
        calls["n"] += 1
        return CriticSdkResult(
            last_message="",
            duration_s=60.0,
            timed_out=True,
            error="timeout",
            error_class=CriticErrorClass.TIMEOUT,
            error_detail="critic SDK session timed out after 60s",
        )

    monkeypatch.setattr("forge_loop._critic_sdk.run_critic_sdk", slow_sdk)
    outcome = critic_mod.review_pr(
        pr_url="https://github.com/owner/repo/pull/4",
        issue_number=10,
        repo=tmp_path,
        logs_dir=_mk_logs(tmp_path),
        timeout_s=60,
    )
    assert calls["n"] == 1  # NOT retried
    assert outcome.verdict == "error"
    assert outcome.error_class is CriticErrorClass.TIMEOUT


# ---------------------------------------------------------------------------
# Secret-lease regression (#283 / PR #289 review): the TRUSTED critic must
# keep its secrets despite run_sdk_session's closed fail-safe default.
# ---------------------------------------------------------------------------


def test_critic_sdk_retains_required_secret_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The critic threads a "keep all my secrets" lease into run_sdk_session.

    Regression: the worker's least-privilege closed default must NOT strip the
    trusted reviewer's SDK auth secret / GITHUB_TOKEN from its effective env.
    """
    from forge_loop._critic_sdk import run_critic_sdk

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-critic")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-critic")

    captured: dict[str, object] = {}

    class FakeOptions:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    async def fake_query(**_kw: object):  # type: ignore[no-untyped-def]
        if False:
            yield None
        return

    run_critic_sdk(
        "review this",
        cwd=tmp_path,
        timeout_s=30,
        query_fn=fake_query,
        options_cls=FakeOptions,
    )

    env = captured["env"]
    assert isinstance(env, dict)
    assert env.get("ANTHROPIC_API_KEY") == "sk-critic"
    assert env.get("GITHUB_TOKEN") == "gh-critic"
