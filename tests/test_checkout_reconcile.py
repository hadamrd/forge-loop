"""Tests for the shared-checkout maintenance reconcile (issue #416).

Dispatch borrows the shared checkout at ``cfg.repo`` and can leave it parked on a
``loop/<n>`` branch (the "drifting-checkout" failure mode). The maintenance-cadence
reconcile :func:`forge_loop.checkout_reconcile.reconcile` (sibling to ``branch_sweep``
/ ``worktree_sweep``) switches it back to ``base_branch`` — but ONLY when on a
``loop/<n>`` branch with a CLEAN tree. It is conservative by construction: a dirty
tree, an already-on-base checkout, or a non-loop branch is never touched.

Per the testing manifesto:

* T1 (state machine ⇒ one test per edge + adversarial default): the decision's
  edges — restored / skipped-base / skipped-non-loop / skipped-dirty / error —
  each get a test, plus the non-loop default arm on several realistic inputs.
* T2 (external yes/no ⇒ test the false case): the dirty-tree question is tested
  for BOTH clean (switch happens) and dirty (switch suppressed).
* T3 (returncode ⇒ both branches): the ``switch`` success and failure
  (returns-False AND raises) branches are both exercised.
* Adversarial (REQUIRED): a real tmp repo on ``loop/42`` with uncommitted tracked
  edits AND an untracked file proves the "never clobber uncommitted work"
  invariant — HEAD stays on ``loop/42``, the dirty changes are fully intact.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from forge_loop.checkout_reconcile import (
    CheckoutReconcileReport,
    ReconcileOutcome,
    is_eligible,
    reconcile,
)
from forge_loop.config import (
    AttemptsConfig,
    Briefs,
    Config,
    CriticConfig,
    Labels,
    LumenConfig,
    POConfig,
)
from forge_loop.runner.tick_checks import run_checkout_reconcile

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _init_repo(path: Path) -> Path:
    """A real git repo whose default (and base) branch is ``trunk``."""
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "trunk"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    (path / "a.txt").write_text("trunk\n")
    _git(["add", "-A"], path)
    _git(["commit", "-m", "init"], path)
    return path


def _read_events(events_path: Path) -> list[dict[str, Any]]:
    if not events_path.exists():
        return []
    return [json.loads(ln) for ln in events_path.read_text().splitlines() if ln.strip()]


def _make_cfg(repo: Path, *, every: int = 5) -> Config:
    return Config(
        repo=repo,
        github_repo="o/r",
        base_branch="trunk",
        parallel=1,
        tick_interval_s=0,
        max_ticks=1,
        worker_timeout_s=60,
        deploy_task="",
        labels=Labels(),
        briefs=Briefs(),
        critic=CriticConfig(enabled=False, timeout_s=10),
        po=POConfig(enabled=False, timeout_s=10, max_to_expand_per_tick=0),
        attempts=AttemptsConfig(enabled=False, max_history_in_brief=5),
        lumen=LumenConfig(),
        maintenance_every_n_ticks=every,
    )


class _Switch:
    """Records calls so a test can assert invoked-once-with-base / never-invoked."""

    def __init__(self, *, ok: bool = True, raises: BaseException | None = None) -> None:
        self.calls: list[str] = []
        self._ok = ok
        self._raises = raises

    def __call__(self, branch: str) -> bool:
        self.calls.append(branch)
        if self._raises is not None:
            raise self._raises
        return self._ok


# --------------------------------------------------------------------------- #
# Unit: pure decision (deps injected, no real git)
# --------------------------------------------------------------------------- #


def test_clean_loop_branch_plans_switch_to_base() -> None:
    sw = _Switch(ok=True)
    report = reconcile(
        read_branch=lambda: "loop/42",
        read_dirty=lambda: False,
        switch=sw,
        base_branch="trunk",
    )
    assert report.outcome is ReconcileOutcome.RESTORED
    assert report.from_branch == "loop/42"
    assert report.to_branch == "trunk"
    assert sw.calls == ["trunk"]  # invoked exactly once with the base branch


def test_dirty_loop_branch_never_switches() -> None:
    sw = _Switch(ok=True)
    report = reconcile(
        read_branch=lambda: "loop/42",
        read_dirty=lambda: True,
        switch=sw,
        base_branch="trunk",
    )
    assert report.outcome is ReconcileOutcome.SKIPPED_DIRTY
    assert report.from_branch == "loop/42"
    assert sw.calls == []  # NEVER invoked on a dirty tree


def test_already_on_base_is_noop() -> None:
    sw = _Switch(ok=True)
    report = reconcile(
        read_branch=lambda: "trunk",
        read_dirty=lambda: False,
        switch=sw,
        base_branch="trunk",
    )
    assert report.outcome is ReconcileOutcome.SKIPPED_BASE
    assert sw.calls == []


@pytest.mark.parametrize("branch", ["feature/x", "main", "develop", "fix/123", "release"])
def test_non_loop_branch_is_noop(branch: str) -> None:
    """The default/fallthrough arm: any branch that is not loop/<n> is left alone."""
    sw = _Switch(ok=True)
    report = reconcile(
        read_branch=lambda: branch,
        read_dirty=lambda: False,
        switch=sw,
        base_branch="trunk",
    )
    assert report.outcome is ReconcileOutcome.SKIPPED_NON_LOOP
    assert report.from_branch == branch
    assert sw.calls == []


def test_loop_branch_with_slug_is_eligible() -> None:
    """``loop/<n>-slug`` form recognized (regex parity with branch_sweep)."""
    assert is_eligible("loop/42-some-slug", "trunk") is True
    assert is_eligible("loop/42", "trunk") is True
    assert is_eligible("feature/x", "trunk") is False
    assert is_eligible("trunk", "trunk") is False

    sw = _Switch(ok=True)
    report = reconcile(
        read_branch=lambda: "loop/42-some-slug",
        read_dirty=lambda: False,
        switch=sw,
        base_branch="trunk",
    )
    assert report.outcome is ReconcileOutcome.RESTORED
    assert sw.calls == ["trunk"]


def test_switch_returns_false_is_error_no_raise() -> None:
    sw = _Switch(ok=False)
    report = reconcile(
        read_branch=lambda: "loop/42",
        read_dirty=lambda: False,
        switch=sw,
        base_branch="trunk",
    )
    assert report.outcome is ReconcileOutcome.ERROR
    assert report.reason == "switch returned False"
    assert sw.calls == ["trunk"]


def test_switch_raises_is_caught_as_error() -> None:
    sw = _Switch(raises=subprocess.TimeoutExpired(cmd="git", timeout=30))
    report = reconcile(
        read_branch=lambda: "loop/42",
        read_dirty=lambda: False,
        switch=sw,
        base_branch="trunk",
    )
    assert report.outcome is ReconcileOutcome.ERROR
    assert "switch" in (report.reason or "")


def test_unreadable_head_is_error() -> None:
    sw = _Switch(ok=True)
    report = reconcile(
        read_branch=lambda: "",
        read_dirty=lambda: False,
        switch=sw,
        base_branch="trunk",
    )
    assert report.outcome is ReconcileOutcome.ERROR
    assert report.reason == "head_unreadable"
    assert sw.calls == []


def test_branch_probe_raise_is_caught() -> None:
    def _boom() -> str:
        raise OSError("git gone")

    report = reconcile(
        read_branch=_boom,
        read_dirty=lambda: False,
        switch=_Switch(),
        base_branch="trunk",
    )
    assert report.outcome is ReconcileOutcome.ERROR


# --------------------------------------------------------------------------- #
# Integration: run_checkout_reconcile wrapper (Config + event file)
# --------------------------------------------------------------------------- #


def test_wrapper_offcadence_is_noop(tmp_path: Path) -> None:
    """tick % every != 0 → returns None, touches nothing, emits nothing."""
    repo = _init_repo(tmp_path / "repo")
    cfg = _make_cfg(repo, every=5)
    sw = _Switch(ok=True)

    result = run_checkout_reconcile(
        cfg,
        tick=3,  # 3 % 5 != 0
        read_branch=lambda: "loop/42",
        read_dirty=lambda: False,
        switch=sw,
    )
    assert result is None
    assert sw.calls == []
    assert _read_events(cfg.events_file) == []


def test_wrapper_oncadence_happy_emits_checkout_restored(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    cfg = _make_cfg(repo, every=5)
    sw = _Switch(ok=True)

    result = run_checkout_reconcile(
        cfg,
        tick=10,  # 10 % 5 == 0
        read_branch=lambda: "loop/42",
        read_dirty=lambda: False,
        switch=sw,
    )
    assert result is not None
    assert result.outcome is ReconcileOutcome.RESTORED
    restored = [e for e in _read_events(cfg.events_file) if e["kind"] == "checkout_restored"]
    assert len(restored) == 1
    assert restored[0]["tick"] == 10
    assert restored[0]["from_branch"] == "loop/42"
    assert restored[0]["to_branch"] == "trunk"


def test_wrapper_oncadence_dirty_emits_no_checkout_restored(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    cfg = _make_cfg(repo, every=5)
    sw = _Switch(ok=True)

    result = run_checkout_reconcile(
        cfg,
        tick=10,
        read_branch=lambda: "loop/42",
        read_dirty=lambda: True,
        switch=sw,
    )
    assert result is not None
    assert result.outcome is ReconcileOutcome.SKIPPED_DIRTY
    assert sw.calls == []
    kinds = [e["kind"] for e in _read_events(cfg.events_file)]
    assert "checkout_restored" not in kinds


# --------------------------------------------------------------------------- #
# e2e: real git in a tmp repo (no injected switch/branch/dirty)
# --------------------------------------------------------------------------- #


def test_e2e_real_git_clean_loop_branch_restored(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "repo")
    _git(["checkout", "-b", "loop/42"], repo)
    assert _abbrev_head(repo) == "loop/42"
    cfg = _make_cfg(repo, every=5)

    result = run_checkout_reconcile(cfg, tick=10)

    assert result is not None
    assert result.outcome is ReconcileOutcome.RESTORED
    assert _abbrev_head(repo) == "trunk"
    restored = [e for e in _read_events(cfg.events_file) if e["kind"] == "checkout_restored"]
    assert len(restored) == 1
    assert restored[0]["from_branch"] == "loop/42"
    assert restored[0]["to_branch"] == "trunk"


def test_e2e_real_git_dirty_tree_never_clobbered(tmp_path: Path) -> None:
    """Adversarial (REQUIRED): uncommitted tracked edit AND an untracked file on
    loop/42 → HEAD stays on loop/42, the dirty changes are fully intact, and no
    checkout_restored event is emitted. Proves "never clobber uncommitted work"."""
    repo = _init_repo(tmp_path / "repo")
    _git(["checkout", "-b", "loop/42"], repo)
    # Uncommitted edit to a tracked file...
    (repo / "a.txt").write_text("DIRTY uncommitted edit\n")
    # ...AND an untracked file.
    (repo / "scratch.txt").write_text("untracked work\n")
    cfg = _make_cfg(repo, every=5)

    result = run_checkout_reconcile(cfg, tick=10)

    assert result is not None
    assert result.outcome is ReconcileOutcome.SKIPPED_DIRTY
    # HEAD is STILL loop/42 — never switched.
    assert _abbrev_head(repo) == "loop/42"
    # The dirty changes are fully intact — no stash, no discard.
    assert (repo / "a.txt").read_text() == "DIRTY uncommitted edit\n"
    assert (repo / "scratch.txt").read_text() == "untracked work\n"
    # No restore event.
    kinds = [e["kind"] for e in _read_events(cfg.events_file)]
    assert "checkout_restored" not in kinds


def _abbrev_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_report_dataclass_defaults() -> None:
    """Report defaults: only outcome is required; the rest are optional."""
    r = CheckoutReconcileReport(outcome=ReconcileOutcome.SKIPPED_BASE)
    assert r.from_branch is None and r.to_branch is None and r.reason is None
