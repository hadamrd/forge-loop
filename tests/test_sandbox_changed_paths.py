"""Unit tests for worker changed-path collection from git output (issue #442)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from forge_loop.sandbox.changed_paths import worker_changed_paths


def test_worker_changed_paths_is_exported_from_sandbox_package() -> None:
    from forge_loop.sandbox import worker_changed_paths as exported

    assert exported is worker_changed_paths


class FakeGit:
    def __init__(
        self,
        *,
        diff: str = "",
        others: str = "",
        raise_on: tuple[str, ...] = (),
    ) -> None:
        self.calls: list[tuple[tuple[str, ...], str]] = []
        self._outputs = {"diff": diff, "ls-files": others}
        self._raise_on = set(raise_on)

    def __call__(self, argv: tuple[str, ...], cwd: str) -> str:
        self.calls.append((argv, cwd))
        command = argv[1]
        if command in self._raise_on:
            raise RuntimeError(f"{command} failed")
        return self._outputs[command]


def _abs(worktree: Path, *names: str) -> tuple[str, ...]:
    return tuple(os.path.normpath(os.path.abspath(str(worktree / name))) for name in names)


@pytest.mark.parametrize(
    ("diff", "others", "expected"),
    [
        ("src/a.py\nsrc/b.py\n", "new.txt\n", ("src/a.py", "src/b.py", "new.txt")),
        (
            "src/a.py\nshared.txt\n",
            "shared.txt\nnew.txt\n",
            ("src/a.py", "shared.txt", "new.txt"),
        ),
        ("./src/../src/a.py\nsub/./x\n", "sub/../new.txt\n", ("src/a.py", "sub/x", "new.txt")),
    ],
)
def test_worker_changed_paths_collects_deduplicates_and_normalizes(
    tmp_path: Path, diff: str, others: str, expected: tuple[str, ...]
) -> None:
    git = FakeGit(diff=diff, others=others)

    assert worker_changed_paths(git, str(tmp_path), base_ref="origin/trunk") == _abs(
        tmp_path, *expected
    )


def test_worker_changed_paths_empty_output_is_empty_tuple(tmp_path: Path) -> None:
    git = FakeGit(diff="\n  \n", others="")

    assert worker_changed_paths(git, str(tmp_path), base_ref="origin/trunk") == ()


@pytest.mark.parametrize(("raise_on", "diff"), [("diff", ""), ("ls-files", "src/a.py\n")])
def test_worker_changed_paths_returns_empty_and_logs_when_git_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, raise_on: str, diff: str
) -> None:
    git = FakeGit(diff=diff, raise_on=(raise_on,))

    caplog.set_level(logging.WARNING, logger="forge_loop.sandbox.changed_paths")

    assert worker_changed_paths(git, str(tmp_path), base_ref="origin/trunk") == ()
    assert "worktree=" in caplog.text
    assert "base_ref=origin/trunk" in caplog.text


def test_worker_changed_paths_invokes_expected_git_commands(tmp_path: Path) -> None:
    git = FakeGit(diff="", others="")

    worker_changed_paths(git, str(tmp_path), base_ref="origin/base")

    assert git.calls == [
        (("git", "diff", "--name-only", "origin/base"), str(tmp_path)),
        (("git", "ls-files", "--others", "--exclude-standard"), str(tmp_path)),
    ]
