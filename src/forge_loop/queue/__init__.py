"""Queue abstraction for distributed task dispatch.

Two backends ship in-tree:

* ``InMemoryQueue`` — process-local, zero infra. Used by default so the
  single-host loop keeps working without any new dependency.
* ``RedisQueue`` — shared across hosts via a Redis list + hash. Selected
  with ``forge-loop run --queue redis://host:port/db``.

The interface is intentionally narrow: ``push``, ``pop_blocking``, ``ack``
and ``nack``. That is the surface every multi-host scheduler we know
about converges on (Celery, Sidekiq, RQ, BullMQ all reduce to it). The
``host_id`` stamp lets the operator trace which runner picked which
task; nothing in the protocol cares whose host pulls a job.
"""

from __future__ import annotations

import socket
import uuid
from dataclasses import dataclass, field
from typing import Protocol


def default_host_id() -> str:
    """Stable-ish identifier for this runner process.

    ``socket.gethostname()`` plus a short random tag means two processes
    on the same physical host don't collide, while operators can still
    eyeball the host portion in logs.
    """

    return f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"


@dataclass
class Task:
    """A unit of work pulled from the queue.

    ``id`` is the queue-assigned handle used by ``ack``/``nack``. ``payload``
    is opaque to the queue — the runner decides what it means. ``host_id``
    is stamped at pop time so we can trace which runner picked the work.
    """

    id: str
    payload: dict[str, object]
    host_id: str = ""
    attempts: int = 0
    enqueued_at: float = 0.0
    metadata: dict[str, str] = field(default_factory=dict)


class QueueUnavailable(RuntimeError):
    """Raised when the backend is unreachable.

    Surfaced (not crashed) so the runner can degrade gracefully and the
    operator gets a clear actionable error instead of a stack trace.
    """


class Queue(Protocol):
    """Minimal contract every backend must satisfy.

    Implementations MUST be safe to call from multiple threads; the
    Redis backend is also safe across processes/hosts.
    """

    def push(self, payload: dict[str, object]) -> str:
        """Enqueue a payload. Returns the task id."""

    def pop_blocking(self, host_id: str, timeout: float = 1.0) -> Task | None:
        """Pop one task. Returns ``None`` on timeout."""

    def ack(self, task_id: str) -> None:
        """Acknowledge successful processing. Removes the task from in-flight."""

    def nack(self, task_id: str, requeue: bool = True) -> None:
        """Negative-ack. If ``requeue`` is True, the task goes back to the head."""

    def depth(self) -> int:
        """Number of tasks waiting (not in flight)."""

    def in_flight(self) -> int:
        """Number of tasks currently checked out."""


def build_queue(url: str | None) -> Queue:
    """Factory: choose a backend from a URL.

    ``None`` or ``"memory"`` returns the in-memory queue. Anything starting
    with ``redis://`` or ``rediss://`` returns the Redis backend.
    """

    if url is None or url == "memory" or url == "memory://":
        from forge_loop.queue.in_memory import InMemoryQueue

        return InMemoryQueue()

    if url.startswith(("redis://", "rediss://")):
        from forge_loop.queue.redis_backend import RedisQueue

        return RedisQueue(url)

    raise ValueError(f"unsupported queue URL: {url!r}")


__all__ = [
    "Queue",
    "Task",
    "QueueUnavailable",
    "build_queue",
    "default_host_id",
]
