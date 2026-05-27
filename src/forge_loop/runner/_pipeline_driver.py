"""Pipeline-driven tick dispatch (issue #49).

When ``.forge/pipeline.yaml`` exists AND the operator opts in via
``LOOP_PIPELINE_DRIVEN=1``, ``runner._tick`` routes the per-issue
worker/critic chain through :class:`forge_loop.pipeline.PipelineExecutor`
instead of the legacy hardcoded ``ThreadPoolExecutor(worker) → critic``
sequence in ``runner/__init__.py``.

Scope of this module — keep it small and orchestrator-shaped:
- Construct a handlers map (po/worker/critic + any operator-supplied
  custom roles) bound to the live cfg.
- Run the executor once per issue and translate the per-step outcomes
  back into the legacy :class:`WorkerOutcome` list the rest of the
  tick body expects.
- A module-level :data:`EXTRA_HANDLERS` lets tests (and, later,
  plugin authors) register additional role handlers without monkey-
  patching this file.

Out of scope (matches the issue's "Out of scope" section):
- Migrating the *default* flow off the hardcoded chain. The chain
  remains the default; pipeline-driven mode is the opt-in.
- Cross-step data passing redesign.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from forge_loop.worker import WorkerOutcome
from forge_loop.worker import run_worker as _default_run_worker

if TYPE_CHECKING:  # pragma: no cover — type-only
    from forge_loop.config import Config
    from forge_loop.pipeline.executor import RoleHandler, StepContext, StepOutcome


# Operator/test-side handler registry. Anything in here overrides the
# built-in defaults for roles of the same name AND adds handlers for
# roles the runner doesn't ship a default for (e.g. a custom
# ``security-reviewer`` role between worker and critic — the headline
# case from the issue's acceptance criteria).
EXTRA_HANDLERS: dict[str, RoleHandler] = {}


def register_handler(role: str, handler: RoleHandler) -> None:
    """Public registration shim. Idempotent; last write wins."""
    EXTRA_HANDLERS[role] = handler


def pipeline_driven_enabled(cfg: Config) -> bool:
    """Return True iff this tick should route through the executor.

    Two gates, AND-ed:
      1. ``LOOP_PIPELINE_DRIVEN=1`` in the environment (opt-in).
      2. ``.forge/pipeline.yaml`` exists at the repo root.

    We intentionally do NOT silently switch behaviour when only the
    yaml exists — the issue explicitly requires the env gate "until
    stable", so an operator who checked in a pipeline.yaml for the
    validator can still get the legacy flow until they opt in.
    """
    if os.environ.get("LOOP_PIPELINE_DRIVEN") != "1":
        return False
    return (cfg.repo / ".forge" / "pipeline.yaml").exists()


def _build_default_handlers(
    cfg: Config,
    *,
    bus_emit: Callable[[str, dict[str, Any]], None],
    master_log_path: Path,
    run_worker_fn: Callable[..., WorkerOutcome] = _default_run_worker,
    critic_review_fn: Callable[..., Any] | None = None,
) -> dict[str, RoleHandler]:
    """Wire (po, worker, critic) handlers to the live runtime functions.

    Each handler returns a :class:`StepOutcome` whose ``payload`` carries
    the underlying domain object (e.g. ``WorkerOutcome``) so the caller
    can translate the per-issue chain result back into the legacy
    list[WorkerOutcome] the rest of ``_tick`` consumes.
    """
    from forge_loop.pipeline.executor import StepOutcome

    if critic_review_fn is None:  # pragma: no cover — wired by the runner
        from forge_loop.critic import review_pr as critic_review_fn  # type: ignore[assignment]

    def po_handler(ctx: StepContext) -> StepOutcome:
        # The PO expansion pass runs ABOVE the dispatch loop in _tick
        # (it operates on the whole batch, not per-issue). In pipeline-
        # driven mode we keep that batch-level pass and let the per-issue
        # ``po`` step become a no-op marker so chains that declare
        # ``po → worker`` still validate and produce events.
        return StepOutcome(role="po", status="ok", detail="batch-level po already ran")

    def worker_handler(ctx: StepContext) -> StepOutcome:
        meta = ctx.extras.get("meta") or {}
        tick = ctx.extras.get("tick", 0)
        out = run_worker_fn(
            ctx.issue, cfg.repo, cfg.logs_dir, cfg.worker_timeout_s,
            risk_gated=meta.get("risk_gated", False),
            past_attempts=meta.get("past_attempts") or [],
            emit=bus_emit,
            lumen_top_k=cfg.lumen.top_k,
            lumen_test_pattern=cfg.lumen_test_pattern,
            coauthor=cfg.coauthor,
            tick=tick,
            model=cfg.worker.model,
            thinking=cfg.worker.thinking,
            allowed_mcp_servers=cfg.worker.allowed_mcp_tools,
        )
        ok = out.status in {"merged", "open"}
        return StepOutcome(
            role="worker",
            status="ok" if ok else "failed",
            detail=out.status,
            payload=out,
        )

    def critic_handler(ctx: StepContext) -> StepOutcome:
        # Critic is a no-op if disabled OR no PR was opened upstream.
        # The worker step may not be a *direct* parent (chains like
        # ``worker → reviewer → critic`` are common), so search the full
        # prior-outcomes table that the driver stashes in ``extras``.
        worker_outcome = None
        all_prior = ctx.extras.get("_all_prior_outcomes") or {}
        worker_step = all_prior.get("worker") or ctx.upstream_outcomes.get("worker")
        if worker_step is not None and isinstance(worker_step.payload, WorkerOutcome):
            worker_outcome = worker_step.payload
        if not cfg.critic.enabled:
            return StepOutcome(role="critic", status="ok", detail="critic disabled")
        if worker_outcome is None or not worker_outcome.pr_url:
            return StepOutcome(role="critic", status="ok", detail="no pr to review")
        if worker_outcome.status not in {"open", "merged"}:
            return StepOutcome(role="critic", status="ok", detail="worker not open/merged")
        try:
            c = critic_review_fn(
                worker_outcome.pr_url, worker_outcome.issue,
                cfg.repo, cfg.logs_dir,
                timeout_s=cfg.critic.timeout_s,
                emit=bus_emit,
                model=cfg.critic.model,
            )
        except Exception as e:  # noqa: BLE001 — boundary
            return StepOutcome(
                role="critic", status="failed",
                detail=f"{type(e).__name__}: {e}",
            )
        return StepOutcome(role="critic", status="ok", detail=getattr(c, "verdict", "?"), payload=c)

    return {"po": po_handler, "worker": worker_handler, "critic": critic_handler}


def dispatch_via_pipeline(
    cfg: Config,
    issues: list[dict[str, Any]],
    workers_meta: list[dict[str, Any]],
    tick: int,
    *,
    master_log_path: Path,
    bus_emit: Callable[[str, dict[str, Any]], None],
    handlers_override: dict[str, RoleHandler] | None = None,
    run_worker_fn: Callable[..., WorkerOutcome] | None = None,
    critic_review_fn: Callable[..., Any] | None = None,
) -> list[WorkerOutcome]:
    """Run the configured pipeline once per issue and return WorkerOutcomes.

    Returns one WorkerOutcome per input issue, in input order. Issues
    whose worker step did not run (e.g. the chain ``po → worker`` got
    skipped because a condition failed upstream) get a synthetic
    ``no_pr`` outcome — same shape the legacy code emits when a worker
    is short-circuited.
    """
    from forge_loop.pipeline import (
        PipelineExecutor,
        build_dag,
        load_pipeline,
    )

    pipeline_yaml = cfg.repo / ".forge" / "pipeline.yaml"
    spec = load_pipeline(pipeline_yaml)
    dag = build_dag(spec)

    handlers = _build_default_handlers(
        cfg,
        bus_emit=bus_emit,
        master_log_path=master_log_path,
        run_worker_fn=run_worker_fn or _default_run_worker,
        critic_review_fn=critic_review_fn,
    )
    # Operator-registered custom roles (e.g. security-reviewer) layer on top.
    handlers.update(EXTRA_HANDLERS)
    if handlers_override:
        handlers.update(handlers_override)

    def _events_sink(e: dict[str, Any]) -> None:
        # Re-emit executor events into the unified bus so the operator's
        # events.jsonl tells the full per-step story. We prefix with
        # ``pipeline_`` so they don't collide with legacy event names.
        kind = e.get("kind", "step")
        bus_emit(f"pipeline_{kind}", {k: v for k, v in e.items() if k != "kind"})

    outcomes: list[WorkerOutcome] = []
    for issue, meta in zip(issues, workers_meta, strict=True):
        # Shared per-issue scratch the handlers can read to see siblings'
        # outcomes (the executor only passes *direct* parents through
        # StepContext.upstream_outcomes). Handlers mutate this dict
        # via a thin wrapper below.
        all_prior: dict[str, StepOutcome] = {}
        extras = {"meta": meta, "cfg": cfg, "tick": tick,
                  "_all_prior_outcomes": all_prior}
        # Wrap each handler to record its outcome into ``all_prior``
        # so later steps (e.g. critic after reviewer after worker) can
        # see what worker produced even when they aren't direct parents.
        wrapped: dict[str, Any] = {}
        for role, h in handlers.items():
            def _wrap(role_=role, h_=h, sink=all_prior):
                def runner(ctx):
                    out = h_(ctx)
                    sink[role_] = out
                    return out
                return runner
            wrapped[role] = _wrap()
        executor = PipelineExecutor(dag, wrapped, events=_events_sink)
        bus_emit("pipeline_run_start", {
            "issue": issue["number"], "roles": list(dag.order),
            "tick": tick,
        })
        result = executor.run(issue, extras=extras)
        bus_emit("pipeline_run_done", {
            "issue": issue["number"], "end_state": result.end_state,
            "tick": tick,
            "statuses": {r: o.status for r, o in result.outcomes.items()},
        })

        worker_step = result.outcomes.get("worker")
        if worker_step is not None and isinstance(worker_step.payload, WorkerOutcome):
            outcomes.append(worker_step.payload)
        else:
            # Worker step did not execute (skipped) or returned a non-
            # WorkerOutcome payload. Synthesise a no-op WorkerOutcome so
            # callers can keep their flat list invariant.
            outcomes.append(WorkerOutcome(
                issue=issue["number"],
                title=issue.get("title", ""),
                pr_url=None,
                status="no_pr",
                duration_s=0.0,
                stdout_tail="",
                error=f"pipeline end_state={result.end_state}",
            ))
    return outcomes
