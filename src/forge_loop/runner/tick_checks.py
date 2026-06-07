"""Best-effort per-tick checks that run beside worker dispatch."""

from __future__ import annotations

import contextlib

from forge_loop.config import Config
from forge_loop.epic_sweep import EpicSweepReport, GhClientLike
from forge_loop.epic_sweep import sweep as _epic_sweep
from forge_loop.maintenance import run_maintenance
from forge_loop.state import append_event, write_state
from forge_loop.stuck_sweep import SweepReport
from forge_loop.stuck_sweep import sweep as _stuck_sweep


def run_stuck_sweep(cfg: Config, tick: int) -> SweepReport | None:
    """Demote issues that repeatedly failed to leave the ready queue."""
    if cfg.github_repo is None or "/" not in cfg.github_repo:
        return None
    owner, repo = cfg.github_repo.split("/", 1)
    try:
        from forge_loop.gh_client import GithubkitClient

        client = GithubkitClient()
    except Exception as ex:  # noqa: BLE001
        append_event(
            cfg.events_file,
            "stuck_sweep_skipped",
            tick=tick,
            reason=f"gh_client_init: {ex}"[:200],
        )
        return None
    try:
        report = _stuck_sweep(
            cfg.events_file,
            client,
            owner=owner,
            repo=repo,
            threshold=cfg.stuck_threshold_attempts,
            ready_label=cfg.labels.ready,
            tail=cfg.stuck_tail_events,
        )
    except Exception as ex:  # noqa: BLE001
        append_event(cfg.events_file, "stuck_sweep_crashed", tick=tick, err=str(ex)[:200])
        return None
    if report.demotions:
        append_event(
            cfg.events_file,
            "stuck_sweep_done",
            tick=tick,
            demoted=[d.issue for d in report.demotions if d.ok],
            failed=list(report.errors),
            scanned=report.scanned,
        )
    return report


def run_epic_sweep(
    cfg: Config, tick: int, *, client: GhClientLike | None = None
) -> EpicSweepReport | None:
    """Auto-close epics whose tracked sub-issues are all resolved (issue #367).

    Runs on the maintenance cadence (``maintenance_every_n_ticks``) only — a
    no-op off-cadence (returns ``None`` without touching GitHub). Deterministic
    Python; spawns NO LLM subagent. Emits a typed ``epic_sweep_done`` summary
    event and returns the report. ``client`` is injectable for tests; in
    production it is the real ``GithubkitClient``.
    """
    if cfg.maintenance_every_n_ticks <= 0 or tick % cfg.maintenance_every_n_ticks != 0:
        return None
    if cfg.github_repo is None or "/" not in cfg.github_repo:
        return None
    owner, repo = cfg.github_repo.split("/", 1)
    if client is None:
        try:
            from forge_loop.gh_client import GithubkitClient

            client = GithubkitClient()
        except Exception as ex:  # noqa: BLE001
            append_event(
                cfg.events_file,
                "epic_sweep_skipped",
                tick=tick,
                reason=f"gh_client_init: {ex}"[:200],
            )
            return None
    try:
        report = _epic_sweep(client, owner=owner, repo=repo, epic_label=cfg.epic_label)
    except Exception as ex:  # noqa: BLE001 — the sweep never raises, but belt-and-braces
        append_event(cfg.events_file, "epic_sweep_crashed", tick=tick, err=str(ex)[:200])
        return None

    from forge_loop.events import EpicSweepDoneEvent, emit

    emit(
        cfg.events_file,
        EpicSweepDoneEvent(
            tick=tick,
            closed=report.closed,
            skipped_open_subs=report.skipped_open_subs,
            skipped_no_subs=report.skipped_no_subs,
            errors=list(report.errors),
        ),
    )
    return report


def run_codebase_audit(cfg: Config, tick: int) -> None:
    """Emit codebase-audit drift events on the maintenance cadence."""
    try:
        from forge_loop.codebase_audit import audit
        from forge_loop.events import AuditCleanEvent, emit

        report = audit(cfg.repo)
    except Exception as ex:  # noqa: BLE001
        append_event(
            cfg.events_file,
            "audit_skipped",
            tick=tick,
            reason=f"{type(ex).__name__}: {ex}"[:200],
        )
        return
    if report.is_clean:
        with contextlib.suppress(Exception):
            emit(cfg.events_file, AuditCleanEvent(probes_run=list(report.probes_run)))
        return
    for violation in report.violations:
        append_event(
            cfg.events_file,
            "audit_violation_observed",
            tick=tick,
            probe=violation.probe,
            target=violation.target,
            severity=violation.severity,
            title=violation.title,
            metrics=violation.metrics,
        )
    for probe_name, err in report.errors.items():
        append_event(
            cfg.events_file,
            "audit_probe_crashed",
            tick=tick,
            probe=probe_name,
            err=err,
        )


def run_maintenance_tick(cfg: Config, tick: int) -> None:
    """Run the AI-as-PM maintenance branch and write its tick state."""
    write_state(cfg.state_file, {"state": "maintenance", "tick": tick})
    append_event(cfg.events_file, "maintenance_start", tick=tick)
    brief = cfg.briefs.maintenance
    outcome = (
        run_maintenance(cfg.repo, cfg.logs_dir, brief=brief)
        if brief
        else run_maintenance(cfg.repo, cfg.logs_dir)
    )
    append_event(
        cfg.events_file,
        "maintenance_done",
        tick=tick,
        acted_on=outcome.acted_on,
        added_ready=outcome.added_ready,
        closed_dupes=outcome.closed_dupes,
        retitled=outcome.retitled,
        duration_s=round(outcome.duration_s, 1),
    )
    write_state(
        cfg.state_file,
        {
            "state": "between-ticks",
            "tick": tick,
            "last_maintenance": {
                "acted_on": outcome.acted_on,
                "added_ready": outcome.added_ready,
            },
        },
    )
