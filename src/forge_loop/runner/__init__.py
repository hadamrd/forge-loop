"""Main loop body — orchestrates ticks (pick → dispatch → wait → maybe-redeploy).

This module is a thin facade. The implementation was split into focused
submodules in issue #50:

- ``runner.boot``     — signal handlers, version-check / self-restart,
                        orphan worktree reaper, ``run`` and ``run_async``
                        entry points.
- ``runner.tick``     — the main ``_tick`` body and immediate helpers.
- ``runner.dispatch`` — worker spawning, critic-loop wiring, and the
                        multirepo dispatch glue (``run_multirepo``).
- ``runner.drift``    — outcome-drift + deploy-drift detection.
- ``runner._helpers`` — pure utility functions (pre-existing).
- ``runner._pipeline_driver`` — opt-in pipeline-driven dispatch (pre-existing).

The names re-exported below preserve every import path callers and tests
previously relied on (``from forge_loop.runner import run``,
``_reap_orphan_worktrees``, ``_installed_version``, ``_tick`` …).
"""

from __future__ import annotations

import sys as _sys
import types as _types

from forge_loop.config import Config as _Config

# Names re-exported here so tests / callers can keep importing them from
# ``forge_loop.runner``. The proxy class below makes any *write* to one of
# these names propagate to the submodule that actually consumes it, so
# legacy ``monkeypatch.setattr(forge_loop.runner, ...)`` calls in tests
# continue to bite the actual call sites after the #50 split.
from forge_loop.deploy import redeploy as redeploy
from forge_loop.gh import fetch_issue as fetch_issue
from forge_loop.gh import top_issues as top_issues
from forge_loop.gh import unlabel as unlabel
from forge_loop.runner import boot as _boot
from forge_loop.runner import dispatch as _dispatch_mod
from forge_loop.runner import iteration as iteration
from forge_loop.runner import tick as _tick_mod
from forge_loop.runner._helpers import (
    consecutive_deploy_fails as _consecutive_deploy_fails_impl,
)
from forge_loop.runner._helpers import (
    consume_force_set as _consume_force_set_impl,  # noqa: F401 — re-export
)
from forge_loop.runner._helpers import (
    error_signature as _error_signature,
)
from forge_loop.runner._helpers import (
    force_retry_file as _force_retry_file_impl,  # noqa: F401 — re-export
)
from forge_loop.runner._helpers import (
    installed_version as _installed_version,
)
from forge_loop.runner._helpers import (
    reap_orphan_worktrees as _reap_orphan_worktrees_impl,  # noqa: F401 — re-export
)
from forge_loop.runner._helpers import (
    reap_worktree as _reap_worktree,
)

# Boot / lifecycle.
from forge_loop.runner.boot import (
    _install_signal_handlers,
    _reap_orphan_worktrees,
    _short_sleep,
    _validate_pipeline_if_configured,
    run,
    run_async,
)

# Dispatch.
from forge_loop.runner.dispatch import (
    _run_critic_for_outcomes,
    _run_workers,
    _sev_counts,
    run_multirepo,
)

# Drift.
from forge_loop.runner.drift import (
    _RECENT_OUTCOMES,
    _check_drift_and_maybe_halt,
    _maybe_deploy_drift_halt,
)

# Tick body + its thin shims.
from forge_loop.runner.tick import (
    _consume_force_set,
    _force_retry_file,
    _tick,
)

# Worker symbol re-export (proxy target — see _RunnerFacadeModule).
from forge_loop.worker import run_worker as run_worker


def _consecutive_deploy_fails(cfg: _Config) -> int:
    """Backward-compat shim — forwards to ``runner._helpers``."""
    return _consecutive_deploy_fails_impl(cfg.events_file)


def __getattr__(name: str):  # pragma: no cover — thin compat shim
    if name == "_RUN":
        return _boot._RUN
    raise AttributeError(f"module 'forge_loop.runner' has no attribute {name!r}")


# ---------------------------------------------------------------------------
# monkeypatch proxy
#
# Tests that historically did
#     monkeypatch.setattr(forge_loop.runner, "top_issues", fake)
# expect the patched callable to be the one ``_tick`` actually invokes. After
# the #50 split, ``_tick`` lives in ``runner.tick`` and binds ``top_issues``
# there. To keep the legacy patch targets working without test edits, we
# install a Module subclass whose ``__setattr__`` mirrors writes onto the
# submodule that actually consumes the symbol.
#
# The mapping below is closed-world (only the names tests have historically
# patched). New names fall through to plain attribute assignment.
# ---------------------------------------------------------------------------
_PROXY_TICK_NAMES = frozenset({
    "top_issues", "fetch_issue", "unlabel", "_reap_worktree",
    "_short_sleep", "redeploy",
})
_PROXY_DISPATCH_NAMES = frozenset({"run_worker"})


class _RunnerFacadeModule(_types.ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        if name in _PROXY_TICK_NAMES:
            _tick_mod.__dict__[name] = value
        if name in _PROXY_DISPATCH_NAMES:
            _dispatch_mod.__dict__[name] = value
        if name == "_short_sleep":
            _boot.__dict__["_short_sleep"] = value
        super().__setattr__(name, value)


_sys.modules[__name__].__class__ = _RunnerFacadeModule


# ---------------------------------------------------------------------------
# Runner class (issue #87) — instance-owned lifecycle replacing the module
# globals (_RUN, _RECENT_OUTCOMES). The legacy ``run(cfg)`` / ``run_async(cfg)``
# functions still work for back-compat and route through this class via the
# default :class:`RunnerState` singleton.
# ---------------------------------------------------------------------------

from forge_loop.runner.state import RunnerState as RunnerState


class Runner:
    """Lifecycle owner for a single dispatch loop.

    Pre-#87 the loop shared mutable state via module globals (boot._RUN,
    drift._RECENT_OUTCOMES). Two Runner instances in the same process
    trampled each other; signal handlers leaked across re-execs; tests
    couldn't drive concurrent loops without module monkey-patching.

    Now each Runner owns its :class:`RunnerState` (stop flag, drift
    outcomes buffer) and binds its signal handlers to that state.

    Usage::

        runner = Runner(cfg)
        runner.run()           # blocks until SIGTERM / stop_file / max_ticks
        # ── from another thread:
        runner.stop()          # equivalent to SIGTERM

    The legacy ``forge_loop.runner.run(cfg)`` top-level function is
    unchanged: it constructs an implicit Runner against the module
    singleton state, identical to the pre-#87 behaviour.
    """

    def __init__(self, cfg: _Config, state: RunnerState | None = None) -> None:
        self.cfg = cfg
        self.state = state if state is not None else RunnerState()

    def stop(self) -> None:
        """Request a clean shutdown — exits the next iteration of the dispatch loop."""
        self.state.request_stop()

    def run(self) -> int:
        """Run the synchronous dispatch loop.

        Delegates to :func:`boot.run` but passes our :class:`RunnerState`
        so the stop flag + drift outcomes are isolated from any other
        Runner in the same process. Tests can construct two Runner
        instances on separate tmp dirs and ``stop()`` one without
        affecting the other.
        """
        return _boot.run(self.cfg, state=self.state)


__all__ = [
    "_RECENT_OUTCOMES",
    "_check_drift_and_maybe_halt",
    "_consecutive_deploy_fails",
    "_consume_force_set",
    "_error_signature",
    "_force_retry_file",
    "_install_signal_handlers",
    "_installed_version",
    "_maybe_deploy_drift_halt",
    "_reap_orphan_worktrees",
    "_reap_worktree",
    "_run_critic_for_outcomes",
    "_run_workers",
    "_sev_counts",
    "_short_sleep",
    "_tick",
    "_validate_pipeline_if_configured",
    "fetch_issue",
    "redeploy",
    "run",
    "run_async",
    "run_multirepo",
    "run_worker",
    "top_issues",
    "unlabel",
]
