"""Guard tests for the deploy/redeploy contract (issue #25).

Acceptance criteria from the issue:
  * Unset task → ``(True, "...skipped...")`` with NO subprocess invocation.
  * ``LOOP_DEPLOY_TASK=foo`` → ``["task", "foo"]`` invoked.
  * Subprocess non-zero → ``(False, tail)`` propagated.
  * Adversarial: ``task`` binary missing → clear message, not a stack trace.
  * Integration: ``runner._tick`` with merged outcomes + empty ``deploy_task``
    emits ZERO ``redeploy`` events (and never invokes ``redeploy``).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from forge_loop import deploy as _deploy
from forge_loop import runner as _runner
from forge_loop.config import (
    AttemptsConfig,
    Briefs,
    Config,
    CriticConfig,
    Labels,
    LumenConfig,
    POConfig,
)
from forge_loop.worker import WorkerOutcome

# ---------------------------------------------------------------------------
# Unit: redeploy() contract
# ---------------------------------------------------------------------------

def test_redeploy_unset_task_is_noop_skip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    spy = MagicMock()
    monkeypatch.setattr(subprocess, "run", spy)

    ok, msg = _deploy.redeploy(tmp_path, task_name="")

    assert ok is True
    assert "skipped" in msg.lower()
    spy.assert_not_called()


def test_redeploy_unset_task_default_arg_is_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Even with env-set LOOP_DEPLOY_TASK, the default arg must remain "" so the
    # module is not sensitive to import-time env state. The runner threads the
    # configured task explicitly; redeploy() with no positional arg = skip.
    monkeypatch.setenv("LOOP_DEPLOY_TASK", "should-be-ignored")
    spy = MagicMock()
    monkeypatch.setattr(subprocess, "run", spy)

    ok, msg = _deploy.redeploy(tmp_path)

    assert ok is True
    assert "skipped" in msg.lower()
    spy.assert_not_called()


def test_redeploy_runs_configured_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        result = MagicMock()
        result.returncode = 0
        result.stdout = "rolling out\n"
        result.stderr = ""
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, tail = _deploy.redeploy(tmp_path, task_name="foo")

    assert ok is True
    assert captured["cmd"] == ["task", "foo"]
    assert captured["cwd"] == str(tmp_path)
    assert "rolling out" in tail


def test_redeploy_nonzero_returns_false_with_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        result = MagicMock()
        result.returncode = 2
        result.stdout = ""
        result.stderr = "task: No Taskfile found\n"
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, tail = _deploy.redeploy(tmp_path, task_name="deploy:k3s:trunk")

    assert ok is False
    assert "No Taskfile" in tail


def test_redeploy_tail_truncated_to_800(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    huge = "x" * 5000

    def fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        result = MagicMock()
        result.returncode = 1
        result.stdout = huge
        result.stderr = ""
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, tail = _deploy.redeploy(tmp_path, task_name="foo")

    assert ok is False
    assert len(tail) == 800


def test_redeploy_task_binary_missing_is_clean_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(*_a, **_kw):  # type: ignore[no-untyped-def]
        raise FileNotFoundError(2, "No such file or directory", "task")

    monkeypatch.setattr(subprocess, "run", fake_run)

    # MUST NOT propagate a stack trace.
    ok, msg = _deploy.redeploy(tmp_path, task_name="foo")

    assert ok is False
    assert "task" in msg.lower()
    assert "not found" in msg.lower()


# ---------------------------------------------------------------------------
# Integration: runner._tick() with empty deploy_task and merged outcome
# ---------------------------------------------------------------------------

def _make_cfg(tmp_path: Path, deploy_task: str = "") -> Config:
    return Config(
        repo=tmp_path,
        github_repo="o/r",
        parallel=1,
        tick_interval_s=0,
        max_ticks=1,
        worker_timeout_s=60,
        deploy_task=deploy_task,
        labels=Labels(),
        briefs=Briefs(),
        critic=CriticConfig(enabled=False, timeout_s=10),
        po=POConfig(enabled=False, timeout_s=10, max_to_expand_per_tick=0),
        attempts=AttemptsConfig(enabled=False, max_history_in_brief=5),
        lumen=LumenConfig(),
    )


def _read_events(cfg: Config) -> list[dict[str, Any]]:
    if not cfg.events_file.exists():
        return []
    return [
        json.loads(line)
        for line in cfg.events_file.read_text().splitlines()
        if line.strip()
    ]


@pytest.fixture
def runner_world(monkeypatch, tmp_path: Path):
    cfg = _make_cfg(tmp_path, deploy_task="")
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.events_file.touch()

    issue = {"number": 99, "title": "demo", "body": "x", "labels": []}

    monkeypatch.setattr(_runner, "top_issues", lambda *a, **k: [dict(issue)])
    monkeypatch.setattr(_runner, "_reap_worktree", lambda *a, **k: None)
    monkeypatch.setattr(_runner, "_short_sleep", lambda *a, **k: None)

    # Worker scripted to "merged" so the redeploy branch is reached.
    def fake_run_worker(issue_, repo, logs_dir, timeout_s, **kwargs):
        return WorkerOutcome(
            issue=issue_["number"], title=issue_["title"],
            pr_url="https://github.com/o/r/pull/1", status="merged",
            duration_s=1.0, stdout_tail="", error=None, events=[],
        )

    monkeypatch.setattr(_runner, "run_worker", fake_run_worker)

    # Boobytrap redeploy — it must NOT be called when deploy_task is empty.
    def trap_redeploy(*a, **k):  # type: ignore[no-untyped-def]
        raise AssertionError("redeploy() must NOT be invoked when deploy_task is empty")

    monkeypatch.setattr(_runner, "redeploy", trap_redeploy)
    return cfg


def test_tick_with_empty_deploy_task_emits_no_redeploy_event(runner_world) -> None:
    cfg = runner_world
    _runner._tick(cfg, tick=1)

    events = _read_events(cfg)
    redeploy_events = [e for e in events if e.get("kind") == "redeploy"]
    # Acceptance: zero redeploy events (or one with kind=skipped). We chose zero.
    assert redeploy_events == [], f"expected no redeploy events, got: {redeploy_events}"

    # Also confirm there are zero redeploy-failure rows specifically.
    failed = [e for e in events if e.get("kind") == "redeploy" and e.get("ok") is False]
    assert failed == []
