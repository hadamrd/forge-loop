"""Fair repair scheduling state — per-PR block counts + round-robin streak.

Issue #248. The blocking-PR repair path (``runner.tick._run_pre_dispatch_repairs``
→ ``repairs.blocking_pr_repairs``) is a *terminal tick body*: when any blocking
PR needs repair it runs the repair workers and returns, so new dispatch is never
reached that tick. With no backoff and no fairness, a PR stuck on
``critic:blocking`` is re-selected every tick forever, pinning every worker slot
and starving the ready backlog (the 2026-06-05 cleanup-sprint incident: six
ready tickets, zero dispatched).

This module owns the small amount of *durable* state the fair scheduler needs,
kept in a sidecar JSON file under the runner state dir (NOT the events log,
which ``consolidate_sprint`` truncates each tick):

    {
      "streak": <consecutive repair-only ticks>,
      "prs": { "<pr-url>": {"blocks": <int>, "last_block": "<iso8601>"} }
    }

Persistence is a plain JSON file (no binary sqlite to commit — see the maestro
rejected-paths list) and every read is failure-soft: a missing/corrupt file
reads as empty state so a scheduling sidecar hiccup never breaks the tick.

The classification itself lives in :func:`forge_loop.attempts.classify_repair_backoff`
— this module reuses that (one cooldown mechanism, not a fork) and only adds the
durable state IO. Forward progress (AC1) is the round-robin streak yield in
``runner.tick._run_fair_blocking_repairs``; this module just persists ``streak``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge_loop.log import get_logger

_log = get_logger(__name__)


@dataclass
class RepairBackoffState:
    """In-memory view of the repair-fairness sidecar.

    ``streak`` counts consecutive repair-only ticks (ticks whose whole body was
    a blocking-PR repair) since the last new-dispatch / yield tick. ``prs`` maps
    a PR url to its ``{"blocks": int, "last_block": iso}`` record.
    """

    streak: int = 0
    prs: dict[str, dict[str, Any]] = field(default_factory=dict)

    def block_count(self, pr_url: str | None) -> int:
        rec = self.prs.get(pr_url or "")
        return int(rec.get("blocks", 0)) if rec else 0

    def last_block_ts(self, pr_url: str | None) -> str | None:
        rec = self.prs.get(pr_url or "")
        ts = rec.get("last_block") if rec else None
        return ts if isinstance(ts, str) else None


def load_state(path: Path) -> RepairBackoffState:
    """Load the sidecar, returning empty state on any read/parse failure."""
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return RepairBackoffState()
    except (OSError, ValueError) as exc:
        # Corrupt/unreadable sidecar — read as empty (failure-soft) but log so a
        # wedged scheduler sidecar is diagnosable instead of silently ignored.
        _log.debug(
            "repair_backoff_state_unreadable",
            path=str(path),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return RepairBackoffState()
    if not isinstance(raw, dict):
        return RepairBackoffState()
    prs_raw = raw.get("prs")
    prs: dict[str, dict[str, Any]] = {}
    if isinstance(prs_raw, dict):
        for url, rec in prs_raw.items():
            if isinstance(url, str) and isinstance(rec, dict):
                prs[url] = {
                    "blocks": int(rec.get("blocks", 0) or 0),
                    "last_block": rec.get("last_block"),
                }
    try:
        streak = int(raw.get("streak", 0) or 0)
    except (TypeError, ValueError):
        streak = 0
    return RepairBackoffState(streak=max(0, streak), prs=prs)


def save_state(path: Path, state: RepairBackoffState) -> None:
    """Persist the sidecar atomically; best-effort (never raises through tick)."""
    payload = {"streak": max(0, int(state.streak)), "prs": state.prs}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str))
        tmp.replace(path)
    except OSError as exc:
        # Scheduling state is advisory — a write failure must not break the tick.
        _log.debug(
            "repair_backoff_state_unwritable",
            path=str(path),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return


def record_block(state: RepairBackoffState, pr_url: str, *, now_iso: str) -> None:
    """Increment the consecutive-block counter for ``pr_url``."""
    rec = state.prs.get(pr_url) or {"blocks": 0, "last_block": None}
    rec["blocks"] = int(rec.get("blocks", 0)) + 1
    rec["last_block"] = now_iso
    state.prs[pr_url] = rec


def clear_pr(state: RepairBackoffState, pr_url: str) -> None:
    """Drop a PR's block history — it cleared the critic / merged."""
    state.prs.pop(pr_url, None)


__all__ = [
    "RepairBackoffState",
    "clear_pr",
    "load_state",
    "record_block",
    "save_state",
]
