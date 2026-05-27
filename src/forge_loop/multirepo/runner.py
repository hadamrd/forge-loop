"""Multirepo tick driver — round-robins through enabled repos.

Each global tick iterates every loaded ``RepoSpec`` in name-sorted order:

* If the repo is disabled (flag file), skip and append a ``repo_skipped``
  event to the *loop home* events file (so the operator sees skips in one
  place) and continue.
* If the repo's checkout is unusable (missing path, not a git repo), skip
  with reason ``checkout_invalid`` — does NOT abort the tick.
* Otherwise, build a per-repo ``Config`` and invoke the same ``_tick``
  body the single-repo runner uses. Per-repo events still land under
  ``<checkout>/docs/ops/`` so per-repo history survives loop reconfig.

The ``last_activity`` map this module exposes is what
``forge-loop repos list`` reads to show "when did each repo last do
anything in this loop process."
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from forge_loop.config import Config
from forge_loop.multirepo.loader import (
    RepoSpec,
    build_config_for_repo,
    is_disabled,
    validate_checkout,
)
from forge_loop.state import append_event


@dataclass
class RepoActivity:
    """In-memory record of the last action taken on a repo this process."""

    name: str
    last_tick: int = 0
    last_action: str = "never"  # "ticked" | "skipped_disabled" | "skipped_invalid" | "never"
    last_reason: str = ""
    last_ts: float = 0.0


@dataclass
class MultirepoRunState:
    """Per-process round-robin bookkeeping."""

    activity: dict[str, RepoActivity] = field(default_factory=dict)

    def record(self, spec: RepoSpec, tick: int, action: str, reason: str = "") -> None:
        act = self.activity.setdefault(spec.name, RepoActivity(name=spec.name))
        act.last_tick = tick
        act.last_action = action
        act.last_reason = reason
        act.last_ts = time.time()


TickFn = Callable[[Config, int], None]


def run_multirepo_tick(
    specs: list[RepoSpec],
    tick: int,
    *,
    state: MultirepoRunState,
    template: Config | None,
    events_file: Path,
    tick_fn: TickFn,
) -> None:
    """Run one global tick across ``specs``.

    ``tick_fn`` is injected so unit tests can probe iteration order + skip
    semantics without spawning real workers. The production wiring passes
    ``forge_loop.runner._tick``.
    """
    if not specs:
        append_event(events_file, "multirepo_tick_empty", tick=tick)
        return

    append_event(
        events_file, "multirepo_tick_start", tick=tick,
        repos=[s.name for s in specs],
    )

    for spec in specs:
        if is_disabled(spec):
            state.record(spec, tick, "skipped_disabled")
            append_event(events_file, "repo_skipped", tick=tick,
                         repo=spec.name, reason="disabled")
            continue
        bad = validate_checkout(spec)
        if bad:
            state.record(spec, tick, "skipped_invalid", reason=bad)
            append_event(events_file, "repo_skipped", tick=tick,
                         repo=spec.name, reason="checkout_invalid", detail=bad)
            continue
        cfg = build_config_for_repo(spec, template=template)
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        cfg.events_file.touch()
        append_event(events_file, "repo_tick_start", tick=tick, repo=spec.name)
        try:
            tick_fn(cfg, tick)
            state.record(spec, tick, "ticked")
            append_event(events_file, "repo_tick_done", tick=tick, repo=spec.name)
        except Exception as e:
            state.record(spec, tick, "ticked", reason=f"error: {e!s}"[:200])
            append_event(events_file, "repo_tick_error", tick=tick,
                         repo=spec.name, err=str(e)[:200])
            continue

    append_event(events_file, "multirepo_tick_done", tick=tick)
