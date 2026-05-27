"""Cluster coordination primitives for the multi-host runner.

* ``LeaderElection`` — Redis SETNX-with-TTL leader lease. Only the leader
  runs the PO + maintenance passes; all followers still pull workers
  from the shared queue.
* ``RunnerRegistry`` — heartbeat tracking so ``forge-loop cluster status``
  can enumerate live runners and their load.

Both classes accept any object that quacks like a ``redis.Redis`` (so we
can unit-test against ``fakeredis`` without spinning up a real broker).
"""

from forge_loop.cluster.coordinator import ClusterCoordinator
from forge_loop.cluster.election import LeaderElection, RunnerRegistry, RunnerStatus

__all__ = ["ClusterCoordinator", "LeaderElection", "RunnerRegistry", "RunnerStatus"]
