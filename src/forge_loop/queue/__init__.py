"""Queue abstraction for the loop's task dispatch.

Two backends ship in-tree:

* ``InMemoryQueue`` — process-local, zero infra. The test default.
* ``SQLiteQueue`` — durable embedded queue (WAL mode). The production
  default: ACID, zero infrastructure, survives crashes. Selected with
  ``forge-loop run --queue sqlite:///path/to/queue.db`` (or the bare
  path ``sqlite:queue.db``).

The interface is intentionally narrow: ``push``, ``pop_blocking``, ``ack``
and ``nack``. That is the surface every single-host scheduler we care
about reduces to. The ``host_id`` stamp is preserved as a trace hint
even though the loop now targets one operator on one box.

Redis + multi-host cluster coordination were removed in #39 — they
were premature distribution with no real operator behind them. Re-add
when (and only when) a 2+ host deployment shows up.
"""

from __future__ import annotations

import socket
import uuid
from dataclasses import dataclass, field
from typing import Protocol


def default_host_id() -> str:
    """Stable-ish identifier for this runner process."""

    return f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"


@dataclass
class Task:
    """A unit of work pulled from the queue."""

    id: str
    payload: dict[str, object]
    host_id: str = ""
    attempts: int = 0
    enqueued_at: float = 0.0
    metadata: dict[str, str] = field(default_factory=dict)


class QueueUnavailable(RuntimeError):
    """Raised when the backend is unreachable / I/O-failed."""


class Queue(Protocol):
    """Minimal contract every backend must satisfy."""

    def push(self, payload: dict[str, object]) -> str: ...
    def pop_blocking(self, host_id: str, timeout: float = 1.0) -> Task | None: ...
    def ack(self, task_id: str) -> None: ...
    def nack(self, task_id: str, requeue: bool = True) -> None: ...
    def depth(self) -> int: ...
    def in_flight(self) -> int: ...


def build_queue(url: str | None) -> Queue:
    """Factory: choose a backend from a URL.

    Accepted forms:
      * ``None`` / ``"memory"`` / ``"memory://"`` → in-memory queue
      * ``"sqlite:///abs/path/queue.db"`` → SQLite (absolute path)
      * ``"sqlite:relative/path.db"`` → SQLite (relative path)
      * ``"sqlite://"`` alone → in-memory SQLite (``:memory:``)
    """

    if url is None or url in ("memory", "memory://"):
        from forge_loop.queue.in_memory import InMemoryQueue

        return InMemoryQueue()

    if url.startswith("sqlite://"):
        rest = url[len("sqlite://") :]
        path = rest or ":memory:"
        from forge_loop.queue.sqlite import SQLiteQueue

        return SQLiteQueue(path)

    if url.startswith("sqlite:"):
        path = url[len("sqlite:") :] or ":memory:"
        from forge_loop.queue.sqlite import SQLiteQueue

        return SQLiteQueue(path)

    if url.startswith(("redis://", "rediss://")):
        raise ValueError(
            "Redis backend was removed in #39 (premature distribution). "
            "Use sqlite:///path/to/queue.db or 'memory' instead."
        )

    raise ValueError(f"unsupported queue URL: {url!r}")


__all__ = [
    "Queue",
    "Task",
    "QueueUnavailable",
    "build_queue",
    "default_host_id",
]
