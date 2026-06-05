"""Drift detection — both per-tick outcome drift and post-deploy drift.

Extracted from ``runner/__init__.py`` (issue #50). Pure mechanical move:
no behaviour change, no signature change.
"""

from __future__ import annotations

import contextlib
import time

from forge_loop.config import Config
from forge_loop.runner._helpers import (
    consecutive_deploy_fails as _consecutive_deploy_fails_impl,
)
from forge_loop.runner.state import RunnerState, get_default_state
from forge_loop.state import append_event

# Back-compat alias for legacy imports (``from forge_loop.runner.drift
# import _RECENT_OUTCOMES``). Aliasing the deque object — mutations via
# either name affect the same instance.
_RECENT_OUTCOMES = get_default_state().recent_outcomes


def _check_drift_and_maybe_halt(cfg: Config, state: RunnerState | None = None) -> bool:
    """Returns True if the loop should halt due to drift.

    ``state`` defaults to the legacy module-level singleton so existing
    callers keep working unchanged. New code passes its Runner's state
    explicitly to enable concurrent-Runner isolation (issue #87).
    """
    if state is None:
        state = get_default_state()
    outcomes = state.recent_outcomes
    if len(outcomes) < 3:
        return False
    # All 3 must be worker-bearing AND all 3 must have failed AND same signature
    sigs = {sig for had_w, all_failed, sig in outcomes if had_w and all_failed}
    if len(sigs) == 1 and all(had_w and all_failed for had_w, all_failed, _ in outcomes):
        sig = next(iter(sigs))
        append_event(cfg.events_file, "loop_drift_halt", signature=sig, last_3=list(outcomes))
        # File a loop:halt issue so the operator wakes up to a clear signal.
        title = f"loop: drift halt — 3 ticks in a row failed ({sig})"
        body = (
            f"The sprint loop self-halted at {time.strftime('%Y-%m-%dT%H:%M:%S%z')} "
            f"after 3 consecutive ticks failed with the same signature: `{sig}`.\n\n"
            f"Last 3 outcomes (had_workers, all_failed, signature):\n"
            + "\n".join(f"- {o}" for o in outcomes)
            + "\n\nSee `docs/ops/loop-runner-events.jsonl` for the full trail. "
            "Resolve the root cause and remove the `docs/ops/loop-runner.stop` "
            "file to resume."
        )
        if cfg.github_repo:
            from forge_loop import gh_issues as _gh

            with contextlib.suppress(Exception):
                _gh.create_issue(title, body, ["loop:halt"], repo=cfg.github_repo)
        # Best-effort push notification via tput-bell + a marker file the
        # operator can grep for.
        with contextlib.suppress(OSError):
            (cfg.state_dir / "loop-runner.HALT").write_text(
                f"drift: {sig}\nseen at: {time.time()}\n"
            )
        cfg.stop_file.touch()
        return True
    return False


def _maybe_deploy_drift_halt(cfg: Config, ok: bool) -> None:
    """Deploy-fail escalation. Default is WARN-ONLY.

    A misconfigured deploy.task (e.g. operator forgot to set it for a non-
    Taskfile project) used to halt the entire loop on tick #3 — which then
    blocked the loop from even fixing the bug. Now we warn first; the
    operator opts in to the hard halt via ``LOOP_DEPLOY_DRIFT_HALT=1``.
    """
    fails = _consecutive_deploy_fails_impl(cfg.events_file)
    if not ok and fails >= 3:
        append_event(cfg.events_file, "deploy_drift_warn", consecutive_fails=fails)
        # Settings-driven (issue #84): was env LOOP_DEPLOY_DRIFT_HALT,
        # now deploy.drift_halt with the unified env > yaml > default precedence.
        from forge_loop.settings import Settings as _Settings

        if _Settings.load().deploy.drift_halt:
            append_event(cfg.events_file, "deploy_drift_halt", consecutive_fails=fails)
            with contextlib.suppress(OSError):
                (cfg.state_dir / "loop-runner.HALT").write_text(
                    "deploy: 3 consecutive failures (opt-in halt)\n"
                )
            cfg.stop_file.touch()
