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
    (fake_repo / "forge-loop.yaml").write_text(dedent("""
        scheduling:
          parallel: 7
          maintenance_every_n_ticks: 3
        deploy:
          task: my-custom-task
        labels:
          ready: "ready-now"
    """))
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
    (fake_repo / "forge-loop.yaml").write_text(dedent("""
        briefs:
          maintenance: |
            Custom maintenance brief content
    """))
    cfg = config_mod.load()
    assert cfg.briefs.maintenance is not None
    assert "Custom maintenance brief" in cfg.briefs.maintenance


def test_explicit_config_path_env_var(fake_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    elsewhere = fake_repo / "custom-config.yaml"
    elsewhere.write_text("scheduling:\n  parallel: 42\n")
    monkeypatch.setenv("LOOP_CONFIG_PATH", str(elsewhere))
    cfg = config_mod.load()
    assert cfg.parallel == 42
