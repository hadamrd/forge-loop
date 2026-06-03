"""Tests for `forge-loop init` pre-commit hook installation."""

from __future__ import annotations

from pathlib import Path

from forge_loop.init import init_project
from forge_loop.precommit import PreCommitHookOutcome, git_hook_path


class FakePreCommitRunner:
    def __init__(self, *, available: bool = True, returncode: int = 0) -> None:
        self.available = available
        self.returncode = returncode
        self.installs: list[Path] = []

    def is_available(self) -> bool:
        return self.available

    def install(self, repo: Path) -> int:
        self.installs.append(repo)
        if self.returncode == 0:
            hook = git_hook_path(repo)
            hook.parent.mkdir(parents=True, exist_ok=True)
            hook.write_text("#!/bin/sh\n")
        return self.returncode


def test_init_with_precommit_config_installs_hook(tmp_path: Path) -> None:
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n")
    runner = FakePreCommitRunner()

    result = init_project(tmp_path, github_repo="example/foo", precommit_runner=runner)

    assert result["precommit"] == [PreCommitHookOutcome.INSTALLED.value]
    assert runner.installs == [tmp_path]


def test_init_without_precommit_config_skips_nonfatally(tmp_path: Path) -> None:
    result = init_project(tmp_path, github_repo="example/foo")

    assert result["precommit"] == [PreCommitHookOutcome.SKIPPED_NO_CONFIG.value]


def test_init_with_missing_precommit_binary_emits_hint_and_exits_zero(tmp_path: Path) -> None:
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n")
    runner = FakePreCommitRunner(available=False)

    result = init_project(tmp_path, github_repo="example/foo", precommit_runner=runner)

    assert result["precommit"] == [PreCommitHookOutcome.SKIPPED_NO_BINARY.value]
    assert result["precommit_hint"]
    assert not runner.installs


def test_init_rerun_when_hook_exists_reports_already_present(tmp_path: Path) -> None:
    hooks = tmp_path / ".git" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "pre-commit").write_text("#!/bin/sh\n")
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n")
    runner = FakePreCommitRunner()

    result = init_project(tmp_path, github_repo="example/foo", precommit_runner=runner)

    assert result["precommit"] == [PreCommitHookOutcome.ALREADY_PRESENT.value]
    assert not runner.installs
