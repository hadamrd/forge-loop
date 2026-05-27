"""Tests for ``forge_loop.queue.sqlite.SQLiteQueue`` (issue #39).

Covers happy-path FIFO, ack/nack semantics, durability across reopens,
and an adversarial concurrent-pop check that asserts no two workers pop
the same task.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from forge_loop.queue import build_queue
from forge_loop.queue.sqlite import SQLiteQueue


def test_round_trip_fifo(tmp_path: Path) -> None:
    """Happy path: push N, pop N — same order, ack removes them."""
    q = SQLiteQueue(tmp_path / "q.db")

    for i in range(5):
        q.push({"i": i})
    assert q.depth() == 5
    assert q.in_flight() == 0

    popped: list[int] = []
    for _ in range(5):
        t = q.pop_blocking("worker-A", timeout=0.5)
        assert t is not None
        popped.append(int(t.payload["i"]))
        q.ack(t.id)

    assert popped == [0, 1, 2, 3, 4]
    assert q.depth() == 0
    assert q.in_flight() == 0
    # ack of an unknown id is a no-op, not an error.
    q.ack("never-existed")


def test_pop_blocking_timeout_returns_none(tmp_path: Path) -> None:
    """Adversarial: pop on an empty queue returns None within timeout."""
    q = SQLiteQueue(tmp_path / "q.db")
    assert q.pop_blocking("w", timeout=0.1) is None


def test_nack_requeues_at_head(tmp_path: Path) -> None:
    """nack(requeue=True) returns the same task on the next pop."""
    q = SQLiteQueue(tmp_path / "q.db")
    q.push({"x": 1})
    q.push({"x": 2})

    t = q.pop_blocking("w", timeout=0.5)
    assert t is not None and t.payload["x"] == 1
    q.nack(t.id, requeue=True)

    assert q.in_flight() == 0
    assert q.depth() == 2

    # First waiting row by rowid is still task 1 — head bias preserved.
    t2 = q.pop_blocking("w", timeout=0.5)
    assert t2 is not None and t2.payload["x"] == 1


def test_nack_drop_deletes(tmp_path: Path) -> None:
    """nack(requeue=False) deletes the task — adversarial 'bad payload' case."""
    q = SQLiteQueue(tmp_path / "q.db")
    q.push({"poison": True})
    t = q.pop_blocking("w", timeout=0.5)
    assert t is not None
    q.nack(t.id, requeue=False)
    assert q.depth() == 0 and q.in_flight() == 0


def test_durability_across_reopen(tmp_path: Path) -> None:
    """Crucial: write, close, reopen → tasks survive (WAL flush)."""
    db = tmp_path / "durable.db"
    q1 = SQLiteQueue(db)
    q1.push({"k": "v1"})
    q1.push({"k": "v2"})
    q1.close()

    q2 = SQLiteQueue(db)
    assert q2.depth() == 2
    t = q2.pop_blocking("w", timeout=0.5)
    assert t is not None and t.payload["k"] == "v1"


def test_pop_increments_attempts(tmp_path: Path) -> None:
    """attempts counter advances on each pop (so retry policy can see it)."""
    q = SQLiteQueue(tmp_path / "q.db")
    q.push({})
    t = q.pop_blocking("w", timeout=0.5)
    assert t is not None and t.attempts == 1
    q.nack(t.id, requeue=True)
    t2 = q.pop_blocking("w", timeout=0.5)
    assert t2 is not None and t2.attempts == 2


def test_concurrent_pop_no_double_delivery(tmp_path: Path) -> None:
    """Adversarial: N threads pop K tasks — no task is popped twice."""
    q = SQLiteQueue(tmp_path / "q.db")
    n_tasks = 50
    for i in range(n_tasks):
        q.push({"i": i})

    seen: list[str] = []
    seen_lock = threading.Lock()

    def worker() -> None:
        while True:
            t = q.pop_blocking("w", timeout=0.05)
            if t is None:
                return
            with seen_lock:
                seen.append(t.id)
            q.ack(t.id)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(seen) == n_tasks
    assert len(set(seen)) == n_tasks  # no duplicates
    assert q.depth() == 0 and q.in_flight() == 0


def test_build_queue_factory_routes_sqlite(tmp_path: Path) -> None:
    """build_queue('sqlite://...') yields a real SQLiteQueue.

    Compare by class name rather than ``isinstance`` because other tests
    in the suite intentionally drop ``forge_loop.queue.sqlite`` from
    ``sys.modules`` and re-import it, which would yield a fresh class
    object that fails ``isinstance`` against the import bound at module
    top-level.
    """
    q = build_queue(f"sqlite://{tmp_path / 'f.db'}")
    assert type(q).__name__ == "SQLiteQueue"
    assert type(q).__module__ == "forge_loop.queue.sqlite"
    tid = q.push({"hello": "world"})
    assert isinstance(tid, str) and tid


def test_build_queue_factory_rejects_redis() -> None:
    """build_queue('redis://...') raises ValueError — backend removed in #39."""
    with pytest.raises(ValueError, match="Redis backend was removed"):
        build_queue("redis://localhost:6379/0")


def test_build_queue_factory_memory_default() -> None:
    """build_queue(None) returns the in-memory queue (test default)."""
    from forge_loop.queue.in_memory import InMemoryQueue

    assert isinstance(build_queue(None), InMemoryQueue)
    assert isinstance(build_queue("memory"), InMemoryQueue)
