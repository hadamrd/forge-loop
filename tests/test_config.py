"""Tests for config.py — YAML loading + env-var precedence."""

from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent

import pytest

from forge_loop import config as config_mod


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pretend the repo root is tmp_path (avoids hitting real git)."""
    monkeypatch.setattr(config_mod, "_repo_root", lambda: tmp_path)
    # clear any LOOP_* env vars that may leak from the parent process
    for k in list(os.environ):
        if k.startswith("LOOP_"):
            monkeypatch.delenv(k, raising=False)
    # Provide a default repo so most tests can call load() without setup
    monkeypatch.setenv("LOOP_GH_REPO", "owner/repo")
    return tmp_path


def test_load_no_yaml_returns_defaults(fake_repo: Path) -> None:
    cfg = config_mod.load()
    assert cfg.parallel == 3
    assert cfg.tick_interval_s == 60
    assert cfg.maintenance_every_n_ticks == 0
    assert cfg.labels.ready == "loop:ready"
    assert cfg.deploy_task == ""
    assert cfg.github_repo == "owner/repo"


def test_load_raises_when_no_repo_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_mod, "_repo_root", lambda: tmp_path)
    for k in list(os.environ):
        if k.startswith("LOOP_"):
            monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match="LOOP_GH_REPO"):
        config_mod.load()


def test_repo_from_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_mod, "_repo_root", lambda: tmp_path)
    for k in list(os.environ):
        if k.startswith("LOOP_"):
            monkeypatch.delenv(k, raising=False)
    (tmp_path / "forge-loop.yaml").write_text("repo:\n  github: foo/bar\n")
    cfg = config_mod.load()
    assert cfg.github_repo == "foo/bar"


def test_base_branch_from_yaml(fake_repo: Path) -> None:
    (fake_repo / "forge-loop.yaml").write_text(
        dedent("""
        repo:
          base_branch: main
    """)
    )
    cfg = config_mod.load()
    assert cfg.base_branch == "main"


def test_base_branch_env_overrides_yaml(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fake_repo / "forge-loop.yaml").write_text(
        dedent("""
        repo:
          base_branch: trunk
    """)
    )
    monkeypatch.setenv("LOOP_BASE_BRANCH", "release")
    cfg = config_mod.load()
    assert cfg.base_branch == "release"


def test_env_overrides_yaml_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_mod, "_repo_root", lambda: tmp_path)
    for k in list(os.environ):
        if k.startswith("LOOP_"):
            monkeypatch.delenv(k, raising=False)
    (tmp_path / "forge-loop.yaml").write_text("repo:\n  github: foo/bar\n")
    monkeypatch.setenv("LOOP_GH_REPO", "env/wins")
    cfg = config_mod.load()
    assert cfg.github_repo == "env/wins"


def test_coauthor_env(fake_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOP_COAUTHOR", "Foo Bar <foo@example.com>")
    cfg = config_mod.load()
    assert cfg.coauthor == "Foo Bar <foo@example.com>"


def test_yaml_in_repo_root_is_picked_up(fake_repo: Path) -> None:
    (fake_repo / "forge-loop.yaml").write_text(
        dedent("""
        scheduling:
          parallel: 7
          maintenance_every_n_ticks: 3
        deploy:
          task: my-custom-task
        labels:
          ready: "ready-now"
    """)
    )
    cfg = config_mod.load()
    assert cfg.parallel == 7
    assert cfg.maintenance_every_n_ticks == 3
    assert cfg.deploy_task == "my-custom-task"
    assert cfg.labels.ready == "ready-now"


def test_yaml_in_dev_sprint_loop_subdir_is_picked_up(fake_repo: Path) -> None:
    (fake_repo / "dev" / "sprint-loop").mkdir(parents=True)
    (fake_repo / "dev" / "sprint-loop" / "forge-loop.yaml").write_text(
        "scheduling:\n  parallel: 9\n"
    )
    cfg = config_mod.load()
    assert cfg.parallel == 9


def test_env_overrides_yaml(fake_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (fake_repo / "forge-loop.yaml").write_text("scheduling:\n  parallel: 7\n")
    monkeypatch.setenv("LOOP_PARALLEL", "12")
    cfg = config_mod.load()
    assert cfg.parallel == 12


def test_briefs_yaml_loads_maintenance_template(fake_repo: Path) -> None:
    (fake_repo / "forge-loop.yaml").write_text(
        dedent("""
        briefs:
          maintenance: |
            Custom maintenance brief content
    """)
    )
    cfg = config_mod.load()
    assert cfg.briefs.maintenance is not None
    assert "Custom maintenance brief" in cfg.briefs.maintenance


def test_explicit_config_path_env_var(fake_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    elsewhere = fake_repo / "custom-config.yaml"
    elsewhere.write_text("scheduling:\n  parallel: 42\n")
    monkeypatch.setenv("LOOP_CONFIG_PATH", str(elsewhere))
    cfg = config_mod.load()
    assert cfg.parallel == 42


# ---------------------------------------------------------------------------
# Per-role model + thinking-budget knobs (issue #34)
# ---------------------------------------------------------------------------


def test_model_defaults_match_issue_34(fake_repo: Path) -> None:
    """Defaults: worker/po → opus-4-7; critic → sonnet-4-6.

    Thinking: worker=medium, po=high, critic=off.
    """
    cfg = config_mod.load()
    assert cfg.worker.model == "claude-opus-4-7"
    assert cfg.worker.provider == "claude"
    assert cfg.worker.thinking == "medium"
    assert cfg.po.model == "claude-opus-4-7"
    assert cfg.po.provider == "claude"
    assert cfg.po.thinking == "high"
    assert cfg.critic.model == "claude-sonnet-4-6"
    assert cfg.critic.provider == "claude"
    assert cfg.critic.thinking == "off"


def test_yaml_overrides_model_defaults(fake_repo: Path) -> None:
    (fake_repo / "forge-loop.yaml").write_text(
        dedent("""
        worker:
          model: claude-sonnet-4-6
          thinking: low
        po:
          model: claude-opus-4-7
          thinking: medium
        critic:
          model: claude-haiku-4-5
          thinking: off
    """)
    )
    cfg = config_mod.load()
    assert cfg.worker.model == "claude-sonnet-4-6"
    assert cfg.worker.thinking == "low"
    assert cfg.po.thinking == "medium"
    assert cfg.critic.model == "claude-haiku-4-5"


def test_env_overrides_yaml_for_role_model(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fake_repo / "forge-loop.yaml").write_text(
        dedent("""
        worker:
          model: claude-opus-4-7
        critic:
          thinking: low
    """)
    )
    monkeypatch.setenv("LOOP_WORKER_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("LOOP_WORKER_THINKING", "high")
    monkeypatch.setenv("LOOP_CRITIC_THINKING", "off")
    cfg = config_mod.load()
    assert cfg.worker.model == "claude-sonnet-4-6"
    assert cfg.worker.thinking == "high"
    assert cfg.critic.thinking == "off"


def test_missing_env_and_yaml_falls_back_to_documented_defaults(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Make explicit: no yaml, no env -> the documented defaults.
    for k in (
        "LOOP_WORKER_MODEL",
        "LOOP_WORKER_THINKING",
        "LOOP_PO_MODEL",
        "LOOP_PO_THINKING",
        "LOOP_CRITIC_MODEL",
        "LOOP_CRITIC_THINKING",
    ):
        monkeypatch.delenv(k, raising=False)
    cfg = config_mod.load()
    assert (cfg.worker.model, cfg.worker.thinking) == ("claude-opus-4-7", "medium")
    assert (cfg.po.model, cfg.po.thinking) == ("claude-opus-4-7", "high")
    assert (cfg.critic.model, cfg.critic.thinking) == ("claude-sonnet-4-6", "off")


def test_unknown_model_alias_raises_clear_error(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adversarial: an invalid alias (issue #34) must error at startup,
    naming the value so the operator can fix it without grepping."""
    monkeypatch.setenv("LOOP_WORKER_MODEL", "opus-99")
    with pytest.raises(config_mod.ModelConfigError) as excinfo:
        config_mod.load()
    msg = str(excinfo.value)
    assert "opus-99" in msg
    assert "LOOP_WORKER_MODEL" in msg


def test_unknown_thinking_value_raises_clear_error(
    fake_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOOP_PO_THINKING", "ultra")
    with pytest.raises(config_mod.ModelConfigError) as excinfo:
        config_mod.load()
    assert "ultra" in str(excinfo.value)


def test_global_codex_provider_uses_codex_cli_default_model(fake_repo: Path) -> None:
    (fake_repo / "forge-loop.yaml").write_text(
        dedent("""
        agent:
          provider: codex
    """)
    )
    cfg = config_mod.load()
    assert cfg.worker.provider == "codex"
    assert cfg.worker.model == ""
    assert cfg.po.provider == "codex"
    assert cfg.critic.provider == "codex"


def test_role_provider_overrides_global_agent_provider(fake_repo: Path) -> None:
    (fake_repo / "forge-loop.yaml").write_text(
        dedent("""
        agent:
          provider: codex
        worker:
          provider: claude
          model: claude-sonnet-4-6
    """)
    )
    cfg = config_mod.load()
    assert cfg.worker.provider == "claude"
    assert cfg.worker.model == "claude-sonnet-4-6"
    assert cfg.po.provider == "codex"


def test_codex_model_alias_is_allowed_from_env(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOOP_WORKER_PROVIDER", "codex")
    monkeypatch.setenv("LOOP_WORKER_MODEL", "gpt-5-codex")
    cfg = config_mod.load()
    assert cfg.worker.provider == "codex"
    assert cfg.worker.model == "gpt-5-codex"


def test_unknown_agent_provider_raises_clear_error(
    fake_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOOP_AGENT_PROVIDER", "wizard")
    with pytest.raises(config_mod.ModelConfigError) as excinfo:
        config_mod.load()
    assert "wizard" in str(excinfo.value)
