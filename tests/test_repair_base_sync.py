"""A repair round must run on the CURRENT base, not the base its branch was cut from.

☠ THE BUG. prep_repair_worktree fetched the PR branch and never brought the base into it, so every
repair round worked against whatever main looked like when the branch was created. Measured: a PR
reached its FIFTH round still sitting on a base hours old, while other PRs had merged underneath it.
The worker reasons about — and the critic reviews against — a repo that no longer exists. Git only
ever warns about TEXTUAL conflicts; two workers independently "fixing" the same thing in incompatible
ways is silent.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from forge_loop.worker_worktree import _sync_base_into_worktree


def _git(*args: str, cwd: Path) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return r.stdout.strip()


def _seed_origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "-b", "main", cwd=origin)
    _git("config", "user.email", "t@t", cwd=origin)
    _git("config", "user.name", "t", cwd=origin)
    (origin / "base.txt").write_text("v1\n", encoding="utf-8")
    _git("add", "-A", cwd=origin)
    _git("commit", "-qm", "base v1", cwd=origin)

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    _git("config", "user.email", "t@t", cwd=clone)
    _git("config", "user.name", "t", cwd=clone)
    return origin, clone


def test_stale_repair_branch_is_brought_forward(tmp_path: Path) -> None:
    origin, clone = _seed_origin_and_clone(tmp_path)

    # A PR branch cut from base v1.
    _git("checkout", "-qb", "loop/1", cwd=clone)
    (clone / "feature.txt").write_text("work\n", encoding="utf-8")
    _git("add", "-A", cwd=clone)
    _git("commit", "-qm", "feature", cwd=clone)

    # main moves on in a file the branch does NOT touch.
    _git("checkout", "-q", "main", cwd=origin)
    (origin / "other.txt").write_text("landed later\n", encoding="utf-8")
    _git("add", "-A", cwd=origin)
    _git("commit", "-qm", "another PR merged", cwd=origin)

    _git("checkout", "-q", "loop/1", cwd=clone)
    assert not (clone / "other.txt").exists(), "precondition: the branch is stale"

    events: list[tuple[str, dict]] = []
    _sync_base_into_worktree(
        clone, clone, "main", "loop/1", 1, lambda k, p: events.append((k, p))
    )

    assert (clone / "other.txt").exists(), "the later commit must be present after the sync"
    assert (clone / "feature.txt").exists(), "the branch's own work must survive"
    assert any(k == "repair_base_synced" for k, _ in events)


def test_conflict_aborts_and_reports_instead_of_leaving_a_half_merged_tree(
    tmp_path: Path,
) -> None:
    """A stale tree is a bad start; a CONFLICTED tree is a worse one."""
    origin, clone = _seed_origin_and_clone(tmp_path)

    _git("checkout", "-qb", "loop/2", cwd=clone)
    (clone / "base.txt").write_text("branch edit\n", encoding="utf-8")
    _git("add", "-A", cwd=clone)
    _git("commit", "-qm", "branch edits base.txt", cwd=clone)

    # main edits the SAME line — a real conflict.
    (origin / "base.txt").write_text("main edit\n", encoding="utf-8")
    _git("add", "-A", cwd=origin)
    _git("commit", "-qm", "main edits base.txt", cwd=origin)

    events: list[tuple[str, dict]] = []
    _sync_base_into_worktree(
        clone, clone, "main", "loop/2", 2, lambda k, p: events.append((k, p))
    )

    assert any(k == "repair_base_sync_conflict" for k, _ in events), "the conflict must be reported"
    # The tree must be clean — no conflict markers, no MERGE_HEAD left behind.
    assert not (clone / ".git" / "MERGE_HEAD").exists(), "the merge must have been aborted"
    assert "branch edit" in (clone / "base.txt").read_text(encoding="utf-8")


def test_already_current_is_silent(tmp_path: Path) -> None:
    """NEV-CTL-04: the emitter can fire (proved above), so silence here is meaningful."""
    origin, clone = _seed_origin_and_clone(tmp_path)
    events: list[tuple[str, dict]] = []
    _sync_base_into_worktree(
        clone, clone, "main", "main", 3, lambda k, p: events.append((k, p))
    )
    assert events == [], "a branch already on the base must not emit noise"
