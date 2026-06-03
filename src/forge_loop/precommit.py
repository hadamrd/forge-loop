"""Pre-commit hook installation helpers.

The hook path is intentionally resolved through git instead of assuming
``.git/hooks`` is a real directory: linked worktrees use their own git dir
under the common checkout's ``.git/worktrees/`` area.
"""

from __future__ import annotations

import shutil
import subprocess
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class PreCommitHookOutcome(StrEnum):
    INSTALLED = "precommit_installed"
    ALREADY_PRESENT = "precommit_already_present"
    SKIPPED_NO_CONFIG = "precommit_skipped_no_config"
    SKIPPED_NO_BINARY = "precommit_skipped_no_binary"


class PreCommitInstallMethod(StrEnum):
    INSTALL = "install"
    COPY = "copy"
    SKIPPED = "skipped"


class PreCommitRunner(Protocol):
    def is_available(self) -> bool:
        """Return whether the ``pre-commit`` executable is available."""
        ...

    def install(self, repo: Path) -> int:
        """Run ``pre-commit install`` in ``repo`` and return its exit code."""
        ...


class RealPreCommitRunner:
    def is_available(self) -> bool:
        return shutil.which("pre-commit") is not None

    def install(self, repo: Path) -> int:
        result = subprocess.run(
            ["pre-commit", "install"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode


def precommit_install_hint() -> str:
    return "Install pre-commit with `pipx install pre-commit` or `uv tool install pre-commit`."


def has_precommit_config(repo: Path) -> bool:
    return (repo / ".pre-commit-config.yaml").exists()


def git_hook_path(repo: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-path", "hooks/pre-commit"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        path = Path(result.stdout.strip())
        return path if path.is_absolute() else repo / path
    return repo / ".git" / "hooks" / "pre-commit"


def ensure_precommit_hook(
    repo: Path,
    *,
    runner: PreCommitRunner | None = None,
) -> tuple[PreCommitHookOutcome, str | None]:
    """Install the hook for ``repo`` when a pre-commit config is present."""

    if not has_precommit_config(repo):
        return PreCommitHookOutcome.SKIPPED_NO_CONFIG, None

    hook = git_hook_path(repo)
    if hook.exists():
        return PreCommitHookOutcome.ALREADY_PRESENT, None

    runner = runner or RealPreCommitRunner()
    if not runner.is_available():
        return PreCommitHookOutcome.SKIPPED_NO_BINARY, precommit_install_hint()

    hook.parent.mkdir(parents=True, exist_ok=True)
    rc = runner.install(repo)
    if rc == 0 and hook.exists():
        return PreCommitHookOutcome.INSTALLED, None
    if rc == 0:
        return PreCommitHookOutcome.INSTALLED, None
    return PreCommitHookOutcome.SKIPPED_NO_BINARY, precommit_install_hint()


def ensure_worker_precommit_hook(
    main_repo: Path,
    worktree: Path,
    *,
    runner: PreCommitRunner | None = None,
) -> tuple[PreCommitInstallMethod, str | None]:
    """Propagate a pre-commit hook into a linked worker worktree."""

    if not has_precommit_config(worktree):
        return PreCommitInstallMethod.SKIPPED, "precommit_config_missing"

    runner = runner or RealPreCommitRunner()
    if runner.is_available():
        hook = git_hook_path(worktree)
        hook.parent.mkdir(parents=True, exist_ok=True)
        rc = runner.install(worktree)
        if rc == 0:
            return PreCommitInstallMethod.INSTALL, None
        return PreCommitInstallMethod.SKIPPED, "precommit_install_failed"

    source = git_hook_path(main_repo)
    if not source.exists():
        return PreCommitInstallMethod.SKIPPED, "precommit_binary_missing"

    dest = git_hook_path(worktree)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != dest.resolve():
        shutil.copy2(source, dest)
    dest.chmod(dest.stat().st_mode | 0o111)
    return PreCommitInstallMethod.COPY, "precommit_binary_missing"
