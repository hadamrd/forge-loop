"""Tests for shared-checkout HEAD restore (issue #401).

A dispatch tick must never leave the shared/main checkout at ``cfg.repo`` sitting
on a worker/feature branch (a "HEAD-hop") — that drifts the next ``git fetch
origin <base>`` sync and makes ``forge-loop doctor`` report spurious drift. The
restore helper :func:`restore_base_branch` returns HEAD to ``base_branch`` at the
end of every tick, idempotently, swallowing-and-emitting on failure (mirroring
``run_branch_sweep``). Worker *worktrees* keep their own ``loop/<n>`` branches.

Per the testing manifesto:

* T1 (state machine ⇒ one test per edge + adversarial default): the helper's
  edges — moved / noop / unreadable-HEAD / checkout-rejected — each get a test.
* T3 (``returncode`` ⇒ both branches): the ``git checkout`` success (``moved``)
  and failure (``base_branch_restore_failed``) branches are both exercised.
* Integration: the restore is driven through the real ``_tick`` orchestrator and
  the worker-worktree scope guardrail is asserted against a real ``prep_worktree``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from forge_loop.config import (
    AttemptsConfig,
    Briefs,
    Config,
    CriticConfig,
    Labels,
    LumenConfig,
    POConfig,
)
from forge_loop.runner import tick as _tick_mod
from forge_loop.runner.rescue import _current_branch
from forge_loop.runner.tick_checks import restore_base_branch

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


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


def _events_path(tmp_path: Path) -> Path:
    return tmp_path / "events.jsonl"


def _read_events(events_path: Path) -> list[dict[str, Any]]:
    if not events_path.exists():
        return []
    return [json.loads(ln) for ln in events_path.read_text().splitlines() if ln.strip()]


def _make_cfg(repo: Path) -> Config:
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
    )


# --------------------------------------------------------------------------- #
# Unit: restore_base_branch edges
# --------------------------------------------------------------------------- #


def test_restore_moves_head_back_to_base(tmp_path: Path) -> None:
    """HEAD on a feature branch → restored to base, returns "moved", one event."""
    repo = _init_repo(tmp_path / "repo")
    _git(["checkout", "-b", "loop/412-foo"], repo)
    assert _current_branch(repo) == "loop/412-foo"
    events = _events_path(tmp_path)

    result = restore_base_branch(repo, "trunk", events_file=events, tick=7)

    assert result == "moved"
    assert _current_branch(repo) == "trunk"
    # Event convergence (#422): the end-of-batch restore emits the SAME typed
    # ``checkout_restored`` event as the maintenance-cadence reconcile.
    moved = [e for e in _read_events(events) if e["kind"] == "checkout_restored"]
    assert len(moved) == 1
    assert moved[0]["from_branch"] == "loop/412-foo"
    assert moved[0]["to_branch"] == "trunk"
    assert moved[0]["tick"] == 7
    # And the legacy untyped name is gone — exactly one name, no third path.
    assert not [e for e in _read_events(events) if e["kind"] == "base_branch_restored"]


def test_restore_is_noop_when_already_on_base(tmp_path: Path) -> None:
    """HEAD already on base → no-op, returns "noop", emits no move event."""
    repo = _init_repo(tmp_path / "repo")
    assert _current_branch(repo) == "trunk"
    events = _events_path(tmp_path)

    result = restore_base_branch(repo, "trunk", events_file=events, tick=1)

    assert result == "noop"
    assert _current_branch(repo) == "trunk"
    assert _read_events(events) == []  # no event when HEAD already correct


def test_restore_reuses_current_branch_probe(tmp_path: Path, monkeypatch: Any) -> None:
    """The helper resolves HEAD via rescue._current_branch (no reinvented probe)."""
    import forge_loop.runner.rescue as _rescue

    repo = _init_repo(tmp_path / "repo")
    _git(["checkout", "-b", "loop/9-x"], repo)
    calls: list[Path] = []
    real = _current_branch

    def _spy(worktree: Path) -> str:
        calls.append(worktree)
        return real(worktree)

    # restore_base_branch imports _current_branch lazily from rescue; patch there.
    monkeypatch.setattr(_rescue, "_current_branch", _spy)

    restore_base_branch(repo, "trunk", events_file=_events_path(tmp_path), tick=1)

    assert calls and calls[0] == repo


# --------------------------------------------------------------------------- #
# Unit / adversarial: checkout-rejected + missing base + unreadable HEAD
# --------------------------------------------------------------------------- #


def test_restore_swallows_dirty_tree_failure(tmp_path: Path) -> None:
    """A dirty change that blocks ``git checkout`` → "failed" + event, no raise."""
    repo = _init_repo(tmp_path / "repo")
    # Diverge a.txt on a feature branch so an uncommitted edit conflicts with base.
    _git(["checkout", "-b", "feature"], repo)
    (repo / "a.txt").write_text("feature\n")
    _git(["commit", "-am", "feature edit"], repo)
    # Uncommitted local edit that `git checkout trunk` would have to overwrite.
    (repo / "a.txt").write_text("dirty-uncommitted\n")
    events = _events_path(tmp_path)

    result = restore_base_branch(repo, "trunk", events_file=events, tick=3)

    assert result == "failed"
    assert _current_branch(repo) == "feature"  # untouched, no crash
    failed = [e for e in _read_events(events) if e["kind"] == "base_branch_restore_failed"]
    assert len(failed) == 1
    assert failed[0]["from_branch"] == "feature"
    assert failed[0]["to_branch"] == "trunk"
    assert failed[0]["err"]  # carries git's stderr


def test_restore_missing_base_branch_degrades(tmp_path: Path) -> None:
    """Base branch absent locally → best-effort "failed" + event, no raise."""
    repo = _init_repo(tmp_path / "repo")
    _git(["checkout", "-b", "loop/5-y"], repo)
    events = _events_path(tmp_path)

    result = restore_base_branch(repo, "does-not-exist", events_file=events, tick=4)

    assert result == "failed"
    failed = [e for e in _read_events(events) if e["kind"] == "base_branch_restore_failed"]
    assert len(failed) == 1


def test_restore_detached_head_does_not_crash(tmp_path: Path) -> None:
    """Detached HEAD with base present → restored, no crash."""
    repo = _init_repo(tmp_path / "repo")
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    _git(["checkout", sha], repo)  # detach
    assert _current_branch(repo) == "HEAD"
    events = _events_path(tmp_path)

    result = restore_base_branch(repo, "trunk", events_file=events, tick=5)

    assert result == "moved"
    assert _current_branch(repo) == "trunk"


def test_restore_swallows_head_probe_raise(tmp_path: Path, monkeypatch: Any) -> None:
    """``_current_branch`` raising (git hang / missing binary) → "failed", no raise.

    ``check=False`` only suppresses non-zero exit codes; ``subprocess.run`` still
    raises ``TimeoutExpired``/``OSError``. The helper runs inside ``_tick``'s
    ``finally``, so a raise here would crash the tick (and mask a body error).
    """
    import forge_loop.runner.rescue as _rescue

    repo = _init_repo(tmp_path / "repo")
    events = _events_path(tmp_path)

    def _boom(_worktree: Path) -> str:
        raise subprocess.TimeoutExpired(cmd="git", timeout=10)

    monkeypatch.setattr(_rescue, "_current_branch", _boom)

    result = restore_base_branch(repo, "trunk", events_file=events, tick=8)

    assert result == "failed"
    failed = [e for e in _read_events(events) if e["kind"] == "base_branch_restore_failed"]
    assert len(failed) == 1
    assert "TimeoutExpired" in failed[0]["reason"]


def test_restore_swallows_checkout_raise(tmp_path: Path, monkeypatch: Any) -> None:
    """``git checkout`` raising ``OSError`` (missing binary) → "failed", no raise."""
    import forge_loop.runner.tick_checks as _tc

    repo = _init_repo(tmp_path / "repo")
    _git(["checkout", "-b", "loop/13-z"], repo)
    events = _events_path(tmp_path)

    real_run = subprocess.run

    def _run(args: Any, *a: Any, **kw: Any) -> Any:
        if isinstance(args, list) and args[:2] == ["git", "checkout"]:
            raise OSError("git binary vanished")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(_tc.subprocess, "run", _run)

    result = restore_base_branch(repo, "trunk", events_file=events, tick=9)

    assert result == "failed"
    assert _current_branch(repo) == "loop/13-z"  # untouched, no crash
    failed = [e for e in _read_events(events) if e["kind"] == "base_branch_restore_failed"]
    assert len(failed) == 1
    assert failed[0]["from_branch"] == "loop/13-z"
    assert "OSError" in failed[0]["err"]


# --------------------------------------------------------------------------- #
# Integration: drive a real _tick + worker-worktree scope guardrail
# --------------------------------------------------------------------------- #


def test_tick_restores_base_branch_after_head_hop(tmp_path: Path, monkeypatch: Any) -> None:
    """A tick whose body hops the main checkout's HEAD ends back on base."""
    repo = _init_repo(tmp_path / "repo")
    cfg = _make_cfg(repo)

    def _hop_then_finish(_cfg: Config, _tick: int, *, short_sleep: Any) -> bool:
        # Simulate a dispatch code path that left the shared checkout drifted.
        _git(["checkout", "-b", "loop/412-foo"], repo)
        return True  # end the dispatch body early (no real workers in this test)

    monkeypatch.setattr(_tick_mod, "_maybe_run_maintenance", _hop_then_finish)

    _tick_mod._tick(cfg, 1)

    assert _current_branch(repo) == "trunk"
    events = _read_events(cfg.events_file)
    restored = [e for e in events if e["kind"] == "checkout_restored"]
    assert len(restored) == 1
    assert restored[0]["from_branch"] == "loop/412-foo"


def test_tick_restores_even_when_body_raises(tmp_path: Path, monkeypatch: Any) -> None:
    """The restore runs in a finally-guard: an in-body exception still restores."""
    repo = _init_repo(tmp_path / "repo")
    cfg = _make_cfg(repo)

    def _hop_then_raise(_cfg: Config, _tick: int, *, short_sleep: Any) -> bool:
        _git(["checkout", "-b", "loop/999-boom"], repo)
        raise RuntimeError("batch blew up mid-flight")

    monkeypatch.setattr(_tick_mod, "_maybe_run_maintenance", _hop_then_raise)

    # The exception propagates, but the finally-guard must still restore HEAD.
    with pytest.raises(RuntimeError, match="batch blew up"):
        _tick_mod._tick(cfg, 1)

    assert _current_branch(repo) == "trunk"
    restored = [e for e in _read_events(cfg.events_file) if e["kind"] == "checkout_restored"]
    assert len(restored) == 1


def test_worker_worktree_branch_untouched_by_restore(tmp_path: Path) -> None:
    """Scope guardrail: a real prep_worktree keeps its loop/<n> branch after restore."""
    from forge_loop.worker_worktree import prep_worktree, worktree_path

    repo = _init_repo(tmp_path / "repo")
    # prep_worktree fetches origin/<base>; give it a real origin to fetch.
    origin = _init_repo(tmp_path / "origin")
    _git(["remote", "add", "origin", str(origin)], repo)
    _git(["fetch", "origin"], repo)
    # Hop the MAIN checkout so the restore has something to do.
    _git(["checkout", "-b", "loop/77-main-drift"], repo)

    wt, err = prep_worktree(repo, 77, "loop/77", base_branch="trunk")
    assert err is None, err
    assert worktree_path(repo, 77) == wt
    assert _current_branch(wt) == "loop/77"

    restore_base_branch(repo, "trunk", events_file=_events_path(tmp_path), tick=1)

    # Main checkout restored; the worker worktree's branch is untouched.
    assert _current_branch(repo) == "trunk"
    assert _current_branch(wt) == "loop/77"


# --------------------------------------------------------------------------- #
# Event-name convergence (#422): both return arcs emit ONE documented kind
# --------------------------------------------------------------------------- #


def test_both_return_arcs_emit_same_checkout_restored_kind(tmp_path: Path) -> None:
    """End-of-batch restore and maintenance reconcile emit the SAME event ``kind``.

    The operational-convergence invariant has two return arcs — the end-of-batch
    ``restore_base_branch`` (finally-guard) and the maintenance ``run_checkout_reconcile``.
    #422 converges their observability onto the single typed ``CheckoutRestoredEvent``;
    this asserts neither path drifts to a second/third name.
    """
    from dataclasses import replace

    from forge_loop.events import CheckoutRestoredEvent
    from forge_loop.runner.tick_checks import run_checkout_reconcile

    # Arc A — end-of-batch restore against a drifted shared checkout.
    repo_a = _init_repo(tmp_path / "repo_a")
    _git(["checkout", "-b", "loop/100-a"], repo_a)
    events_a = tmp_path / "events_a.jsonl"
    assert restore_base_branch(repo_a, "trunk", events_file=events_a, tick=2) == "moved"
    kinds_a = {e["kind"] for e in _read_events(events_a)}

    # Arc B — maintenance-cadence reconcile against an equivalent drift.
    repo_b = _init_repo(tmp_path / "repo_b")
    cfg_b = replace(_make_cfg(repo_b), maintenance_every_n_ticks=5)
    run_checkout_reconcile(
        cfg_b, tick=10, read_branch=lambda: "loop/100-b", read_dirty=lambda: False, switch=lambda _b: True
    )
    kinds_b = {e["kind"] for e in _read_events(cfg_b.events_file)}

    assert CheckoutRestoredEvent.KIND == "checkout_restored"
    assert CheckoutRestoredEvent.KIND in kinds_a
    assert CheckoutRestoredEvent.KIND in kinds_b
    # The legacy untyped name must not reappear on either arc.
    assert "base_branch_restored" not in (kinds_a | kinds_b)
