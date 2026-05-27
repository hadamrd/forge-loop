"""Tests for the async orchestrator (issue #7).

Covers:
- Happy path: pipeline drains end-to-end.
- Non-blocking PO: a fast worker proceeds while a slow PO is still in flight.
- Critic isolation: one hanging critic does not block merge of other PRs.
- Speedup: 3-PO + 3-worker + 3-critic concurrent run beats sequential timing.
- Adversarial: queue overflow drops oldest and emits ``queue_overflow``.
- Adversarial: worker/critic exceptions are isolated; pipeline keeps draining.
- Adversarial: stage timeouts surface as ``stage_timeout`` events.

Note: this project does NOT depend on pytest-asyncio. Each test wraps its
coroutine body in ``asyncio.run`` so the suite runs under stock pytest.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from forge_loop.runner_async import (
    AsyncOrchestrator,
    AsyncPools,
    AsyncQueueCaps,
    run_async_tick,
)


def _run(coro_fn: Callable[[], Any]) -> Any:
    return asyncio.run(coro_fn())


def _make_emit() -> tuple[list[tuple[str, dict]], Callable[[str, dict], None]]:
    captured: list[tuple[str, dict]] = []

    def _emit(kind: str, payload: dict) -> None:
        captured.append((kind, payload))

    return captured, _emit


def test_happy_path_drains_all_issues() -> None:
    async def body() -> None:
        async def po(issue):
            await asyncio.sleep(0)
            return {"issue": issue["number"], "skipped": False, "reason": "ok"}

        async def worker(issue, _po):
            await asyncio.sleep(0)
            return {
                "issue": issue["number"], "status": "merged",
                "pr_url": f"http://x/{issue['number']}",
            }

        async def critic(wr):
            await asyncio.sleep(0)
            return {"issue": wr["issue"], "verdict": "approved", "reasons": []}

        issues = [{"number": n, "title": f"t{n}"} for n in range(5)]
        results, stats = await run_async_tick(
            issues,
            pools=AsyncPools(po=2, worker=3, critic=2),
            caps=AsyncQueueCaps(),
            po_fn=po, worker_fn=worker, critic_fn=critic,
        )
        assert len(results) == 5
        assert {r["issue"] for r in results} == set(range(5))
        assert all(r["critic"]["verdict"] == "approved" for r in results)
        assert stats.po.processed == 5
        assert stats.worker.processed == 5
        assert stats.critic.processed == 5
        assert stats.po.overflows == 0

    asyncio.run(body())


def test_slow_po_does_not_block_other_workers() -> None:
    """A slow PO must not block a worker whose issue is already po_ready.

    Pools=(po=1, worker=2). Issue #1's PO hangs. We pre-seed po_ready with
    issue #2 (simulating an earlier tick that already expanded it), and the
    worker pool must drain it while #1's PO is still blocked.
    """
    async def body() -> None:
        po_started = asyncio.Event()
        po_release = asyncio.Event()

        async def po(issue):
            if issue["number"] == 1:
                po_started.set()
                await po_release.wait()
            return {"issue": issue["number"], "skipped": False, "reason": "ok"}

        worker_completed = asyncio.Event()

        async def worker(issue, _po):
            worker_completed.set()
            return {"issue": issue["number"], "status": "merged",
                    "pr_url": f"http://x/{issue['number']}"}

        async def critic(wr):
            return {"issue": wr["issue"], "verdict": "approved", "reasons": []}

        orch = AsyncOrchestrator(
            pools=AsyncPools(po=1, worker=2, critic=1),
            caps=AsyncQueueCaps(),
            po_fn=po, worker_fn=worker, critic_fn=critic,
        )
        orch.start()
        await orch.submit_issue({"number": 1, "title": "slow-po"})
        await po_started.wait()
        await orch.po_ready.put((
            {"number": 2, "title": "fast-path"},
            {"issue": 2, "skipped": False, "reason": "ok"},
        ))
        orch.stats.worker.enqueued += 1

        await asyncio.wait_for(worker_completed.wait(), timeout=2.0)
        assert not po_release.is_set(), \
            "slow PO completed before worker drained — pipeline is serial"

        po_release.set()
        results = await asyncio.wait_for(orch.drain(), timeout=5.0)
        assert {r["issue"] for r in results} == {1, 2}

    asyncio.run(body())


def test_hanging_critic_does_not_block_other_merges() -> None:
    """One hung critic must only hold its own slot — other PRs merge."""
    async def body() -> None:
        async def po(issue):
            return {"issue": issue["number"], "skipped": False, "reason": "ok"}

        async def worker(issue, _po):
            return {"issue": issue["number"], "status": "merged",
                    "pr_url": f"http://x/{issue['number']}"}

        hang_release = asyncio.Event()

        async def critic(wr):
            if wr["issue"] == 1:
                await hang_release.wait()
            return {"issue": wr["issue"], "verdict": "approved", "reasons": []}

        orch = AsyncOrchestrator(
            pools=AsyncPools(po=2, worker=2, critic=2),
            caps=AsyncQueueCaps(),
            po_fn=po, worker_fn=worker, critic_fn=critic,
        )
        orch.start()
        await orch.submit_issue({"number": 1, "title": "hang"})
        await asyncio.sleep(0.05)
        for n in (2, 3, 4):
            await orch.submit_issue({"number": n, "title": f"ok-{n}"})

        deadline = time.monotonic() + 3.0
        done: list = []
        while time.monotonic() < deadline:
            done = [r for r in orch._results
                    if (r.get("critic") or {}).get("verdict") == "approved"]
            if len(done) >= 3:
                break
            await asyncio.sleep(0.02)
        assert len(done) >= 3, \
            f"only {len(done)} critics drained while one hung"
        assert not hang_release.is_set()

        hang_release.set()
        results = await asyncio.wait_for(orch.drain(), timeout=5.0)
        assert {r["issue"] for r in results} == {1, 2, 3, 4}

    asyncio.run(body())


def test_concurrent_pipeline_beats_sequential_timing() -> None:
    """Integration timing: 3 issues × (PO + worker + critic), each ~100ms.

    Sequential lower bound: 9 × 100ms = 900ms.
    With pools=3/3/3 and pipelining we expect well under 600ms.
    """
    async def body() -> None:
        DELAY = 0.10

        async def po(issue):
            await asyncio.sleep(DELAY)
            return {"issue": issue["number"], "skipped": False, "reason": "ok"}

        async def worker(issue, _po):
            await asyncio.sleep(DELAY)
            return {"issue": issue["number"], "status": "merged",
                    "pr_url": f"http://x/{issue['number']}"}

        async def critic(wr):
            await asyncio.sleep(DELAY)
            return {"issue": wr["issue"], "verdict": "approved", "reasons": []}

        issues = [{"number": n, "title": f"t{n}"} for n in range(3)]
        t0 = time.monotonic()
        results, _ = await run_async_tick(
            issues,
            pools=AsyncPools(po=3, worker=3, critic=3),
            caps=AsyncQueueCaps(),
            po_fn=po, worker_fn=worker, critic_fn=critic,
        )
        elapsed = time.monotonic() - t0
        sequential = 9 * DELAY
        assert len(results) == 3
        assert elapsed < sequential * 0.7, (
            f"async pipeline took {elapsed:.3f}s, "
            f"expected < {sequential*0.7:.3f}s"
        )

    asyncio.run(body())


def test_queue_overflow_drops_oldest_and_emits_event() -> None:
    """Adversarial: full PO queue → oldest dropped + queue_overflow emitted."""
    async def body() -> None:
        block = asyncio.Event()

        async def po(issue):
            await block.wait()
            return {"issue": issue["number"], "skipped": False, "reason": "x"}

        async def worker(issue, _po):
            return {"issue": issue["number"], "status": "open", "pr_url": "u"}

        async def critic(wr):
            return {"issue": wr["issue"], "verdict": "approved", "reasons": []}

        captured, emit = _make_emit()
        orch = AsyncOrchestrator(
            pools=AsyncPools(po=1, worker=1, critic=1),
            caps=AsyncQueueCaps(fresh=2, po_ready=2, pr_pending_critic=2),
            po_fn=po, worker_fn=worker, critic_fn=critic, emit=emit,
        )
        orch.start()
        # PO worker pulls #1 immediately; queue then fills with #2, #3.
        await orch.submit_issue({"number": 1, "title": "a"})
        await asyncio.sleep(0.02)
        await orch.submit_issue({"number": 2, "title": "b"})
        await orch.submit_issue({"number": 3, "title": "c"})
        # Queue at capacity (2). Next submit must overflow.
        await orch.submit_issue({"number": 4, "title": "d"})

        overflows = [p for k, p in captured if k == "queue_overflow"]
        assert len(overflows) >= 1
        ov = overflows[0]
        assert ov["stage"] == "fresh_issues"
        assert ov["dropped"] is not None
        assert ov["dropped"].get("issue") == 2
        assert ov["kept"].get("issue") == 4
        assert orch.stats.po.overflows >= 1

        block.set()
        await asyncio.wait_for(orch.drain(), timeout=5.0)

    asyncio.run(body())


def test_worker_exception_is_isolated() -> None:
    """A worker raising must not poison the pool — other issues still drain."""
    async def body() -> None:
        async def po(i):
            return {"issue": i["number"], "skipped": False, "reason": "ok"}

        async def worker(issue, _po):
            if issue["number"] == 99:
                raise RuntimeError("boom")
            return {"issue": issue["number"], "status": "merged",
                    "pr_url": f"http://x/{issue['number']}"}

        async def critic(wr):
            return {"issue": wr["issue"], "verdict": "approved", "reasons": []}

        captured, emit = _make_emit()
        results, stats = await run_async_tick(
            [{"number": n, "title": str(n)} for n in (1, 99, 2, 3)],
            pools=AsyncPools(po=2, worker=2, critic=2),
            caps=AsyncQueueCaps(),
            po_fn=po, worker_fn=worker, critic_fn=critic, emit=emit,
        )
        by_issue = {r["issue"]: r for r in results}
        assert by_issue[99]["worker"]["status"] == "failed"
        assert by_issue[99]["critic"] is None
        for n in (1, 2, 3):
            assert by_issue[n]["worker"]["status"] == "merged"
            assert by_issue[n]["critic"]["verdict"] == "approved"
        assert stats.worker.errors == 1
        assert any(k == "stage_error" for k, _ in captured)

    asyncio.run(body())


def test_stage_timeout_uses_wait_for_not_signal() -> None:
    """Per-stage timeout via asyncio.wait_for surfaces stage_timeout event.

    Acceptance criterion: "Watchdog uses asyncio.wait_for, not signals."
    """
    async def body() -> None:
        async def po(i):
            await asyncio.sleep(5.0)
            return {"issue": i["number"], "skipped": False, "reason": "ok"}

        async def worker(i, _po):
            return {"issue": i["number"], "status": "open",
                    "pr_url": f"http://x/{i['number']}"}

        async def critic(wr):
            return {"issue": wr["issue"], "verdict": "approved", "reasons": []}

        captured, emit = _make_emit()
        results, stats = await run_async_tick(
            [{"number": 7, "title": "t"}],
            pools=AsyncPools(po=1, worker=1, critic=1),
            caps=AsyncQueueCaps(),
            po_fn=po, worker_fn=worker, critic_fn=critic, emit=emit,
            po_timeout_s=0.1,
        )
        assert stats.po.timeouts == 1
        assert any(k == "stage_timeout" and p["stage"] == "po"
                   for k, p in captured)
        # Downstream still drains — worker sees skipped po_result and continues.
        assert len(results) == 1
        assert results[0]["worker"]["status"] == "open"

    asyncio.run(body())


def test_pools_from_env_reads_loop_vars(monkeypatch) -> None:
    """Acceptance criterion: env vars LOOP_PO_POOL / LOOP_WORKER_POOL /
    LOOP_CRITIC_POOL control per-stage concurrency.
    """
    monkeypatch.setenv("LOOP_PO_POOL", "4")
    monkeypatch.setenv("LOOP_WORKER_POOL", "7")
    monkeypatch.setenv("LOOP_CRITIC_POOL", "2")
    p = AsyncPools.from_env(default_worker=3)
    assert (p.po, p.worker, p.critic) == (4, 7, 2)
