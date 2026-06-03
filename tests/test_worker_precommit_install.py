"""Tests for pre-commit hook propagation into worker worktrees."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from forge_loop.precommit import PreCommitInstallMethod, git_hook_path
from forge_loop.worker_worktree import prep_worktree


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)


def _init_fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    assert _run(["git", "init", "-b", "trunk"], repo).returncode == 0
    assert _run(["git", "config", "user.email", "tester@example.com"], repo).returncode == 0
    assert _run(["git", "config", "user.name", "Tester"], repo).returncode == 0
    (repo / "README.md").write_text("seed\n")
    assert _run(["git", "add", "README.md"], repo).returncode == 0
    assert _run(["git", "commit", "-m", "seed"], repo).returncode == 0
    assert _run(["git", "remote", "add", "origin", str(repo)], repo).returncode == 0
    assert _run(["git", "fetch", "origin", "trunk"], repo).returncode == 0
    return repo


class FakePreCommitRunner:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.installs: list[Path] = []

    def is_available(self) -> bool:
        return self.available

    def install(self, repo: Path) -> int:
        self.installs.append(repo)
        hook = git_hook_path(repo)
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho installed hook\nexit 0\n")
        hook.chmod(0o755)
        return 0


def test_prep_worktree_installs_precommit_hook_and_emits_install_event(tmp_path: Path) -> None:
    repo = _init_fixture_repo(tmp_path)
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n")
    assert _run(["git", "add", ".pre-commit-config.yaml"], repo).returncode == 0
    assert _run(["git", "commit", "-m", "add precommit config"], repo).returncode == 0
    events: list[tuple[str, dict[str, object]]] = []
    runner = FakePreCommitRunner()

    worktree, err = prep_worktree(
        repo,
        8158,
        "loop/8158-precommit",
        emit=lambda kind, payload: events.append((kind, payload)),
        precommit_runner=runner,
    )

    try:
        assert err is None
        hook = git_hook_path(worktree)
        assert hook.exists()
        assert runner.installs == [worktree]
        assert events[-1] == (
            "worker_precommit_installed",
            {"worktree_path": str(worktree), "method": "install"},
        )
    finally:
        _run(["git", "worktree", "remove", "--force", str(worktree)], repo)


def test_prep_worktree_copies_main_hook_when_precommit_binary_missing(tmp_path: Path) -> None:
    repo = _init_fixture_repo(tmp_path)
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n")
    main_hook = repo / ".git" / "hooks" / "pre-commit"
    main_hook.parent.mkdir(parents=True, exist_ok=True)
    main_hook.write_text("#!/bin/sh\necho copied hook\nexit 0\n")
    main_hook.chmod(0o755)
    assert _run(["git", "add", ".pre-commit-config.yaml"], repo).returncode == 0
    assert _run(["git", "commit", "-m", "add precommit config"], repo).returncode == 0
    events: list[tuple[str, dict[str, object]]] = []

    worktree, err = prep_worktree(
        repo,
        8159,
        "loop/8159-precommit",
        emit=lambda kind, payload: events.append((kind, payload)),
        precommit_runner=FakePreCommitRunner(available=False),
    )

    try:
        assert err is None
        hook = git_hook_path(worktree)
        assert hook.exists()
        assert hook.read_text() == main_hook.read_text()
        assert events[-1] == (
            "worker_precommit_installed",
            {
                "worktree_path": str(worktree),
                "method": "copy",
                "reason": "precommit_binary_missing",
            },
        )
    finally:
        _run(["git", "worktree", "remove", "--force", str(worktree)], repo)


def test_worker_precommit_install_event_is_typed(tmp_path: Path) -> None:
    from forge_loop.events import WorkerPreCommitInstalledEvent, emit

    events_file = tmp_path / "events.jsonl"
    emit(
        events_file,
        WorkerPreCommitInstalledEvent(
            worktree_path="/tmp/wt-loop-1",
            method=PreCommitInstallMethod.INSTALL,
        ),
    )

    rec = json.loads(events_file.read_text())
    assert rec["kind"] == "worker_precommit_installed"
    assert rec["worktree_path"] == "/tmp/wt-loop-1"
    assert rec["method"] == "install"


@pytest.mark.parametrize("n", [8160, 8161])
def test_concurrent_workers_both_get_precommit_hooks(tmp_path: Path, n: int) -> None:
    repo = _init_fixture_repo(tmp_path / str(n))
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n")
    assert _run(["git", "add", ".pre-commit-config.yaml"], repo).returncode == 0
    assert _run(["git", "commit", "-m", "add precommit config"], repo).returncode == 0

    worktree, err = prep_worktree(
        repo,
        n,
        f"loop/{n}-precommit",
        precommit_runner=FakePreCommitRunner(),
    )

    try:
        assert err is None
        assert git_hook_path(worktree).exists()
    finally:
        _run(["git", "worktree", "remove", "--force", str(worktree)], repo)
