"""Worker spawning, critic-loop wiring, and multirepo dispatch glue.

Extracted from ``runner/__init__.py`` (issue #50). Pure mechanical move:
no behaviour change, no signature change.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from forge_loop import gh as _gh
from forge_loop import master_log as _mlog
from forge_loop.config import Config
from forge_loop.critic import review_pr as _critic_review
from forge_loop.critic_actions import apply_critic_report
from forge_loop.state import append_event
from forge_loop.worker import WorkerOutcome, run_repair_worker, run_worker
from forge_loop.worker_sessions import WorkerSessionStore


def free_dispatch_slots(store: WorkerSessionStore, parallel: int) -> int:
    """Return the number of free parallel dispatch slots on this tick.

    This is the headline efficiency win of issue #112 (toward epic #95):
    the dispatcher reads :meth:`WorkerSessionStore.active_count` — which
    only counts ``RUNNING`` and ``REVISING`` sessions — instead of an
    in-memory ``len(self._running_futures)`` that would also count
    paused ``AWAITING_CRITIC`` sessions against the budget.

    A session sitting in ``AWAITING_CRITIC`` has handed control back to
    the critic agent; it is not consuming a worker thread. Counting it
    against ``parallel`` starves the loop of forward progress: with
    ``parallel=3`` and three AWAITING_CRITIC sessions, the legacy
    accounting would refuse to dispatch ANY new worker until the
    critic finished. The store-based accounting frees those slots
    immediately, so the dispatcher can fill them with fresh work.

    Args:
        store: the SQLite session store driving the runner.
        parallel: the configured worker-parallelism cap.

    Returns:
        ``max(0, parallel - store.active_count())``. The ``max(0, …)``
        guards against transient over-subscription (e.g. an operator
        lowering ``parallel`` mid-run with live workers): the dispatcher
        simply refuses to launch more until the count drains, instead
        of returning a negative number that downstream callers would
        have to special-case.
    """
    return max(0, parallel - store.active_count())


def _sev_counts(outcome: Any) -> dict[str, int]:
    """Tally sev1/sev2/sev3 from a CriticOutcome.report. Safe on None."""
    report = getattr(outcome, "report", None)
    counts = {"sev1": 0, "sev2": 0, "sev3": 0}
    if report is None:
        return counts
    for f in report.findings:
        if f.severity in counts:
            counts[f.severity] += 1
    return counts


def _run_workers(
    cfg: Config,
    issues: list[dict[str, Any]],
    workers_meta: list[dict[str, Any]],
    tick: int,
    master_log_path: Path,
    bus_emit: Any,
) -> tuple[list[WorkerOutcome], bool]:
    """Spawn workers (pipeline-driven if enabled, else legacy ThreadPool).

    Returns ``(outcomes, used_pipeline)``. When ``used_pipeline`` is True the
    critic step ran inside the pipeline chain and the caller must skip the
    legacy critic block.
    """
    outcomes: list[WorkerOutcome] = []
    dispatch = list(zip(issues, workers_meta, strict=True))

    # Issue #49 — pipeline-driven dispatch path. When `.forge/pipeline.yaml`
    # is present AND the operator opted in via LOOP_PIPELINE_DRIVEN=1, route
    # the per-issue worker/critic chain through pipeline.executor.run()
    # instead of the legacy hardcoded ThreadPoolExecutor → critic loop
    # below. The legacy path remains the default. The pipeline path emits
    # its own per-step events and applies the critic via the chain itself,
    # so the post-worker critic block further down is skipped when this
    # path takes over.
    from forge_loop.runner._pipeline_driver import (
        dispatch_via_pipeline as _pipeline_dispatch,
    )
    from forge_loop.runner._pipeline_driver import (
        pipeline_driven_enabled as _pipeline_enabled,
    )

    used_pipeline = False
    if _pipeline_enabled(cfg):
        used_pipeline = True
        append_event(
            cfg.events_file,
            "pipeline_dispatch_start",
            tick=tick,
            issues=[i["number"] for i in issues],
        )
        try:
            outcomes = _pipeline_dispatch(
                cfg,
                issues,
                workers_meta,
                tick,
                master_log_path=master_log_path,
                bus_emit=bus_emit,
            )
        except Exception as ex_:  # noqa: BLE001 — must not kill the tick
            append_event(cfg.events_file, "pipeline_dispatch_failed", tick=tick, err=str(ex_)[:300])
            # Fall back to the legacy chain so a broken pipeline.yaml
            # does not strand the loop.
            used_pipeline = False

    if not used_pipeline:
        with ThreadPoolExecutor(max_workers=cfg.parallel) as ex:
            futures = [
                ex.submit(
                    run_worker,
                    i,
                    cfg.repo,
                    cfg.logs_dir,
                    cfg.worker_timeout_s,
                    risk_gated=meta["risk_gated"],
                    past_attempts=meta["past_attempts"],
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
                )
                for i, meta in dispatch
            ]
            for fut in futures:
                outcomes.append(fut.result())

    for o in outcomes:
        _mlog.info(
            master_log_path,
            f"worker #{o.issue} {o.status} ({o.duration_s:.0f}s) pr={o.pr_url or '-'}",
        )

    return outcomes, used_pipeline


def _run_repair_workers(
    cfg: Config,
    repairs: list[tuple[dict[str, Any], dict[str, Any], str]],
    tick: int,
    master_log_path: Path,
    bus_emit: Any,
) -> list[WorkerOutcome]:
    """Spawn workers that repair existing PR branches."""
    outcomes: list[WorkerOutcome] = []
    with ThreadPoolExecutor(max_workers=cfg.parallel) as ex:
        futures = [
            ex.submit(
                run_repair_worker,
                issue,
                pr,
                review_context,
                cfg.repo,
                cfg.logs_dir,
                cfg.worker_timeout_s,
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
            )
            for issue, pr, review_context in repairs
        ]
        for fut in futures:
            outcomes.append(fut.result())
    for o in outcomes:
        _mlog.info(
            master_log_path,
            f"repair worker #{o.issue} {o.status} ({o.duration_s:.0f}s) pr={o.pr_url or '-'}",
        )
    return outcomes


def _run_critic_for_outcomes(
    cfg: Config,
    outcomes: list[WorkerOutcome],
    bus_emit: Any,
) -> None:
    """Critic agent: review PRs the workers opened, before auto-merge fires."""
    for o in outcomes:
        if o.status in {"open", "merged"} and o.pr_url:
            try:
                critic_outcome = _critic_review(
                    o.pr_url,
                    o.issue,
                    cfg.repo,
                    cfg.logs_dir,
                    timeout_s=cfg.critic.timeout_s,
                    emit=bus_emit,
                    model=cfg.critic.model,
                    provider=getattr(cfg.critic, "provider", "claude"),
                )
                append_event(
                    cfg.events_file,
                    "critic_done",
                    issue=o.issue,
                    pr=o.pr_url,
                    verdict=critic_outcome.verdict,
                    reasons=critic_outcome.reasons,
                    duration_s=round(critic_outcome.duration_s, 1),
                    sev_counts=_sev_counts(critic_outcome),
                    parse_retries=critic_outcome.parse_retries,
                )
                if critic_outcome.report is not None:
                    try:
                        lines = _gh.pr_changed_lines(o.pr_url, repo=cfg.github_repo)
                        plan = apply_critic_report(
                            critic_outcome.report,
                            o.pr_url,
                            lines,
                            cfg.critic.block_on_sev2,
                            cfg.critic.min_findings_for_approve,
                            gh=_gh,
                            repo=cfg.github_repo,
                            emit=bus_emit,
                        )
                        if not plan.block_merge:
                            _gh.remove_pr_label(o.pr_url, "critic:blocking", repo=cfg.github_repo)
                            _gh.remove_pr_label(o.pr_url, "critic:suspicious", repo=cfg.github_repo)
                    except Exception as act_ex:
                        append_event(
                            cfg.events_file,
                            "critic_actions_failed",
                            issue=o.issue,
                            err=str(act_ex)[:200],
                        )
            except Exception as ex_:
                append_event(cfg.events_file, "critic_failed", issue=o.issue, err=str(ex_)[:200])


def run_multirepo(
    repos_dir: Path,
    template: Config | None = None,
) -> int:
    """Run the loop across N repos discovered under ``repos_dir``.

    Each global tick iterates every enabled repo in name-sorted order and
    runs the regular single-repo ``_tick`` body against a per-repo
    ``Config``. Per-repo state / events stay under each checkout; the
    cross-repo orchestration events (start/done, skips) land in a small
    sidecar log under ``<loop_home>/.forge/multirepo-events.jsonl``.
    """
    from forge_loop.multirepo import RepoLoadError, load_repos
    from forge_loop.multirepo.runner import MultirepoRunState, run_multirepo_tick
    from forge_loop.runner import boot as _boot
    from forge_loop.runner.tick import _tick

    try:
        specs = load_repos(repos_dir)
    except RepoLoadError as e:
        import sys

        sys.stderr.write(f"[multirepo] failed to load repos: {e}\n")
        return 2

    loop_home = repos_dir.parent.parent  # <home>/.forge/repos/ → <home>
    sidecar_events = loop_home / ".forge" / "multirepo-events.jsonl"
    sidecar_events.parent.mkdir(parents=True, exist_ok=True)
    state = MultirepoRunState()

    append_event(sidecar_events, "multirepo_loop_start", repos=[s.name for s in specs])

    tick = 0
    while _boot._RUN:
        tick += 1
        run_multirepo_tick(
            specs,
            tick,
            state=state,
            template=template,
            events_file=sidecar_events,
            tick_fn=_tick,
        )
        if template and template.max_ticks and tick >= template.max_ticks:
            append_event(sidecar_events, "max_ticks_reached", tick=tick)
            break
        interval = template.tick_interval_s if template else 60
        time.sleep(interval)

    append_event(sidecar_events, "multirepo_loop_stop", tick=tick)
    return 0
