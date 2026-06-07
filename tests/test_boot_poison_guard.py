"""Unit + integration tests for the #144 boot poison guard.

The detection logic is pure: it consumes a parsed ``pip show`` payload and a
worktree-root prefix and returns a structured :class:`PoisonResult` — no live
``pip`` subprocess is touched in the unit tests (AC #144).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from forge_loop._testing.pip_show import FakePipShowReader
from forge_loop.runner.poison_guard import (
    DEFAULT_PACKAGE,
    EnvironmentPoisonedError,
    HealResult,
    PipShowPayload,
    PoisonResult,
    RealHealFilesystem,
    SubprocessPipShowReader,
    check_environment_not_poisoned,
    detect_poisoned_environment,
    heal_poison,
    is_worktree_shaped,
    plan_heal,
)


def _payload(location: str, *, editable: bool = True, rc: int = 0) -> PipShowPayload:
    lines = ["Name: forge-loop", "Version: 0.1.0", f"Location: {location}"]
    if editable:
        lines.append(f"Editable project location: {location}")
    return PipShowPayload(returncode=rc, stdout="\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# Pure detection — the AC unit matrix
# --------------------------------------------------------------------------


def test_editable_location_into_worktree_is_poisoned() -> None:
    payload = _payload("/tmp/wt-loop-1/src")
    result = detect_poisoned_environment(payload, worktree_root="/tmp")
    assert result.poisoned is True
    assert result.offending_path == "/tmp/wt-loop-1/src"


def test_uv_managed_location_is_not_poisoned() -> None:
    location = str(Path.home() / ".local/share/uv/tools/forge-loop/lib/python3.12/site-packages")
    payload = _payload(location, editable=False)
    result = detect_poisoned_environment(payload, worktree_root="/tmp")
    assert result.poisoned is False
    assert result.offending_path is None


def test_package_not_installed_returns_not_poisoned_not_error() -> None:
    payload = PipShowPayload(returncode=1, stdout="")
    result = detect_poisoned_environment(payload, worktree_root="/tmp")
    assert result.poisoned is False


def test_per_repo_namespaced_worktree_shape_is_detected() -> None:
    # /tmp/forge-<repo>/wt-loop-<n> — the current namespaced scheme. Even with
    # a non-matching prefix the ``wt-loop-*`` segment alone trips the guard.
    payload = _payload("/tmp/forge-myrepo/wt-loop-144/src")
    result = detect_poisoned_environment(payload, worktree_root="/some/other/root")
    assert result.poisoned is True


def test_location_under_configured_worktree_root_without_wt_loop_segment() -> None:
    payload = _payload("/custom/wtroot/sub/src")
    result = detect_poisoned_environment(payload, worktree_root="/custom/wtroot")
    assert result.poisoned is True


def test_cleanup_commands_name_offending_path_and_all_three_steps() -> None:
    payload = _payload("/tmp/wt-loop-9/src")
    result = detect_poisoned_environment(
        payload,
        worktree_root="/tmp",
        site_packages="/home/op/.local/lib/python3.12/site-packages",
        reinstall_target="/home/op/forge-loop",
    )
    joined = "\n".join(result.cleanup_commands)
    assert "pip uninstall -y forge-loop" in joined
    assert "rm -rf /home/op/.local/lib/python3.12/site-packages/forge_loop" in joined
    assert "uv tool install --reinstall --force /home/op/forge-loop" in joined


def test_editable_location_line_preferred_over_plain_location() -> None:
    stdout = (
        "Name: forge-loop\nVersion: 0.1.0\n"
        "Location: /home/op/.local/lib/python3.12/site-packages\n"
        "Editable project location: /tmp/wt-loop-3/src\n"
    )
    result = detect_poisoned_environment(
        PipShowPayload(returncode=0, stdout=stdout), worktree_root="/tmp"
    )
    assert result.poisoned is True
    assert result.offending_path == "/tmp/wt-loop-3/src"


def test_empty_stdout_with_zero_rc_is_not_poisoned() -> None:
    # Adversarial: returncode 0 but no Location field at all.
    result = detect_poisoned_environment(
        PipShowPayload(returncode=0, stdout=""), worktree_root="/tmp"
    )
    assert result.poisoned is False


def test_none_worktree_root_only_matches_wt_loop_shape() -> None:
    assert (
        detect_poisoned_environment(_payload("/tmp/wt-loop-1/src"), worktree_root=None).poisoned
        is True
    )
    assert (
        detect_poisoned_environment(_payload("/opt/elsewhere/src"), worktree_root=None).poisoned
        is False
    )


# --------------------------------------------------------------------------
# render_error / EnvironmentPoisonedError
# --------------------------------------------------------------------------


def test_render_error_contains_path_and_commands() -> None:
    result = PoisonResult(
        poisoned=True,
        offending_path="/tmp/wt-loop-1/src",
        cleanup_commands=("python -m pip uninstall -y forge-loop", "rm -rf x", "uv tool install ."),
    )
    text = result.render_error()
    assert "/tmp/wt-loop-1/src" in text
    assert "pip uninstall -y forge-loop" in text
    assert "uv tool install" in text


def test_environment_poisoned_error_message_carries_result() -> None:
    result = PoisonResult(poisoned=True, offending_path="/tmp/wt-loop-1/src")
    err = EnvironmentPoisonedError(result)
    assert err.result is result
    assert "/tmp/wt-loop-1/src" in str(err)


# --------------------------------------------------------------------------
# check_environment_not_poisoned — boundary wiring via the Fake
# --------------------------------------------------------------------------


def test_check_with_fake_editable_reader_is_poisoned() -> None:
    reader = FakePipShowReader.editable("/tmp/wt-loop-7/src")
    result = check_environment_not_poisoned(reader, worktree_root="/tmp", reinstall_target=".")
    assert result.poisoned is True
    assert reader.calls == [DEFAULT_PACKAGE]


def test_check_with_fake_not_installed_reader_is_clean() -> None:
    reader = FakePipShowReader.not_installed()
    result = check_environment_not_poisoned(reader, worktree_root="/tmp")
    assert result.poisoned is False


# --------------------------------------------------------------------------
# T4 contract test — real SubprocessPipShowReader returns the Fake's shape
# --------------------------------------------------------------------------


def test_real_reader_returns_payload_shape_for_known_package() -> None:
    # ``pip`` itself is always importable in our toolchain; inspect it so the
    # call returns rc==0 with a Location, matching the Fake's payload shape.
    real = SubprocessPipShowReader(python_executable=sys.executable)
    payload = real.pip_show("pip")
    assert isinstance(payload, PipShowPayload)
    assert isinstance(payload.returncode, int)
    assert isinstance(payload.stdout, str)


def test_real_reader_nonzero_for_absent_package() -> None:
    real = SubprocessPipShowReader(python_executable=sys.executable)
    payload = real.pip_show("forge-loop-definitely-not-installed-xyz")
    assert payload.returncode != 0


# --------------------------------------------------------------------------
# Integration — forge_loop.runner.boot.run refuses to start when poisoned
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
    )


def test_run_exits_nonzero_with_cleanup_in_stderr_when_poisoned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from forge_loop.runner import boot

    cfg = _make_cfg(tmp_path)

    # Non-worktree-shaped offending path: NOT self-healable under #315, so the
    # #144 refuse-to-start render path is preserved (this test guards that render).
    def _poisoned(_cfg: object) -> PoisonResult:
        return PoisonResult(
            poisoned=True,
            offending_path="/usr/lib/python3.12/site-packages",
            cleanup_commands=(
                "python -m pip uninstall -y forge-loop",
                "rm -rf /site/forge_loop /site/roles",
                "uv tool install --reinstall --force .",
            ),
        )

    monkeypatch.setattr(boot, "_check_environment_poison", _poisoned)

    rc = boot.run(cfg)  # type: ignore[arg-type]

    assert rc != 0
    err = capsys.readouterr().err
    assert "/usr/lib/python3.12/site-packages" in err
    assert "uv tool install --reinstall --force" in err
    # Boot must NOT proceed to dispatch: no loop_start event written.
    events = cfg.events_file.read_text(encoding="utf-8") if cfg.events_file.exists() else ""
    assert "boot_environment_poisoned" in events
    assert "loop_start" not in events


def test_run_does_not_block_clean_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Guard returns clean → run() proceeds past the guard. We stop it
    # immediately by making the first tick raise so the test stays fast.
    from forge_loop.runner import boot

    cfg = _make_cfg(tmp_path)
    cfg.stop_file = tmp_path / "stop"
    cfg.pause_file = tmp_path / "pause"
    cfg.parallel = 1
    cfg.max_ticks = 1
    cfg.tick_interval_s = 0
    cfg.labels = SimpleNamespace(ready="ready")

    monkeypatch.setattr(
        boot, "_check_environment_poison", lambda _cfg: PoisonResult(poisoned=False)
    )

    sentinel = RuntimeError("reached-dispatch")

    def _boom(*_a: object, **_k: object) -> None:
        raise sentinel

    # Past the guard, run() does heavy wiring; assert we get past the guard by
    # confirming the events file gains a non-poison record before any failure.
    monkeypatch.setattr(boot, "_rotate_events_file_at_boot", _boom)
    with pytest.raises(RuntimeError, match="reached-dispatch"):
        boot.run(cfg)  # type: ignore[arg-type]

    events = cfg.events_file.read_text(encoding="utf-8") if cfg.events_file.exists() else ""
    assert "boot_environment_poisoned" not in events


# --------------------------------------------------------------------------
# Guard error is surfaced, not swallowed (error-handling.md#EH-001, #144)
# --------------------------------------------------------------------------


def test_guard_error_fails_open_but_appends_boot_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bug inside the guard must not block a clean boot, but it must be
    observable: ``_check_environment_poison`` fails open (``poisoned=False``)
    *and* appends a ``boot_poison_guard_error`` event with the error detail."""
    from forge_loop.runner import boot

    cfg = _make_cfg(tmp_path)

    def _explode(*_a: object, **_k: object) -> PoisonResult:
        raise RuntimeError("synthetic guard bug")

    # boot imports the symbol locally from poison_guard, so patch it there.
    monkeypatch.setattr("forge_loop.runner.poison_guard.check_environment_not_poisoned", _explode)

    result = boot._check_environment_poison(cfg)  # type: ignore[arg-type]

    assert result.poisoned is False
    events = cfg.events_file.read_text(encoding="utf-8")
    assert "boot_poison_guard_error" in events
    assert "synthetic guard bug" in events
    assert "RuntimeError" in events


def test_current_site_packages_missing_purelib_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``current_site_packages`` is a display-only hint: a ``purelib``-less
    install scheme degrades to ``None`` (so the cleanup command falls back to
    the generic sysconfig snippet) rather than raising."""
    from forge_loop.runner import poison_guard

    monkeypatch.setattr(poison_guard.sysconfig, "get_paths", lambda: {})
    assert poison_guard.current_site_packages() is None


# ==========================================================================
# #315 — self-heal: worktree-shape gate (stricter than detection)
# ==========================================================================


def test_is_worktree_shaped_matches_wt_loop_segment() -> None:
    assert is_worktree_shaped("/tmp/wt-loop-9/src") is True
    assert is_worktree_shaped("/tmp/forge-myrepo/wt-loop-296/src") is True


def test_is_worktree_shaped_rejects_non_worktree_and_none() -> None:
    # uv-managed / canonical / None → NOT healable → #144 refuse path preserved.
    assert is_worktree_shaped("/usr/lib/python3.12/site-packages") is False
    assert is_worktree_shaped(str(Path.home() / ".local/share/uv/tools/forge-loop")) is False
    assert is_worktree_shaped(None) is False


# ==========================================================================
# #315 — self-heal: pure planner (plan_heal)
# ==========================================================================

_SITES = {"/site": ("_editable_impl_forge_loop.pth", "forge_loop", "roles", "numpy", "click.py")}


def test_plan_heal_worktree_plans_only_forge_loop_artifacts() -> None:
    plan = plan_heal("/tmp/wt-loop-9/src", sites=_SITES)
    assert plan.healable is True
    assert plan.remove_paths == (
        "/site/_editable_impl_forge_loop.pth",
        "/site/forge_loop",
        "/site/roles",
    )
    # AC #3: never the offending worktree path, never unrelated packages.
    assert "/tmp/wt-loop-9/src" not in plan.remove_paths
    assert "/site/numpy" not in plan.remove_paths


def test_plan_heal_non_worktree_location_not_healable() -> None:
    uv = str(Path.home() / ".local/share/uv/tools/forge-loop/lib/python3.12/site-packages")
    plan = plan_heal(uv, sites=_SITES)
    assert plan.healable is False
    assert plan.remove_paths == ()


def test_plan_heal_none_location_not_healable() -> None:
    assert plan_heal(None, sites=_SITES).healable is False


def test_plan_heal_dangling_worktree_still_planned() -> None:
    # Adversarial: worktree already reaped. The planner is existence-independent
    # — the artifacts live in the operator site, not the reaped worktree.
    plan = plan_heal("/tmp/forge-myrepo/wt-loop-296/src", sites=_SITES)
    assert plan.healable is True
    assert "/site/_editable_impl_forge_loop.pth" in plan.remove_paths


def test_plan_heal_handles_versioned_editable_pth() -> None:
    sites = {"/site": ("__editable__.forge_loop-0.1.0.pth", "__editable___forge_loop_finder.py")}
    plan = plan_heal("/tmp/wt-loop-1/src", sites=sites)
    assert set(plan.remove_paths) == {
        "/site/__editable__.forge_loop-0.1.0.pth",
        "/site/__editable___forge_loop_finder.py",
    }


# ==========================================================================
# #315 — self-heal executor behind a Fake HealFilesystem boundary (Q2/T4)
# ==========================================================================


@dataclass
class _FakeHealFs:
    """In-memory :class:`HealFilesystem`: records removals; ``raise_on`` fails."""

    entries: dict[str, tuple[str, ...]]
    removed: list[str] | None = None
    raise_on: str | None = None

    def __post_init__(self) -> None:
        if self.removed is None:
            self.removed = []

    def list_site_entries(self, site_dir: str) -> tuple[str, ...]:
        return self.entries.get(site_dir, ())

    def remove(self, path: str) -> None:
        assert self.removed is not None
        if self.raise_on is not None and path == self.raise_on:
            raise OSError(f"permission denied: {path}")
        self.removed.append(path)


def test_heal_poison_removes_artifacts_and_records() -> None:
    fs = _FakeHealFs(entries={"/site": ("_editable_impl_forge_loop.pth", "forge_loop", "numpy")})
    result = heal_poison("/tmp/wt-loop-9/src", site_dirs=["/site"], fs=fs)
    assert result.healable is True and result.healed is True
    assert set(result.removed) == {"/site/_editable_impl_forge_loop.pth", "/site/forge_loop"}
    assert fs.removed == list(result.removed)
    assert "/site/numpy" not in result.removed  # no collateral deletion


def test_heal_poison_non_worktree_is_not_healable() -> None:
    fs = _FakeHealFs(entries={"/site": ("forge_loop",)})
    result = heal_poison("/usr/lib/python3.12/site-packages", site_dirs=["/site"], fs=fs)
    assert result.healable is False and result.healed is False
    assert fs.removed == []  # nothing deleted on the refuse path


def test_heal_poison_raising_fs_returns_heal_failed() -> None:
    fs = _FakeHealFs(
        entries={"/site": ("_editable_impl_forge_loop.pth",)},
        raise_on="/site/_editable_impl_forge_loop.pth",
    )
    result = heal_poison("/tmp/wt-loop-9/src", site_dirs=["/site"], fs=fs)
    assert result.healable is True and result.healed is False
    assert result.error is not None and "OSError" in result.error


# ==========================================================================
# #315 — RealHealFilesystem contract (real impl on a tmpdir; T4)
# ==========================================================================


def test_real_heal_fs_lists_and_removes_file_dir_and_dangling_symlink(tmp_path: Path) -> None:
    fs = RealHealFilesystem()

    f = tmp_path / "_editable_impl_forge_loop.pth"
    f.write_text("/tmp/wt-loop-1/src\n")
    d = tmp_path / "forge_loop"
    d.mkdir()
    (d / "__init__.py").write_text("")
    dangling = tmp_path / "roles"
    dangling.symlink_to(tmp_path / "does-not-exist")  # broken symlink (reaped worktree)

    assert set(fs.list_site_entries(str(tmp_path))) == {
        "_editable_impl_forge_loop.pth",
        "forge_loop",
        "roles",
    }
    fs.remove(str(f))
    fs.remove(str(d))
    fs.remove(str(dangling))
    assert not f.exists() and not d.exists() and not dangling.is_symlink()


def test_real_heal_fs_list_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert RealHealFilesystem().list_site_entries(str(tmp_path / "nope")) == ()


def test_real_heal_fs_remove_missing_is_noop(tmp_path: Path) -> None:
    # remove() must not raise on an already-absent path (best-effort).
    RealHealFilesystem().remove(str(tmp_path / "nope"))


# ==========================================================================
# #315 — boot.run integration: self-heal vs refuse fallback
# ==========================================================================


def _heal_cfg(tmp_path: Path) -> SimpleNamespace:
    cfg = _make_cfg(tmp_path)
    cfg.stop_file = tmp_path / "stop"
    cfg.pause_file = tmp_path / "pause"
    cfg.parallel = 1
    cfg.max_ticks = 1
    cfg.tick_interval_s = 0
    cfg.labels = SimpleNamespace(ready="ready")
    return cfg


def test_run_self_heals_and_proceeds_past_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #1/#2: a healable poison → run() cleans the operator site, emits
    ``boot_environment_self_healed`` and proceeds past the guard (no return 3)."""
    from forge_loop.runner import boot, poison_guard

    cfg = _heal_cfg(tmp_path)
    site = tmp_path / "site"
    site.mkdir()
    pth = site / "_editable_impl_forge_loop.pth"
    pth.write_text("/tmp/wt-loop-296/src\n")
    (site / "forge_loop").mkdir()

    monkeypatch.setattr(
        boot,
        "_check_environment_poison",
        lambda _cfg: PoisonResult(poisoned=True, offending_path="/tmp/wt-loop-296/src"),
    )
    # CONFINE the heal to the tmp site only — ``_heal_environment_poison`` scans
    # ``operator_site_dirs()`` so pointing it at the tmp site means the real
    # RealHealFilesystem can never touch the actual operator install.
    monkeypatch.setattr(poison_guard, "operator_site_dirs", lambda: (str(site),))

    # Prove we got PAST the guard by making the next boot step raise.
    sentinel = RuntimeError("reached-dispatch")

    def _boom(*_a: object, **_k: object) -> None:
        raise sentinel

    monkeypatch.setattr(boot, "_rotate_events_file_at_boot", _boom)
    with pytest.raises(RuntimeError, match="reached-dispatch"):
        boot.run(cfg)  # type: ignore[arg-type]

    events = cfg.events_file.read_text(encoding="utf-8")
    assert "boot_environment_self_healed" in events
    assert "boot_environment_poisoned" not in events
    # AC #5 shape: the operator site is free of the editable artifacts afterward.
    assert not pth.exists()
    assert not (site / "forge_loop").exists()


def test_run_heal_failure_falls_back_to_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC #4: if the heal itself fails, emit ``boot_environment_heal_failed`` AND
    fall back to the #144 refuse-to-start (exit 3 + cleanup in stderr)."""
    from forge_loop.runner import boot, poison_guard

    cfg = _heal_cfg(tmp_path)
    monkeypatch.setattr(
        boot,
        "_check_environment_poison",
        lambda _cfg: PoisonResult(
            poisoned=True,
            offending_path="/tmp/wt-loop-296/src",
            cleanup_commands=("python -m pip uninstall -y forge-loop",),
        ),
    )
    # Poison is healable (worktree-shaped) but the executor reports a failed
    # heal → heal_failed event + refuse fallback.
    monkeypatch.setattr(poison_guard, "operator_site_dirs", lambda: ("/site",))
    monkeypatch.setattr(
        poison_guard,
        "heal_poison",
        lambda offending_path, *, site_dirs, fs: HealResult(
            healable=True, healed=False, offending_path=offending_path, error="boom"
        ),
    )

    rc = boot.run(cfg)  # type: ignore[arg-type]

    assert rc == 3
    events = cfg.events_file.read_text(encoding="utf-8")
    assert "boot_environment_heal_failed" in events
    assert "boot_environment_poisoned" in events
    assert "loop_start" not in events
    assert "/tmp/wt-loop-296/src" in capsys.readouterr().err


def test_run_unhealable_poison_preserves_refuse_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #3: a non-worktree (uv-managed) offending location is NOT healable →
    the old #144 refuse-to-start path is preserved (return 3, no heal event)."""
    from forge_loop.runner import boot, poison_guard

    cfg = _heal_cfg(tmp_path)
    monkeypatch.setattr(
        boot,
        "_check_environment_poison",
        lambda _cfg: PoisonResult(poisoned=True, offending_path="/usr/lib/python3.12/site-packages"),
    )
    # Not worktree-shaped → heal_poison returns a non-healable result; no fs
    # scan / removal happens and the refuse path is preserved.
    monkeypatch.setattr(poison_guard, "operator_site_dirs", lambda: ("/site",))

    rc = boot.run(cfg)  # type: ignore[arg-type]

    assert rc == 3
    events = cfg.events_file.read_text(encoding="utf-8")
    assert "boot_environment_poisoned" in events
    assert "boot_environment_self_healed" not in events


# ==========================================================================
# #315 — isolation (root cause): worker pip can't reach the operator site
# ==========================================================================


def test_worker_sdk_env_requires_virtualenv_for_pip(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC #5 isolation: the worker child env forces ``PIP_REQUIRE_VIRTUALENV=1``
    so a bare ``pip install -e .`` REFUSES to run and can no longer poison the
    operator/user site — the root cause that #144 only detected after the fact."""
    from forge_loop import _worker_sdk

    monkeypatch.delenv("PIP_REQUIRE_VIRTUALENV", raising=False)
    env = _worker_sdk._clean_sdk_env()
    assert env["PIP_REQUIRE_VIRTUALENV"] == "1"
