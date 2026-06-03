"""End-to-end proof that worker worktree commits run pre-commit hooks."""

from __future__ import annotations

import subprocess
from pathlib import Path

from forge_loop.worker_worktree import prep_worktree


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)


def test_worker_worktree_commit_fails_when_precommit_hook_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _run(["git", "init", "-b", "trunk"], repo).returncode == 0
    assert _run(["git", "config", "user.email", "tester@example.com"], repo).returncode == 0
    assert _run(["git", "config", "user.name", "Tester"], repo).returncode == 0
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n")
    (repo / "README.md").write_text("seed\n")
    assert _run(["git", "add", "."], repo).returncode == 0
    assert _run(["git", "commit", "-m", "seed"], repo).returncode == 0
    assert _run(["git", "remote", "add", "origin", str(repo)], repo).returncode == 0
    assert _run(["git", "fetch", "origin", "trunk"], repo).returncode == 0

    main_hook = repo / ".git" / "hooks" / "pre-commit"
    main_hook.write_text("#!/bin/sh\necho worker hook fired\nexit 1\n")
    main_hook.chmod(0o755)

    class MissingPreCommitRunner:
        def is_available(self) -> bool:
            return False

        def install(self, repo: Path) -> int:
            raise AssertionError("install should not run when binary is unavailable")

    worktree, err = prep_worktree(
        repo,
        8162,
        "loop/8162-precommit",
        precommit_runner=MissingPreCommitRunner(),
    )

    try:
        assert err is None
        assert _run(["git", "config", "user.email", "tester@example.com"], worktree).returncode == 0
        assert _run(["git", "config", "user.name", "Tester"], worktree).returncode == 0
        (worktree / "change.txt").write_text("dirty\n")
        assert _run(["git", "add", "change.txt"], worktree).returncode == 0

        commit = _run(["git", "commit", "-m", "prove hook"], worktree)

        assert commit.returncode != 0
        assert "worker hook fired" in (commit.stdout + commit.stderr)
    finally:
        _run(["git", "worktree", "remove", "--force", str(worktree)], repo)
