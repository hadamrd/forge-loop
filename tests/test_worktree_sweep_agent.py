"""Integration tests for the agent-worktree GC second root (issue #405).

``run_worktree_sweep`` now reconciles TWO roots: ``cfg.worktree_root`` (task
worktrees, liveness = in-flight lease) and ``<repo>/.claude/worktrees`` (agent
worktrees, liveness = git porcelain ``locked``/``prunable`` markers). These tests
exercise the porcelain marker parser and the orchestrator wiring (both roots fed,
combined ``worktree_sweep_done`` event), plus the maintenance-cadence guard.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from forge_loop.config import Briefs, Config, Labels
from forge_loop.runner import tick_checks as tc


def _make_cfg(tmp_path: Path) -> Config:
    return Config(
        repo=tmp_path,
        github_repo="o/r",
        labels=Labels(),
        briefs=Briefs(),
        worktree_root=Path("/tmp/forge-x"),
        maintenance_every_n_ticks=5,
    )


def _read_events(cfg: Config) -> list[dict[str, Any]]:
    if not cfg.events_file.exists():
        return []
    return [json.loads(ln) for ln in cfg.events_file.read_text().splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #
# porcelain marker parser
# --------------------------------------------------------------------------- #


def test_parse_worktree_records_extracts_locked_and_prunable() -> None:
    porcelain = (
        "worktree /home/u/forge-loop\nHEAD abc\nbranch refs/heads/trunk\n\n"
        "worktree /home/u/forge-loop/.claude/worktrees/wt-dead\nHEAD def\n"
        "prunable gitdir file points to non-existent location\n\n"
        "worktree /home/u/forge-loop/.claude/worktrees/wt-locked\nHEAD ghi\n"
        "locked agent in use\n\n"
        "worktree /home/u/forge-loop/.claude/worktrees/wt-normal\nHEAD jkl\nbranch refs/heads/x\n"
    )
    recs = tc._parse_worktree_records(porcelain)
    assert recs == [
        ("/home/u/forge-loop", False, False),
        ("/home/u/forge-loop/.claude/worktrees/wt-dead", False, True),
        ("/home/u/forge-loop/.claude/worktrees/wt-locked", True, False),
        ("/home/u/forge-loop/.claude/worktrees/wt-normal", False, False),
    ]


def test_parse_worktree_records_empty_input() -> None:
    """Adversarial: porcelain unavailable/empty → no records, no crash."""
    assert tc._parse_worktree_records("") == []


def test_agent_live_paths_keeps_all_but_prunable_unlocked(monkeypatch: Any) -> None:
    """Only a prunable-and-not-locked agent worktree is excluded from the live set;
    locked, normal (no marker), and prunable+locked entries are all kept live."""
    repo = Path("/home/u/forge-loop")
    porcelain = (
        "worktree /home/u/forge-loop\nHEAD a\n\n"
        "worktree /home/u/forge-loop/.claude/worktrees/wt-dead\nprunable stale\n\n"
        "worktree /home/u/forge-loop/.claude/worktrees/wt-locked\nlocked busy\n\n"
        "worktree /home/u/forge-loop/.claude/worktrees/wt-normal\nbranch refs/heads/x\n\n"
        "worktree /home/u/forge-loop/.claude/worktrees/wt-locked-prunable\n"
        "locked busy\nprunable stale\n"
    )
    monkeypatch.setattr(tc, "_worktree_porcelain", lambda _repo: porcelain)
    live = tc._agent_live_paths(repo)
    assert live == {
        "/home/u/forge-loop/.claude/worktrees/wt-locked",
        "/home/u/forge-loop/.claude/worktrees/wt-normal",
        "/home/u/forge-loop/.claude/worktrees/wt-locked-prunable",
    }
    assert "/home/u/forge-loop/.claude/worktrees/wt-dead" not in live  # reapable


# --------------------------------------------------------------------------- #
# run_worktree_sweep — orchestrator feeds BOTH roots
# --------------------------------------------------------------------------- #


def test_run_worktree_sweep_reconciles_both_roots(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    agent = str(tmp_path / ".claude" / "worktrees")
    removed: list[str] = []
    report = tc.run_worktree_sweep(
        cfg,
        tick=5,  # maintenance cadence (5 % 5 == 0)
        worktrees=[
            "/tmp/forge-x/wt-1",  # task-root orphan → reap
            f"{agent}/wt-2",  # agent-root orphan → reap
            f"{agent}/wt-3",  # agent-root live → keep
            str(tmp_path),  # main checkout → never
        ],
        live_paths={f"{agent}/wt-3"},
        remove=lambda p: removed.append(p) or True,
    )
    assert report is not None
    assert set(report.reaped) == {"/tmp/forge-x/wt-1", f"{agent}/wt-2"}
    assert report.kept_live == [f"{agent}/wt-3"]
    assert str(tmp_path) not in removed

    evts = [e for e in _read_events(cfg) if e["kind"] == "worktree_sweep_done"]
    assert len(evts) == 1
    assert set(evts[0]["reaped"]) == {"/tmp/forge-x/wt-1", f"{agent}/wt-2"}
    assert evts[0]["kept_live"] == 1


def test_run_worktree_sweep_off_cadence_short_circuits_both_roots(tmp_path: Path) -> None:
    """Guard: a non-maintenance tick reaps nothing and emits no event for EITHER root."""
    cfg = _make_cfg(tmp_path)
    agent = str(tmp_path / ".claude" / "worktrees")
    removed: list[str] = []
    report = tc.run_worktree_sweep(
        cfg,
        tick=3,  # 3 % 5 != 0 → off cadence
        worktrees=["/tmp/forge-x/wt-1", f"{agent}/wt-2"],
        live_paths=set(),
        remove=lambda p: removed.append(p) or True,
    )
    assert report is None
    assert removed == []
    assert _read_events(cfg) == []
