"""Unit + integration tests for the #144 boot poison guard.

The detection logic is pure: it consumes a parsed ``pip show`` payload and a
worktree-root prefix and returns a structured :class:`PoisonResult` — no live
``pip`` subprocess is touched in the unit tests (AC #144).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from forge_loop._testing.pip_show import FakePipShowReader
from forge_loop.runner.poison_guard import (
    DEFAULT_PACKAGE,
    EnvironmentPoisonedError,
    PipShowPayload,
    PoisonResult,
    SubprocessPipShowReader,
    check_environment_not_poisoned,
    detect_poisoned_environment,
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
        detect_poisoned_environment(
            _payload("/opt/elsewhere/src"), worktree_root=None
        ).poisoned
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

    def _poisoned(_cfg: object) -> PoisonResult:
        return PoisonResult(
            poisoned=True,
            offending_path="/tmp/wt-loop-124/src",
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
    assert "/tmp/wt-loop-124/src" in err
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
