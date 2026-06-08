"""Tests for deterministic worktree GC (forge_loop.worktree_sweep).

Conservative by construction: only worktrees UNDER worktree_root that no live lease
owns are reaped; the main checkout / off-root worktrees / live worktrees are never
touched. These pin each edge.
"""

from __future__ import annotations

from forge_loop.worktree_sweep import plan_reap, sweep, sweep_roots


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


# --------------------------------------------------------------------------- #
# sweep_roots — multi-root reconciliation (issue #405: worktree_root + agent root)
# --------------------------------------------------------------------------- #

CHECKOUT = "/home/u/forge-loop"
WT_ROOT = "/tmp/forge-x"
AGENT_ROOT = "/home/u/forge-loop/.claude/worktrees"


def test_sweep_roots_second_root_orphan_reaped_live_kept() -> None:
    """An agent worktree not in the live set is reaped; a live one is kept; the main
    checkout passed in the same list is ignored (off second-root + protected)."""
    removed: list[str] = []
    rep = sweep_roots(
        lambda p: removed.append(p) or True,
        [f"{AGENT_ROOT}/wt-dead", f"{AGENT_ROOT}/wt-live", CHECKOUT],
        roots=[WT_ROOT, AGENT_ROOT],
        live_paths={f"{AGENT_ROOT}/wt-live"},
        protected={CHECKOUT},
    )
    assert rep.reaped == [f"{AGENT_ROOT}/wt-dead"]
    assert rep.kept_live == [f"{AGENT_ROOT}/wt-live"]
    assert CHECKOUT not in removed


def test_sweep_roots_both_roots_in_one_pass() -> None:
    """Mixing a worktree_root orphan, an agent-root orphan, an agent-root live entry,
    and the main checkout → exactly {wt-1, wt-2} reaped, wt-3 kept, checkout untouched."""
    removed: list[str] = []
    rep = sweep_roots(
        lambda p: removed.append(p) or True,
        [f"{WT_ROOT}/wt-1", f"{AGENT_ROOT}/wt-2", f"{AGENT_ROOT}/wt-3", CHECKOUT],
        roots=[WT_ROOT, AGENT_ROOT],
        live_paths={f"{AGENT_ROOT}/wt-3"},
        protected={CHECKOUT},
    )
    assert set(rep.reaped) == {f"{WT_ROOT}/wt-1", f"{AGENT_ROOT}/wt-2"}
    assert rep.kept_live == [f"{AGENT_ROOT}/wt-3"]
    assert CHECKOUT not in removed


def test_sweep_roots_unknown_liveness_preserved() -> None:
    """Adversarial / fail-safe: when an agent worktree is in the live set (because its
    liveness is unknown/non-prunable) it is preserved, never reaped."""
    removed: list[str] = []
    rep = sweep_roots(
        lambda p: removed.append(p) or True,
        [f"{AGENT_ROOT}/wt-unknown"],
        roots=[WT_ROOT, AGENT_ROOT],
        live_paths={f"{AGENT_ROOT}/wt-unknown"},  # treated live ⇒ fail-safe keep
        protected={CHECKOUT},
    )
    assert rep.reaped == []
    assert removed == []
    assert rep.kept_live == [f"{AGENT_ROOT}/wt-unknown"]


def test_sweep_roots_records_remove_failure_without_crash() -> None:
    """A removal that returns False is recorded in errors; the sweep does not crash."""
    rep = sweep_roots(
        lambda p: False,
        [f"{AGENT_ROOT}/wt-dead"],
        roots=[WT_ROOT, AGENT_ROOT],
        live_paths=set(),
        protected={CHECKOUT},
    )
    assert rep.reaped == []
    assert rep.errors[f"{AGENT_ROOT}/wt-dead"] == "remove returned False"


def test_sweep_roots_never_reaps_main_checkout_nested_above_agent_root() -> None:
    """The main checkout is the PARENT of the agent root; both the off-root guard
    (checkout not under agent root) and the protected guard must hold."""
    removed: list[str] = []
    sweep_roots(
        lambda p: removed.append(p) or True,
        [CHECKOUT, f"{AGENT_ROOT}/wt-dead"],
        roots=[WT_ROOT, AGENT_ROOT],
        live_paths=set(),
        protected={CHECKOUT},
    )
    assert removed == [f"{AGENT_ROOT}/wt-dead"]
