"""Git client adapter — typed shim around ``subprocess.run(["git", ...])``.

The Protocol :class:`GitClient` is the contract production code consumes;
:class:`SubprocessGit` is the real impl; :class:`FakeGitClient` records
calls in-memory for tests.

The 24 ``git`` callsites scattered across worker.py / tick.py /
_helpers.py migrate per follow-up PR. This PR ships the framework + a
first-wave migration of the simplest callsite (``reap_worktree``) to
prove the pattern; the rest are mechanical follow-ups.

A ``GitError`` is raised when a subprocess invocation returns non-zero
AND the caller declared ``check=True``. Callers that want best-effort
behaviour (worktree remove, branch delete) pass ``check=False`` and
inspect ``returncode`` / ``stderr`` on the returned :class:`GitResult`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class GitError(RuntimeError):
    """Raised when a checked git invocation fails.

    Carries the failing argv and stderr tail so callers + log readers
    can reconstruct what happened without re-running git.
    """

    def __init__(self, argv: list[str], returncode: int, stderr: str) -> None:
        super().__init__(
            f"git {argv[:3]} ... exited {returncode}: {stderr[:300].strip()}"
        )
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr


@dataclass
class GitResult:
    """Result of one git invocation. Mirrors ``CompletedProcess`` but
    with typed fields and the original argv preserved for diagnostics."""

    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class GitClient(Protocol):
    """Subset of git operations the loop uses. Methods take ``cwd`` so
    one client instance can operate against multiple repos / worktrees
    (matches existing call-site shape — no per-repo singleton)."""

    def worktree_add(
        self, cwd: Path, path: Path, *, branch: str | None = None, base: str | None = None
    ) -> GitResult: ...

    def worktree_remove(self, cwd: Path, path: Path, *, force: bool = False) -> GitResult: ...

    def worktree_list(self, cwd: Path) -> GitResult: ...

    def worktree_prune(self, cwd: Path) -> GitResult: ...

    def branch_list(self, cwd: Path) -> GitResult: ...

    def status(self, cwd: Path, *, porcelain: bool = True) -> GitResult: ...

    def add(self, cwd: Path, *paths: str) -> GitResult: ...

    def commit(
        self, cwd: Path, message: str, *, no_verify: bool = False, allow_empty: bool = False
    ) -> GitResult: ...

    def push(self, cwd: Path, remote: str, branch: str, *, set_upstream: bool = False) -> GitResult: ...

    def log(self, cwd: Path, *args: str) -> GitResult: ...

    def rev_parse(self, cwd: Path, *args: str) -> GitResult: ...

    def branch_create(self, cwd: Path, name: str, *, start_point: str | None = None) -> GitResult: ...

    def branch_delete(self, cwd: Path, name: str, *, force: bool = False) -> GitResult: ...

    def fetch(self, cwd: Path, remote: str, *refspecs: str, prune: bool = False) -> GitResult: ...


def _run(argv: list[str], cwd: Path, *, check: bool, timeout: float | None) -> GitResult:
    """Run one git invocation. Centralised so :class:`SubprocessGit` doesn't
    have to repeat the subprocess + GitError boilerplate per method."""
    proc = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )
    res = GitResult(
        argv=argv, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
    )
    if check and not res.ok:
        raise GitError(argv, proc.returncode, proc.stderr)
    return res


class SubprocessGit:
    """Real GitClient — shells out via :mod:`subprocess`.

    Each method maps to one ``git <subcommand>`` invocation. ``check``
    defaults match the existing call-site idioms: invocations whose
    failure modes are routine (worktree-remove on a missing path,
    branch-delete on an unmerged branch) default to ``check=False``
    so the caller can branch on ``result.ok``.
    """

    def __init__(self, *, default_timeout: float = 120.0) -> None:
        self._timeout = default_timeout

    def worktree_add(
        self, cwd: Path, path: Path, *, branch: str | None = None, base: str | None = None
    ) -> GitResult:
        argv = ["git", "worktree", "add", str(path)]
        if branch:
            argv += ["-B", branch]
        if base:
            argv.append(base)
        return _run(argv, cwd, check=False, timeout=self._timeout)

    def worktree_remove(self, cwd: Path, path: Path, *, force: bool = False) -> GitResult:
        argv = ["git", "worktree", "remove"]
        if force:
            argv.append("--force")
        argv.append(str(path))
        return _run(argv, cwd, check=False, timeout=self._timeout)

    def worktree_list(self, cwd: Path) -> GitResult:
        return _run(["git", "worktree", "list", "--porcelain"], cwd, check=False, timeout=self._timeout)

    def worktree_prune(self, cwd: Path) -> GitResult:
        return _run(["git", "worktree", "prune"], cwd, check=False, timeout=self._timeout)

    def branch_list(self, cwd: Path) -> GitResult:
        # One local-branch name per line; cheap, read-only (manifesto Q9).
        return _run(
            ["git", "branch", "--format=%(refname:short)"],
            cwd,
            check=False,
            timeout=self._timeout,
        )

    def status(self, cwd: Path, *, porcelain: bool = True) -> GitResult:
        argv = ["git", "status"]
        if porcelain:
            argv.append("--porcelain")
        return _run(argv, cwd, check=False, timeout=self._timeout)

    def add(self, cwd: Path, *paths: str) -> GitResult:
        return _run(["git", "add", *paths], cwd, check=False, timeout=self._timeout)

    def commit(
        self, cwd: Path, message: str, *, no_verify: bool = False, allow_empty: bool = False
    ) -> GitResult:
        argv = ["git", "commit", "-m", message]
        if no_verify:
            argv.append("--no-verify")
        if allow_empty:
            argv.append("--allow-empty-message")
        return _run(argv, cwd, check=False, timeout=self._timeout)

    def push(self, cwd: Path, remote: str, branch: str, *, set_upstream: bool = False) -> GitResult:
        argv = ["git", "push"]
        if set_upstream:
            argv.append("-u")
        argv += [remote, branch]
        return _run(argv, cwd, check=False, timeout=self._timeout)

    def log(self, cwd: Path, *args: str) -> GitResult:
        return _run(["git", "log", *args], cwd, check=False, timeout=self._timeout)

    def rev_parse(self, cwd: Path, *args: str) -> GitResult:
        return _run(["git", "rev-parse", *args], cwd, check=False, timeout=self._timeout)

    def branch_create(self, cwd: Path, name: str, *, start_point: str | None = None) -> GitResult:
        argv = ["git", "branch", name]
        if start_point:
            argv.append(start_point)
        return _run(argv, cwd, check=False, timeout=self._timeout)

    def branch_delete(self, cwd: Path, name: str, *, force: bool = False) -> GitResult:
        argv = ["git", "branch", "-D" if force else "-d", name]
        return _run(argv, cwd, check=False, timeout=self._timeout)

    def fetch(self, cwd: Path, remote: str, *refspecs: str, prune: bool = False) -> GitResult:
        argv = ["git", "fetch"]
        if prune:
            argv.append("--prune")
        argv.append(remote)
        argv += list(refspecs)
        return _run(argv, cwd, check=False, timeout=self._timeout)


@dataclass
class FakeGitClient:
    """Test fake — records every method call, returns canned results.

    Defaults: every method returns ``GitResult(returncode=0)``. Tests
    that need failure modes set ``results_by_method`` for specific
    operations or assign a one-shot via ``next_result``.

    Inspection: ``calls`` is a list of ``(method_name, args, kwargs)``
    tuples in call order. Tests assert against this directly rather
    than monkeypatching subprocess.run.
    """

    results_by_method: dict[str, GitResult] = field(default_factory=dict)
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)
    next_result: GitResult | None = None

    def _capture(self, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> GitResult:
        self.calls.append((method, args, kwargs))
        if self.next_result is not None:
            r = self.next_result
            self.next_result = None
            return r
        return self.results_by_method.get(method, GitResult(argv=["git", method], returncode=0))

    def worktree_add(
        self, cwd: Path, path: Path, *, branch: str | None = None, base: str | None = None
    ) -> GitResult:  # noqa: D102
        return self._capture("worktree_add", (cwd, path), {"branch": branch, "base": base})

    def worktree_remove(self, cwd: Path, path: Path, *, force: bool = False) -> GitResult:  # noqa: D102
        return self._capture("worktree_remove", (cwd, path), {"force": force})

    def worktree_list(self, cwd: Path) -> GitResult:  # noqa: D102
        return self._capture("worktree_list", (cwd,), {})

    def worktree_prune(self, cwd: Path) -> GitResult:  # noqa: D102
        return self._capture("worktree_prune", (cwd,), {})

    def branch_list(self, cwd: Path) -> GitResult:  # noqa: D102
        return self._capture("branch_list", (cwd,), {})

    def status(self, cwd: Path, *, porcelain: bool = True) -> GitResult:  # noqa: D102
        return self._capture("status", (cwd,), {"porcelain": porcelain})

    def add(self, cwd: Path, *paths: str) -> GitResult:  # noqa: D102
        return self._capture("add", (cwd, *paths), {})

    def commit(
        self, cwd: Path, message: str, *, no_verify: bool = False, allow_empty: bool = False
    ) -> GitResult:  # noqa: D102
        return self._capture("commit", (cwd, message), {"no_verify": no_verify, "allow_empty": allow_empty})

    def push(
        self, cwd: Path, remote: str, branch: str, *, set_upstream: bool = False
    ) -> GitResult:  # noqa: D102
        return self._capture("push", (cwd, remote, branch), {"set_upstream": set_upstream})

    def log(self, cwd: Path, *args: str) -> GitResult:  # noqa: D102
        return self._capture("log", (cwd, *args), {})

    def rev_parse(self, cwd: Path, *args: str) -> GitResult:  # noqa: D102
        return self._capture("rev_parse", (cwd, *args), {})

    def branch_create(
        self, cwd: Path, name: str, *, start_point: str | None = None
    ) -> GitResult:  # noqa: D102
        return self._capture("branch_create", (cwd, name), {"start_point": start_point})

    def branch_delete(self, cwd: Path, name: str, *, force: bool = False) -> GitResult:  # noqa: D102
        return self._capture("branch_delete", (cwd, name), {"force": force})

    def fetch(self, cwd: Path, remote: str, *refspecs: str, prune: bool = False) -> GitResult:  # noqa: D102
        return self._capture("fetch", (cwd, remote, *refspecs), {"prune": prune})


__all__ = ["FakeGitClient", "GitClient", "GitError", "GitResult", "SubprocessGit"]
