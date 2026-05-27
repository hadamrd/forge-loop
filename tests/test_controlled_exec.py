"""Tests for controlled_exec.py — timeout, exit codes, callback wiring."""

from __future__ import annotations

from forge_loop import controlled_exec as cx


def test_run_success_returns_exit_zero() -> None:
    r = cx.run(["true"], timeout_s=5, label="true")
    assert r.exit_code == 0
    assert r.timed_out is False
    assert r.label == "true"


def test_run_failure_returns_nonzero() -> None:
    r = cx.run(["false"], timeout_s=5, label="false")
    assert r.exit_code != 0
    assert r.timed_out is False


def test_run_timeout_returns_124_and_flag_true() -> None:
    r = cx.run(["sleep", "10"], timeout_s=1, label="sleep")
    assert r.timed_out is True
    assert r.exit_code == 124  # GNU coreutils convention


def test_run_clamps_zero_timeout_to_minimum() -> None:
    # Zero timeout should be clamped, not raise — exec just gets MIN_TIMEOUT_S
    r = cx.run(["true"], timeout_s=0, label="zero-timeout")
    assert r.exit_code == 0  # `true` finishes in <1s easily


def test_run_clamps_huge_timeout_to_ceiling() -> None:
    # Huge timeout should be silently clamped to MAX_TIMEOUT_S
    r = cx.run(["true"], timeout_s=1_000_000, label="huge")
    assert r.exit_code == 0
    # No public way to read the actual timeout applied, but it ran fine


def test_run_captures_stdout_tail() -> None:
    r = cx.run(["sh", "-c", "echo hello-world"], timeout_s=5)
    assert "hello-world" in r.stdout_tail


def test_run_callbacks_get_invoked_in_order() -> None:
    calls: list[tuple[str, dict]] = []

    def on(kind: str, payload: dict) -> None:
        calls.append((kind, payload))

    cx.run(["true"], timeout_s=5, label="cb", on_start=on, on_done=on)

    kinds = [k for k, _ in calls]
    assert kinds == ["controlled_exec_start", "controlled_exec_done"]
    assert calls[0][1]["label"] == "cb"
    assert calls[1][1]["exit_code"] == 0


def test_run_swallows_callback_errors() -> None:
    def boom(kind: str, payload: dict) -> None:
        raise RuntimeError("logging hiccup")

    r = cx.run(["true"], timeout_s=5, on_start=boom, on_done=boom)
    assert r.exit_code == 0  # exec still succeeded despite callback raising
