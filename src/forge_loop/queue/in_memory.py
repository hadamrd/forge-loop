"""In-memory ``Queue`` implementation.

This is the default backend: zero infra, thread-safe, fast. Tasks live in
a ``deque`` guarded by a ``Condition`` so ``pop_blocking`` actually blocks
without a busy loop. In-flight tasks live in a dict so ``ack``/``nack``
can find them by id.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque

from forge_loop.queue import Task


class InMemoryQueue:
    """Process-local FIFO queue with ack/nack semantics."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._waiting: deque[Task] = deque()
        self._inflight: dict[str, Task] = {}

    def push(self, payload: dict[str, object]) -> str:
        tid = uuid.uuid4().hex
        task = Task(id=tid, payload=dict(payload), enqueued_at=time.time())
        with self._cond:
            self._waiting.append(task)
            self._cond.notify()
        return tid

    def pop_blocking(self, host_id: str, timeout: float = 1.0) -> Task | None:
        with self._cond:
            if not self._waiting:
                self._cond.wait(timeout=timeout)
            if not self._waiting:
                return None
            task = self._waiting.popleft()
            task.host_id = host_id
            task.attempts += 1
            self._inflight[task.id] = task
            return task

    def ack(self, task_id: str) -> None:
        with self._cond:
            self._inflight.pop(task_id, None)

    def nack(self, task_id: str, requeue: bool = True) -> None:
        with self._cond:
            task = self._inflight.pop(task_id, None)
            if task is None:
                return
            if requeue:
                self._waiting.appendleft(task)
                self._cond.notify()

    def depth(self) -> int:
        with self._cond:
            return len(self._waiting)

    def in_flight(self) -> int:
        with self._cond:
            return len(self._inflight)
