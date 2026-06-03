"""Test fakes for the pre-commit adapter."""

from __future__ import annotations

from pathlib import Path

from forge_loop.precommit import git_hook_path


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
            hook.write_text("#!/bin/sh\necho fake pre-commit\nexit 0\n")
            hook.chmod(0o755)
        return self.returncode
