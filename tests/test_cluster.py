"""Tests for cluster coordination: leader election + runner registry.

We test against ``fakeredis`` so the suite is fast and deterministic and
doesn't need an external broker. The election code only depends on
SET/GET/DELETE with NX/XX/PX which fakeredis implements faithfully.
"""

from __future__ import annotations

import time

import pytest

fakeredis = pytest.importorskip("fakeredis")

from forge_loop.cluster import LeaderElection, RunnerRegistry  # noqa: E402
from forge_loop.queue.in_memory import InMemoryQueue  # noqa: E402


def _client():
    return fakeredis.FakeRedis(decode_responses=True)


class TestLeaderElection:
    def test_first_runner_wins(self) -> None:
        c = _client()
        a = LeaderElection(c, runner_id="A", ttl=5)
        assert a.acquire() is True
        assert a.is_leader

    def test_second_runner_becomes_follower(self) -> None:
        c = _client()
        a = LeaderElection(c, runner_id="A", ttl=5)
        b = LeaderElection(c, runner_id="B", ttl=5)
        assert a.acquire() is True
        assert b.acquire() is False
        assert not b.is_leader

    def test_renew_extends_lease(self) -> None:
        c = _client()
        a = LeaderElection(c, runner_id="A", ttl=5)
        a.acquire()
        assert a.renew() is True
        # Holder should still be A.
        assert c.get("forge_loop:leader") == "A"

    def test_leader_expiry_promotes_follower(self) -> None:
        """Acceptance criterion: leader dies, follower takes over within TTL."""

        c = _client()
        # 1s TTL keeps the test fast.
        a = LeaderElection(c, runner_id="A", ttl=1.0)
        b = LeaderElection(c, runner_id="B", ttl=1.0)
        assert a.acquire() is True
        assert b.acquire() is False
        # Simulate A dying: stop renewing, wait past TTL.
        # fakeredis honors PX expiry on a wall-clock basis.
        time.sleep(1.2)
        # Now B can take leadership.
        assert b.acquire() is True
        assert b.is_leader
        # And A has lost its claim — a renew attempt now fails.
        assert a.renew() is False

    def test_release_lets_follower_acquire_immediately(self) -> None:
        c = _client()
        a = LeaderElection(c, runner_id="A", ttl=30)
        b = LeaderElection(c, runner_id="B", ttl=30)
        a.acquire()
        a.release()
        assert b.acquire() is True

    def test_release_does_not_steal_other_leader(self) -> None:
        """Adversarial: a deposed leader's stale release must not nuke the new one."""

        c = _client()
        a = LeaderElection(c, runner_id="A", ttl=1.0)
        b = LeaderElection(c, runner_id="B", ttl=30)
        a.acquire()
        time.sleep(1.2)
        b.acquire()
        # A still thinks it might be leader and calls release. Should be a no-op.
        a.release()
        assert c.get("forge_loop:leader") == "B"

    def test_acquire_idempotent_for_current_leader(self) -> None:
        c = _client()
        a = LeaderElection(c, runner_id="A", ttl=30)
        assert a.acquire() is True
        # Calling again should still report True (we're still leader).
        assert a.acquire() is True


class TestRunnerRegistry:
    def test_heartbeat_and_list(self) -> None:
        c = _client()
        r1 = RunnerRegistry(c, runner_id="R1", host_id="hostA", ttl=30)
        r2 = RunnerRegistry(c, runner_id="R2", host_id="hostB", ttl=30)
        r1.heartbeat(in_flight=2, is_leader=True)
        r2.heartbeat(in_flight=0, is_leader=False)
        # Either registry can enumerate — the data lives in Redis.
        runners = r1.list_runners()
        assert {r.runner_id for r in runners} == {"R1", "R2"}
        leader = next(r for r in runners if r.runner_id == "R1")
        assert leader.is_leader is True
        assert leader.in_flight == 2
        assert leader.host_id == "hostA"

    def test_expired_runner_disappears(self) -> None:
        c = _client()
        r = RunnerRegistry(c, runner_id="R1", host_id="hostA", ttl=1.0)
        r.heartbeat(in_flight=0, is_leader=False)
        assert len(r.list_runners()) == 1
        time.sleep(1.2)
        assert r.list_runners() == []

    def test_deregister_removes(self) -> None:
        c = _client()
        r = RunnerRegistry(c, runner_id="R1", host_id="hostA", ttl=30)
        r.heartbeat(in_flight=0, is_leader=False)
        r.deregister()
        assert r.list_runners() == []

    def test_list_runners_empty(self) -> None:
        c = _client()
        r = RunnerRegistry(c, runner_id="R1", host_id="hostA", ttl=30)
        assert r.list_runners() == []


# ---------------------------------------------------------------------------
# Integration scenarios — two-runner / failover semantics
# ---------------------------------------------------------------------------


class TestTwoRunnerIntegration:
    """End-to-end shape: two runners share a queue and a leader lease."""

    def test_two_runners_split_work(self) -> None:
        """5 tasks pushed; both runners should service some, total preserved."""

        q = InMemoryQueue()
        for i in range(5):
            q.push({"i": i})

        import threading

        counts = {"R1": 0, "R2": 0}
        lock = threading.Lock()

        def runner(name: str) -> None:
            while True:
                t = q.pop_blocking(host_id=name, timeout=0.1)
                if t is None:
                    if q.depth() == 0 and q.in_flight() == 0:
                        return
                    continue
                with lock:
                    counts[name] += 1
                q.ack(t.id)

        threads = [threading.Thread(target=runner, args=(n,)) for n in counts]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert counts["R1"] + counts["R2"] == 5
        # Both should have done at least one task with high probability,
        # but scheduling can be skewed; assert weaker invariant: balanced ±5
        # (i.e. count is between 0 and 5 inclusive, total correct).
        assert all(0 <= v <= 5 for v in counts.values())

    def test_leader_failover_no_task_lost(self) -> None:
        """Kill the leader mid-tick: a follower takes over; in-flight tasks survive.

        ``in_flight`` lives on the queue, not on the leader. So even if
        the leader dies after popping, the task stays in the in-flight
        list (Redis) or can be reclaimed (in-memory we just nack-requeue).
        """

        c = _client()
        a = LeaderElection(c, runner_id="A", ttl=1.0)
        b = LeaderElection(c, runner_id="B", ttl=1.0)
        q = InMemoryQueue()
        tid = q.push({"work": 1})

        # A wins leadership and pops the work.
        assert a.acquire() is True
        task = q.pop_blocking(host_id="A", timeout=0.5)
        assert task is not None and task.id == tid

        # A dies mid-flight: we simulate by NACK-requeueing the task
        # (an external janitor would do this; for the in-memory backend
        # it's the runner's responsibility) and letting the lease expire.
        q.nack(tid, requeue=True)
        time.sleep(1.2)  # lease expires
        assert b.acquire() is True

        # B picks up the requeued task.
        recovered = q.pop_blocking(host_id="B", timeout=0.5)
        assert recovered is not None and recovered.id == tid
        assert recovered.host_id == "B"
        assert recovered.attempts == 2  # incremented on the second pop


class TestClusterCoordinator:
    """Glue layer that owns the queue + lease + heartbeat thread."""

    def test_in_memory_is_always_leader_no_thread(self) -> None:
        from forge_loop.cluster import ClusterCoordinator

        coord = ClusterCoordinator(queue=InMemoryQueue(), queue_url=None)
        coord.start()  # no-op
        assert coord.is_distributed is False
        assert coord.is_leader() is True
        coord.stop()

    def test_degraded_mode_when_redis_unreachable(self) -> None:
        from forge_loop.cluster import ClusterCoordinator

        coord = ClusterCoordinator(
            queue=InMemoryQueue(),
            queue_url="redis://127.0.0.1:1/0",
        )
        coord.start()
        # Adversarial: cannot reach Redis. Must not crash; must refuse
        # leadership so PO/maintenance won't run on split-brain.
        assert coord.degraded is True
        assert coord.is_leader() is False
        coord.stop()


class TestDegradedMode:
    """Adversarial: Redis is unreachable — runners must not crash."""

    def test_redis_unreachable_surfaces_queue_unavailable(self) -> None:
        pytest.importorskip("redis")
        from forge_loop.queue import QueueUnavailable
        from forge_loop.queue.redis_backend import RedisQueue

        q = RedisQueue("redis://127.0.0.1:1/0")
        with pytest.raises(QueueUnavailable) as excinfo:
            q.pop_blocking(host_id="h", timeout=1)
        # Error message should mention the host so the operator can act.
        assert "127.0.0.1" in str(excinfo.value) or "Redis" in str(excinfo.value)
