"""Protocol-typed adapters for external I/O (issue #86).

Three subsystems get the same treatment: ``git``, ``fs``, ``clock``.
Each is a :class:`typing.Protocol` describing the surface, with one
real (subprocess/os/stdlib-backed) implementation and one in-memory
``Fake*`` for tests.

Production code consumes instances via :class:`forge_loop.container.Container`,
not by importing concrete classes directly. Tests inject Fakes — no
more ``monkeypatch.setattr("subprocess.run", ...)`` per call site.

The migration from raw ``subprocess.run(["git", ...])`` is per-domain
(per follow-up PR after the framework lands). Mechanical shape:

    # before
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)],
                   cwd=repo, capture_output=True)

    # after
    container.git.worktree_remove(repo, wt, force=True)
"""

from forge_loop.adapters.clock import Clock, FakeClock, SystemClock
from forge_loop.adapters.fs import FakeFileSystem, FileSystem, OsFileSystem
from forge_loop.adapters.git import (
    FakeGitClient,
    GitClient,
    GitError,
    SubprocessGit,
)

__all__ = [
    "Clock",
    "FakeClock",
    "FakeFileSystem",
    "FakeGitClient",
    "FileSystem",
    "GitClient",
    "GitError",
    "OsFileSystem",
    "SubprocessGit",
    "SystemClock",
]
