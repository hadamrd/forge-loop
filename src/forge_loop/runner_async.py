"""Async orchestrator — three-stage pipeline with independent worker pools.

The legacy sync runner ticks like this:
    PO(all issues) → wait → workers(all) → wait → critics(all) → next tick

That serialises across stages: a 5-min PO call blocks every worker, and a
hanging critic blocks the merge of a fast ticket. This module replaces that
with three independent stages connected by ``asyncio.Queue``\\s:

    fresh_issues  →  [PO pool]   →  po_ready
                                       ↓
                                  [Worker pool]  →  pr_pending_critic
                                                          ↓
                                                    [Critic pool]

Each stage drains its inbound queue with N concurrent workers (env-tunable:
``LOOP_PO_POOL``, ``LOOP_WORKER_POOL``, ``LOOP_CRITIC_POOL``). A slow PO call
holds one PO slot; the other PO slots and ALL worker/critic slots keep moving.

Watchdog uses ``asyncio.wait_for`` (cooperative) rather than ``signal.alarm``
so it composes with the event loop and works on non-POSIX hosts.

Queues are bounded. On overflow we drop the OLDEST item (newest is freshest /
most likely still relevant) and emit a ``queue_overflow`` event — never a
silent loss.

This module is dependency-injected: pass in ``po_fn``, ``worker_fn``,
``critic_fn`` callables. ``runner.py`` wires the real implementations; tests
swap in fakes that simulate slow calls deterministically.
"""


from __future__ import annotations


# Experimental gate (issue #39): refuse to import unless the [experimental]
# extra is installed. Stable surface only in the default install.
from forge_loop._extras import require_experimental as _require_experimental
_require_experimental('runner_async')
import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

# Public payload type aliases — just dicts so we don't drag worker.WorkerOutcome
# / po.POOutcome into this module's import surface (keeps it cheap to test).
Issue = dict[str, Any]
POResult = dict[str, Any]      # {"issue": int, "skipped": bool, ...}
WorkerResult = dict[str, Any]  # {"issue": int, "status": str, "pr_url": str|None, ...}
CriticResult = dict[str, Any]  # {"issue": int, "verdict": str, ...}

EmitFn = Callable[[str, dict[str, Any]], None]
POFn = Callable[[Issue], Awaitable[POResult]]
WorkerFn = Callable[[Issue, POResult], Awaitable[WorkerResult]]
CriticFn = Callable[[WorkerResult], Awaitable[CriticResult]]


@dataclass(frozen=True)
class AsyncPools:
    """Per-stage concurrency. Each value is a slot count, not a thread count."""
    po: int = 1
    worker: int = 3
    critic: int = 2

    @classmethod
    def from_env(cls, *, default_worker: int = 3) -> AsyncPools:
        return cls(
            po=_env_int("LOOP_PO_POOL", 1),
            worker=_env_int("LOOP_WORKER_POOL", default_worker),
            critic=_env_int("LOOP_CRITIC_POOL", 2),
        )


@dataclass(frozen=True)
class AsyncQueueCaps:
    """Bounded-queue sizes. Overflow drops oldest + emits queue_overflow."""
    fresh: int = 32
    po_ready: int = 32
    pr_pending_critic: int = 32


@dataclass
class StageStats:
    enqueued: int = 0
    processed: int = 0
    errors: int = 0
    overflows: int = 0
    timeouts: int = 0


@dataclass
class AsyncOrchestratorStats:
    po: StageStats = field(default_factory=StageStats)
    worker: StageStats = field(default_factory=StageStats)
    critic: StageStats = field(default_factory=StageStats)


def _env_int(key: str, fallback: int) -> int:
    val = os.environ.get(key)
    try:
        return int(val) if val is not None else fallback
    except ValueError:
        return fallback


async def _put_with_overflow(
    queue: asyncio.Queue[Any],
    item: Any,
    *,
    stage: str,
    emit: EmitFn,
    stats: StageStats,
) -> None:
    """Enqueue without blocking; on full queue drop the OLDEST item.

    Why drop oldest, not newest: newer items are more likely to be live /
    still-relevant work. Dropping the head also keeps producers responsive.
    Either policy must emit ``queue_overflow`` — silent loss is the failure
    mode the spec explicitly forbids.
    """
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        dropped = None
        try:
            dropped = queue.get_nowait()
            queue.task_done()
        except asyncio.QueueEmpty:
            pass
        stats.overflows += 1
        emit("queue_overflow", {
            "stage": stage,
            "dropped": _summary(dropped),
            "kept": _summary(item),
            "queue_max": queue.maxsize,
        })
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            # Concurrent producer raced in; drop the new item too rather than
            # block indefinitely. Still observable via the second overflow.
            stats.overflows += 1
            emit("queue_overflow", {
                "stage": stage,
                "dropped": _summary(item),
                "kept": None,
                "queue_max": queue.maxsize,
                "note": "double_overflow",
            })


def _summary(item: Any) -> dict[str, Any] | None:
    if item is None:
        return None
    if isinstance(item, dict):
        keys = ("issue", "number", "title", "status", "verdict", "pr_url")
        out = {k: item.get(k) for k in keys if k in item}
        if "issue" not in out and "number" in item:
            out["issue"] = item["number"]
        return out
    return {"repr": repr(item)[:120]}


class AsyncOrchestrator:
    """Three-stage async pipeline with bounded queues + per-stage timeouts.

    Lifecycle:
        orch = AsyncOrchestrator(pools=..., caps=..., po_fn=..., ...)
        await orch.submit_many(issues)
        await orch.drain()   # waits for in-flight work, then shuts workers down

    Or, for a long-running loop, call ``orch.start()``, push issues with
    ``submit_issue`` as they arrive, and ``orch.stop()`` to shut down.
    """

    def __init__(
        self,
        *,
        pools: AsyncPools,
        caps: AsyncQueueCaps,
        po_fn: POFn,
        worker_fn: WorkerFn,
        critic_fn: CriticFn,
        emit: EmitFn | None = None,
        po_timeout_s: float = 600.0,
        worker_timeout_s: float = 7200.0,
        critic_timeout_s: float = 600.0,
        pr_recover_fn: Callable[[Issue], str | None] | None = None,
    ) -> None:
        self.pools = pools
        self.caps = caps
        self.po_fn = po_fn
        self.worker_fn = worker_fn
        self.critic_fn = critic_fn
        self.emit: EmitFn = emit or (lambda _k, _p: None)
        # Issue #213: when a worker is cancelled at the deadline AFTER opening
        # its PR, this recovers the PR URL (from the worktree log / events) so
        # the synthetic timeout/error result carries ``pr_url`` instead of
        # ``None`` — otherwise the PR is orphaned and never revisited. Default
        # ``None`` preserves the legacy ``pr_url=None`` behaviour for callers
        # that don't wire a recoverer.
        self.pr_recover_fn: Callable[[Issue], str | None] | None = pr_recover_fn
        self.po_timeout_s = po_timeout_s
        self.worker_timeout_s = worker_timeout_s
        self.critic_timeout_s = critic_timeout_s

        self.fresh: asyncio.Queue[Issue] = asyncio.Queue(maxsize=caps.fresh)
        self.po_ready: asyncio.Queue[tuple[Issue, POResult]] = asyncio.Queue(
            maxsize=caps.po_ready
        )
        self.pr_pending_critic: asyncio.Queue[WorkerResult] = asyncio.Queue(
            maxsize=caps.pr_pending_critic
        )

        self.stats = AsyncOrchestratorStats()
        self._tasks: list[asyncio.Task[None]] = []
        self._results: list[CriticResult] = []
        self._results_lock = asyncio.Lock()
        self._started = False

    # -------- public API --------

    async def submit_issue(self, issue: Issue) -> None:
        await _put_with_overflow(
            self.fresh, issue,
            stage="fresh_issues", emit=self.emit, stats=self.stats.po,
        )
        self.stats.po.enqueued += 1

    async def submit_many(self, issues: Iterable[Issue]) -> None:
        for i in issues:
            await self.submit_issue(i)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for slot in range(self.pools.po):
            self._tasks.append(asyncio.create_task(
                self._po_worker(slot), name=f"po-{slot}"
            ))
        for slot in range(self.pools.worker):
            self._tasks.append(asyncio.create_task(
                self._worker_worker(slot), name=f"worker-{slot}"
            ))
        for slot in range(self.pools.critic):
            self._tasks.append(asyncio.create_task(
                self._critic_worker(slot), name=f"critic-{slot}"
            ))

    async def drain(self) -> list[CriticResult]:
        """Wait for the pipeline to fully empty, then shut down workers.

        Use after ``submit_many`` when the caller knows no further issues
        will be enqueued in this tick.
        """
        if not self._started:
            self.start()
        await self.fresh.join()
        await self.po_ready.join()
        await self.pr_pending_critic.join()
        await self.stop()
        return list(self._results)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        # Collect cancellations; suppress CancelledError noise.
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
        self._started = False

    # -------- helpers --------

    def _recover_pr(self, issue: Issue) -> str | None:
        """Recover an orphaned PR URL for a cancelled/failed worker (#213).

        Best-effort: a missing or throwing ``pr_recover_fn`` yields ``None``
        so the worker stage degrades to the legacy ``pr_url=None`` behaviour
        rather than crashing the pipeline.
        """
        if self.pr_recover_fn is None:
            return None
        try:
            url = self.pr_recover_fn(issue)
        except Exception as ex:  # noqa: BLE001 — recovery must never kill the stage
            self.emit("orphan_pr_recover_failed", {
                "issue": issue.get("number"), "err": str(ex)[:200],
            })
            return None
        return url if isinstance(url, str) and url else None

    # -------- stage workers --------

    async def _po_worker(self, slot: int) -> None:
        while True:
            issue = await self.fresh.get()
            try:
                try:
                    po_result = await asyncio.wait_for(
                        self.po_fn(issue), timeout=self.po_timeout_s
                    )
                except TimeoutError:
                    self.stats.po.timeouts += 1
                    self.emit("stage_timeout", {
                        "stage": "po", "slot": slot,
                        "issue": issue.get("number"),
                        "timeout_s": self.po_timeout_s,
                    })
                    # Still forward to worker — let it decide whether the
                    # un-expanded body is workable. Mark the PO result so the
                    # worker brief can flag it.
                    po_result = {
                        "issue": issue.get("number"), "skipped": True,
                        "reason": "po_timeout", "sections_added": [],
                    }
                except Exception as ex:
                    self.stats.po.errors += 1
                    self.emit("stage_error", {
                        "stage": "po", "slot": slot,
                        "issue": issue.get("number"), "err": str(ex)[:200],
                    })
                    po_result = {
                        "issue": issue.get("number"), "skipped": True,
                        "reason": "po_error", "sections_added": [],
                        "error": str(ex)[:200],
                    }
                self.stats.po.processed += 1
                await _put_with_overflow(
                    self.po_ready, (issue, po_result),
                    stage="po_ready", emit=self.emit, stats=self.stats.worker,
                )
                self.stats.worker.enqueued += 1
            finally:
                self.fresh.task_done()

    async def _worker_worker(self, slot: int) -> None:
        while True:
            issue, po_result = await self.po_ready.get()
            try:
                try:
                    wr = await asyncio.wait_for(
                        self.worker_fn(issue, po_result),
                        timeout=self.worker_timeout_s,
                    )
                except TimeoutError:
                    self.stats.worker.timeouts += 1
                    self.emit("stage_timeout", {
                        "stage": "worker", "slot": slot,
                        "issue": issue.get("number"),
                        "timeout_s": self.worker_timeout_s,
                    })
                    wr = {
                        "issue": issue.get("number"), "status": "timeout",
                        "pr_url": self._recover_pr(issue), "error": "worker_timeout",
                    }
                except Exception as ex:
                    self.stats.worker.errors += 1
                    self.emit("stage_error", {
                        "stage": "worker", "slot": slot,
                        "issue": issue.get("number"), "err": str(ex)[:200],
                    })
                    wr = {
                        "issue": issue.get("number"), "status": "failed",
                        "pr_url": self._recover_pr(issue), "error": str(ex)[:200],
                    }
                self.stats.worker.processed += 1
                # Only PRs that opened/merged need a critic. Failed/no-pr
                # results bypass the critic queue but still land in results.
                if wr.get("pr_url") and wr.get("status") in {"open", "merged"}:
                    await _put_with_overflow(
                        self.pr_pending_critic, wr,
                        stage="pr_pending_critic",
                        emit=self.emit, stats=self.stats.critic,
                    )
                    self.stats.critic.enqueued += 1
                else:
                    async with self._results_lock:
                        self._results.append({
                            "issue": wr.get("issue"),
                            "worker": wr, "critic": None,
                        })
            finally:
                self.po_ready.task_done()

    async def _critic_worker(self, slot: int) -> None:
        while True:
            wr = await self.pr_pending_critic.get()
            try:
                try:
                    cr = await asyncio.wait_for(
                        self.critic_fn(wr), timeout=self.critic_timeout_s
                    )
                except TimeoutError:
                    self.stats.critic.timeouts += 1
                    self.emit("stage_timeout", {
                        "stage": "critic", "slot": slot,
                        "issue": wr.get("issue"),
                        "timeout_s": self.critic_timeout_s,
                    })
                    cr = {
                        "issue": wr.get("issue"), "verdict": "error",
                        "reasons": ["critic_timeout"],
                    }
                except Exception as ex:
                    self.stats.critic.errors += 1
                    self.emit("stage_error", {
                        "stage": "critic", "slot": slot,
                        "issue": wr.get("issue"), "err": str(ex)[:200],
                    })
                    cr = {
                        "issue": wr.get("issue"), "verdict": "error",
                        "reasons": [str(ex)[:200]],
                    }
                self.stats.critic.processed += 1
                async with self._results_lock:
                    self._results.append({
                        "issue": wr.get("issue"),
                        "worker": wr, "critic": cr,
                    })
            finally:
                self.pr_pending_critic.task_done()


async def run_async_tick(
    issues: list[Issue],
    *,
    pools: AsyncPools,
    caps: AsyncQueueCaps,
    po_fn: POFn,
    worker_fn: WorkerFn,
    critic_fn: CriticFn,
    emit: EmitFn | None = None,
    po_timeout_s: float = 600.0,
    worker_timeout_s: float = 7200.0,
    critic_timeout_s: float = 600.0,
    pr_recover_fn: Callable[[Issue], str | None] | None = None,
) -> tuple[list[CriticResult], AsyncOrchestratorStats]:
    """One-shot helper: build orchestrator, submit, drain, return results."""
    orch = AsyncOrchestrator(
        pools=pools, caps=caps,
        po_fn=po_fn, worker_fn=worker_fn, critic_fn=critic_fn,
        emit=emit,
        po_timeout_s=po_timeout_s,
        worker_timeout_s=worker_timeout_s,
        critic_timeout_s=critic_timeout_s,
        pr_recover_fn=pr_recover_fn,
    )
    orch.start()
    await orch.submit_many(issues)
    results = await orch.drain()
    return results, orch.stats
