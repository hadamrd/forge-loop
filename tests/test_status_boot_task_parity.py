"""Parity contract: control-plane *status* and *boot* agree on task/lease health.

Issue #374. The control plane exposes two operator-facing readers of task/lease
health and nothing pins their *counting logic* together:

* :func:`collect_control_plane_status` reports ``tasks.in_flight_count`` and
  ``tasks.stale_lease_count``.
* :func:`assemble_boot_context` reconstructs ``in_flight_task_ids`` /
  ``stale_saga_ids``.

A drift in either reader's "in-flight" definition or stale-lease cutoff is a
classic *wrong-but-green* divergence: every fast unit test stays green while
operators see contradictory health.

IMPORTANT — what this test actually pins (and an honest caveat):

The two readers do **not** load the same durable store today. ``status`` reads
``docs/ops/worker-sessions.db`` (``WorkerSessionStore``); ``boot`` reads
``.forge/tasks.db`` (``SqliteTaskSagaStore``). The issue's premise that both
derive from a single ``.forge/tasks.db`` does not hold against the current code.
Per the ticket's "do not change production logic; file a separate bug" rule we
do **not** unify them here. Instead this contract seeds *both* durable stores
with logically-equivalent state at one fixed ``now`` and pins that their
*counting logic* agrees — which is the falsifiable headline: the test fails iff
``status``'s task counts disagree with ``boot``'s reconstructed in-flight/stale
saga sets for the same ``repo`` and ``now``.

It also characterises the stale cutoff boundary: a lease that expires exactly at
``now`` must be classified consistently by both readers (see
:class:`TestStaleLeaseBoundaryDivergence`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from forge_loop.control.boot import (
    BootContext,
    assemble_boot_context,
    build_boot_sources,
    canonical_task_saga_path,
)
from forge_loop.control.status import collect_control_plane_status
from forge_loop.eventlog import SqliteEventLog
from forge_loop.frontier import FrontierCursor, FrontierStore
from forge_loop.memory import SqliteMemoryStore
from forge_loop.tasks import SqliteTaskSagaStore
from forge_loop.worker_sessions import WorkerSessionStore
from forge_loop.worker_state import WorkerState

# A fixed UTC clock shared by both readers so the stale-lease boundary is
# deterministic — never ``datetime.now()``.
T0 = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)
_FRESH = T0 + timedelta(minutes=5)  # lease in the future  -> in-flight, not stale
_STALE = T0 - timedelta(minutes=5)  # lease in the past    -> in-flight, stale
_ACQUIRED = T0 - timedelta(hours=1)  # any time before the lease expiry


class _Spec(NamedTuple):
    """One logical task/session to materialise into *both* durable stores."""

    issue: int
    lease: datetime | None
    terminal: bool


# 2 in-flight (one fresh lease, one expired lease) + 1 terminal.
_MIXED: tuple[_Spec, ...] = (
    _Spec(issue=101, lease=_FRESH, terminal=False),
    _Spec(issue=102, lease=_STALE, terminal=False),
    _Spec(issue=103, lease=None, terminal=True),
)


def _seed_durable_base(repo: Path) -> None:
    """Seed the ``.forge`` stores ``build_boot_sources`` requires to load."""
    forge = repo / ".forge"
    forge.mkdir(parents=True, exist_ok=True)
    FrontierStore(forge / "frontier.yaml").save(
        FrontierCursor(
            product_goal="status and boot agree on task/lease health",
            current_problem="two readers count in-flight/stale work independently",
            next_expansion="pin their counting logic with a parity contract",
            why_now="wrong-but-green divergence is invisible to fast unit tests",
        )
    )
    # Open (and so create) an empty event log + memory store: boot opens both
    # unconditionally. An empty log means latest_sequence()==0 so projection
    # replay is skipped, keeping the fixture about task/lease health only.
    SqliteEventLog(forge / "events.db")
    SqliteMemoryStore(forge / "memory.db")


def _seed_task_sagas(repo: Path, specs: tuple[_Spec, ...]) -> None:
    """Materialise ``specs`` into the durable ``.forge/tasks.db`` boot reads."""
    store = SqliteTaskSagaStore(canonical_task_saga_path(repo))
    try:
        for spec in specs:
            task_id = f"task-{spec.issue}"
            store.create(
                task_id=task_id,
                saga_id=f"saga-{spec.issue}",
                issue=spec.issue,
                branch=f"loop/{spec.issue}",
                worktree=f"/tmp/wt-{spec.issue}",
                compensations=(),
            )
            if spec.terminal:
                store.mark_completed(task_id)
            elif spec.lease is not None:
                store.acquire_lease(
                    task_id,
                    owner_id=f"worker-{spec.issue}",
                    expires_at=spec.lease,
                    acquired_at=_ACQUIRED,
                )
    finally:
        store.close()


def _seed_worker_sessions(repo: Path, specs: tuple[_Spec, ...]) -> None:
    """Materialise ``specs`` into the durable worker-sessions store status reads."""
    ops = repo / "docs" / "ops"
    ops.mkdir(parents=True, exist_ok=True)
    store = WorkerSessionStore(ops / "worker-sessions.db")
    try:
        for spec in specs:
            session = store.create(issue=spec.issue, branch=f"loop/{spec.issue}")
            if spec.terminal:
                store.transition_to(session.session_id, WorkerState.ABANDONED)
                continue
            store.transition_to(session.session_id, WorkerState.RUNNING)
            if spec.lease is not None:
                store.set_lease_expires_at(session.session_id, spec.lease.isoformat())
    finally:
        store.close()


def _seed(repo: Path, specs: tuple[_Spec, ...]) -> None:
    """Seed one real ``.forge`` repo, with equivalent state in *both* readers' stores."""
    _seed_durable_base(repo)
    _seed_task_sagas(repo, specs)
    _seed_worker_sessions(repo, specs)


def _status_tasks(repo: Path) -> dict[str, object]:
    return collect_control_plane_status(repo, T0)["tasks"]


def _boot(repo: Path) -> BootContext:
    return assemble_boot_context(build_boot_sources(repo), now=T0)


class TestStatusBootTaskParity:
    """status's task counts == boot's reconstructed in-flight/stale set sizes."""

    def test_in_flight_count_matches_boot_in_flight_ids(self, tmp_path: Path) -> None:
        _seed(tmp_path, _MIXED)

        status_tasks = _status_tasks(tmp_path)
        boot = _boot(tmp_path)

        assert status_tasks["in_flight_count"] == len(boot.in_flight_task_ids)
        # Non-vacuous: an accidental empty store cannot make parity pass trivially.
        assert status_tasks["in_flight_count"] == 2

    def test_stale_lease_count_matches_boot_stale_ids(self, tmp_path: Path) -> None:
        _seed(tmp_path, _MIXED)

        status_tasks = _status_tasks(tmp_path)
        boot = _boot(tmp_path)

        assert status_tasks["stale_lease_count"] == len(boot.stale_saga_ids)
        assert status_tasks["stale_lease_count"] == 1

    def test_counter_sanity_exact_seeded_numbers(self, tmp_path: Path) -> None:
        # Unit-level counter sanity: a regression in the *fixture* is
        # distinguishable from a regression in the *readers*.
        _seed(tmp_path, _MIXED)

        status_tasks = _status_tasks(tmp_path)
        boot = _boot(tmp_path)

        assert status_tasks["in_flight_count"] == 2
        assert status_tasks["stale_lease_count"] == 1
        assert len(boot.in_flight_task_ids) == 2
        assert len(boot.stale_saga_ids) == 1
        # Teeth: both counters are non-trivially exercised (not just 0 == 0).
        assert status_tasks["in_flight_count"] != 0
        assert status_tasks["stale_lease_count"] != 0


class TestTerminalExclusion:
    """A terminal saga/session is counted as in-flight by *neither* reader."""

    def test_terminal_saga_is_not_in_flight_for_either_reader(self, tmp_path: Path) -> None:
        _seed(tmp_path, _MIXED)

        status_tasks = _status_tasks(tmp_path)
        boot = _boot(tmp_path)

        # issue 103 is terminal; only the two non-terminal issues remain.
        assert status_tasks["in_flight_count"] == 2
        assert len(boot.in_flight_task_ids) == 2
        assert "task-103" not in boot.in_flight_task_ids
        assert "saga-103" not in boot.stale_saga_ids


class TestVacuousPassGuard:
    """Empty stores -> both readers report 0, so parity can't pass vacuously."""

    def test_empty_stores_report_zero_for_both_readers(self, tmp_path: Path) -> None:
        # Create the durable stores but seed *no* sagas/sessions, so both stay
        # "available" with empty contents (not "unavailable").
        _seed(tmp_path, ())

        status_tasks = _status_tasks(tmp_path)
        boot = _boot(tmp_path)

        assert status_tasks["in_flight_count"] == 0
        assert status_tasks["stale_lease_count"] == 0
        assert len(boot.in_flight_task_ids) == 0
        assert len(boot.stale_saga_ids) == 0

    def test_mixed_seed_is_non_zero_so_parity_is_not_vacuous(self, tmp_path: Path) -> None:
        _seed(tmp_path, _MIXED)

        status_tasks = _status_tasks(tmp_path)
        boot = _boot(tmp_path)

        assert status_tasks["in_flight_count"] > 0
        assert status_tasks["stale_lease_count"] > 0
        assert len(boot.in_flight_task_ids) > 0
        assert len(boot.stale_saga_ids) > 0


class TestStaleLeaseBoundaryDivergence:
    """Characterise the lease-expiry boundary shared by status and boot."""

    _BOUNDARY: tuple[_Spec, ...] = (_Spec(issue=201, lease=T0, terminal=False),)

    def test_in_flight_count_still_agrees_at_the_boundary(self, tmp_path: Path) -> None:
        _seed(tmp_path, self._BOUNDARY)

        status_tasks = _status_tasks(tmp_path)
        boot = _boot(tmp_path)

        assert status_tasks["in_flight_count"] == len(boot.in_flight_task_ids) == 1

    def test_stale_cutoff_agrees_at_the_boundary(self, tmp_path: Path) -> None:
        _seed(tmp_path, self._BOUNDARY)

        status_tasks = _status_tasks(tmp_path)
        boot = _boot(tmp_path)

        assert status_tasks["stale_lease_count"] == len(boot.stale_saga_ids) == 1

    def test_readers_should_agree_at_the_boundary(self, tmp_path: Path) -> None:
        _seed(tmp_path, self._BOUNDARY)

        status_tasks = _status_tasks(tmp_path)
        boot = _boot(tmp_path)

        assert status_tasks["stale_lease_count"] == len(boot.stale_saga_ids)
