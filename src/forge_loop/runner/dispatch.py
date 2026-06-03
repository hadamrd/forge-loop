"""Worker spawning, critic-loop wiring, and multirepo dispatch glue.

Extracted from ``runner/__init__.py`` (issue #50). Pure mechanical move:
no behaviour change, no signature change.
"""

from __future__ import annotations

import contextlib
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from forge_loop import gh as _gh
from forge_loop import master_log as _mlog
from forge_loop.config import Config
from forge_loop.critic import review_pr as _critic_review
from forge_loop.critic_actions import apply_critic_report
from forge_loop.runner.critic_flow import (
    NEEDS_HUMAN_LABEL,
    NEEDS_REVIEW_LABEL,
    enforce_critic_iteration_cap,
    format_critic_followup_prompt,
    handle_critic_verdict,
    resume_kwargs_for,
)
from forge_loop.runner.critic_flow import (
    sev_counts as _sev_counts,
)
from forge_loop.runner.persistent_dispatch import (
    get_or_resume_session,
    mark_running,
    open_default_store,
    persistent_worker_enabled,
    record_outcome,
)
from forge_loop.sandbox import CapabilityPolicy, FilesystemScope, McpGrant, NetworkPolicy
from forge_loop.state import append_event
from forge_loop.tasks import Compensation, SqliteTaskSagaStore, TaskSaga, TaskSagaStore, TaskState
from forge_loop.worker import WorkerOutcome, run_repair_worker, run_worker
from forge_loop.worker_sessions import WorkerSessionStore

__all__ = [
    "NEEDS_HUMAN_LABEL",
    "NEEDS_REVIEW_LABEL",
    "_sev_counts",
    "enforce_critic_iteration_cap",
    "format_critic_followup_prompt",
    "handle_critic_verdict",
    "resume_kwargs_for",
]


def persist_sdk_result(
    *,
    store: WorkerSessionStore,
    session_id: str,
    result: Any,
    emit: Any = None,
) -> None:
    """Save the SDK's session id and emit cost telemetry.

    Called once after every ``run_sdk_session(...)`` invocation. Two
    side effects:

    1. If ``result.sdk_session_id`` is set, write it through
       :meth:`WorkerSessionStore.set_sdk_session_id` so the next
       dispatch can pass ``resume=`` and reuse the prompt cache.
    2. Emit a ``cost_telemetry`` event carrying input/output tokens and
       a ``cache_hit_ratio`` — operators watch this ratio climb on
       round 2+ to confirm the persistent-worker epic is paying off.

    ``emit`` may be ``None`` (legacy callers / unit tests); both side
    effects are best-effort so a telemetry failure never prevents the
    SDK session id from landing.
    """
    sdk_id = getattr(result, "sdk_session_id", None)
    if isinstance(sdk_id, str) and sdk_id:
        with contextlib.suppress(Exception):
            store.set_sdk_session_id(session_id, sdk_id)

    if emit is None:
        return
    usage = dict(getattr(result, "usage", {}) or {})
    with contextlib.suppress(Exception):
        emit(
            "cost_telemetry",
            session_id=session_id,
            sdk_session_id=sdk_id,
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
            cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
            cache_hit_ratio=round(float(getattr(result, "cache_hit_ratio", 0.0) or 0.0), 4),
            cost_usd=float(getattr(result, "cost_usd", 0.0) or 0.0),
            model=getattr(result, "model", "") or "",
        )


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


def _branch_for_issue(issue: dict[str, Any]) -> str:
    """Recompute the branch the worker subprocess will use.

    We need this BEFORE spawning the worker so the WorkerSessionStore
    row carries the canonical branch name. ``run_worker`` derives the
    same value internally — we deliberately import the helper rather
    than re-implementing the slug logic so a future tweak to one updates
    the other.
    """
    from forge_loop.worker import _branch_name

    return _branch_name(issue["number"], issue["title"])


def capability_policy_for_worker(
    *,
    repo: Path,
    worktree_path: str,
    allowed_mcp_servers: tuple[str, ...] | None,
) -> CapabilityPolicy:
    """Build the explicit capability record bound to one worker worktree."""
    return CapabilityPolicy(
        filesystem=FilesystemScope(
            read_roots=(str(repo), worktree_path),
            write_roots=(worktree_path,),
        ),
        network=NetworkPolicy(
            allow_domains=("github.com", "api.github.com"),
            deny_by_default=True,
        ),
        mcp=tuple(McpGrant(server=server, tools=("*",)) for server in (allowed_mcp_servers or ())),
        secret_names=(),
    )


def record_worker_task_policy(
    *,
    state_dir: Path,
    task_id: str,
    saga_id: str,
    issue: int,
    branch: str,
    worktree_path: str,
    capability_policy: CapabilityPolicy,
) -> TaskSaga:
    store = SqliteTaskSagaStore(state_dir / "task-sagas.db")
    existing = store.get(task_id)
    if existing is not None:
        return existing
    return store.put(
        TaskSaga(
            task_id=task_id,
            saga_id=saga_id,
            state=TaskState.DISPATCHED,
            issue=issue,
            branch=branch,
            worktree=worktree_path,
            compensations=(
                Compensation(
                    kind="remove-worktree",
                    target=worktree_path,
                    reason="cleanup worker worktree after task terminal state",
                ),
            ),
            capability_policy=capability_policy,
        )
    )


def _record_worker_task_saga(
    task_store: TaskSagaStore | None,
    *,
    state_dir: Path,
    issue: dict[str, Any],
    branch: str,
    worktree_path: str,
    capability_policy: CapabilityPolicy,
) -> None:
    n = int(issue["number"])
    task_id = f"task-{n}-worker"
    saga_id = f"saga-{n}-worker"
    if task_store is None:
        record_worker_task_policy(
            state_dir=state_dir,
            task_id=task_id,
            saga_id=saga_id,
            issue=n,
            branch=branch,
            worktree_path=worktree_path,
            capability_policy=capability_policy,
        )
        return
    if task_store.get(task_id) is not None:
        return
    task_store.create(
        task_id=task_id,
        saga_id=saga_id,
        issue=n,
        branch=branch,
        worktree=worktree_path,
        compensations=(
            Compensation(
                kind="remove-worktree",
                target=worktree_path,
                reason="cleanup after worker task terminal state",
            ),
        ),
        capability_policy=capability_policy,
    )


def _dispatch_one_worker(
    cfg: Config,
    issue: dict[str, Any],
    meta: dict[str, Any],
    *,
    tick: int,
    bus_emit: Any,
    store: WorkerSessionStore | None,
) -> WorkerOutcome:
    """Run one worker, threading the persistent-worker FSM if enabled.

    With ``store=None`` this is a pure passthrough to ``run_worker`` —
    the legacy ``persistent_worker=False`` contract: no rows touched.

    With ``store`` set:

    1. Resolve a non-terminal session for the issue via
       :func:`get_or_resume_session` (or seed a fresh DISPATCHED row).
    2. Transition DISPATCHED → RUNNING immediately before invoking the
       SDK.
    3. Invoke ``run_worker`` (unchanged).
    4. Apply the outcome edge via :func:`record_outcome` —
       AWAITING_CRITIC + ``pr_url`` on success, ABANDONED on failure.

    Exceptions escaping ``run_worker`` are caught and converted into an
    ``ABANDONED`` transition so the store can never be left holding a
    RUNNING row whose subprocess died. The original exception is then
    re-raised so the ThreadPoolExecutor surfaces it to the caller.
    """
    worktree_path = f"/tmp/wt-loop-{issue['number']}"
    capability_policy = capability_policy_for_worker(
        repo=cfg.repo,
        worktree_path=worktree_path,
        allowed_mcp_servers=cfg.worker.allowed_mcp_tools,
    )
    if capability_policy is None:
        raise RuntimeError(f"worker {issue['number']} missing capability policy record")
    branch = _branch_for_issue(issue)
    _record_worker_task_saga(
        getattr(cfg, "task_store", None),
        state_dir=cfg.state_dir,
        issue=issue,
        branch=branch,
        worktree_path=worktree_path,
        capability_policy=capability_policy,
    )

    if store is None:
        return run_worker(
            issue,
            cfg.repo,
            cfg.logs_dir,
            cfg.worker_timeout_s,
            risk_gated=meta["risk_gated"],
            past_attempts=meta["past_attempts"],
            blocking_comments=meta.get("blocking_comments") or [],
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
            capability_policy=capability_policy,
        )

    sess, _resumed = get_or_resume_session(
        store,
        issue=issue["number"],
        branch=branch,
        worktree_path=worktree_path,
        events_file=cfg.events_file,
    )
    sess = mark_running(store, session=sess, events_file=cfg.events_file)

    try:
        outcome = run_worker(
            issue,
            cfg.repo,
            cfg.logs_dir,
            cfg.worker_timeout_s,
            risk_gated=meta["risk_gated"],
            past_attempts=meta["past_attempts"],
            blocking_comments=meta.get("blocking_comments") or [],
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
            capability_policy=capability_policy,
        )
    except BaseException as ex_:
        # The subprocess crashed before producing a WorkerOutcome. We
        # MUST close out the FSM row — otherwise a recovery walk would
        # treat this as a still-RUNNING session and try to resume it.
        synthetic = WorkerOutcome(
            issue=issue["number"],
            title=issue.get("title", ""),
            pr_url=None,
            status="failed",
            duration_s=0.0,
            stdout_tail="",
            error=f"{type(ex_).__name__}: {ex_!s:.200}",
        )
        with contextlib.suppress(Exception):
            record_outcome(
                store,
                session=sess,
                outcome=synthetic,
                events_file=cfg.events_file,
            )
        raise

    record_outcome(
        store,
        session=sess,
        outcome=outcome,
        events_file=cfg.events_file,
    )
    return outcome


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
        # Issue #108: route every dispatch through WorkerSessionStore
        # when ``settings.iteration.persistent_worker`` is enabled. The
        # store is the source of truth for what's in flight; the FSM
        # edges (DISPATCHED → RUNNING → AWAITING_CRITIC|ABANDONED) are
        # driven by ``persistent_dispatch.{mark_running,record_outcome}``.
        # With the flag OFF, ``store`` stays ``None`` and zero rows are
        # touched — the legacy fire-and-forget path is preserved.
        store: WorkerSessionStore | None = None
        if persistent_worker_enabled():
            try:
                store = open_default_store(cfg.state_dir)
            except Exception as ex_:  # noqa: BLE001 — never fail dispatch
                append_event(
                    cfg.events_file,
                    "persistent_dispatch_store_open_failed",
                    err=str(ex_)[:200],
                )
                store = None

        with ThreadPoolExecutor(max_workers=cfg.parallel) as ex:
            futures = [
                ex.submit(
                    _dispatch_one_worker,
                    cfg,
                    i,
                    meta,
                    tick=tick,
                    bus_emit=bus_emit,
                    store=store,
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
                        if plan.block_merge:
                            o.status = "open"
                            reason = "; ".join(critic_outcome.reasons) or critic_outcome.verdict
                            note = f"critic blocked merge: {reason}"[:200]
                            o.error = f"{o.error}; {note}" if o.error else note
                        else:
                            for label in ("critic:blocking", "critic:suspicious"):
                                ok = _gh.remove_pr_label(
                                    o.pr_url,
                                    label,
                                    repo=cfg.github_repo,
                                )
                                if not ok:
                                    bus_emit(
                                        "critic_actions_failed",
                                        {
                                            "pr": o.pr_url,
                                            "method": "remove_pr_label",
                                            "label": label,
                                            "auth_source": getattr(
                                                _gh,
                                                "auth_source",
                                                "github-client",
                                            ),
                                        },
                                    )
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
