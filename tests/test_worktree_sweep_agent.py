"""Integration tests for the agent-worktree GC second root (issue #405).

``run_worktree_sweep`` now reconciles TWO roots: ``cfg.worktree_root`` (task
worktrees, liveness = in-flight lease) and ``<repo>/.claude/worktrees`` (agent
worktrees, liveness = git porcelain ``locked``/``prunable`` markers). These tests
exercise the porcelain marker parser and the orchestrator wiring (both roots fed,
combined ``worktree_sweep_done`` event), plus the maintenance-cadence guard.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

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


def test_agent_live_paths_reaps_intact_but_stale_dir(tmp_path: Path, monkeypatch: Any) -> None:
    """Positive reap path (#405 sev1): git NEVER marks an intact crashed worktree dir
    ``prunable``, so liveness falls back to a conservative age floor — a stale intact
    dir is dropped from the live set (reapable); a fresh one is kept."""
    agent_root = tmp_path / ".claude" / "worktrees"
    stale, fresh = agent_root / "wt-stale", agent_root / "wt-fresh"
    stale.mkdir(parents=True)
    fresh.mkdir(parents=True)
    now = time.time()
    os.utime(stale, (now - 48 * 3600, now - 48 * 3600))  # idle 48h → past the floor
    os.utime(fresh, (now, now))
    porcelain = (
        f"worktree {tmp_path}\nHEAD a\n\n"
        f"worktree {stale}\nbranch refs/heads/x\n\n"
        f"worktree {fresh}\nbranch refs/heads/y\n"
    )
    monkeypatch.setattr(tc, "_worktree_porcelain", lambda _repo: porcelain)
    live = tc._agent_live_paths(tmp_path, min_age_s=24 * 3600, now=now)
    assert str(fresh) in live  # too young → kept (fail-safe)
    assert str(stale) not in live  # intact but stale → reapable


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


# --------------------------------------------------------------------------- #
# E2E — real git: an intact-but-stale agent worktree is reaped, checkout intact
# --------------------------------------------------------------------------- #


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_e2e_reaps_intact_stale_agent_worktree(tmp_path: Path) -> None:
    """Real git (#405 sev1): an intact ``.claude/worktrees/*`` left by a crashed run is
    NOT prunable, yet the age floor reaps it while the main checkout + ``.git`` survive."""
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("x\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "init")

    wt = repo / ".claude" / "worktrees" / "wt-orphan"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt))
    assert wt.is_dir()
    old = time.time() - 48 * 3600  # past the 24h floor
    os.utime(wt, (old, old))

    cfg = Config(
        repo=repo,
        github_repo="o/r",
        labels=Labels(),
        briefs=Briefs(),
        worktree_root=tmp_path / "forge-x",  # disjoint from the checkout
        maintenance_every_n_ticks=5,
    )
    report = tc.run_worktree_sweep(cfg, tick=5)  # default live derivation (no leases)

    assert report is not None
    assert str(wt) in report.reaped
    assert not wt.exists()  # orphan gone
    assert (repo / ".git").exists()  # checkout intact
    assert (repo / "f.txt").read_text() == "x\n"


def test_run_worktree_sweep_clamps_system_temp_root(tmp_path: Path) -> None:
    """#451 — `worktree_root: /tmp` (a system temp dir) must NOT make every
    /tmp worktree reapable. A live run reaped two OPERATOR worktrees
    seconds after creation because the config declared all of /tmp as the
    loop's namespace. Over-broad roots get clamped to the loop's own
    per-repo base (worktree_base) and a clamp event is emitted."""
    cfg = Config(
        repo=tmp_path,
        github_repo="o/r",
        labels=Labels(),
        briefs=Briefs(),
        worktree_root=Path("/tmp"),  # the footgun config
        maintenance_every_n_ticks=5,
    )
    removed: list[str] = []
    report = tc.run_worktree_sweep(
        cfg,
        tick=5,
        worktrees=[
            "/tmp/wt-operator",            # operator worktree → MUST survive
            "/tmp/clone-x",                # operator clone-ish dir → MUST survive
        ],
        live_paths=set(),
        remove=lambda p: removed.append(p) or True,
    )
    assert removed == []
    assert report is None or report.reaped == []
    evts = [e for e in _read_events(cfg) if e["kind"] == "worktree_root_clamped"]
    assert len(evts) == 1
    assert "/tmp" in evts[0]["configured"]
