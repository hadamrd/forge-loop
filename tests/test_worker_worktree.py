"""Tests for worker worktree preparation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from forge_loop.worker import _prep_worktree
from forge_loop.worker_worktree import worktree_base, worktree_path


def test_prep_worktree_uses_configured_base_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class _Completed:
        returncode = 0
        stderr = ""

    def fake_run(cmd: list[str], **kwargs: Any) -> _Completed:
        calls.append(cmd)
        return _Completed()

    monkeypatch.setattr("forge_loop.worker_worktree.subprocess.run", fake_run)
    monkeypatch.setattr("forge_loop.worker_worktree.drop_permissive_settings", lambda _wt: None)

    worktree, err = _prep_worktree(tmp_path, 12, "loop/12-demo", base_branch="main")

    assert err is None
    # Worktrees are namespaced per-repo (/tmp/forge-<repo>/wt-loop-<n>) so two
    # loops on different checkouts never collide or reap each other's worktrees.
    assert worktree == worktree_path(tmp_path, 12)
    assert [
        "git",
        "fetch",
        "--prune",
        "origin",
        "+refs/heads/main:refs/remotes/origin/main",
    ] in calls
    assert ["git", "worktree", "add", str(worktree), "-B", "loop/12-demo", "origin/main"] in calls


def test_prep_worktree_quarantines_undeletable_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry after uid-mismatch cleanup failure by quarantining stale worktrees."""
    import shutil as _real_shutil

    real_rmtree = _real_shutil.rmtree

    base = worktree_base(tmp_path)
    base.mkdir(parents=True, exist_ok=True)
    blocking = worktree_path(tmp_path, 9999)
    for q in base.glob("wt-loop-9999*"):
        real_rmtree(q, ignore_errors=True)
    blocking.mkdir(exist_ok=True)
    (blocking / "marker").write_text("planted")

    class _Completed:
        returncode = 0
        stderr = ""

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> _Completed:
        calls.append(cmd)
        return _Completed()

    def boom_rmtree(_path: str | Path) -> None:
        raise PermissionError("simulated: planted by another uid")

    monkeypatch.setattr("forge_loop.worker_worktree.subprocess.run", fake_run)
    monkeypatch.setattr("forge_loop.worker_worktree.shutil.rmtree", boom_rmtree)
    monkeypatch.setattr("forge_loop.worker_worktree.drop_permissive_settings", lambda _wt: None)

    try:
        worktree, err = _prep_worktree(tmp_path, 9999, "loop/9999-demo")
        assert err is None
        assert any(cmd[:3] == ["git", "worktree", "add"] for cmd in calls), (
            f"worktree add was not called: {calls!r}"
        )
        assert not blocking.exists(), "blocking dir should have been quarantined"
        quarantined = sorted(
            base.glob("wt-loop-9999.stale-*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        assert quarantined, "quarantine dir was not created"
        assert (quarantined[0] / "marker").read_text() == "planted"
    finally:
        for q in base.glob("wt-loop-9999*"):
            real_rmtree(q, ignore_errors=True)


def test_worktree_paths_are_namespaced_per_repo() -> None:
    """Two repos with the SAME issue number must not share a worktree path.

    Regression for the cross-project clash: under the old flat
    ``/tmp/wt-loop-<issue>`` scheme, repo A's #155 and repo B's #155
    collided, and the boot reaper's ``/tmp/wt-loop-*`` glob would reap the
    other loop's live worktrees.
    """
    repo_a = Path("/home/x/project-alpha")
    repo_b = Path("/home/x/project-beta")

    assert worktree_path(repo_a, 155) != worktree_path(repo_b, 155)
    # Each repo's reaper glob is confined to its own base, so it can never
    # match the other repo's worktrees.
    assert worktree_path(repo_b, 155).parent != worktree_base(repo_a)
    assert not str(worktree_path(repo_a, 155)).startswith(str(worktree_base(repo_b)))
