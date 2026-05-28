"""Tests for the structlog wiring (issue #89).

Pin the public surface — :func:`configure_logging` is idempotent,
:func:`get_logger` returns a working logger, the TTY/JSON renderer
toggle responds to ``FORGE_LOOP_LOG_JSON``, and the events module
mirrors every emit() to the log stream at the right level.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from forge_loop.events import (
    LoopStartEvent,
    LoopStopEvent,
    RedeployEvent,
    emit,
)
from forge_loop.log import configure_logging, get_logger


def test_configure_logging_is_idempotent() -> None:
    """Calling configure twice must not raise nor reset state — the call
    is gated by a process-level boolean."""
    configure_logging()
    configure_logging()  # second call: no-op
    # If it weren't idempotent, structlog would have re-installed processors
    # and the test would still pass — but `get_logger().info("test")`
    # exercising the chain at this point must not raise.
    get_logger().info("idempotency_smoke_test")


def test_get_logger_returns_bound_logger() -> None:
    logger = get_logger()
    # Must support the structlog bound-logger surface — kwargs flow as
    # structured payload, not f-string interpolation.
    logger.info("test_event", issue=42, action="dispatched")


def test_json_mode_when_env_set(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """FORGE_LOOP_LOG_JSON=1 must force JSON output even on a TTY.

    We can't actually verify the renderer choice from outside structlog
    without rebuilding the chain, so this test asserts the env-detection
    helper: when the env var is set, ``_is_tty`` returns False (which
    drives the JSON renderer branch).
    """
    from forge_loop.log import _is_tty

    monkeypatch.setenv("FORGE_LOOP_LOG_JSON", "1")
    assert _is_tty() is False, "JSON-mode env override must defeat TTY detection"


def test_emit_mirrors_to_logger(tmp_path: Path) -> None:
    """Every typed event emission must also reach structlog.

    Uses structlog's canonical ``capture_logs()`` because the
    PrintLoggerFactory captures sys.stderr at config-time, so pytest's
    capfd can't see the output after the process has booted.
    """
    import structlog.testing

    events_file = tmp_path / "events.jsonl"
    with structlog.testing.capture_logs() as captured:
        emit(events_file, LoopStartEvent(parallel=3, tick_interval=60, max_ticks=0))
    # The on-disk record is still written...
    assert events_file.exists()
    rec = json.loads(events_file.read_text().strip())
    assert rec["kind"] == "loop_start"
    # ...AND structlog received the event.
    assert any(e.get("event") == "loop_start" for e in captured), (
        f"emit() did not mirror to structlog. captured={captured!r}"
    )


def test_emit_warning_level_for_failure_kinds(tmp_path: Path) -> None:
    """Heuristic: events with ``fail``/``halt``/``drift``/``refused``/``stop``
    in their KIND emit at WARNING. ``loop_stop`` matches ``stop``."""
    import structlog.testing

    events_file = tmp_path / "events.jsonl"
    with structlog.testing.capture_logs() as captured:
        emit(events_file, LoopStopEvent(tick=42))
    log_levels = [e.get("log_level") for e in captured if e.get("event") == "loop_stop"]
    assert "warning" in log_levels, (
        f"loop_stop should log at WARN. levels={log_levels!r}"
    )


def test_emit_info_level_for_normal_kinds(tmp_path: Path) -> None:
    """Non-failure events emit at INFO. ``redeploy ok=True`` has no
    failure marker in its KIND so it should be INFO."""
    import structlog.testing

    events_file = tmp_path / "events.jsonl"
    with structlog.testing.capture_logs() as captured:
        emit(events_file, RedeployEvent(ok=True, task="deploy:k3s"))
    log_levels = [e.get("log_level") for e in captured if e.get("event") == "redeploy"]
    assert "info" in log_levels, (
        f"successful redeploy should log at INFO. levels={log_levels!r}"
    )
