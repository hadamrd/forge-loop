"""Tests for issue #34: critic dispatch threads ``--model`` into the CLI argv."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from forge_loop import critic


def test_build_critic_argv_includes_model_when_set(tmp_path: Path) -> None:
    argv = critic._build_critic_argv("b", tmp_path, model="claude-sonnet-4-6")
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4-6"


def test_build_critic_argv_omits_model_when_none(tmp_path: Path) -> None:
    argv = critic._build_critic_argv("b", tmp_path, model=None)
    assert "--model" not in argv


def test_review_pr_threads_model_into_subprocess(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv, **_kw):
        captured["argv"] = list(argv)
        return MagicMock(returncode=0)

    with (
        patch.object(critic, "subprocess", MagicMock(run=fake_run, TimeoutExpired=Exception)),
        patch.object(critic, "ensure_subagent_trusted", lambda *_a, **_k: None),
    ):
        critic.review_pr(
            "https://github.com/o/r/pull/1",
            123,
            tmp_path,
            tmp_path / "logs",
            timeout_s=10,
            brief_template="dummy",
            model="claude-sonnet-4-6",
        )
    argv = captured["argv"]
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4-6"


def test_review_pr_without_model_omits_flag(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv, **_kw):
        captured["argv"] = list(argv)
        return MagicMock(returncode=0)

    with (
        patch.object(critic, "subprocess", MagicMock(run=fake_run, TimeoutExpired=Exception)),
        patch.object(critic, "ensure_subagent_trusted", lambda *_a, **_k: None),
    ):
        critic.review_pr(
            "https://github.com/o/r/pull/1",
            123,
            tmp_path,
            tmp_path / "logs",
            timeout_s=10,
            brief_template="dummy",
        )
    assert "--model" not in captured["argv"]


def test_review_pr_codex_provider_uses_codex_backend(tmp_path: Path, monkeypatch) -> None:
    from forge_loop import agent_backend

    captured: dict[str, object] = {}

    def fake_codex(**kwargs):
        captured.update(kwargs)
        return agent_backend.AgentRunResult(
            provider="codex",
            log_path=kwargs["log_path"],
            last_message='{"overall": "approve", "findings": []}',
            duration_s=0.5,
        )

    monkeypatch.setattr(agent_backend, "run_codex_exec", fake_codex)
    with patch.object(critic, "ensure_subagent_trusted", lambda *_a, **_k: None):
        outcome = critic.review_pr(
            "https://github.com/o/r/pull/1",
            123,
            tmp_path,
            tmp_path / "logs",
            timeout_s=10,
            brief_template="dummy",
            provider="codex",
            model="gpt-5-codex",
        )
    assert captured["model"] == "gpt-5-codex"
    assert outcome.verdict == "approved"
