"""Redis-based leader election + heartbeat registry.

The election is the classic "SET key value NX PX ttl" pattern:

* Whichever process wins ``SET ... NX`` becomes the leader for ``ttl_ms``.
* The leader renews periodically via ``SET ... XX PX ttl_ms``.
* If the leader dies, the key expires; the next ``acquire`` from a
  follower wins and promotion happens within one TTL window.

This is the same pattern Redlock describes for a single-master Redis
deployment; we deliberately do not implement the multi-master Redlock
algorithm because the loop runs with a single Redis already and the
extra complexity isn't worth the marginal safety improvement at our
scale (see ACM's "How to do distributed locking" critique).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class RunnerStatus:
    """Snapshot of a runner reported via ``RunnerRegistry``."""

    runner_id: str
    host_id: str
    started_at: float
    last_heartbeat: float
    in_flight: int
    is_leader: bool


class LeaderElection:
    """Single-leader election using Redis SETNX with TTL.

    ``ttl`` is in seconds (TTL is 30s by spec, but configurable). Callers
    are expected to call ``renew`` more often than the TTL — a 1/3 ratio
    (renew every 10s for a 30s TTL) is the standard safety margin to
    survive a single missed renewal.
    """

    def __init__(
        self,
        client: Any,
        runner_id: str,
        key: str = "forge_loop:leader",
        ttl: float = 30.0,
    ) -> None:
        self._client = client
        self._runner_id = runner_id
        self._key = key
        self._ttl_ms = int(ttl * 1000)
        self._is_leader = False

    @property
    def runner_id(self) -> str:
        return self._runner_id

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    def acquire(self) -> bool:
        """Try to become leader. Returns True if we now hold the lease.

        Idempotent: if we already hold it, this is a no-op success. If
        another runner holds it, we return False without blocking.
        """

        # NX = only set if not exists; PX = TTL in ms. Atomic.
        ok = self._client.set(self._key, self._runner_id, nx=True, px=self._ttl_ms)
        if ok:
            self._is_leader = True
            return True
        # Were we already leader? Check the current holder.
        holder = self._client.get(self._key)
        if holder == self._runner_id:
            self._is_leader = True
            return True
        self._is_leader = False
        return False

    def renew(self) -> bool:
        """Extend our lease if we still hold it.

        Returns False if we lost the lease (e.g. paused longer than TTL).
        We use SET ... XX to refuse to claim the lease if it was never
        ours; the runner_id check via WATCH would be safer but the
        single-Redis trade-off is documented above.
        """

        holder = self._client.get(self._key)
        if holder != self._runner_id:
            self._is_leader = False
            return False
        # Reset TTL by re-SET-ing with the same value + new PX. XX so
        # we don't accidentally re-acquire after expiry — that path
        # must go through ``acquire`` and pay attention to the race.
        ok = self._client.set(self._key, self._runner_id, xx=True, px=self._ttl_ms)
        self._is_leader = bool(ok)
        return self._is_leader

    def release(self) -> None:
        """Release the lease if we still own it. Safe to call multiple times."""

        # Best-effort: read-then-delete is racy under heavy contention,
        # but we only call this on graceful shutdown where the worst
        # case is a follower waiting one TTL to take over.
        try:
            holder = self._client.get(self._key)
            if holder == self._runner_id:
                self._client.delete(self._key)
        finally:
            self._is_leader = False


class RunnerRegistry:
    """Heartbeat-based registry of live runners.

    Each runner writes ``<prefix>:runners:<id>`` with a TTL slightly longer
    than its heartbeat interval. ``list_runners`` enumerates the live
    entries; expired ones simply disappear because Redis evicted them.
    """

    def __init__(
        self,
        client: Any,
        runner_id: str,
        host_id: str,
        prefix: str = "forge_loop",
        ttl: float = 30.0,
    ) -> None:
        self._client = client
        self._runner_id = runner_id
        self._host_id = host_id
        self._prefix = prefix
        self._ttl_ms = int(ttl * 1000)
        self._started_at = time.time()

    def _key(self, runner_id: str | None = None) -> str:
        return f"{self._prefix}:runners:{runner_id or self._runner_id}"

    def _scan_pattern(self) -> str:
        return f"{self._prefix}:runners:*"

    def heartbeat(self, in_flight: int, is_leader: bool) -> None:
        """Write our status with a fresh TTL."""

        status = RunnerStatus(
            runner_id=self._runner_id,
            host_id=self._host_id,
            started_at=self._started_at,
            last_heartbeat=time.time(),
            in_flight=in_flight,
            is_leader=is_leader,
        )
        self._client.set(self._key(), json.dumps(asdict(status)), px=self._ttl_ms)

    def deregister(self) -> None:
        """Best-effort removal on shutdown."""

        import contextlib

        with contextlib.suppress(Exception):
            self._client.delete(self._key())

    def list_runners(self) -> list[RunnerStatus]:
        """Enumerate live runners. Expired entries are simply absent."""

        runners: list[RunnerStatus] = []
        # SCAN is preferred over KEYS for production safety (non-blocking).
        cursor = 0
        seen: set[str] = set()
        while True:
            cursor, batch = self._client.scan(cursor=cursor, match=self._scan_pattern(), count=100)
            for key in batch:
                if key in seen:
                    continue
                seen.add(key)
                raw = self._client.get(key)
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                runners.append(RunnerStatus(**data))
            if cursor == 0:
                break
        runners.sort(key=lambda r: r.runner_id)
        return runners
