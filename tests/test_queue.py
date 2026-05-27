"""Tests for the queue abstraction.

Covers the in-memory backend exhaustively and the Redis backend through
``fakeredis`` when it is available — in CI without that dep we skip the
Redis tests rather than failing, because the production stack does not
require Redis to be installed.
"""

from __future__ import annotations

import threading
import time

import pytest

from forge_loop.queue import (
    QueueUnavailable,
    Task,
    build_queue,
    default_host_id,
)
from forge_loop.queue.in_memory import InMemoryQueue

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _redis_backend():
    """Return a RedisQueue wrapping fakeredis, or skip the test if unavailable."""

    fakeredis = pytest.importorskip("fakeredis")
    from forge_loop.queue.redis_backend import RedisQueue

    q = RedisQueue("redis://localhost:6379/0", prefix=f"test_{int(time.time() * 1000)}")
    q._client = fakeredis.FakeRedis(decode_responses=True)
    return q


# ---------------------------------------------------------------------------
# in-memory backend
# ---------------------------------------------------------------------------


class TestInMemoryQueue:
    """Happy path + adversarial cases for the default backend."""

    def test_push_pop_round_trip(self) -> None:
        q = InMemoryQueue()
        tid = q.push({"issue": 42})
        assert q.depth() == 1
        task = q.pop_blocking(host_id="h1", timeout=0.1)
        assert task is not None
        assert task.id == tid
        assert task.payload == {"issue": 42}
        assert task.host_id == "h1"
        assert task.attempts == 1
        assert q.depth() == 0
        assert q.in_flight() == 1

    def test_pop_blocks_until_push(self) -> None:
        q = InMemoryQueue()
        result: list[Task | None] = []

        def consume() -> None:
            result.append(q.pop_blocking(host_id="h1", timeout=2.0))

        t = threading.Thread(target=consume)
        t.start()
        time.sleep(0.05)
        q.push({"issue": 1})
        t.join(timeout=2.0)
        assert result and result[0] is not None
        assert result[0].payload == {"issue": 1}

    def test_pop_returns_none_on_timeout(self) -> None:
        q = InMemoryQueue()
        # Adversarial: empty queue, short timeout, must NOT raise.
        assert q.pop_blocking(host_id="h", timeout=0.05) is None

    def test_ack_clears_inflight(self) -> None:
        q = InMemoryQueue()
        tid = q.push({"x": 1})
        task = q.pop_blocking(host_id="h", timeout=0.1)
        assert task is not None
        q.ack(tid)
        assert q.in_flight() == 0

    def test_nack_requeues(self) -> None:
        q = InMemoryQueue()
        tid = q.push({"x": 1})
        task = q.pop_blocking(host_id="h", timeout=0.1)
        assert task is not None
        q.nack(tid, requeue=True)
        assert q.depth() == 1
        assert q.in_flight() == 0
        # Re-popped task should preserve attempts counter increment.
        again = q.pop_blocking(host_id="h", timeout=0.1)
        assert again is not None
        assert again.attempts == 2

    def test_nack_drop(self) -> None:
        q = InMemoryQueue()
        tid = q.push({"x": 1})
        q.pop_blocking(host_id="h", timeout=0.1)
        q.nack(tid, requeue=False)
        assert q.depth() == 0
        assert q.in_flight() == 0

    def test_ack_unknown_id_is_noop(self) -> None:
        """Adversarial: ack/nack on a never-seen id must not raise."""

        q = InMemoryQueue()
        q.ack("does-not-exist")
        q.nack("does-not-exist")  # default requeue=True, must still be safe

    def test_balanced_pop_across_two_workers(self) -> None:
        """Integration: two consumers split 5 tasks roughly evenly."""

        q = InMemoryQueue()
        for i in range(5):
            q.push({"i": i})
        counts = {"a": 0, "b": 0}
        lock = threading.Lock()
        stop = threading.Event()

        def worker(name: str) -> None:
            while not stop.is_set():
                t = q.pop_blocking(host_id=name, timeout=0.05)
                if t is None:
                    if q.depth() == 0:
                        return
                    continue
                with lock:
                    counts[name] += 1
                q.ack(t.id)

        threads = [threading.Thread(target=worker, args=(n,)) for n in counts]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        stop.set()
        # The strong invariant we care about is "no task lost"; under
        # the GIL one consumer can outpace the other so we don't assert
        # a specific balance ratio (the spec calls for ±1 in the Redis
        # integration test where two real processes consume).
        assert counts["a"] + counts["b"] == 5


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------


class TestBuildQueue:
    def test_default_is_memory(self) -> None:
        assert isinstance(build_queue(None), InMemoryQueue)
        assert isinstance(build_queue("memory"), InMemoryQueue)
        assert isinstance(build_queue("memory://"), InMemoryQueue)

    def test_unsupported_url_raises(self) -> None:
        with pytest.raises(ValueError, match="unsupported"):
            build_queue("kafka://broker:9092")

    def test_redis_url_constructs(self) -> None:
        pytest.importorskip("redis")
        q = build_queue("redis://localhost:6379/0")
        # We don't actually connect — just verify the type/url.
        from forge_loop.queue.redis_backend import RedisQueue

        assert isinstance(q, RedisQueue)
        assert q.url == "redis://localhost:6379/0"


class TestDefaultHostId:
    def test_unique_per_call(self) -> None:
        a, b = default_host_id(), default_host_id()
        assert a != b
        # Should at least contain the hostname-ish prefix.
        assert "-" in a


# ---------------------------------------------------------------------------
# Redis backend (via fakeredis)
# ---------------------------------------------------------------------------


class TestRedisQueue:
    def test_push_pop_ack(self) -> None:
        q = _redis_backend()
        tid = q.push({"hello": "world"})
        assert q.depth() == 1
        task = q.pop_blocking(host_id="hostA", timeout=1)
        assert task is not None
        assert task.id == tid
        assert task.payload == {"hello": "world"}
        assert task.host_id == "hostA"
        assert q.in_flight() == 1
        q.ack(tid)
        assert q.in_flight() == 0

    def test_nack_requeues_at_head(self) -> None:
        q = _redis_backend()
        a = q.push({"order": 1})
        b = q.push({"order": 2})
        # pop A, nack — should be next to come back before B.
        first = q.pop_blocking(host_id="h", timeout=1)
        assert first is not None and first.id == a
        q.nack(a, requeue=True)
        # Now both a and b are waiting; a was reinserted at head.
        second = q.pop_blocking(host_id="h", timeout=1)
        assert second is not None
        assert second.id == a  # head-of-line retry
        q.ack(a)
        third = q.pop_blocking(host_id="h", timeout=1)
        assert third is not None and third.id == b

    def test_pop_timeout_returns_none(self) -> None:
        q = _redis_backend()
        # fakeredis honors brpoplpush timeout; min clamp is 1s.
        assert q.pop_blocking(host_id="h", timeout=1) is None

    def test_unreachable_redis_raises_queue_unavailable(self) -> None:
        """Adversarial: surface a clear error when broker is unreachable."""

        pytest.importorskip("redis")
        from forge_loop.queue.redis_backend import RedisQueue

        # Port 1 is reserved; nothing will be listening.
        q = RedisQueue("redis://127.0.0.1:1/0")
        with pytest.raises(QueueUnavailable):
            q.push({"x": 1})
