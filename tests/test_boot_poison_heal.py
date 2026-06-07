"""Self-heal tests for the #315 boot poison guard.

The heal is split into a PURE planner (:func:`plan_heal`) and a MUTATION behind
the :class:`SiteHealer` Protocol — so the planner is tested without touching the
filesystem and the executor is tested against a real tmp ``site-packages`` dir.
Integration tests drive ``boot.run`` with the heal seam controlled so no real
operator site is ever mutated.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from forge_loop._testing.pip_show import FakeSiteHealer
from forge_loop.runner.poison_guard import (
    FilesystemSiteHealer,
    HealOutcome,
    HealResult,
    PoisonResult,
    heal_poisoned_environment,
    plan_heal,
)


def _poison(path: str) -> PoisonResult:
    return PoisonResult(poisoned=True, offending_path=path)


# --------------------------------------------------------------------------
# Pure planner (AC #1/#3/#6)
# --------------------------------------------------------------------------


def test_plan_heal_for_worktree_editable_returns_plan() -> None:
    plan = plan_heal(_poison("/tmp/wt-loop-9/src"), site_packages="/site")
    assert plan is not None
    assert plan.offending_path == "/tmp/wt-loop-9/src"
    assert plan.site_packages == "/site"
    assert plan.module == "forge_loop"


def test_plan_heal_for_non_worktree_location_returns_none() -> None:
    # AC #3 — a uv-managed / canonical-checkout editable is NOT worktree-shaped.
    uv = str(Path.home() / ".local/share/uv/tools/forge-loop/lib/python3.12/site-packages")
    assert plan_heal(_poison(uv), site_packages="/site") is None
    assert plan_heal(_poison("/home/op/forge-loop/src"), site_packages="/site") is None


def test_plan_heal_returns_none_when_not_poisoned_or_no_site() -> None:
    assert plan_heal(PoisonResult(poisoned=False), site_packages="/site") is None
    assert plan_heal(_poison("/tmp/wt-loop-1/src"), site_packages=None) is None
    assert plan_heal(_poison("/tmp/wt-loop-1/src"), site_packages="") is None


def test_plan_heal_respects_custom_package_name() -> None:
    plan = plan_heal(_poison("/tmp/wt-loop-1/src"), site_packages="/site", package="my-pkg")
    assert plan is not None
    assert plan.module == "my_pkg"


# --------------------------------------------------------------------------
# Filesystem executor against a real tmp site-packages (AC #1/#3)
# --------------------------------------------------------------------------


def _seed_poisoned_site(site: Path, worktree: str = "/tmp/wt-loop-9/src") -> None:
    """Plant the editable artifacts a poisoned operator site contains."""
    site.mkdir(parents=True, exist_ok=True)
    (site / "__editable__.forge_loop-0.1.0.pth").write_text(
        "import __editable___forge_loop_0_1_0_finder\n", encoding="utf-8"
    )
    (site / "__editable___forge_loop_0_1_0_finder.py").write_text(
        f"MAPPING = {{'forge_loop': '{worktree}/forge_loop'}}\n", encoding="utf-8"
    )
    (site / "_editable_impl_forge_loop.pth").write_text(worktree + "\n", encoding="utf-8")
    (site / "roles.pth").write_text(worktree + "\n", encoding="utf-8")
    dist = site / "forge_loop-0.1.0.dist-info"
    dist.mkdir()
    (dist / "RECORD").write_text("", encoding="utf-8")


def test_filesystem_healer_removes_only_forge_editable_artifacts(tmp_path: Path) -> None:
    site = tmp_path / "site-packages"
    _seed_poisoned_site(site)
    # Bystanders that MUST survive (AC #3): an unrelated package + an unrelated
    # .pth whose target sits under the worktree root but is not forge-loop.
    (site / "requests").mkdir()
    (site / "requests" / "__init__.py").write_text("", encoding="utf-8")
    (site / "other.pth").write_text("/tmp/wt-loop-9/other\n", encoding="utf-8")

    plan = plan_heal(_poison("/tmp/wt-loop-9/src"), site_packages=str(site))
    assert plan is not None
    removed = FilesystemSiteHealer().heal(plan)

    removed_names = {Path(p).name for p in removed}
    assert removed_names == {
        "__editable__.forge_loop-0.1.0.pth",
        "__editable___forge_loop_0_1_0_finder.py",
        "_editable_impl_forge_loop.pth",
        "roles.pth",
        "forge_loop-0.1.0.dist-info",
    }
    # Bystanders untouched.
    assert (site / "requests" / "__init__.py").exists()
    assert (site / "other.pth").exists()


def test_filesystem_healer_removes_dangling_worktree_symlink(tmp_path: Path) -> None:
    # Adversarial: a forge_loop shim symlink whose worktree was already reaped.
    site = tmp_path / "site-packages"
    site.mkdir()
    link = site / "forge_loop"
    link.symlink_to("/tmp/wt-loop-9/src/forge_loop")  # target does NOT exist
    assert link.is_symlink()

    plan = plan_heal(_poison("/tmp/wt-loop-9/src"), site_packages=str(site))
    assert plan is not None
    removed = FilesystemSiteHealer().heal(plan)
    assert [Path(p).name for p in removed] == ["forge_loop"]
    assert not link.is_symlink() and not link.exists()


def test_filesystem_healer_keeps_non_worktree_symlink(tmp_path: Path) -> None:
    # AC #3 — a forge_loop symlink into a legit (non-worktree) location survives.
    site = tmp_path / "site-packages"
    site.mkdir()
    link = site / "forge_loop"
    link.symlink_to("/opt/legit/forge_loop")
    plan = plan_heal(_poison("/tmp/wt-loop-9/src"), site_packages=str(site))
    assert plan is not None
    removed = FilesystemSiteHealer().heal(plan)
    assert removed == ()
    assert link.is_symlink()


def test_filesystem_healer_keeps_real_package_dir(tmp_path: Path) -> None:
    # AC #3 — a REAL (non-symlink) forge_loop dir is never rmtree'd by the heal.
    site = tmp_path / "site-packages"
    site.mkdir()
    pkg = site / "forge_loop"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    plan = plan_heal(_poison("/tmp/wt-loop-9/src"), site_packages=str(site))
    assert plan is not None
    removed = FilesystemSiteHealer().heal(plan)
    assert removed == ()
    assert (pkg / "__init__.py").exists()


def test_filesystem_healer_missing_site_is_noop(tmp_path: Path) -> None:
    plan = plan_heal(_poison("/tmp/wt-loop-9/src"), site_packages=str(tmp_path / "nope"))
    assert plan is not None
    assert FilesystemSiteHealer().heal(plan) == ()


# --------------------------------------------------------------------------
# Orchestrator outcomes via the Fake boundary (AC #1/#3/#4)
# --------------------------------------------------------------------------


def test_heal_orchestrator_healed_records_removed() -> None:
    healer = FakeSiteHealer(removed=("/site/forge_loop.pth",))
    res = heal_poisoned_environment(_poison("/tmp/wt-loop-1/src"), healer, site_packages="/site")
    assert res.outcome is HealOutcome.HEALED
    assert res.removed == ("/site/forge_loop.pth",)
    assert res.offending_path == "/tmp/wt-loop-1/src"
    assert healer.plans and healer.plans[0].site_packages == "/site"


def test_heal_orchestrator_not_healable_for_non_worktree() -> None:
    healer = FakeSiteHealer(removed=("/site/x",))
    res = heal_poisoned_environment(_poison("/opt/legit"), healer, site_packages="/site")
    assert res.outcome is HealOutcome.NOT_HEALABLE
    assert healer.plans == []  # planner short-circuited before the executor


def test_heal_orchestrator_failed_when_executor_raises() -> None:
    healer = FakeSiteHealer(raises=OSError("permission denied"))
    res = heal_poisoned_environment(_poison("/tmp/wt-loop-1/src"), healer, site_packages="/site")
    assert res.outcome is HealOutcome.FAILED
    assert res.error is not None
    assert "permission denied" in res.error


# --------------------------------------------------------------------------
# Integration — boot.run self-heals / refuses (AC #1/#2/#4)
# --------------------------------------------------------------------------


def _make_cfg(tmp_path: Path) -> SimpleNamespace:
    repo = tmp_path / "repo"
    (repo / "docs" / "ops").mkdir(parents=True)
    (repo / "logs").mkdir(parents=True)
    return SimpleNamespace(
        repo=repo,
        state_dir=repo / "docs" / "ops",
        logs_dir=repo / "logs",
        events_file=repo / "docs" / "ops" / "events.jsonl",
        state_file=repo / "docs" / "ops" / "state.json",
        worktree_root=Path("/tmp"),
        stop_file=tmp_path / "stop",
        pause_file=tmp_path / "pause",
        parallel=1,
        max_ticks=1,
        tick_interval_s=0,
        labels=SimpleNamespace(ready="ready"),
    )


def test_run_self_heals_and_proceeds_past_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC #1/#2 — a healable poison: run() does NOT return 3, writes
    # ``boot_environment_self_healed``, and proceeds past the guard. We stop the
    # boot just after the guard by making the next step raise.
    from forge_loop.runner import boot

    cfg = _make_cfg(tmp_path)

    monkeypatch.setattr(
        boot,
        "_check_environment_poison",
        lambda _cfg: PoisonResult(poisoned=True, offending_path="/tmp/wt-loop-7/src"),
    )
    monkeypatch.setattr(
        boot,
        "_heal_environment_poison",
        lambda _cfg, _p: HealResult(
            outcome=HealOutcome.HEALED,
            offending_path="/tmp/wt-loop-7/src",
            removed=("/site/_editable_impl_forge_loop.pth",),
        ),
    )

    sentinel = RuntimeError("reached-dispatch")

    def _boom(*_a: object, **_k: object) -> None:
        raise sentinel

    monkeypatch.setattr(boot, "_rotate_events_file_at_boot", _boom)
    with pytest.raises(RuntimeError, match="reached-dispatch"):
        boot.run(cfg)  # type: ignore[arg-type]

    events = cfg.events_file.read_text(encoding="utf-8")
    assert "boot_environment_self_healed" in events
    assert "_editable_impl_forge_loop.pth" in events
    assert "boot_environment_poisoned" not in events


def test_run_emits_heal_failed_and_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # AC #4 — heal fails → ``boot_environment_heal_failed`` AND refuse (exit 3).
    from forge_loop.runner import boot

    cfg = _make_cfg(tmp_path)

    monkeypatch.setattr(
        boot,
        "_check_environment_poison",
        lambda _cfg: PoisonResult(
            poisoned=True,
            offending_path="/tmp/wt-loop-7/src",
            cleanup_commands=("python -m pip uninstall -y forge-loop",),
        ),
    )
    monkeypatch.setattr(
        boot,
        "_heal_environment_poison",
        lambda _cfg, _p: HealResult(
            outcome=HealOutcome.FAILED,
            offending_path="/tmp/wt-loop-7/src",
            error="OSError: denied",
        ),
    )

    rc = boot.run(cfg)  # type: ignore[arg-type]
    assert rc == 3
    err = capsys.readouterr().err
    assert "/tmp/wt-loop-7/src" in err
    events = cfg.events_file.read_text(encoding="utf-8")
    assert "boot_environment_heal_failed" in events
    assert "OSError: denied" in events
    assert "loop_start" not in events


def test_heal_helper_never_crashes_on_guard_bug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC #4 — a bug inside the heal seam degrades to FAILED + an event, not an
    # unhandled exception out of boot.
    from forge_loop.runner import boot

    cfg = _make_cfg(tmp_path)

    def _explode(*_a: object, **_k: object) -> object:
        raise RuntimeError("synthetic heal bug")

    monkeypatch.setattr("forge_loop.runner.poison_guard.heal_poisoned_environment", _explode)
    res = boot._heal_environment_poison(  # type: ignore[arg-type]
        cfg, PoisonResult(poisoned=True, offending_path="/tmp/wt-loop-7/src")
    )
    assert res.outcome is HealOutcome.FAILED
    events = cfg.events_file.read_text(encoding="utf-8")
    assert "boot_poison_heal_error" in events
    assert "synthetic heal bug" in events
