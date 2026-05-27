"""Redis-backed ``Queue`` implementation.

Layout:

* ``<prefix>:waiting`` — Redis list, ``LPUSH`` to enqueue, ``BRPOPLPUSH``
  to atomically move into ``inflight``. Using the reliable-queue pattern
  means a crash between pop and ack doesn't lose the task.
* ``<prefix>:inflight`` — Redis list of task ids currently checked out.
* ``<prefix>:tasks:<id>`` — JSON-serialised ``Task`` payload.

The connection is lazy: we only require the ``redis`` package when this
module is imported, so the rest of the codebase keeps working without it.
A clean ``QueueUnavailable`` is raised when the broker can't be reached
so the runner can surface a useful error instead of crashing.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING, Any

from forge_loop.queue import QueueUnavailable, Task

if TYPE_CHECKING:  # pragma: no cover - type-only import
    import redis as _redis_t


def _load_redis() -> Any:
    try:
        import redis  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - exercised in import-error path
        raise QueueUnavailable(
            "redis backend requested but the `redis` package is not installed; "
            "install with: pip install 'forge-loop[redis]'"
        ) from exc
    return redis


class RedisQueue:
    """Reliable Redis-list queue.

    The ``LPUSH``/``BRPOPLPUSH`` pattern is the same one Sidekiq and RQ
    use: pop is atomic across the waiting and in-flight lists, so even a
    SIGKILL between pop and ack leaves the task recoverable (an operator
    or the leader can re-enqueue stuck in-flight ids).
    """

    def __init__(self, url: str, prefix: str = "forge_loop") -> None:
        self.url = url
        self.prefix = prefix
        self._k_waiting = f"{prefix}:waiting"
        self._k_inflight = f"{prefix}:inflight"
        self._client: _redis_t.Redis | None = None  # type: ignore[name-defined]

    # ---- internals ---------------------------------------------------

    def _connect(self) -> Any:
        if self._client is not None:
            return self._client
        redis = _load_redis()
        try:
            client = redis.Redis.from_url(self.url, decode_responses=True)
            client.ping()
        except Exception as exc:  # noqa: BLE001 - we translate any redis error
            raise QueueUnavailable(f"cannot reach Redis at {self.url}: {exc}") from exc
        self._client = client
        return client

    def _task_key(self, task_id: str) -> str:
        return f"{self.prefix}:tasks:{task_id}"

    # ---- Queue protocol ---------------------------------------------

    def push(self, payload: dict[str, object]) -> str:
        client = self._connect()
        tid = uuid.uuid4().hex
        task_blob = json.dumps(
            {
                "id": tid,
                "payload": payload,
                "attempts": 0,
                "enqueued_at": time.time(),
            }
        )
        try:
            pipe = client.pipeline()
            pipe.set(self._task_key(tid), task_blob)
            pipe.lpush(self._k_waiting, tid)
            pipe.execute()
        except Exception as exc:  # noqa: BLE001
            raise QueueUnavailable(f"push failed: {exc}") from exc
        return tid

    def pop_blocking(self, host_id: str, timeout: float = 1.0) -> Task | None:
        client = self._connect()
        try:
            # BRPOPLPUSH is the reliable-queue primitive: atomic move
            # from waiting → inflight. ``timeout`` is in seconds; Redis
            # uses 0 to mean "block forever", so a 0 here would hang the
            # runner shutdown — clamp to a small positive value instead.
            tid = client.brpoplpush(
                self._k_waiting,
                self._k_inflight,
                timeout=max(int(timeout), 1),
            )
        except Exception as exc:  # noqa: BLE001
            raise QueueUnavailable(f"pop failed: {exc}") from exc
        if tid is None:
            return None
        try:
            raw = client.get(self._task_key(tid))
        except Exception as exc:  # noqa: BLE001
            raise QueueUnavailable(f"pop fetch failed: {exc}") from exc
        if not raw:
            # Defensive: orphaned id in the list. Drop it.
            import contextlib

            with contextlib.suppress(Exception):
                client.lrem(self._k_inflight, 1, tid)
            return None
        data = json.loads(raw)
        data["attempts"] = int(data.get("attempts", 0)) + 1
        try:
            client.set(self._task_key(tid), json.dumps(data))
        except Exception as exc:  # noqa: BLE001
            raise QueueUnavailable(f"pop stamp failed: {exc}") from exc
        return Task(
            id=tid,
            payload=data["payload"],
            host_id=host_id,
            attempts=data["attempts"],
            enqueued_at=float(data.get("enqueued_at", 0.0)),
        )

    def ack(self, task_id: str) -> None:
        client = self._connect()
        try:
            pipe = client.pipeline()
            pipe.lrem(self._k_inflight, 1, task_id)
            pipe.delete(self._task_key(task_id))
            pipe.execute()
        except Exception as exc:  # noqa: BLE001
            raise QueueUnavailable(f"ack failed: {exc}") from exc

    def nack(self, task_id: str, requeue: bool = True) -> None:
        client = self._connect()
        try:
            pipe = client.pipeline()
            pipe.lrem(self._k_inflight, 1, task_id)
            if requeue:
                # Right-push to put it at the head so the next worker
                # retries this task before fresh work — same head-bias
                # the in-memory backend uses.
                pipe.rpush(self._k_waiting, task_id)
            else:
                pipe.delete(self._task_key(task_id))
            pipe.execute()
        except Exception as exc:  # noqa: BLE001
            raise QueueUnavailable(f"nack failed: {exc}") from exc

    def depth(self) -> int:
        client = self._connect()
        try:
            return int(client.llen(self._k_waiting))
        except Exception as exc:  # noqa: BLE001
            raise QueueUnavailable(f"depth failed: {exc}") from exc

    def in_flight(self) -> int:
        client = self._connect()
        try:
            return int(client.llen(self._k_inflight))
        except Exception as exc:  # noqa: BLE001
            raise QueueUnavailable(f"in_flight failed: {exc}") from exc
