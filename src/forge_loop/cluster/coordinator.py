"""ClusterCoordinator — glues queue, leader election, and heartbeat together.

This is the runner-facing surface: one object owns the Redis connection,
the leader lease, and the heartbeat thread. The runner asks
``coordinator.is_leader()`` before running the PO / maintenance passes;
all other paths (worker dispatch) are leader-agnostic and just pull from
the shared queue.

Designed so the rest of the runner can ignore Redis entirely when the
queue URL is in-memory (``start()`` is a no-op in that case).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from forge_loop.cluster.election import LeaderElection, RunnerRegistry
from forge_loop.queue import Queue, QueueUnavailable, default_host_id

log = logging.getLogger(__name__)


class ClusterCoordinator:
    """Cluster membership + leader lease for one runner process.

    When ``queue_url`` is in-memory, this collapses to a degenerate
    single-host coordinator: ``is_leader`` is always True, ``start``
    is a no-op, and there is no heartbeat thread. That keeps the
    zero-infra default path lightweight.
    """

    def __init__(
        self,
        queue: Queue,
        queue_url: str | None,
        ttl: float = 30.0,
        heartbeat_interval: float = 10.0,
        host_id: str | None = None,
    ) -> None:
        self._queue = queue
        self._queue_url = queue_url
        self._ttl = ttl
        self._interval = heartbeat_interval
        self.host_id = host_id or default_host_id()
        self.runner_id = self.host_id
        self._election: LeaderElection | None = None
        self._registry: RunnerRegistry | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._degraded = False

    # ---- lifecycle --------------------------------------------------

    @property
    def is_distributed(self) -> bool:
        """True if backed by Redis (multi-host capable)."""

        return bool(
            self._queue_url and self._queue_url.startswith(("redis://", "rediss://"))
        )

    def _connect_redis(self) -> Any | None:
        """Return a Redis client or None if degraded.

        We surface this as ``degraded mode`` rather than crash because
        the acceptance criteria call for graceful handling: log loudly,
        stop dispatching, but keep the process alive.
        """

        if not self.is_distributed:
            return None
        try:
            from forge_loop.queue.redis_backend import _load_redis  # noqa: PLC2701

            redis = _load_redis()
            client = redis.Redis.from_url(self._queue_url, decode_responses=True)
            client.ping()
            return client
        except QueueUnavailable as exc:
            log.error("cluster: redis unavailable (%s) — degraded mode", exc)
            self._degraded = True
            return None
        except Exception as exc:  # noqa: BLE001
            log.error("cluster: redis connect failed (%s) — degraded mode", exc)
            self._degraded = True
            return None

    def start(self) -> None:
        """Begin heartbeat + leader-election loop in a daemon thread."""

        if not self.is_distributed:
            return
        client = self._connect_redis()
        if client is None:
            return
        self._election = LeaderElection(client, runner_id=self.runner_id, ttl=self._ttl)
        self._registry = RunnerRegistry(
            client, runner_id=self.runner_id, host_id=self.host_id, ttl=self._ttl
        )
        self._thread = threading.Thread(
            target=self._tick_loop, name=f"cluster-{self.runner_id}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 1.0)
        if self._election is not None:
            import contextlib

            with contextlib.suppress(Exception):
                self._election.release()
        if self._registry is not None:
            self._registry.deregister()

    # ---- accessors --------------------------------------------------

    def is_leader(self) -> bool:
        """Whether this runner currently holds the leader lease.

        In single-host mode every runner is its own leader.
        """

        if not self.is_distributed:
            return True
        if self._degraded or self._election is None:
            # Degraded mode: refuse to claim leadership rather than
            # silently run PO+maintenance on a split-brain.
            return False
        return self._election.is_leader

    @property
    def degraded(self) -> bool:
        return self._degraded

    # ---- internal ---------------------------------------------------

    def _tick_loop(self) -> None:
        """Renew lease + heartbeat every ``interval`` seconds."""

        assert self._election is not None and self._registry is not None
        while not self._stop.is_set():
            try:
                if self._election.is_leader:
                    if not self._election.renew():
                        # Lost lease — try to re-acquire on the next pass.
                        self._election.acquire()
                else:
                    self._election.acquire()
                self._registry.heartbeat(
                    in_flight=self._queue.in_flight(),
                    is_leader=self._election.is_leader,
                )
            except QueueUnavailable as exc:
                log.warning("cluster: heartbeat skipped — %s", exc)
                self._degraded = True
            except Exception as exc:  # noqa: BLE001
                log.warning("cluster: heartbeat error — %s", exc)
                self._degraded = True
            # Wake early on stop so shutdown is snappy.
            self._stop.wait(self._interval)
