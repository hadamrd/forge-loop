"""Worker spawning, critic-loop wiring, and multirepo dispatch glue.

Extracted from ``runner/__init__.py`` (issue #50). Pure mechanical move:
no behaviour change, no signature change.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from forge_loop import gh_issues as _gh
from forge_loop import master_log as _mlog
from forge_loop.config import Config
from forge_loop.control.boot import canonical_task_saga_path
from forge_loop.critic import review_pr as _critic_review
from forge_loop.critic import serialize_findings as _serialize_findings
from forge_loop.critic_actions import apply_critic_report
from forge_loop.events import CriticReviewErroredEvent
from forge_loop.events import emit as _emit_typed
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
from forge_loop.tasks import (
    Compensation,
    CompensationKind,
    SqliteTaskSagaStore,
    TaskSaga,
    TaskSagaStore,
    TaskState,
)
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


# #262: how many dispatch slots a tick reserves for NEW work when repair work
# would otherwise claim the whole tick. A single reserved slot is enough to
# guarantee forward progress on the backlog (a stuck repair can no longer
# starve every ``loop:ready`` issue) without a fairness-queue rewrite.
RESERVED_NEW_WORK_SLOTS = 1


def reserved_new_work_slots(
    parallel: int,
    *,
    repairs_pending: int,
    ready_count: int,
    reserve: int = RESERVED_NEW_WORK_SLOTS,
) -> int:
    """Slots to steal back from repairs for NEW dispatch this tick (issue #262).

    The tick is structured so repair work is a terminal tick body: when a
    repair path fires the loop returns early and fresh ``loop:ready`` issues
    are never picked up. A single perpetually-blocked repair therefore starves
    the whole backlog to zero forward progress. This helper is the ONE fairness
    mechanism that prevents it: when repair work is in flight AND a ready
    candidate is waiting, it reserves ``>=1`` of the ``parallel`` worker slots
    for new dispatch.

    Args:
        parallel: the configured worker-parallelism cap.
        repairs_pending: number of repairs the tick is running (``0`` ⇒ no
            repair work in flight).
        ready_count: number of dispatchable ``loop:ready`` candidates waiting.
        reserve: the desired reserve count (defaults to
            :data:`RESERVED_NEW_WORK_SLOTS`).

    Returns:
        The reserved-slot count, clamped to ``[0, parallel - 1]``:

        * ``0`` when there are no repairs in flight (new work already gets all
          ``parallel`` slots) OR no ready candidates (repairs take all slots) —
          both cases are byte-identical to the pre-#262 behaviour.
        * otherwise ``min(reserve, parallel - 1)`` — repairs always keep at
          least one slot. The ``parallel - 1`` ceiling is the documented
          tie-break: on ``parallel == 1`` the reservation is ``0`` and
          **repairs win the whole tick** (raise ``parallel`` to enable the
          anti-starvation pre-emption).
    """
    if repairs_pending <= 0 or ready_count <= 0 or reserve <= 0:
        return 0
    # ☠ USE EVERY GENUINELY FREE SLOT, not a constant 1.
    #
    # `reserve` is a FLOOR (never starve new work), never a ceiling. The old
    # `min(reserve, parallel - 1)` capped new dispatch at ONE issue whenever any repair
    # was in flight — so raising `parallel` bought nothing and the extra workers idled
    # while the backlog waited. Repairs keep exactly the slots they are actually using
    # (`repairs_pending`); everything left over goes to new work.
    #
    # Measured: with parallel=2 and one repair, one ready issue was dispatched and the
    # second slot sat empty for the whole tick.
    free = parallel - repairs_pending
    return max(0, min(max(reserve, free), parallel - 1))


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


def _resolve_task_saga_store(cfg: Config) -> TaskSagaStore | None:
    """Open the saga store at the canonical path (or an injected override).

    Called ONCE per tick (#227); the returned store's single, thread-safe
    connection is then shared across that tick's worker dispatch + heartbeat +
    policy recording instead of being re-opened per worker. Best-effort: never
    let saga bookkeeping break worker dispatch.
    """
    explicit: TaskSagaStore | None = getattr(cfg, "task_store", None)
    if explicit is not None:
        return explicit
    try:
        return SqliteTaskSagaStore(canonical_task_saga_path(cfg.repo))
    except Exception:  # noqa: BLE001 - saga state is advisory, dispatch must go on
        return None


def record_worker_task_policy(
    *,
    repo: Path,
    task_id: str,
    saga_id: str,
    issue: int,
    branch: str,
    worktree_path: str,
    capability_policy: CapabilityPolicy,
) -> TaskSaga:
    # Last-resort fallback only: reached when the tick-scoped saga store failed
    # to resolve (``_seed_worker_saga`` calls this on its ``task_store is None``
    # branch, where there is no shared store to reuse). The normal #227 reuse
    # path never gets here — it threads the tick store straight into
    # ``task_store.create``.
    store = SqliteTaskSagaStore(canonical_task_saga_path(repo))
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
            compensations=_worker_compensations(worktree_path=worktree_path, branch=branch),
            capability_policy=capability_policy,
        )
    )


def _seed_worker_saga(
    task_store: TaskSagaStore | None,
    *,
    repo: Path,
    issue: dict[str, Any],
    branch: str,
    worktree_path: str,
    capability_policy: CapabilityPolicy,
) -> None:
    n = int(issue["number"])
    task_id = _worker_task_id(n)
    saga_id = f"saga-{n}-worker"
    if task_store is None:
        record_worker_task_policy(
            repo=repo,
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
        compensations=_worker_compensations(worktree_path=worktree_path, branch=branch),
        capability_policy=capability_policy,
    )


def _worker_compensations(*, worktree_path: str, branch: str) -> tuple[Compensation, ...]:
    """Build the compensation tuple registered when a worker saga is created.

    Two cleanups are registered at dispatch, in the order they must run on
    recovery: drop the worktree first, then delete the branch it planted.

    * ``REMOVE_WORKTREE`` reaps the ``/tmp`` worktree the worker ran in.
    * ``DELETE_BRANCH`` (#433, epic "Compensate the branch a failed worker
      abandons") reclaims the exact ``loop/<n>`` branch this worker planted —
      ``target`` is the canonical branch name derived for the issue, so a
      failed worker can never leak a branch the control plane doesn't know to
      delete. Both seed paths (the shared-store ``create`` and the
      ``task_store is None`` fallback) build the tuple here so they can never
      drift apart.
    """
    return (
        Compensation(
            kind=CompensationKind.REMOVE_WORKTREE,
            target=worktree_path,
            reason="cleanup worker worktree after task terminal state",
        ),
        Compensation(
            kind=CompensationKind.DELETE_BRANCH,
            target=branch,
            reason="reclaim worker loop branch after task terminal state",
        ),
    )


def _worker_task_id(issue_number: int) -> str:
    return f"task-{issue_number}-worker"


# Liveness: the lease TTL is a small multiple of the heartbeat interval, NOT
# the (hours-long) worker wall ceiling. A live worker keeps it fresh by
# heartbeating; a hard-killed one goes stale within ~interval*factor, so
# recovery reaps it in minutes instead of hours. The factor is a grace margin —
# several consecutive missed beats are tolerated before a live worker could be
# mistaken for dead.
_HEARTBEAT_INTERVAL_S = 60
_HEARTBEAT_LEASE_FACTOR = 5


def _heartbeat_interval_s(cfg: Config) -> float:
    """Resolve the worker-heartbeat interval (seconds), floored at 1.0.

    ``worker_heartbeat_interval_s`` is a real, configurable field on
    :class:`Config` (env/yaml override: LOOP_WORKER_HEARTBEAT_INTERVAL_S /
    ``scheduling.worker_heartbeat_interval_s``). The ``getattr`` fallback
    to ``_HEARTBEAT_INTERVAL_S`` is retained only as a safety net for
    Config-shaped test stubs that predate the field.
    """
    interval = getattr(cfg, "worker_heartbeat_interval_s", _HEARTBEAT_INTERVAL_S)
    return max(float(interval), 1.0)


def _lease_worker_saga(
    task_store: TaskSagaStore | None,
    *,
    task_id: str,
    owner_id: str,
    lease_ttl_s: float,
) -> None:
    """Mark the saga RUNNING under a short, heartbeat-renewed lease.

    The lease lapses ``lease_ttl_s`` after the last heartbeat; a dead worker
    (no more beats) then reads as stale for recovery. All best-effort — saga
    state must never break dispatch.
    """
    if task_store is None:
        return
    now = datetime.now(UTC)
    with contextlib.suppress(Exception):
        task_store.acquire_lease(
            task_id,
            owner_id=owner_id,
            expires_at=now + timedelta(seconds=max(lease_ttl_s, 1.0)),
            acquired_at=now,
        )


def _start_worker_heartbeat(
    saga_store: TaskSagaStore,
    *,
    task_id: str,
    owner_id: str,
    interval_s: float,
    lease_ttl_s: float,
) -> tuple[threading.Event, threading.Thread]:
    """Renew the saga lease on a timer so a *live* worker never reads as stale.

    Runs in its own daemon thread but reuses the **tick-scoped** saga store
    (#227) instead of opening its own connection: the store's single connection
    is shared thread-safely (``check_same_thread=False`` + an internal lock), so
    the heartbeat no longer re-runs schema creation + compat migration per
    worker. It beats on a wall-clock timer independent of worker activity, so
    even a worker silent for 10+ minutes of extended thinking stays leased.
    Fully best-effort: a heartbeat failure never touches the worker.
    """
    stop = threading.Event()

    def _beat() -> None:
        while not stop.wait(interval_s):
            now = datetime.now(UTC)
            with contextlib.suppress(Exception):
                saga_store.heartbeat(
                    task_id,
                    owner_id=owner_id,
                    heartbeat_at=now,
                    expires_at=now + timedelta(seconds=max(lease_ttl_s, 1.0)),
                )

    thread = threading.Thread(target=_beat, name=f"hb-{task_id}", daemon=True)
    thread.start()
    return stop, thread


def _stop_worker_heartbeat(handle: tuple[threading.Event, threading.Thread] | None) -> None:
    if handle is None:
        return
    stop, thread = handle
    stop.set()
    thread.join(timeout=2.0)


def _finalize_worker_saga(
    task_store: TaskSagaStore | None,
    *,
    task_id: str,
    status: str,
) -> None:
    """Drive the saga to a terminal state from the worker outcome.

    ``merged``/``open`` complete the saga; everything else fails it (the
    remove-worktree compensation rides along from seeding). Best-effort so a
    saga-store hiccup never masks the real worker outcome.
    """
    if task_store is None:
        return
    with contextlib.suppress(Exception):
        if status in ("merged", "open"):
            task_store.mark_completed(task_id, reason=f"worker outcome: {status}")
        else:
            task_store.mark_failed(task_id, reason=f"worker outcome: {status}")


def _dispatch_one_worker(
    cfg: Config,
    issue: dict[str, Any],
    meta: dict[str, Any],
    *,
    tick: int,
    bus_emit: Any,
    store: WorkerSessionStore | None,
    maestro_context: str = "",
    saga_store: TaskSagaStore | None = None,
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
    from forge_loop.worker_worktree import worktree_path as _worktree_path

    worktree_path = str(_worktree_path(cfg.repo, issue["number"]))
    capability_policy = capability_policy_for_worker(
        repo=cfg.repo,
        worktree_path=worktree_path,
        allowed_mcp_servers=cfg.worker.allowed_mcp_tools,
    )
    if capability_policy is None:
        raise RuntimeError(f"worker {issue['number']} missing capability policy record")
    branch = _branch_for_issue(issue)
    n = int(issue["number"])
    task_id = _worker_task_id(n)
    # #227: reuse the tick-scoped saga store when the caller supplies one so the
    # store (and its schema + compat migration) is opened once per tick, not
    # re-``__init__``'d per worker. Fall back to a per-call open only when no
    # shared store was threaded in (direct unit-test calls / legacy callers).
    if saga_store is None:
        saga_store = _resolve_task_saga_store(cfg)
    with contextlib.suppress(Exception):
        _seed_worker_saga(
            saga_store,
            repo=cfg.repo,
            issue=issue,
            branch=branch,
            worktree_path=worktree_path,
            capability_policy=capability_policy,
        )
    owner_id = f"worker-{n}-tick-{tick}"
    interval_s = _heartbeat_interval_s(cfg)
    lease_ttl_s = interval_s * _HEARTBEAT_LEASE_FACTOR
    _lease_worker_saga(saga_store, task_id=task_id, owner_id=owner_id, lease_ttl_s=lease_ttl_s)

    heartbeat: tuple[threading.Event, threading.Thread] | None = None
    if saga_store is not None:
        heartbeat = _start_worker_heartbeat(
            saga_store,
            task_id=task_id,
            owner_id=owner_id,
            interval_s=interval_s,
            lease_ttl_s=lease_ttl_s,
        )

    try:
        return _run_worker_with_saga(
            cfg,
            issue,
            meta,
            tick=tick,
            bus_emit=bus_emit,
            store=store,
            saga_store=saga_store,
            task_id=task_id,
            capability_policy=capability_policy,
            worktree_path=worktree_path,
            branch=branch,
            maestro_context=maestro_context,
        )
    finally:
        _stop_worker_heartbeat(heartbeat)


def _recover_orphan_pr_url(cfg: Config, issue_number: int, worktree_path: str) -> str | None:
    """Recover a PR URL for a worker that crashed/was cancelled (issue #213).

    Looks first at the worktree's ``sprint-events.jsonl`` (the worker emits a
    ``pr_opened`` event there), then at the most recent ``worker-<n>-*.log``
    under ``cfg.logs_dir``. Best-effort: any failure yields ``None`` so the
    existing ``pr_url=None`` semantics are preserved.
    """
    from forge_loop.worker import recover_orphaned_pr_url

    worktree = Path(worktree_path)
    latest_log: Path | None = None
    try:
        logs = sorted(cfg.logs_dir.glob(f"worker-{issue_number}-*.log"))
        latest_log = logs[-1] if logs else None
    except OSError:
        latest_log = None
    try:
        return recover_orphaned_pr_url(worktree, latest_log)
    except Exception as exc:  # noqa: BLE001 — recovery must never mask the real crash
        # EH-001 / issue #213 AC5: log the boundary with context — never a
        # silent drop. Mirrors the async sibling's ``orphan_pr_recover_failed``.
        append_event(
            cfg.events_file,
            "orphan_pr_recover_failed",
            issue=issue_number,
            worktree=worktree_path,
            err=str(exc)[:200],
        )
        return None


def _run_worker_with_saga(
    cfg: Config,
    issue: dict[str, Any],
    meta: dict[str, Any],
    *,
    tick: int,
    bus_emit: Any,
    store: WorkerSessionStore | None,
    saga_store: TaskSagaStore | None,
    task_id: str,
    capability_policy: CapabilityPolicy,
    worktree_path: str,
    branch: str,
    maestro_context: str,
) -> WorkerOutcome:
    """Invoke the worker and drive the saga terminal edge (legacy + FSM paths)."""
    if store is None:
        try:
            legacy_outcome = run_worker(
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
                events_file=cfg.events_file,
                maestro_context=maestro_context,
                env_path_prepend=getattr(cfg.worker, "env_path_prepend", ()),
                env_vars=getattr(cfg.worker, "env_vars", {}),
                env_require=getattr(cfg.worker, "env_require", ()),
                verify_commands=getattr(cfg.worker, "verify_commands", ()),
                scope_soft_loc_cap=getattr(cfg.worker, "scope_soft_loc_cap", 150),
            )
        except BaseException:
            _finalize_worker_saga(saga_store, task_id=task_id, status="failed")
            raise
        _finalize_worker_saga(saga_store, task_id=task_id, status=legacy_outcome.status)
        return legacy_outcome

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
            events_file=cfg.events_file,
            maestro_context=maestro_context,
            permissions=getattr(cfg.worker, "permissions", "full"),
            env_path_prepend=getattr(cfg.worker, "env_path_prepend", ()),
            env_vars=getattr(cfg.worker, "env_vars", {}),
            env_require=getattr(cfg.worker, "env_require", ()),
            verify_commands=getattr(cfg.worker, "verify_commands", ()),
            scope_soft_loc_cap=getattr(cfg.worker, "scope_soft_loc_cap", 150),
        )
    except BaseException as ex_:
        # The subprocess crashed before producing a WorkerOutcome. We
        # MUST close out the FSM row — otherwise a recovery walk would
        # treat this as a still-RUNNING session and try to resume it.
        #
        # Issue #213: the crash/cancellation may have landed AFTER the
        # worker opened its PR. Recover the URL from the worktree's
        # sprint-events / worker log so the synthetic outcome carries it
        # and the next tick's adoption scan can re-critic + merge the PR
        # instead of orphaning it.
        recovered_pr = _recover_orphan_pr_url(cfg, issue["number"], worktree_path)
        synthetic = WorkerOutcome(
            issue=issue["number"],
            title=issue.get("title", ""),
            pr_url=recovered_pr,
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
        _finalize_worker_saga(saga_store, task_id=task_id, status="failed")
        raise

    record_outcome(
        store,
        session=sess,
        outcome=outcome,
        events_file=cfg.events_file,
    )
    _finalize_worker_saga(saga_store, task_id=task_id, status=outcome.status)
    return outcome


def _run_workers(
    cfg: Config,
    issues: list[dict[str, Any]],
    workers_meta: list[dict[str, Any]],
    tick: int,
    master_log_path: Path,
    bus_emit: Any,
    *,
    maestro_context: str = "",
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

        # #227: open the durable saga store ONCE per tick and share the single
        # (thread-safe) connection across every worker's dispatch + heartbeat +
        # policy recording, instead of re-``__init__``-ing it (full schema script
        # + compat ALTER probing) 2-3x per worker. Best-effort: a failed open
        # leaves ``saga_store=None`` and each worker falls back to its own.
        # Own the tick store so we can close it once the tick is done. An
        # injected ``cfg.task_store`` is owned by the caller, so only close a
        # store we opened here (the ``isinstance`` + identity guard below).
        injected_store = getattr(cfg, "task_store", None)
        tick_saga_store = _resolve_task_saga_store(cfg)
        try:
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
                        maestro_context=maestro_context,
                        saga_store=tick_saga_store,
                    )
                    for i, meta in dispatch
                ]
                for fut in futures:
                    outcomes.append(fut.result())
        finally:
            # By here the ThreadPool has drained and each worker has *requested*
            # its heartbeat thread stop (``_dispatch_one_worker`` joins its own
            # in ``finally``). That join is best-effort with a 2.0s timeout
            # (``_stop_worker_heartbeat``), so a heartbeat blocked on the shared
            # lock could in theory outlive the join — worst case a single
            # ``sqlite3.Error`` on a beat that lands after close. Both this
            # close and the heartbeat op suppress that, so it is harmless; we
            # close here to avoid leaking one sqlite connection per tick (#227).
            if isinstance(tick_saga_store, SqliteTaskSagaStore) and (
                tick_saga_store is not injected_store
            ):
                # Narrowed to ``sqlite3.Error`` (not bare ``Exception``) per
                # EH-001: only sqlite close failures are expected/best-effort.
                with contextlib.suppress(sqlite3.Error):
                    tick_saga_store.close()

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
    from forge_loop.worker_worktree import worktree_path as _worktree_path

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
                # The repair worktree merges this forward before the round runs — a 5th-round
                # worker must not still be sitting on the base its branch was cut from.
                base_branch=cfg.base_branch,
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
                capability_policy=capability_policy_for_worker(
                    repo=cfg.repo,
                    worktree_path=str(_worktree_path(cfg.repo, issue["number"])),
                    allowed_mcp_servers=cfg.worker.allowed_mcp_tools,
                ),
                events_file=cfg.events_file,
                env_path_prepend=getattr(cfg.worker, "env_path_prepend", ()),
                env_vars=getattr(cfg.worker, "env_vars", {}),
                env_require=getattr(cfg.worker, "env_require", ()),
                verify_commands=getattr(cfg.worker, "verify_commands", ()),
                scope_soft_loc_cap=getattr(cfg.worker, "scope_soft_loc_cap", 150),
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


def _clear_stale_critic_block_labels(
    pr_url: str,
    *,
    repo: str | None,
    bus_emit: Any,
) -> None:
    """Remove ``critic:blocking`` / ``critic:suspicious`` from a PR.

    Shared by the non-blocking plan path AND the ``verdict=error`` path
    (issue #245) so a block label is only ever held when the CURRENT head's
    verdict actually says so — a crashed review re-derives "no block" rather
    than carrying a prior round's label forward. Each removal is best-effort;
    a failure is surfaced via ``critic_actions_failed`` and never raises.
    """
    for label in ("critic:blocking", "critic:suspicious"):
        ok = _gh.remove_pr_label(pr_url, label, repo=repo)
        if not ok:
            bus_emit(
                "critic_actions_failed",
                {
                    "pr": pr_url,
                    "method": "remove_pr_label",
                    "label": label,
                    "auth_source": getattr(_gh, "auth_source", "github-client"),
                },
            )


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
                    sev3_demotion_round_threshold=getattr(
                        cfg.critic, "sev3_demotion_round_threshold", 3
                    ),
                )
                # Issue #267 (safety): record the verdict ON the outcome so the
                # merge gate can require an AFFIRMATIVE "approved" to auto-merge.
                # This is set for EVERY reviewed PR — including the verdict=error
                # branch below — so a crashed review can never fall through the
                # gate's allow-list and auto-merge an unreviewed PR.
                o.critic_verdict = critic_outcome.verdict
                # Issue #404: persist the per-finding detail + the ordered
                # minimal_path_to_green ON the durable payload, sourced from the
                # report already in hand (no critic re-run). The legacy mirror
                # passes these fields straight through to ``critique.issued`` so
                # the console can render them on replay.
                _report = critic_outcome.report
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
                    error_class=(
                        critic_outcome.error_class.value
                        if critic_outcome.error_class is not None
                        else None
                    ),
                    findings=(
                        _serialize_findings(_report.findings) if _report is not None else []
                    ),
                    minimal_path_to_green=(
                        list(_report.minimal_path_to_green) if _report is not None else []
                    ),
                )
                if critic_outcome.report is not None:
                    try:
                        lines = _gh.pr_changed_lines(o.pr_url, repo=cfg.github_repo)
                        # #311 self-clearing guard: a large zero-finding approve is
                        # held as suspicious UNLESS the PR added tests (best-effort).
                        try:
                            _changed = _gh.pr_changed_files(
                                o.pr_url, repo=cast(str, cfg.github_repo)
                            )
                        except Exception:
                            _changed = []
                        # Broad, multi-language test-file detection. A MISSED convention
                        # (e.g. Rust `*_tests.rs`, JS `*.test.tsx`) wrongly flags a
                        # well-tested PR as a suspicious rubber-stamp and blocks its merge
                        # FOREVER (the critic re-approves, the hold re-fires, every tick).
                        def _looks_like_test(p: str) -> bool:
                            b = p.lower().rsplit("/", 1)[-1]
                            return (
                                "tests/" in p
                                or "/test/" in p
                                or p.startswith(("tests/", "test/"))
                                or b.startswith("test_")
                                or b.endswith(
                                    (
                                        "_test.rs",
                                        "_tests.rs",
                                        "_test.py",
                                        "_test.go",
                                        "_spec.rb",
                                        "_test.exs",
                                    )
                                )
                                or b == "tests.rs"
                                or any(
                                    b.endswith(f".{kind}.{ext}")
                                    for kind in ("test", "spec")
                                    for ext in ("js", "jsx", "ts", "tsx", "mjs", "cjs")
                                )
                            )

                        _touches_tests = any(
                            _looks_like_test(p)
                            for p in (_changed or [])
                            if isinstance(p, str)
                        )
                        plan = apply_critic_report(
                            critic_outcome.report,
                            o.pr_url,
                            lines,
                            cfg.critic.block_on_sev2,
                            cfg.critic.min_findings_for_approve,
                            gh=_gh,
                            repo=cfg.github_repo,
                            emit=bus_emit,
                            pr_touches_tests=_touches_tests,
                        )
                        if plan.block_merge:
                            o.status = "open"
                            reason = "; ".join(critic_outcome.reasons) or critic_outcome.verdict
                            note = f"critic blocked merge: {reason}"[:200]
                            o.error = f"{o.error}; {note}" if o.error else note
                            # Loop-breaker (PR #361 burned ~9h / 43 re-reviews here): a
                            # SUSPICIOUS-approve block is NOT repairable by a worker — the
                            # critic APPROVED, there is nothing to fix — so re-reviewing it
                            # every tick spins forever. Only a human can adjudicate "looks
                            # like a rubber-stamp". Escalate ONCE (drop loop:ready, add
                            # loop:needs-human) so the dispatcher stops re-picking it.
                            if plan.suspicious_approve:
                                try:
                                    _gh.update_issue(
                                        o.issue,
                                        add_labels=["loop:needs-human"],
                                        remove_labels=["loop:ready"],
                                        repo=cfg.github_repo,
                                    )
                                except Exception:  # noqa: BLE001
                                    pass
                                append_event(
                                    cfg.events_file,
                                    "suspicious_approve_escalated_to_human",
                                    issue=o.issue,
                                    pr=o.pr_url,
                                )
                        else:
                            _clear_stale_critic_block_labels(
                                o.pr_url, repo=cfg.github_repo, bus_emit=bus_emit
                            )
                    except Exception as act_ex:
                        append_event(
                            cfg.events_file,
                            "critic_actions_failed",
                            issue=o.issue,
                            err=str(act_ex)[:200],
                        )
                else:
                    # verdict == "error": a crashed / timed-out / parse-failed
                    # re-review has NO opinion (issue #245). It MUST NOT carry a
                    # prior round's critic:blocking forward unevaluated — that
                    # froze PR #231 for ~2.5h. Re-derive the block from the
                    # CURRENT head (no real verdict → no block) by clearing the
                    # stale labels, surface the crash LOUD via a typed event, and
                    # leave the PR open so the next tick re-reviews from scratch.
                    # Never auto-approve: an error is never an approval.
                    o.status = "open"
                    _emit_typed(
                        cfg.events_file,
                        CriticReviewErroredEvent(
                            issue=o.issue,
                            pr=o.pr_url,
                            verdict=critic_outcome.verdict,
                            error=(critic_outcome.error or "")[:200],
                        ),
                    )
                    try:
                        _clear_stale_critic_block_labels(
                            o.pr_url, repo=cfg.github_repo, bus_emit=bus_emit
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
