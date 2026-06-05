"""Best-effort per-tick checks that run beside worker dispatch."""

from __future__ import annotations

import contextlib

from forge_loop.config import Config
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


def run_brainstormer_audit(cfg: Config, tick: int) -> bool:
    """Periodic backlog audit (#125): demote cosmetic tickets to ``loop:cold``.

    Mirrors :func:`run_maintenance_tick`'s structure (write_state → emit start
    → call → emit done → write_state). Returns ``True`` when the audit ran and
    the tick should short-circuit (the caller then sleeps + returns), ``False``
    when the audit was skipped (no repo, or a missing/invalid ``.forge/axes.yaml``)
    so the tick continues into normal dispatch.
    """
    import time

    from forge_loop.brainstormer import Brainstormer
    from forge_loop.events import BrainstormerAuditDoneEvent, emit
    from forge_loop.product_vision import MissingVisionError

    if cfg.github_repo is None or "/" not in cfg.github_repo:
        append_event(cfg.events_file, "brainstormer_audit_skipped", tick=tick, reason="no_repo")
        return False

    write_state(cfg.state_file, {"state": "brainstormer_audit", "tick": tick})
    append_event(cfg.events_file, "brainstormer_audit_start", tick=tick)
    owner, name = cfg.github_repo.split("/", 1)
    started = time.monotonic()
    try:
        bs = Brainstormer(repo_path=cfg.repo, owner=owner, repo=name)
        outcome = bs.audit_backlog(cfg.github_repo, events_file=cfg.events_file)
    except MissingVisionError as ex:
        append_event(
            cfg.events_file,
            "brainstormer_audit_skipped",
            tick=tick,
            reason=f"axes_yaml: {ex}"[:200],
        )
        write_state(cfg.state_file, {"state": "between-ticks", "tick": tick})
        return False
    except Exception as ex:  # noqa: BLE001 — never crash the tick on the audit
        append_event(
            cfg.events_file,
            "brainstormer_audit_crashed",
            tick=tick,
            err=f"{type(ex).__name__}: {ex}"[:200],
        )
        write_state(cfg.state_file, {"state": "between-ticks", "tick": tick})
        return False
    emit(
        cfg.events_file,
        BrainstormerAuditDoneEvent(
            tick=tick,
            demoted=outcome.demoted,
            kept=outcome.kept,
            duration_s=round(time.monotonic() - started, 3),
        ),
    )
    write_state(
        cfg.state_file,
        {
            "state": "between-ticks",
            "tick": tick,
            "last_brainstormer_audit": {
                "demoted": outcome.demoted,
                "kept": outcome.kept,
            },
        },
    )
    return True


def run_maintenance_tick(cfg: Config, tick: int) -> None:
    """Run the AI-as-PM maintenance branch and write its tick state."""
    write_state(cfg.state_file, {"state": "maintenance", "tick": tick})
    append_event(cfg.events_file, "maintenance_start", tick=tick)
    brief = cfg.briefs.maintenance
    outcome = run_maintenance(cfg.repo, cfg.logs_dir, brief=brief) if brief else run_maintenance(
        cfg.repo, cfg.logs_dir
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
