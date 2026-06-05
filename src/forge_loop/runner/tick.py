"""The main ``_tick`` body and its immediate per-tick helpers.

Extracted from ``runner/__init__.py`` (issue #50). Pure mechanical move:
no behaviour change, no signature change.
"""

# ruff: noqa: I001

from __future__ import annotations

import functools
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
from forge_loop.gh_issues import (
    fetch_issue,
    open_prs,
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
    repair_slot_budget,
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
    LOOP_ADOPTED_LABEL as _LOOP_ADOPTED_LABEL,
)
from forge_loop.runner.repairs import (
    orphaned_clean_pr_adoptions as _orphaned_clean_pr_adoptions_impl,
)
from forge_loop.runner.repairs import (
    ready_issue_open_pr_repairs as _ready_issue_open_pr_repairs_impl,
)
from forge_loop.runner.repairs import (
    load_repair_tick_counter as _load_repair_tick_counter,
)
from forge_loop.runner.repairs import (
    save_repair_tick_counter as _save_repair_tick_counter,
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


def _orphaned_clean_pr_adoptions(
    cfg: Config,
) -> list[tuple[WorkerOutcome, dict[str, Any]]]:
    return _orphaned_clean_pr_adoptions_impl(
        cfg,
        open_prs_fn=open_prs,
        fetch_issue_fn=fetch_issue,
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
    from forge_loop import gh_issues as _gh

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


def _enable_automerge_for_adopted_prs(
    cfg: Config,
    adoptions: list[tuple[WorkerOutcome, dict[str, Any]]],
    *,
    refused_issues: set[int],
    emit: Any,
) -> None:
    """Put critic-approved, mergeable adopted PRs back on the merge conveyor.

    Issue #213, acceptance criterion 2/3. Mirrors
    ``_enable_automerge_for_reviewed_outcomes`` but adds the adoption-specific
    gates: skip if the critic just blocked (``outcome.error`` set), if the
    source issue closed mid-tick (``refused_issues``), or if the PR is not
    ``mergeStateStatus == CLEAN``. Every skip emits ``orphan_pr_skipped`` with
    a ``reason`` — no silent drop.

    Issue #230: leftover *critic* sev3 inline-comment threads are NOT a gate
    here — a critic-approved PR keeps them open forever, and gating on them held
    approved + CLEAN PRs back indefinitely (the #229 multi-hour stall). But an
    unresolved *human* request-changes thread DOES gate (AC3): it skips with
    ``reason="human_review_unresolved"``. The leftover critic-thread count is
    recorded on ``orphan_pr_automerge_enabled`` for visibility.
    """
    from forge_loop import gh_issues as _gh

    for outcome, pr in adoptions:
        if outcome.status != "open" or not outcome.pr_url:
            continue
        if outcome.issue in refused_issues:
            # The issue-closed gate already emitted merge_refused_issue_closed.
            continue
        if outcome.error:
            # The critic blocked this PR during adoption — leave it for the
            # repair loop (it now carries critic:blocking / critic:suspicious).
            append_event(
                cfg.events_file,
                "orphan_pr_skipped",
                issue=outcome.issue,
                pr=outcome.pr_url,
                reason="critic_blocked",
            )
            continue
        merge_state = str(pr.get("mergeStateStatus") or "").upper()
        # AC2: require mergeStateStatus == CLEAN. An absent/unknown state must
        # be treated as NOT mergeable (skip) — never bypass the gate. These
        # skips are transient: the PR is left UNstamped so the next adoption
        # scan re-evaluates it once it goes CLEAN (see the stamping rule below).
        if merge_state != "CLEAN":
            append_event(
                cfg.events_file,
                "orphan_pr_skipped",
                issue=outcome.issue,
                pr=outcome.pr_url,
                reason=f"not_mergeable:{merge_state.lower() or 'unknown'}",
            )
            continue
        # #230: an approved + CLEAN PR whose only open review threads are
        # leftover sev3 *critic* inline comments is TERMINAL — it must merge,
        # not be held back. Blocking on those threads is exactly what produced
        # the #229 multi-hour stall (a critic-approved PR keeps its sev3 threads
        # open forever, so the gate never lets it land). AC3, however, requires
        # an unresolved *human* request-changes thread to still hold the PR
        # back: a human inline-comment thread does NOT flip merge state off
        # CLEAN, so we must inspect authorship explicitly rather than trust
        # mergeStateStatus alone. ``human_unresolved_threads`` filters out the
        # critic's own leftover findings (the call is now load-bearing, not
        # decorative). GitHub branch protection still gates the real merge once
        # auto-merge is enabled.
        threads = _gh.unresolved_review_threads(outcome.pr_url, repo=cfg.github_repo)
        human_threads = _gh.human_unresolved_threads(threads)
        if human_threads:
            append_event(
                cfg.events_file,
                "orphan_pr_skipped",
                issue=outcome.issue,
                pr=outcome.pr_url,
                reason="human_review_unresolved",
                unresolved_human_threads=len(human_threads),
            )
            continue
        if _gh.enable_pr_auto_merge(outcome.pr_url, repo=cfg.github_repo):
            outcome.status = "merged"
            append_event(
                cfg.events_file,
                "orphan_pr_automerge_enabled",
                issue=outcome.issue,
                pr=outcome.pr_url,
                over_unresolved_critic_threads=len(threads),
            )
        else:
            append_event(
                cfg.events_file,
                "orphan_pr_automerge_failed",
                issue=outcome.issue,
                pr=outcome.pr_url,
            )


def _run_adoption_tick(
    cfg: Config,
    tick: int,
    adoptions: list[tuple[WorkerOutcome, dict[str, Any]]],
    *,
    bus_emit: Any,
    short_sleep: Any,
) -> None:
    """Adopt orphaned clean PRs: re-critic (if no verdict) + merge-gate (#213).

    Never spawns a worker — adoption is critic + merge-gate only (out of
    scope: repair-worker dispatch). Idempotent: a ``loop:adopted`` label is
    stamped on each successfully-adopted PR so a re-run of this scan excludes
    it (no duplicate critic runs, no double auto-merge).
    """
    from forge_loop import gh_issues as _gh
    from forge_loop.runner.merge_gate import apply_issue_closed_gate

    master_log_path = cfg.logs_dir / "master.log"
    outcomes = [o for o, _pr in adoptions]
    issue_nums = [o.issue for o in outcomes]
    write_state(
        cfg.state_file,
        {
            "state": "adopting",
            "tick": tick,
            "dispatched": [{"issue": o.issue, "pr": o.pr_url} for o in outcomes],
        },
    )
    append_event(
        cfg.events_file,
        "orphan_pr_adoption_tick_start",
        tick=tick,
        issues=issue_nums,
        prs=[o.pr_url for o in outcomes],
    )
    _mlog.info(master_log_path, f"tick {tick} adopting orphaned PR(s): {issue_nums}")
    for o in outcomes:
        append_event(cfg.events_file, "orphan_pr_adopted", issue=o.issue, pr=o.pr_url)

    # Re-critic. The selector already excluded PRs that carry a verdict
    # (critic:blocking/suspicious) or were previously adopted, so this never
    # re-runs the critic on a PR that already has one.
    if cfg.critic.enabled:
        _run_critic_for_outcomes(cfg, outcomes, bus_emit)

    # Pre-merge issue-closed gate (defence-in-depth for an issue closed
    # between the adoption scan and this merge step).
    refused = apply_issue_closed_gate(
        outcomes,
        gh=_gh,
        repo=cfg.github_repo,
        events_file=cfg.events_file,
        emit=bus_emit,
    )

    _enable_automerge_for_adopted_prs(
        cfg,
        adoptions,
        refused_issues=set(refused),
        emit=bus_emit,
    )

    # Idempotency marker: stamp ONLY PRs that reached a terminal adoption
    # outcome — auto-merged (status=="merged") or critic-blocked (error set;
    # the critic:blocking label already excludes them and the repair loop owns
    # them). PRs skipped for TRANSIENT reasons (not_mergeable:<state>,
    # unresolved_review_threads, automerge enable failed) are left UNstamped so
    # the next scan re-evaluates them once they go CLEAN / threads resolve —
    # otherwise the marker would permanently re-orphan the PRs this feature
    # exists to rescue (issue #213 regression caught in review).
    refused_set = set(refused)
    for o in outcomes:
        terminal = o.status == "merged" or bool(o.error)
        if o.pr_url and terminal and o.issue not in refused_set:
            _gh.add_pr_label(o.pr_url, [_LOOP_ADOPTED_LABEL], repo=cfg.github_repo)

    append_event(
        cfg.events_file,
        "orphan_pr_adoption_tick_done",
        tick=tick,
        outcomes=[asdict(o) for o in outcomes],
    )
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


# --------------------------------------------------------------------------- #
# Issue #225 — ``_tick`` decomposition.
#
# ``_tick`` was a ~580-line god-function (the program's central control flow).
# It is now a readable orchestrator that calls named, individually-testable
# phase helpers below. Each helper owns one phase of the tick and is small
# enough to unit-test in isolation. Behaviour, ordering and emitted events are
# byte-for-byte unchanged from the pre-#225 inline body — this was a pure
# mechanical extraction, no semantics moved.
# --------------------------------------------------------------------------- #


def _maybe_run_maintenance(cfg: Config, tick: int, *, short_sleep: Any) -> bool:
    """Codebase audit (#156) + maintenance sub-tick on the maintenance cadence.

    The audit runs *before* the maintenance branch so it fires even when the
    maintenance subagent is the body of the tick. Returns True when the
    maintenance sub-tick ran and the caller must return immediately.
    """
    if cfg.maintenance_every_n_ticks > 0 and tick % cfg.maintenance_every_n_ticks == 0:
        _run_codebase_audit(cfg, tick)
    if cfg.maintenance_every_n_ticks > 0 and tick % cfg.maintenance_every_n_ticks == 0:
        _run_maintenance_tick(cfg, tick)
        short_sleep(cfg.tick_interval_s, cfg)
        return True
    return False


def _any_ready_issue(cfg: Config) -> bool:
    """Cheap probe: are there any ``loop:ready`` issues waiting? (issue #248).

    Used by the dispatch-slot reservation to decide whether a blocking-PR
    repair tick should yield to new dispatch. A ``gh`` failure is treated as
    "no ready work" so the reservation never starves repairs on a transient
    list error (the normal candidate fetch later in the tick surfaces it).
    """
    try:
        return bool(top_issues(cfg.labels.ready, 1, repo=cfg.github_repo))
    except Exception:  # noqa: BLE001
        return False


def _repair_reserve_should_yield(
    cfg: Config,
    tick: int,
    repairs: list[tuple[dict[str, Any], dict[str, Any], str]],
) -> bool:
    """Decide whether to yield this tick to new dispatch instead of repairs.

    Issue #248 forward-progress guarantee. When repair fair-scheduling is
    enabled and ready issues are waiting, we track consecutive repair-terminal
    ticks; after ``reserve_dispatch_after_ticks`` of them in a row we yield the
    tick to new dispatch (reserving its worker slots), guaranteeing a ready
    issue is dispatched within ``reserve_dispatch_after_ticks + 1`` ticks. The
    counter resets whenever there is no ready work (no starvation risk) so the
    legacy "repairs run every tick" behaviour holds when the backlog is empty.

    Disabled (``repair.enabled`` False / ``reserve_dispatch_after_ticks<=0``)
    ⇒ always returns False (byte-identical legacy behaviour).
    """
    if not cfg.repair.enabled or cfg.repair.reserve_dispatch_after_ticks <= 0:
        return False
    counter_file = cfg.repair_scheduler_file
    if not _any_ready_issue(cfg):
        # No ready work waiting — repairs cannot starve anything. Reset.
        _save_repair_tick_counter(counter_file, 0)
        return False
    consecutive = _load_repair_tick_counter(counter_file)
    if consecutive >= cfg.repair.reserve_dispatch_after_ticks:
        _save_repair_tick_counter(counter_file, 0)
        _, reserved = repair_slot_budget(
            cfg.parallel, len(repairs), ready_present=True, reserve=1
        )
        append_event(
            cfg.events_file,
            "repair_dispatch_slot_reserved",
            tick=tick,
            consecutive_repair_ticks=consecutive,
            repairs_deferred=len(repairs),
            dispatch_slots_reserved=reserved,
        )
        return True
    _save_repair_tick_counter(counter_file, consecutive + 1)
    return False


def _run_pre_dispatch_repairs(
    cfg: Config,
    tick: int,
    *,
    bus_emit: Any,
    short_sleep: Any,
) -> bool:
    """Stuck-issue sweep (#129) + blocking-PR repair + orphaned-clean-PR adoption.

    Each repair/adoption path is a terminal tick body: when one runs, the caller
    must return immediately. Returns True if any of them ran. The stuck sweep
    fires first so an issue the iteration loop gave up on gets caught here, not
    re-picked by ``top_issues`` later in the tick.
    """
    _run_stuck_sweep(cfg, tick)

    repairs = _blocking_pr_repairs(cfg)
    if not repairs and cfg.repair.enabled:
        # No blocking repairs ⇒ no starvation pressure; clear the consecutive
        # repair-tick counter so a future repair streak starts from zero.
        _save_repair_tick_counter(cfg.repair_scheduler_file, 0)
    if repairs and _repair_reserve_should_yield(cfg, tick, repairs):
        # Issue #248: reserve this tick for new dispatch so in-flight repairs
        # cannot starve the ready backlog. The blocking PRs are simply not
        # repaired this tick; they are re-selected next tick (subject to the
        # per-PR backoff). Fall through to candidate selection / dispatch.
        return False
    if repairs:
        _run_repair_tick(
            cfg,
            tick,
            repairs,
            bus_emit=bus_emit,
            short_sleep=short_sleep,
            start_event="repair_tick_start",
            done_event="repair_tick_done",
            log_action="repairing blocked PR(s)",
            remove_ready=False,
        )
        return True

    # Issue #213 — adopt orphaned clean PRs. A worker can open its PR and then
    # trip ``worker_timeout_s`` before the post-critic merge step runs, leaving
    # a CLEAN / never-critic'd PR open forever (it matches neither the blocking
    # nor the ready-issue repair selectors). Re-critic + merge-gate it here.
    adoptions = _orphaned_clean_pr_adoptions(cfg)
    if adoptions:
        _run_adoption_tick(
            cfg,
            tick,
            adoptions,
            bus_emit=bus_emit,
            short_sleep=short_sleep,
        )
        return True
    return False


def _resolve_axis_filter(cfg: Config, tick: int) -> list[str]:
    """Resolve the active axis filter (issue #126).

    ``LOOP_AXIS_FILTER`` (set via ``forge-loop run --axis ...``) wins. Otherwise,
    when ``.forge/axes.yaml`` exists, default to filtering by ANY known axis
    label so the legacy maintenance LLM (or stray ops) can't smuggle
    non-axis-aligned issues onto the dispatch path. No axes.yaml ⇒ empty filter
    ⇒ byte-identical legacy behaviour.
    """
    from forge_loop.axis import parse_filter_env

    axis_filter = parse_filter_env()
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
    return axis_filter


def _select_candidates(
    cfg: Config,
    tick: int,
    *,
    short_sleep: Any,
) -> list[dict[str, Any]] | None:
    """Resolve the axis filter, fetch ready issues, apply the filter.

    Returns the candidate issue list, or ``None`` when the tick should end now:
    a ``gh`` list failure (emits ``gh_list_failed``) or no candidates after
    filtering (emits ``tick_idle``). When a filter is active the fetch window is
    widened to ``max(parallel, 50)`` so the filter has something to chew on,
    then trimmed back to ``cfg.parallel``.
    """
    from forge_loop.axis import filter_issues_by_axes

    axis_filter = _resolve_axis_filter(cfg, tick)
    fetch_limit = max(cfg.parallel, 50) if axis_filter else cfg.parallel
    try:
        issues = top_issues(cfg.labels.ready, fetch_limit, repo=cfg.github_repo)
    except subprocess.CalledProcessError as e:
        append_event(cfg.events_file, "gh_list_failed", err=(e.stderr or "")[:200])
        write_state(cfg.state_file, {"state": "gh_error", "tick": tick})
        short_sleep(60, cfg)
        return None

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
            append_event(cfg.events_file, "axis_filter_empty", tick=tick, axes=axis_filter)

    if not issues:
        append_event(cfg.events_file, "tick_idle", tick=tick)
        write_state(
            cfg.state_file,
            {"state": "idle", "tick": tick, "next_check_s": cfg.tick_interval_s},
        )
        short_sleep(cfg.tick_interval_s, cfg)
        return None
    return issues


def _expand_specs(cfg: Config, tick: int, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """PO spec-expansion pass (rewrites thin issue bodies to feature-grade specs).

    Idempotent — issues already expanded carry the marker. Returns ``issues``,
    with any rewritten bodies re-fetched so workers see the new spec, not the
    stale snapshot captured at tick start. No-op when ``cfg.po`` is disabled.
    """
    if not cfg.po.enabled:
        return issues
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
    if expanded_nums:
        refreshed = []
        for issue in issues:
            if issue["number"] in expanded_nums:
                fresh = fetch_issue(issue["number"], repo=cfg.github_repo)
                refreshed.append(fresh or issue)
            else:
                refreshed.append(issue)
        issues = refreshed
    return issues


def _run_ready_issue_repairs(
    cfg: Config,
    tick: int,
    issues: list[dict[str, Any]],
    *,
    bus_emit: Any,
    short_sleep: Any,
) -> bool:
    """Repair open PRs attached to ready issues; terminal tick body if any run."""
    open_pr_repairs = _ready_issue_open_pr_repairs(cfg, issues)
    if open_pr_repairs:
        _run_repair_tick(
            cfg,
            tick,
            open_pr_repairs,
            bus_emit=bus_emit,
            short_sleep=short_sleep,
            start_event="ready_issue_open_pr_repair_tick_start",
            done_event="ready_issue_open_pr_repair_tick_done",
            log_action="repairing open PR(s)",
            remove_ready=True,
        )
        return True
    return False


def _apply_maestro_plan(
    cfg: Config,
    tick: int,
    issues: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """Maestro step (additive, best-effort): reorder candidates + build a brief.

    Lets the durable frontier + curated memory inform dispatch (aligned first,
    rejected last) and hand each worker an advisory context block. A control-
    plane read failure leaves ``issues`` untouched and dispatch byte-identical
    to legacy. Returns ``(issues, maestro_context)``.
    """
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
    return issues, maestro_context


def _classify_issue_for_dispatch(
    cfg: Config,
    i: dict[str, Any],
    *,
    force_set: set[int],
    cooldown_s: int,
    brief_hash: str,
    risk_gate_label: str,
) -> dict[str, Any] | None:
    """Build the ``workers_meta`` entry for one candidate, or ``None`` to skip it.

    Detects the risk gate, fetches past attempt history + blocking comments
    (one ``gh issue view --comments`` round-trip), computes the brief
    fingerprint, then applies the fingerprint-based skip guards. Returns a meta
    dict to dispatch the issue, or ``None`` to skip it this tick — emitting
    ``worker_skip_in_flight`` (and dropping the ready label) or
    ``worker_skip_cooldown`` exactly as the inline loop did. ``forced`` issues
    bypass the skip guards.
    """
    labels = [lab.get("name", "") for lab in (i.get("labels") or [])]
    gated = bool(risk_gate_label) and risk_gate_label in labels
    past: list[dict[str, Any]] = []
    blocking_comments: list[str] = []
    corrupt = 0
    if cfg.attempts.enabled:
        attempts_view = _attempts.fetch_issue_attempts(i["number"], repo=cfg.github_repo)
        past, corrupt = attempts_view.history, attempts_view.corrupt
        blocking_comments = attempts_view.blocking_comments
        if corrupt:
            append_event(cfg.events_file, "attempts_corrupt", issue=i["number"], rows=corrupt)
    fingerprint_body = i.get("body") or ""
    if blocking_comments:
        fingerprint_body = fingerprint_body + "\n\n" + "\n\n".join(blocking_comments)
    fp = _attempts.compute_fingerprint(i["number"], fingerprint_body, brief_hash)
    forced = i["number"] in force_set
    if cfg.attempts.enabled and not forced:
        decision = _attempts.classify_skip(past, fp, cooldown_s=cooldown_s)
        if decision.kind == "in_flight":
            append_event(
                cfg.events_file,
                "worker_skip_in_flight",
                issue=i["number"],
                pr_url=decision.pr_url,
                fingerprint=fp[:12],
                matched_ts=decision.matched_ts,
            )
            _remove_ready_label(cfg, i["number"], status="in_flight", pr_url=decision.pr_url)
            return None
        if decision.kind == "cooldown":
            append_event(
                cfg.events_file,
                "worker_skip_cooldown",
                issue=i["number"],
                fingerprint=fp[:12],
                cooldown_remaining_s=decision.cooldown_remaining_s,
                matched_ts=decision.matched_ts,
            )
            return None
    trimmed = past[-cfg.attempts.max_history_in_brief :] if past else []
    return {
        "risk_gated": gated,
        "past_attempts": trimmed,
        "blocking_comments": blocking_comments,
        "brief_fingerprint": fp,
        "forced": forced,
    }


def _select_dispatch_set(
    cfg: Config,
    issues: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Filter candidates through the per-issue skip guards.

    Returns ``(issues_to_dispatch, workers_meta)``, aligned index-for-index:
    ``workers_meta[k]`` is the dispatch meta for ``issues_to_dispatch[k]``.
    Issues skipped by ``_classify_issue_for_dispatch`` are dropped from both.
    """
    force_set = _consume_force_set(cfg)
    cooldown_s = _attempts.cooldown_from_env()
    brief_hash = _worker.brief_template_hash()
    risk_gate_label = cfg.labels.risk_gate
    issues_to_dispatch: list[dict[str, Any]] = []
    workers_meta: list[dict[str, Any]] = []
    for i in issues:
        meta = _classify_issue_for_dispatch(
            cfg,
            i,
            force_set=force_set,
            cooldown_s=cooldown_s,
            brief_hash=brief_hash,
            risk_gate_label=risk_gate_label,
        )
        if meta is None:
            continue
        workers_meta.append(meta)
        issues_to_dispatch.append(i)
    return issues_to_dispatch, workers_meta


def _dispatch_follow_up_worker(
    _issue: dict[str, Any],
    brief: str,
    *,
    cfg: Config,
    tick: int,
    bus_emit: Any,
) -> WorkerOutcome:
    """Dispatch one follow-up worker session reusing the worktree.

    Module-level (issue #225): this was a closure defined *inside* the dispatch
    ``for`` loop, which re-created the function object per outcome and captured
    the loop variables implicitly. It now captures nothing implicitly — the
    caller binds ``cfg``/``tick``/``bus_emit`` explicitly via
    ``functools.partial``, yielding the ``(issue, brief) -> WorkerOutcome``
    callable ``run_iteration_loop`` expects.
    """
    from forge_loop.worker import run_worker

    return run_worker(
        _issue,
        cfg.repo,
        cfg.logs_dir,
        cfg.worker_timeout_s,
        risk_gated=False,
        past_attempts=[],
        emit=bus_emit,
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
        permissions=getattr(cfg.worker, "permissions", "full"),
    )


def _run_worker_iterations(
    cfg: Config,
    tick: int,
    issues: list[dict[str, Any]],
    outcomes: list[WorkerOutcome],
    *,
    bus_emit: Any,
) -> None:
    """Worker iteration loop (issue #78). Mutates ``outcomes`` in place.

    For each outcome that didn't reach ``merged`` (and hasn't already opened a
    PR), probe the worker state and dispatch a focused follow-up session — up to
    ``cfg.worker_max_iterations`` attempts. A worker that already opened a PR has
    met the dispatch contract; leave it for the normal critic / merge-gate path.
    Iteration-loop bugs emit ``worker_iteration_failed`` and never fail the tick.
    """
    if not _should_run_worker_iterations(cfg, outcomes):
        return
    from forge_loop.runner.iteration import run_iteration_loop
    from forge_loop.worker_worktree import worktree_path

    issue_by_n = {i["number"]: i for i in issues}
    dispatch = functools.partial(_dispatch_follow_up_worker, cfg=cfg, tick=tick, bus_emit=bus_emit)
    for idx, o in enumerate(list(outcomes)):
        if o.status == "merged":
            continue
        if o.status == "open" and o.pr_url:
            continue
        issue_for_iteration = issue_by_n.get(o.issue)
        if issue_for_iteration is None:
            continue
        wt = worktree_path(cfg.repo, o.issue)
        try:
            new_outcome = run_iteration_loop(
                o,
                issue_for_iteration,
                repo=cfg.github_repo or "",
                base_branch=cfg.base_branch,
                worktree=wt,
                max_iterations=cfg.worker_max_iterations,
                dispatch_worker=dispatch,
                emit=bus_emit,
                coauthor=cfg.coauthor,
            )
            outcomes[idx] = new_outcome
        except Exception as ex_:  # noqa: BLE001 — never fail the tick on iteration loop bugs
            append_event(
                cfg.events_file,
                "worker_iteration_failed",
                issue=o.issue,
                err=str(ex_)[:200],
            )


def _dispatch_and_iterate(
    cfg: Config,
    tick: int,
    issues: list[dict[str, Any]],
    workers_meta: list[dict[str, Any]],
    *,
    bus_emit: Any,
    maestro_context: str,
    master_log_path: Path,
) -> tuple[list[WorkerOutcome], bool]:
    """Dispatch the worker fleet, then run the follow-up iteration loop.

    Returns ``(outcomes, used_pipeline)``. ``used_pipeline`` tells the merge gate
    whether the critic already ran as a chain step (pipeline-driven mode).
    """
    outcomes, used_pipeline = _run_workers(
        cfg,
        issues,
        workers_meta,
        tick,
        master_log_path=master_log_path,
        bus_emit=bus_emit,
        maestro_context=maestro_context,
    )
    _run_worker_iterations(cfg, tick, issues, outcomes, bus_emit=bus_emit)
    return outcomes, used_pipeline


def _run_merge_gate(
    cfg: Config,
    outcomes: list[WorkerOutcome],
    *,
    risk_gated_issues: set[int],
    used_pipeline: bool,
    bus_emit: Any,
    master_log_path: Path,
) -> None:
    """Critic → ready-label cleanup → issue-closed gate → enable auto-merge.

    Issue #65: the pre-merge issue-closed gate runs AFTER the critic has had its
    say but BEFORE any outcome is declared ``merged`` — an operator who closed
    the issue mid-flight (dup / not-planned / scope-change) wants the loop to
    STOP, and the gate is conservative on ``gh`` failure (refuse rather than
    land work on a closed ticket).
    """
    if cfg.critic.enabled and not used_pipeline:
        _run_critic_for_outcomes(cfg, outcomes, bus_emit)

    for o in outcomes:
        if o.status in {"open", "merged"} and o.pr_url:
            _remove_ready_label(cfg, o.issue, status=o.status, pr_url=o.pr_url)

    from forge_loop import gh_issues as _gh
    from forge_loop.runner.merge_gate import apply_issue_closed_gate

    refused = apply_issue_closed_gate(
        outcomes,
        gh=_gh,
        repo=cfg.github_repo,
        events_file=cfg.events_file,
        emit=bus_emit,
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


def _record_attempts(
    cfg: Config,
    outcomes: list[WorkerOutcome],
    *,
    fingerprint_by_issue: dict[int, str],
) -> None:
    """Persist each attempt as a GH issue comment, post critic + merge gates.

    Done after review so the ledger reflects the real post-review state, not the
    worker's optimistic pre-critic status. A history-write error never fails the
    tick — it emits ``attempt_record_failed`` instead.
    """
    if not cfg.attempts.enabled:
        return
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
        except Exception as ex_:  # noqa: BLE001 — don't fail tick on history-write error
            append_event(
                cfg.events_file, "attempt_record_failed", issue=o.issue, err=str(ex_)[:200]
            )


def _rescue_and_reap(cfg: Config, outcomes: list[WorkerOutcome]) -> None:
    """Auto-rescue uncommitted work, then reap (or preserve) each worktree.

    Real failure mode observed in dogfooding: workers write real implementation
    + tests over 50-90 turns then exit cleanly without ever running
    ``git commit`` — ``final_result.result == ""`` and no PR. BEFORE reaping any
    non-merged worktree, check for uncommitted changes; if present, auto-commit
    + push + open a draft PR labelled ``loop:needs-review`` and treat as
    ``open`` so the reap proceeds. Worktrees that decline rescue (no dirty
    changes / push failed) are preserved for operator inspection.
    """
    reapable = frozenset({"merged", "open"})
    for o in outcomes:
        if o.status not in reapable:
            rescued = _rescue_uncommitted_work(o, cfg)
            if rescued is not None:
                o.status = "open"
                o.pr_url = rescued
                append_event(
                    cfg.events_file,
                    "worker_work_rescued",
                    issue=o.issue,
                    pr=rescued,
                    hint="Worker exited dirty; loop auto-committed + opened draft PR. Review for completeness.",
                )

        if o.status in reapable:
            _reap_worktree(cfg.repo, o.issue)
            append_event(cfg.events_file, "worktree_reaped", issue=o.issue, status=o.status)
        else:
            from forge_loop.worker_worktree import worktree_path

            wt_path = str(worktree_path(cfg.repo, o.issue))
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


def _finalize_tick(
    cfg: Config,
    tick: int,
    outcomes: list[WorkerOutcome],
    *,
    fingerprint_by_issue: dict[int, str],
    short_sleep: Any,
) -> None:
    """Record attempts → promote memory → reap → redeploy → drift → consolidate.

    The terminal phase of a dispatching tick. If the drift detector halts the
    loop (3 identical failures in a row) the consolidation/sleep tail is skipped
    via an early return, exactly as the inline body did.
    """
    _record_attempts(cfg, outcomes, fingerprint_by_issue=fingerprint_by_issue)

    merged_nums = [o.issue for o in outcomes if o.status == "merged"]

    # Close the cognition feedback loop: durable episodic memory from real
    # merged outcomes. Strictly best-effort — never breaks the tick.
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

    _rescue_and_reap(cfg, outcomes)

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
    short_sleep(cfg.tick_interval_s, cfg)


def _tick(cfg: Config, tick: int) -> None:
    """Orchestrate one loop tick.

    Reads as a sequence of named phases (issue #225 decomposition). Each phase
    helper owns one slice of the tick and is unit-testable in isolation; this
    body only wires them together and handles the early-return control flow.
    """
    # Imported lazily to avoid an import cycle (boot.py imports tick.py).
    from forge_loop.runner.boot import _short_sleep

    def _bus_emit(kind: str, payload: dict[str, Any]) -> None:
        append_event(cfg.events_file, kind, **payload)

    if _maybe_run_maintenance(cfg, tick, short_sleep=_short_sleep):
        return

    if _run_pre_dispatch_repairs(cfg, tick, bus_emit=_bus_emit, short_sleep=_short_sleep):
        return

    issues = _select_candidates(cfg, tick, short_sleep=_short_sleep)
    if issues is None:
        return

    issues = _expand_specs(cfg, tick, issues)

    if _run_ready_issue_repairs(cfg, tick, issues, bus_emit=_bus_emit, short_sleep=_short_sleep):
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

    issues, maestro_context = _apply_maestro_plan(cfg, tick, issues)

    issues, workers_meta = _select_dispatch_set(cfg, issues)
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
    fingerprint_by_issue = {
        i["number"]: meta.get("brief_fingerprint", "")
        for i, meta in zip(issues, workers_meta, strict=True)
    }

    master_log_path = cfg.logs_dir / "master.log"
    _mlog.info(
        master_log_path,
        f"tick {tick} dispatching {len(issues)} worker(s): {[i['number'] for i in issues]}",
    )

    outcomes, used_pipeline = _dispatch_and_iterate(
        cfg,
        tick,
        issues,
        workers_meta,
        bus_emit=_bus_emit,
        maestro_context=maestro_context,
        master_log_path=master_log_path,
    )

    _run_merge_gate(
        cfg,
        outcomes,
        risk_gated_issues=risk_gated_issues,
        used_pipeline=used_pipeline,
        bus_emit=_bus_emit,
        master_log_path=master_log_path,
    )

    _finalize_tick(
        cfg,
        tick,
        outcomes,
        fingerprint_by_issue=fingerprint_by_issue,
        short_sleep=_short_sleep,
    )
