"""Per-Runner state isolation (issue #87).

Pre-#87 the dispatch loop's mutable state lived on module globals
(``boot._RUN``, ``drift._RECENT_OUTCOMES``). Two Runner instances in the
same process trampled each other; stopping one stopped both. Now each
:class:`forge_loop.runner.Runner` owns its own :class:`RunnerState` and
behaves independently — these tests pin that contract.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from forge_loop.runner import Runner
from forge_loop.runner.drift import _check_drift_and_maybe_halt
from forge_loop.runner.state import RunnerState


def _mk_cfg(tmp_path: Path):
    """Tiny Config-like stub. The drift check only reads cfg.events_file +
    cfg.github_repo + cfg.state_dir + cfg.stop_file — no need for the full
    Settings stack here."""

    from forge_loop.config import (
        AttemptsConfig,
        Briefs,
        Config,
        CriticConfig,
        Labels,
        LumenConfig,
        POConfig,
        WorkerConfig,
    )

    state_dir = tmp_path / "ops"
    state_dir.mkdir(parents=True, exist_ok=True)
    return Config(
        repo=tmp_path,
        github_repo="owner/repo",
        labels=Labels(),
        briefs=Briefs(),
        critic=CriticConfig(),
        po=POConfig(),
        worker=WorkerConfig(),
        attempts=AttemptsConfig(),
        lumen=LumenConfig(),
    )


# ---------------------------------------------------------------------------
# Stop flag isolation — stopping one Runner does not stop the other.
# ---------------------------------------------------------------------------


def test_two_runners_have_independent_stop_flags(tmp_path: Path) -> None:
    cfg_a = _mk_cfg(tmp_path / "a")
    cfg_b = _mk_cfg(tmp_path / "b")
    a = Runner(cfg_a)
    b = Runner(cfg_b)

    # Both start as "should_run"...
    assert a.state.should_run
    assert b.state.should_run

    # Stopping a does not affect b — the core regression pin.
    a.stop()
    assert not a.state.should_run
    assert b.state.should_run, "stopping Runner a leaked into Runner b's stop flag"


def test_runner_stop_is_idempotent(tmp_path: Path) -> None:
    r = Runner(_mk_cfg(tmp_path))
    r.stop()
    r.stop()  # second call must not raise
    assert not r.state.should_run


# ---------------------------------------------------------------------------
# Recent-outcomes isolation — drift detector reads per-instance buffer.
# ---------------------------------------------------------------------------


def test_drift_check_uses_per_instance_outcomes(tmp_path: Path) -> None:
    """If we pre-populate Runner A's recent outcomes with 3 identical
    failures, the drift check against A's state must halt — but against
    a fresh B state it must not. Pre-#87, the shared module deque made
    the second assertion impossible to satisfy without monkeypatching.
    """
    cfg = _mk_cfg(tmp_path)
    state_a = RunnerState()
    state_b = RunnerState()

    state_a.recent_outcomes.extend([
        (True, True, "oom-exit-137"),
        (True, True, "oom-exit-137"),
        (True, True, "oom-exit-137"),
    ])

    # A halts (3 identical worker-bearing failures)
    assert _check_drift_and_maybe_halt(cfg, state=state_a) is True
    # B's empty buffer means no halt — proves per-instance isolation.
    assert _check_drift_and_maybe_halt(cfg, state=state_b) is False


def test_drift_check_no_halt_when_under_three(tmp_path: Path) -> None:
    cfg = _mk_cfg(tmp_path)
    state = RunnerState()
    state.recent_outcomes.extend([
        (True, True, "oom-exit-137"),
        (True, True, "oom-exit-137"),
    ])
    assert _check_drift_and_maybe_halt(cfg, state=state) is False


# ---------------------------------------------------------------------------
# Threading — Runner.stop() called from another thread must wake the
# main thread without polling. Tests the threading.Event semantics that
# replace the bare ``_RUN`` bool.
# ---------------------------------------------------------------------------


def test_stop_event_wakes_blocking_wait(tmp_path: Path) -> None:
    """A thread calling ``runner.stop()`` must unblock a ``state.stop_event.wait()``
    immediately — proves we got a real Event, not a polled bool."""
    r = Runner(_mk_cfg(tmp_path))

    def _stop_after_delay() -> None:
        import time
        time.sleep(0.05)
        r.stop()

    t = threading.Thread(target=_stop_after_delay, daemon=True)
    t.start()
    # If stop_event isn't a real Event, this would block past the timeout.
    fired = r.state.stop_event.wait(timeout=2.0)
    t.join(timeout=1.0)
    assert fired, "stop_event.wait() did not unblock — Runner.stop() did not fire the Event"
    assert not r.state.should_run


# ---------------------------------------------------------------------------
# Back-compat — the module-level singleton still works for legacy callers.
# ---------------------------------------------------------------------------


def test_default_state_singleton_round_trips() -> None:
    from forge_loop.runner.state import get_default_state

    s = get_default_state()
    assert s is get_default_state(), "get_default_state must return the same singleton"
    # Mutating + clearing must work cleanly across tests
    s.request_stop()
    assert not s.should_run
    s.clear_for_test()
    assert s.should_run
