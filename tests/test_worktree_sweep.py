"""Tests for deterministic worktree GC (forge_loop.worktree_sweep).

Conservative by construction: only worktrees UNDER worktree_root that no live lease
owns are reaped; the main checkout / off-root worktrees / live worktrees are never
touched. These pin each edge.
"""

from __future__ import annotations

from forge_loop.worktree_sweep import plan_reap, sweep


def test_plan_reap_only_orphans_under_root() -> None:
    paths = [
        "/home/u/forge-loop",  # main checkout, outside root → ignored
        "/tmp/wt/task-1",  # under root, not live → reap
        "/tmp/wt/task-2",  # under root, live → keep
        "/tmp/other/x",  # outside root → ignored
    ]
    reap = plan_reap(
        paths, live_paths={"/tmp/wt/task-2"}, root="/tmp/wt", protected={"/home/u/forge-loop"}
    )
    assert reap == ["/tmp/wt/task-1"]


def test_plan_reap_protected_wins_even_under_root() -> None:
    reap = plan_reap(
        ["/tmp/wt/main", "/tmp/wt/t1"], live_paths=set(), root="/tmp/wt", protected={"/tmp/wt/main"}
    )
    assert reap == ["/tmp/wt/t1"]


def test_sweep_reaps_orphans_only() -> None:
    removed: list[str] = []

    def remove(p: str) -> bool:
        removed.append(p)
        return True

    rep = sweep(
        remove,
        ["/tmp/wt/a", "/tmp/wt/b", "/tmp/wt/live", "/elsewhere/c"],
        live_paths={"/tmp/wt/live"},
        root="/tmp/wt",
        protected=set(),
    )
    assert set(rep.reaped) == {"/tmp/wt/a", "/tmp/wt/b"}
    assert rep.kept_live == ["/tmp/wt/live"]
    assert "/elsewhere/c" not in removed  # off-root never touched
    assert removed == rep.reaped


def test_sweep_records_remove_failure() -> None:
    rep = sweep(lambda p: False, ["/tmp/wt/a"], live_paths=set(), root="/tmp/wt", protected=set())
    assert rep.reaped == []
    assert rep.errors["/tmp/wt/a"] == "remove returned False"


def test_sweep_normalizes_trailing_slashes() -> None:
    rep = sweep(
        lambda p: True, ["/tmp/wt/a/"], live_paths={"/tmp/wt/a"}, root="/tmp/wt/", protected=set()
    )
    assert rep.reaped == []  # a/ == a is live → kept
    assert rep.kept_live == ["/tmp/wt/a/"]


def test_empty_live_set_still_protects_main_and_offroot() -> None:
    """An empty live-set (e.g. tasks.db unreadable) must NOT cause the main checkout
    or off-root worktrees to be reaped — root + protected guards still hold."""
    removed: list[str] = []
    sweep(
        lambda p: removed.append(p) or True,
        ["/home/u/forge-loop", "/tmp/wt/orphan", "/somewhere/else"],
        live_paths=set(),
        root="/tmp/wt",
        protected={"/home/u/forge-loop"},
    )
    assert removed == ["/tmp/wt/orphan"]  # only the under-root orphan
