"""The main ``_tick`` body and its immediate per-tick helpers.

Extracted from ``runner/__init__.py`` (issue #50). Pure mechanical move:
no behaviour change, no signature change.
"""

# ruff: noqa: I001

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from forge_loop import attempts as _attempts
from forge_loop import master_log as _mlog
from forge_loop import worker as _worker
from forge_loop.config import Config
from forge_loop.deploy import redeploy
from forge_loop.gh import (
    fetch_issue,
    pr_review_context,
    prs_by_label,
    prs_requiring_repair,
    top_issues,
    unlabel,
)
from forge_loop.po import expand_thin_specs as _po_expand
from forge_loop.runner._helpers import consume_force_set as _consume_force_set_impl
from forge_loop.runner._helpers import error_signature as _error_signature
from forge_loop.runner._helpers import force_retry_file as _force_retry_file_impl
from forge_loop.runner._helpers import reap_worktree as _reap_worktree
from forge_loop.runner.dispatch import (
    _run_critic_for_outcomes,
    _run_repair_workers,
    _run_workers,
)
from forge_loop.runner.drift import (
    _RECENT_OUTCOMES,
    _check_drift_and_maybe_halt,
    _maybe_deploy_drift_halt,
)
from forge_loop.runner.label_hygiene import remove_ready_label as _remove_ready_label_impl
from forge_loop.runner.repairs import blocking_pr_repairs as _blocking_pr_repairs_impl
from forge_loop.runner.repairs import (
    enable_automerge_for_repaired_prs as _enable_automerge_for_repaired_prs,
)
from forge_loop.runner.repairs import (
    ready_issue_open_pr_repairs as _ready_issue_open_pr_repairs_impl,
)
from forge_loop.runner.rescue import rescue_uncommitted_work as _rescue_uncommitted_work
from forge_loop.runner.tick_checks import run_codebase_audit as _run_codebase_audit
from forge_loop.runner.tick_checks import run_maintenance_tick as _run_maintenance_tick
from forge_loop.runner.tick_checks import run_stuck_sweep as _run_stuck_sweep
from forge_loop.state import append_event, consolidate_sprint, write_state
from forge_loop.worker import WorkerOutcome


def _force_retry_file(cfg: Config) -> Path:
    return _force_retry_file_impl(cfg.state_dir)


def _consume_force_set(cfg: Config) -> set[int]:
    return _consume_force_set_impl(cfg.state_dir)


def _issue_number_from_pr(pr: dict[str, Any]) -> int | None:
    from forge_loop.runner.repairs import issue_number_from_pr

    return issue_number_from_pr(pr)


def _blocking_pr_repairs(cfg: Config) -> list[tuple[dict[str, Any], dict[str, Any], str]]:
    return _blocking_pr_repairs_impl(
        cfg,
        prs_requiring_repair_fn=prs_requiring_repair,
        fetch_issue_fn=fetch_issue,
        pr_review_context_fn=pr_review_context,
    )


def _ready_issue_open_pr_repairs(
    cfg: Config,
    ready_issues: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any], str]]:
    return _ready_issue_open_pr_repairs_impl(
        cfg,
        ready_issues,
        open_prs_fn=prs_by_label,
        pr_review_context_fn=pr_review_context,
    )


def _remove_ready_label(
    cfg: Config,
    issue: int,
    *,
    status: str,
    pr_url: str | None = None,
) -> None:
    _remove_ready_label_impl(cfg, issue, status=status, pr_url=pr_url, unlabel_fn=unlabel)


def _record_merged_memory(cfg: Config, merged: list[WorkerOutcome]) -> None:
    """Best-effort: promote episodic memory from merged outcomes.

    Opens the canonical ``.forge/memory.db`` store and records one EPISODIC
    item per merged issue. Wrapped so a failure here never breaks the tick: it
    emits ``memory_promoted`` (count) on success or ``memory_promote_failed``
    (err) on any error.
    """
    if not merged:
        return
    try:
        from forge_loop.control.boot import canonical_task_saga_path
        from forge_loop.memory.store import SqliteMemoryStore
        from forge_loop.runner.learning import record_merged_outcomes

        memory_path = canonical_task_saga_path(cfg.repo).parent / "memory.db"
        store = SqliteMemoryStore(memory_path)
        promoted = record_merged_outcomes(store, merged)
        append_event(
            cfg.events_file,
            "memory_promoted",
            count=len(promoted),
            memory_ids=list(promoted),
        )
    except Exception as ex_:  # noqa: BLE001 — best-effort, must not break tick
        append_event(cfg.events_file, "memory_promote_failed", err=str(ex_)[:200])


def _enable_automerge_for_reviewed_outcomes(
    cfg: Config,
    outcomes: list[WorkerOutcome],
    *,
    risk_gated_issues: set[int],
    refused_issues: set[int],
) -> None:
    """Enable auto-merge only after critic and merge gates have passed."""
    from forge_loop import gh as _gh

    for outcome in outcomes:
        if outcome.status != "open" or not outcome.pr_url:
            continue
        if outcome.issue in risk_gated_issues or outcome.issue in refused_issues:
            continue
        if outcome.error:
            continue
        if _gh.enable_pr_auto_merge(outcome.pr_url, repo=cfg.github_repo):
            outcome.status = "merged"
            append_event(
                cfg.events_file,
                "post_critic_automerge_enabled",
                issue=outcome.issue,
                pr=outcome.pr_url,
            )
        else:
            append_event(
                cfg.events_file,
                "post_critic_automerge_failed",
                issue=outcome.issue,
                pr=outcome.pr_url,
            )


def _should_run_worker_iterations(cfg: Any, outcomes: Sequence[object]) -> bool:
    """Return whether a tick may dispatch follow-up worker iterations."""
    if cfg.worker_max_iterations <= 1 or not outcomes:
        return False
    return not (cfg.stop_file.exists() or cfg.pause_file.exists())


def _run_repair_tick(
    cfg: Config,
    tick: int,
    repairs: list[tuple[dict[str, Any], dict[str, Any], str]],
    *,
    bus_emit: Any,
    short_sleep: Any,
    start_event: str,
    done_event: str,
    log_action: str,
    remove_ready: bool,
) -> None:
    master_log_path = cfg.logs_dir / "master.log"
    issue_nums = [issue["number"] for issue, _, _ in repairs]
    write_state(
        cfg.state_file,
        {
            "state": "repairing",
            "tick": tick,
            "dispatched": [
                {"issue": issue["number"], "title": issue["title"]} for issue, _, _ in repairs
            ],
        },
    )
    append_event(
        cfg.events_file,
        start_event,
        tick=tick,
        issues=issue_nums,
        prs=[pr.get("url") for _, pr, _ in repairs],
    )
    _mlog.info(master_log_path, f"tick {tick} {log_action}: {issue_nums}")
    outcomes = _run_repair_workers(
        cfg,
        repairs,
        tick,
        master_log_path=master_log_path,
        bus_emit=bus_emit,
    )
    if cfg.critic.enabled:
        _run_critic_for_outcomes(cfg, outcomes, bus_emit)
    _enable_automerge_for_repaired_prs(cfg, outcomes, bus_emit)
    append_event(
        cfg.events_file,
        done_event,
        tick=tick,
        outcomes=[asdict(o) for o in outcomes],
    )
    for o in outcomes:
        if o.status in {"open", "merged"}:
            if remove_ready:
                _remove_ready_label(cfg, o.issue, status=o.status, pr_url=o.pr_url)
            _reap_worktree(cfg.repo, o.issue)
            append_event(cfg.events_file, "worktree_reaped", issue=o.issue, status=o.status)
    summary = consolidate_sprint(
        cfg.events_file,
        cfg.summaries_file,
        tick,
        [asdict(o) for o in outcomes],
    )
    write_state(
        cfg.state_file,
        {"state": "between-ticks", "tick": tick, "last_summary": summary},
    )
    short_sleep(cfg.tick_interval_s, cfg)


def _tick(cfg: Config, tick: int) -> None:
    # Imported lazily to avoid an import cycle (boot.py imports tick.py).
    from forge_loop.runner.boot import _short_sleep

    # Codebase-state audit (issue #156) — same cadence as maintenance.
    # Runs *before* the maintenance branch so it fires even when the
    # maintenance subagent is the body of the tick.
    if cfg.maintenance_every_n_ticks > 0 and tick % cfg.maintenance_every_n_ticks == 0:
        _run_codebase_audit(cfg, tick)

    if cfg.maintenance_every_n_ticks > 0 and tick % cfg.maintenance_every_n_ticks == 0:
        _run_maintenance_tick(cfg, tick)
        _short_sleep(cfg.tick_interval_s, cfg)
        return

    def _bus_emit(kind: str, payload: dict[str, Any]) -> None:
        append_event(cfg.events_file, kind, **payload)

    # Stuck-issue sweep (issue #129) — fires before the next dispatch
    # so any issue the iteration loop gave up on but failed to demote
    # gets caught here, not re-picked by top_issues below.
    _run_stuck_sweep(cfg, tick)

    repairs = _blocking_pr_repairs(cfg)
    if repairs:
        _run_repair_tick(
            cfg,
            tick,
            repairs,
            bus_emit=_bus_emit,
            short_sleep=_short_sleep,
            start_event="repair_tick_start",
            done_event="repair_tick_done",
            log_action="repairing blocked PR(s)",
            remove_ready=False,
        )
        return

    # Issue #126 — axis-aware dispatch filter. When ``LOOP_AXIS_FILTER``
    # is set (via ``forge-loop run --axis ...``), the dispatcher pulls a
    # wider window of ready issues than ``cfg.parallel`` so the filter
    # has something to chew on, then trims back to ``cfg.parallel`` from
    # the matched subset. When the env var is empty, behaviour is
    # byte-identical to today (same call, same limit).
    from forge_loop.axis import filter_issues_by_axes, parse_filter_env

    axis_filter = parse_filter_env()
    # When .forge/axes.yaml exists, default to filtering by ANY known
    # axis label so the legacy maintenance LLM (or stray ops) can't
    # smuggle non-axis-aligned issues onto the dispatch path. Dogfood-
    # caught: maintenance daemon re-labeled 4 cosmetic tickets as
    # loop:ready after the brainstormer had explicitly omitted them.
    # Explicit env override (LOOP_AXIS_FILTER) still wins.
    if not axis_filter:
        try:
            from forge_loop.product_vision import discover as _discover_vision

            _vision = _discover_vision(cfg.repo)
            axis_filter = sorted({a.name.lower() for a in _vision.axes})
            append_event(
                cfg.events_file,
                "axis_filter_auto_from_axes_yaml",
                tick=tick,
                axes=axis_filter,
            )
        except Exception:  # noqa: BLE001 — no axes.yaml or unreadable; preserve legacy
            axis_filter = []
    fetch_limit = max(cfg.parallel, 50) if axis_filter else cfg.parallel
    try:
        issues = top_issues(cfg.labels.ready, fetch_limit, repo=cfg.github_repo)
    except subprocess.CalledProcessError as e:
        append_event(cfg.events_file, "gh_list_failed", err=(e.stderr or "")[:200])
        write_state(cfg.state_file, {"state": "gh_error", "tick": tick})
        _short_sleep(60, cfg)
        return

    if axis_filter:
        append_event(
            cfg.events_file,
            "axis_filter_active",
            tick=tick,
            axes=axis_filter,
            candidates=len(issues),
        )
        issues = filter_issues_by_axes(issues, axis_filter)[: cfg.parallel]
        if not issues:
            append_event(
                cfg.events_file,
                "axis_filter_empty",
                tick=tick,
                axes=axis_filter,
            )

    if not issues:
        append_event(cfg.events_file, "tick_idle", tick=tick)
        write_state(
            cfg.state_file,
            {"state": "idle", "tick": tick, "next_check_s": cfg.tick_interval_s},
        )
        _short_sleep(cfg.tick_interval_s, cfg)
        return

    # PO spec-expansion pass (gap: workers ship janitor PRs when issue bodies
    # are thin; the PO subagent rewrites bodies to feature-grade specs before
    # dispatch). Idempotent — issues already expanded carry the marker.
    if cfg.po.enabled:
        write_state(cfg.state_file, {"state": "po_expanding", "tick": tick})
        append_event(cfg.events_file, "po_start", tick=tick, issues=[i["number"] for i in issues])
        po_outcomes = _po_expand(
            issues,
            cfg.repo,
            cfg.logs_dir,
            github_repo=cfg.github_repo or "",
            timeout_s=cfg.po.timeout_s,
            max_to_expand=cfg.po.max_to_expand_per_tick,
            model=cfg.po.model,
            provider=getattr(cfg.po, "provider", "claude"),
        )
        expanded_nums = [o.issue for o in po_outcomes if not o.skipped]
        append_event(
            cfg.events_file,
            "po_done",
            tick=tick,
            expanded=expanded_nums,
            skipped=[o.issue for o in po_outcomes if o.skipped],
            outcomes=[
                {
                    "issue": o.issue,
                    "skipped": o.skipped,
                    "reason": o.reason,
                    "sections_added": o.sections_added,
                    "duration_s": round(o.duration_s, 1),
                    "error": o.error,
                }
                for o in po_outcomes
            ],
        )
        # Re-fetch any issues whose bodies were just rewritten so the workers
        # see the new spec, not the stale snapshot we captured at tick start.
        if expanded_nums:
            refreshed = []
            for issue in issues:
                if issue["number"] in expanded_nums:
                    fresh = fetch_issue(issue["number"], repo=cfg.github_repo)
                    refreshed.append(fresh or issue)
                else:
                    refreshed.append(issue)
            issues = refreshed

    open_pr_repairs = _ready_issue_open_pr_repairs(cfg, issues)
    if open_pr_repairs:
        _run_repair_tick(
            cfg,
            tick,
            open_pr_repairs,
            bus_emit=_bus_emit,
            short_sleep=_short_sleep,
            start_event="ready_issue_open_pr_repair_tick_start",
            done_event="ready_issue_open_pr_repair_tick_done",
            log_action="repairing open PR(s)",
            remove_ready=True,
        )
        return

    write_state(
        cfg.state_file,
        {
            "state": "running",
            "tick": tick,
            "dispatched": [{"issue": i["number"], "title": i["title"]} for i in issues],
        },
    )
    append_event(cfg.events_file, "tick_start", tick=tick, issues=[i["number"] for i in issues])

    # Maestro step (additive, best-effort): let the durable frontier + curated
    # memory inform dispatch — reorder candidates (aligned first, rejected last)
    # and hand each worker an advisory context block. A control-plane read
    # failure leaves `issues` untouched and dispatch byte-identical to legacy.
    maestro_context = ""
    try:
        from forge_loop.runner.maestro import build_maestro_plan, load_maestro_inputs

        _frontier, _rejected = load_maestro_inputs(cfg)
        if _frontier is not None or _rejected:
            plan = build_maestro_plan(issues, frontier=_frontier, rejected_path_titles=_rejected)
            by_number = {i["number"]: i for i in issues}
            issues = [by_number[n] for n in plan.prioritized_issue_numbers]
            maestro_context = plan.brief_context
            append_event(cfg.events_file, "maestro_plan", tick=tick, **plan.event_payload())
    except Exception as ex_:  # noqa: BLE001 — the maestro step must never break the tick
        append_event(cfg.events_file, "maestro_plan_failed", tick=tick, err=str(ex_)[:200])
        maestro_context = ""

    # Per-issue: detect risk-gate + fetch past attempt history (if enabled).
    # Also apply the fingerprint-based skip guards (in-flight / cooldown) so
    # a half-finished prior dispatch doesn't get re-done and dupe a PR.
    risk_gate_label = cfg.labels.risk_gate
    workers_meta: list[dict[str, Any]] = []
    force_set = _consume_force_set(cfg)
    cooldown_s = _attempts.cooldown_from_env()
    brief_hash = _worker.brief_template_hash()
    issues_to_dispatch: list[dict[str, Any]] = []
    for i in issues:
        labels = [lab.get("name", "") for lab in (i.get("labels") or [])]
        gated = bool(risk_gate_label) and risk_gate_label in labels
        past: list[dict[str, Any]] = []
        blocking_comments: list[str] = []
        corrupt = 0
        if cfg.attempts.enabled:
            past, corrupt = _attempts.fetch_history_strict(
                i["number"],
                repo=cfg.github_repo,
            )
            blocking_comments = _attempts.fetch_blocking_comments(
                i["number"],
                repo=cfg.github_repo,
            )
            if corrupt:
                append_event(
                    cfg.events_file,
                    "attempts_corrupt",
                    issue=i["number"],
                    rows=corrupt,
                )
        fingerprint_body = i.get("body") or ""
        if blocking_comments:
            fingerprint_body = fingerprint_body + "\n\n" + "\n\n".join(blocking_comments)
        fp = _attempts.compute_fingerprint(i["number"], fingerprint_body, brief_hash)
        forced = i["number"] in force_set
        if cfg.attempts.enabled and not forced:
            decision = _attempts.classify_skip(
                past,
                fp,
                cooldown_s=cooldown_s,
            )
            if decision.kind == "in_flight":
                append_event(
                    cfg.events_file,
                    "worker_skip_in_flight",
                    issue=i["number"],
                    pr_url=decision.pr_url,
                    fingerprint=fp[:12],
                    matched_ts=decision.matched_ts,
                )
                _remove_ready_label(
                    cfg,
                    i["number"],
                    status="in_flight",
                    pr_url=decision.pr_url,
                )
                continue
            if decision.kind == "cooldown":
                append_event(
                    cfg.events_file,
                    "worker_skip_cooldown",
                    issue=i["number"],
                    fingerprint=fp[:12],
                    cooldown_remaining_s=decision.cooldown_remaining_s,
                    matched_ts=decision.matched_ts,
                )
                continue
        trimmed = past[-cfg.attempts.max_history_in_brief :] if past else []
        workers_meta.append(
            {
                "risk_gated": gated,
                "past_attempts": trimmed,
                "blocking_comments": blocking_comments,
                "brief_fingerprint": fp,
                "forced": forced,
            }
        )
        issues_to_dispatch.append(i)
    issues = issues_to_dispatch

    if not issues:
        # All candidates were skipped (in-flight or cooldown). Idle the tick.
        append_event(cfg.events_file, "tick_all_skipped", tick=tick)
        write_state(
            cfg.state_file,
            {"state": "idle", "tick": tick, "next_check_s": cfg.tick_interval_s},
        )
        _short_sleep(cfg.tick_interval_s, cfg)
        return

    risk_gated_issues = {
        issue["number"]
        for issue, meta in zip(issues, workers_meta, strict=True)
        if meta.get("risk_gated")
    }

    master_log_path = cfg.logs_dir / "master.log"
    _mlog.info(
        master_log_path,
        f"tick {tick} dispatching {len(issues)} worker(s): {[i['number'] for i in issues]}",
    )

    # forge-loop assumes Claude Code subscription-mode billing (flat). The
    # per-tick token-cost gate was removed in issue #38: it only made sense
    # under per-token billing, and the implementation was buggy under the
    # subscription operator persona we actually support.
    outcomes: list[WorkerOutcome]
    outcomes, _used_pipeline = _run_workers(
        cfg,
        issues,
        workers_meta,
        tick,
        master_log_path=master_log_path,
        bus_emit=_bus_emit,
        maestro_context=maestro_context,
    )

    # Issue #78 — worker iteration loop. For each outcome that didn't reach
    # ``merged`` on attempt 1, probe the worker state (DIRTY_NO_COMMIT,
    # COMMITTED_NOT_PUSHED, PUSHED_NO_PR, PR_OPEN_BLOCKED, PR_OPEN_CI_FAILED,
    # PR_OPEN_CONFLICT, PR_OPEN_HEALTHY, CLEAN_NOTHING) and dispatch a
    # focused follow-up worker session — up to ``cfg.worker_max_iterations``
    # attempts. After N attempts without merge, the issue gets labeled
    # ``loop:needs-human``.
    if _should_run_worker_iterations(cfg, outcomes):
        from forge_loop.runner.iteration import run_iteration_loop

        issue_by_n = {i["number"]: i for i in issues}
        for idx, o in enumerate(list(outcomes)):
            if o.status == "merged":
                continue
            # A worker that already opened a PR has completed the dispatch
            # contract. Let the normal critic / ready-label / merge-gate path
            # handle it instead of probing the worktree and accidentally
            # converting a good PR into a follow-up failure.
            if o.status == "open" and o.pr_url:
                continue
            issue_for_iteration = issue_by_n.get(o.issue)
            if issue_for_iteration is None:
                continue
            wt = Path(f"/tmp/wt-loop-{o.issue}")

            def _dispatch_follow_up(_issue: dict[str, Any], brief: str) -> WorkerOutcome:
                """Dispatch one follow-up worker session reusing the worktree."""
                from forge_loop.worker import run_worker

                return run_worker(
                    _issue,
                    cfg.repo,
                    cfg.logs_dir,
                    cfg.worker_timeout_s,
                    risk_gated=False,
                    past_attempts=[],
                    emit=_bus_emit,
                    lumen_top_k=cfg.lumen.top_k,
                    lumen_test_pattern=cfg.lumen_test_pattern,
                    coauthor=cfg.coauthor,
                    tick=tick,
                    model=cfg.worker.model,
                    thinking=cfg.worker.thinking,
                    provider=getattr(cfg.worker, "provider", "claude"),
                    allowed_mcp_servers=cfg.worker.allowed_mcp_tools,
                    load_timeout_ms=cfg.worker.load_timeout_ms,
                    strict_mcp_config=cfg.worker.strict_mcp_config,
                    mcp_servers=cfg.worker.mcp_servers,
                    base_branch=cfg.base_branch,
                    brief_override=brief,
                )

            try:
                new_outcome = run_iteration_loop(
                    o,
                    issue_for_iteration,
                    repo=cfg.github_repo or "",
                    base_branch=cfg.base_branch,
                    worktree=wt,
                    max_iterations=cfg.worker_max_iterations,
                    dispatch_worker=_dispatch_follow_up,
                    emit=_bus_emit,
                    coauthor=cfg.coauthor,
                )
                outcomes[idx] = new_outcome
            except Exception as ex_:  # never fail the tick on iteration loop bugs
                append_event(
                    cfg.events_file,
                    "worker_iteration_failed",
                    issue=o.issue,
                    err=str(ex_)[:200],
                )

    fingerprint_by_issue = {
        i["number"]: meta.get("brief_fingerprint", "")
        for i, meta in zip(issues, workers_meta, strict=True)
    }

    # Critic agent: review PRs the workers opened, before auto-merge fires.
    # In pipeline-driven mode the critic ran as a chain step already.
    if cfg.critic.enabled and not _used_pipeline:
        _run_critic_for_outcomes(cfg, outcomes, _bus_emit)

    for o in outcomes:
        if o.status in {"open", "merged"} and o.pr_url:
            _remove_ready_label(cfg, o.issue, status=o.status, pr_url=o.pr_url)

    # Issue #65 — pre-merge gate. AFTER the critic has had its say but
    # BEFORE we declare any outcome "merged", re-check that the source
    # issue is still OPEN. An operator who closed it mid-flight
    # (close-as-dup / not-planned / scope-change) wants the loop to STOP,
    # even if the worker raced to the finish. Conservative on gh failure:
    # refuse rather than risk landing a 1300-LOC refactor on a closed
    # ticket.
    from forge_loop import gh as _gh
    from forge_loop.runner.merge_gate import apply_issue_closed_gate

    refused = apply_issue_closed_gate(
        outcomes,
        gh=_gh,
        repo=cfg.github_repo,
        events_file=cfg.events_file,
        emit=_bus_emit,
    )
    _enable_automerge_for_reviewed_outcomes(
        cfg,
        outcomes,
        risk_gated_issues=risk_gated_issues,
        refused_issues=set(refused),
    )
    if refused:
        _mlog.info(
            master_log_path,
            f"merge gate refused {len(refused)} PR(s) — closed issues: {refused}",
        )

    # Persist this attempt as a GH issue comment after critic + merge gates so
    # the ledger reflects the real post-review state, not the worker's
    # optimistic pre-critic status.
    if cfg.attempts.enabled:
        for o in outcomes:
            try:
                _attempts.record(
                    o.issue,
                    status=o.status,
                    pr_url=o.pr_url,
                    duration_s=o.duration_s,
                    note=(o.error or "")[:200],
                    event_count=len(o.events or []),
                    repo=cfg.github_repo,
                    brief_fingerprint=fingerprint_by_issue.get(o.issue, ""),
                )
            except Exception as ex_:  # don't fail tick on history-write error
                append_event(
                    cfg.events_file, "attempt_record_failed", issue=o.issue, err=str(ex_)[:200]
                )

    merged_nums = [o.issue for o in outcomes if o.status == "merged"]

    # Close the cognition feedback loop: write durable episodic memory from
    # the real merged outcomes so future ticks/boots know what shipped. This
    # is strictly best-effort — a memory write failing must NEVER break the
    # tick, so the whole step is wrapped and only emits an event either way.
    _record_merged_memory(cfg, [o for o in outcomes if o.status == "merged"])

    append_event(
        cfg.events_file,
        "tick_done",
        tick=tick,
        merged=merged_nums,
        outcomes=[asdict(o) for o in outcomes],
    )
    write_state(
        cfg.state_file,
        {
            "state": "redeploying" if merged_nums else "finishing-tick",
            "tick": tick,
            "outcomes": [asdict(o) for o in outcomes],
        },
    )

    # Post-tick: auto-rescue uncommitted work, then reap.
    #
    # Real failure mode observed in dogfooding:
    # workers consume 50-90 turns writing + editing real implementation +
    # tests, then exit cleanly without ever running ``git commit``.
    # ``final_result.result == ""`` and there's no PR. With the old reap
    # policy the worktree was nuked and the work was lost ($16+ wasted in
    # one night across 3 issues).
    #
    # Fix: BEFORE reaping any non-merged worktree, check if it has
    # uncommitted changes. If yes, the loop AUTO-COMMITS + pushes + opens
    # a draft PR labelled ``loop:needs-review`` so the operator can pick
    # up the work. The outcome's pr_url + status get updated to reflect
    # the rescue. After rescue, the worktree gets reaped normally (the
    # work is on origin).
    _REAPABLE_STATUSES = frozenset({"merged", "open"})
    for o in outcomes:
        if o.status not in _REAPABLE_STATUSES:
            rescued = _rescue_uncommitted_work(o, cfg)
            if rescued is not None:
                # Rescue succeeded — outcome was mutated in place.
                # Treat as "open" so the reap proceeds normally.
                o.status = "open"
                o.pr_url = rescued
                append_event(
                    cfg.events_file,
                    "worker_work_rescued",
                    issue=o.issue,
                    pr=rescued,
                    hint="Worker exited dirty; loop auto-committed + opened draft PR. Review for completeness.",
                )

        if o.status in _REAPABLE_STATUSES:
            _reap_worktree(cfg.repo, o.issue)
            append_event(
                cfg.events_file,
                "worktree_reaped",
                issue=o.issue,
                status=o.status,
            )
        else:
            # Preserve for operator inspection (rescue declined the work —
            # e.g. no uncommitted changes, or push failed).
            wt_path = f"/tmp/wt-loop-{o.issue}"
            append_event(
                cfg.events_file,
                "worktree_preserved",
                issue=o.issue,
                status=o.status,
                path=wt_path,
                hint=(
                    f"Worker exited with status={o.status!r} and auto-rescue "
                    "either found no dirty changes or couldn't push. Inspect "
                    f"{wt_path} manually. Reaped at next loop boot unless "
                    "you `git worktree remove --force` it sooner."
                ),
            )

    if merged_nums and cfg.deploy_task:
        ok, log = redeploy(cfg.repo, cfg.deploy_task)
        append_event(cfg.events_file, "redeploy", task=cfg.deploy_task, ok=ok, detail=log)
        _maybe_deploy_drift_halt(cfg, ok)

    # Drift detector (gap #3): record outcome signature, halt if 3-in-a-row.
    had_workers = bool(outcomes)
    all_failed = had_workers and all(o.status not in {"merged", "open"} for o in outcomes)
    sig = (
        "ok"
        if not all_failed
        else _error_signature(
            outcomes[0].error if outcomes else None,
            outcomes[0].stdout_tail if outcomes else "",
        )
    )
    _RECENT_OUTCOMES.append((had_workers, all_failed, sig))
    if _check_drift_and_maybe_halt(cfg):
        return

    # End-of-tick consolidation — write a 1-line summary, flush noisy events.
    summary = consolidate_sprint(
        cfg.events_file,
        cfg.summaries_file,
        tick,
        [asdict(o) for o in outcomes],
    )
    append_event(cfg.events_file, "sprint_consolidated", **summary)

    write_state(cfg.state_file, {"state": "between-ticks", "tick": tick, "last_summary": summary})
    _short_sleep(cfg.tick_interval_s, cfg)
