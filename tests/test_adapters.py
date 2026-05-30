"""Tests for the adapter framework (issue #86).

Covers Clock + FileSystem + GitClient Protocols and their default
real-impl + fake-impl pairs. The Container is asserted as a
default-wired-with-real-impls bag.

GitClient real-impl is exercised against the runtime git binary by
spawning a tiny repo in tmp_path; flaky if git isn't installed but
this is also true of every other test in the suite. The Fake covers
the offline path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from forge_loop.adapters import (
    FakeClock,
    FakeFileSystem,
    FakeGitClient,
    GitError,
    OsFileSystem,
    SubprocessGit,
    SystemClock,
)
from forge_loop.adapters.git import GitResult, _run
from forge_loop.container import Container, get_container


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------


def test_system_clock_advances_monotonically() -> None:
    c = SystemClock()
    a = c.monotonic()
    c.sleep(0.001)
    b = c.monotonic()
    assert b > a


def test_fake_clock_advances_on_sleep_without_sleeping() -> None:
    c = FakeClock(start=100.0)
    c.sleep(30)
    c.sleep(10)
    assert c.now() == 140.0
    assert c.monotonic() == 140.0
    assert list(c.sleeps) == [30, 10]


def test_fake_clock_advance_does_not_record_sleep() -> None:
    c = FakeClock(start=0.0)
    c.advance(5)
    assert c.now() == 5.0
    assert list(c.sleeps) == []


def test_fake_clock_rejects_negative_sleep() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        FakeClock().sleep(-1)


# ---------------------------------------------------------------------------
# FileSystem
# ---------------------------------------------------------------------------


def test_os_filesystem_write_then_read(tmp_path: Path) -> None:
    fs = OsFileSystem()
    p = tmp_path / "x.txt"
    fs.write_text(p, "hello")
    assert fs.exists(p)
    assert fs.read_text(p) == "hello"


def test_fake_filesystem_read_missing_raises() -> None:
    fs = FakeFileSystem()
    with pytest.raises(FileNotFoundError):
        fs.read_text(Path("/never-written"))


def test_fake_filesystem_write_then_read() -> None:
    fs = FakeFileSystem()
    p = Path("/tmp/x")
    fs.write_text(p, "hello")
    assert fs.exists(p)
    assert fs.read_text(p) == "hello"


def test_fake_filesystem_glob_prefix_star() -> None:
    fs = FakeFileSystem()
    fs.write_text(Path("/tmp/wt-loop-1"), "a")
    fs.write_text(Path("/tmp/wt-loop-2"), "b")
    fs.write_text(Path("/tmp/other"), "c")
    assert sorted(fs.glob("/tmp/wt-loop-*")) == ["/tmp/wt-loop-1", "/tmp/wt-loop-2"]


def test_fake_filesystem_mkdir_parents() -> None:
    fs = FakeFileSystem()
    fs.mkdir(Path("/tmp/a/b/c"), parents=True, exist_ok=False)
    assert fs.exists(Path("/tmp/a"))
    assert fs.exists(Path("/tmp/a/b"))
    assert fs.exists(Path("/tmp/a/b/c"))


# ---------------------------------------------------------------------------
# GitClient — Fake
# ---------------------------------------------------------------------------


def test_fake_git_records_calls_in_order() -> None:
    g = FakeGitClient()
    g.status(Path("/tmp/repo"))
    g.worktree_remove(Path("/tmp/repo"), Path("/tmp/wt"), force=True)
    assert [c[0] for c in g.calls] == ["status", "worktree_remove"]
    assert g.calls[1][1] == (Path("/tmp/repo"), Path("/tmp/wt"))
    assert g.calls[1][2] == {"force": True}


def test_fake_git_default_result_is_ok() -> None:
    g = FakeGitClient()
    r = g.status(Path("/tmp/repo"))
    assert r.ok
    assert r.returncode == 0


def test_fake_git_per_method_result_override() -> None:
    g = FakeGitClient(results_by_method={
        "worktree_remove": GitResult(argv=["git", "worktree", "remove"], returncode=1, stderr="boom"),
    })
    r = g.worktree_remove(Path("/tmp/repo"), Path("/tmp/wt"))
    assert not r.ok
    assert r.stderr == "boom"


def test_fake_git_next_result_one_shot() -> None:
    g = FakeGitClient()
    g.next_result = GitResult(argv=["git", "fetch"], returncode=128, stderr="network unreachable")
    r1 = g.fetch(Path("/tmp/repo"), "origin")
    r2 = g.fetch(Path("/tmp/repo"), "origin")
    assert not r1.ok
    assert r2.ok, "next_result must apply to exactly one call"


# ---------------------------------------------------------------------------
# GitClient — Subprocess (against a real tiny repo)
# ---------------------------------------------------------------------------


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "f").write_text("x")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def test_subprocess_git_status_returns_clean_when_no_changes(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    g = SubprocessGit()
    r = g.status(repo)
    assert r.ok
    assert r.stdout.strip() == ""


def test_subprocess_git_status_returns_porcelain_after_change(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "new").write_text("hello")
    g = SubprocessGit()
    r = g.status(repo)
    assert r.ok
    assert "?? new" in r.stdout


def test_subprocess_git_rev_parse_head_returns_a_sha(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    g = SubprocessGit()
    r = g.rev_parse(repo, "HEAD")
    assert r.ok
    assert len(r.stdout.strip()) == 40  # full SHA


def test_run_raises_git_error_when_check_true(tmp_path: Path) -> None:
    """Adversarial: a corrupted git invocation against a non-repo path
    raises typed GitError, not generic CalledProcessError."""
    non_repo = tmp_path / "not-a-repo"
    non_repo.mkdir()
    with pytest.raises(GitError) as excinfo:
        _run(["git", "rev-parse", "HEAD"], non_repo, check=True, timeout=10)
    assert "rev-parse" in str(excinfo.value) or "rev-parse" in str(excinfo.value.argv)


# ---------------------------------------------------------------------------
# Container
# ---------------------------------------------------------------------------


def test_default_container_wires_real_impls() -> None:
    ct = Container()
    assert isinstance(ct.git, SubprocessGit)
    assert isinstance(ct.fs, OsFileSystem)
    assert isinstance(ct.clock, SystemClock)


def test_container_with_fakes_passes_through() -> None:
    ct = Container(git=FakeGitClient(), fs=FakeFileSystem(), clock=FakeClock())
    assert isinstance(ct.git, FakeGitClient)
    assert isinstance(ct.fs, FakeFileSystem)
    assert isinstance(ct.clock, FakeClock)


def test_get_container_returns_singleton() -> None:
    a = get_container()
    b = get_container()
    assert a is b, "get_container must return the process-wide singleton"
