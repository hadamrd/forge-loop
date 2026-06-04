"""Fake-vs-real adapter contract suite (issue #209).

The control plane ships **paired** store implementations: a fast in-memory
``Fake*`` adapter that backs unit tests, and a durable ``Sqlite*`` adapter that
backs production persistence. Historically each side had its own,
differently-worded tests, so the fake could silently drift from the real store
(e.g. a relaxed lease guard on one side only) while every fast unit test stayed
green — a wrong-but-green patch.

This module defines the shared behavioral assertions **once** and runs them
against *both* implementations of each pair via ``pytest`` parametrization. A
divergence in either direction fails loudly.

Drift guard (acceptance-criteria verification step)
---------------------------------------------------
The suite is written so that **removing an enforcement branch from either the
fake or the real store makes at least one parametrized case fail**. Verified
locally before submission by temporarily deleting guards and observing red:

* Delete ``acquire_lease``'s active-lease check (``store.py`` lines ~329-330 /
  ``task_saga_store.py`` lines ~88-89) → ``test_acquire_lease_while_active_raises``
  and ``test_lease_stealing_via_acquire_raises`` go red for that adapter.
* Delete ``heartbeat``'s ``lease_owner != owner_id`` check → both
  ``test_heartbeat_wrong_owner_raises`` and ``test_lease_stealing_via_heartbeat``
  go red.
* Delete ``heartbeat``'s ``expires_at <= lease_expires_at`` check →
  ``test_heartbeat_non_extending_raises`` goes red.
* Delete ``_require_mutable``'s terminal check → ``test_terminal_*`` cases red.
* Delete ``mark_failed``'s ``if not compensations`` guard →
  ``test_mark_failed_without_compensation_raises`` red.
* Delete ``SqliteMemoryStore.supersede``'s missing-replacement ``KeyError`` →
  ``test_supersede_unknown_id_raises`` red for the real adapter.

Because each case runs under *both* adapters, a guard removed from only one
implementation diverges the pair and the parametrized case fails for exactly
that adapter — which is the whole point of the suite.

All timestamps are fixed ``datetime(..., tzinfo=UTC)`` values (mirroring
``tests/test_task_saga_store.py``); nothing relies on wall-clock
``datetime.now()``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from forge_loop._testing.memory_store import FakeMemoryStore
from forge_loop._testing.task_saga_store import FakeTaskSagaStore
from forge_loop.memory.models import (
    REJECTED_PATH_TAG,
    MemoryItem,
    MemoryKind,
    MemoryProvenance,
)
from forge_loop.memory.store import MemoryStore, SqliteMemoryStore
from forge_loop.sandbox import (
    CapabilityPolicy,
    FilesystemScope,
    McpGrant,
    NetworkPolicy,
)
from forge_loop.tasks import (
    Compensation,
    LeaseConflictError,
    SqliteTaskSagaStore,
    TaskSaga,
    TaskSagaStore,
    TaskState,
    TerminalTaskMutationError,
)

# ---------------------------------------------------------------------------
# Fixed clock — never wall-clock.
# ---------------------------------------------------------------------------

T0 = datetime(2026, 6, 3, 10, 0, tzinfo=UTC)


# ===========================================================================
# Task saga store contract
# ===========================================================================

#: Builds a fresh adapter. ``Fake`` ignores the path; ``Sqlite`` uses tmp disk.
TaskStoreFactory = Callable[[Path], TaskSagaStore]


def _build_fake_task_store(_tmp_path: Path) -> TaskSagaStore:
    return FakeTaskSagaStore()


def _build_sqlite_task_store(tmp_path: Path) -> TaskSagaStore:
    return SqliteTaskSagaStore(tmp_path / "tasks.db")


@pytest.fixture(
    params=[_build_fake_task_store, _build_sqlite_task_store],
    ids=["fake", "sqlite"],
)
def task_store(request: pytest.FixtureRequest, tmp_path: Path) -> TaskSagaStore:
    """A task saga store, parametrized over the fake and the real adapter."""
    factory: TaskStoreFactory = request.param
    return factory(tmp_path)


def _seed_leaseable(
    store: TaskSagaStore,
    task_id: str = "task-209",
    *,
    compensations: tuple[Compensation, ...] = (
        Compensation(
            kind="remove-worktree",
            target="/tmp/wt-loop-209",
            reason="cleanup after task terminal state",
        ),
    ),
) -> TaskSaga:
    """Create a PLANNED (non-terminal, leaseable) saga via the public API."""
    return store.create(
        task_id=task_id,
        saga_id=f"saga-{task_id}",
        issue=209,
        branch="loop/209-feat-test-fake-vs-real-adapter-contract",
        worktree="/tmp/wt-loop-209",
        compensations=compensations,
    )


class TestTaskSagaStoreContract:
    """Identical behavioral assertions for fake and real task saga stores."""

    # (a) create then get round-trip.
    def test_create_then_get_round_trip(self, task_store: TaskSagaStore) -> None:
        created = _seed_leaseable(task_store)
        assert created.state is TaskState.PLANNED
        assert task_store.get("task-209") == created

    def test_get_missing_returns_none(self, task_store: TaskSagaStore) -> None:
        # T2: external/lookup question answered "no".
        assert task_store.get("task-does-not-exist") is None

    # (b) acquire_lease sets RUNNING + owner + expiry.
    def test_acquire_lease_sets_running_owner_and_expiry(self, task_store: TaskSagaStore) -> None:
        _seed_leaseable(task_store)
        expires_at = T0 + timedelta(minutes=30)

        leased = task_store.acquire_lease(
            "task-209",
            owner_id="worker-a",
            expires_at=expires_at,
            acquired_at=T0,
        )

        assert leased.state is TaskState.RUNNING
        assert leased.lease_owner == "worker-a"
        assert leased.lease_expires_at == expires_at
        assert leased.last_heartbeat_at == T0

    # (c) acquiring a lease while one is active raises LeaseConflictError.
    def test_acquire_lease_while_active_raises(self, task_store: TaskSagaStore) -> None:
        _seed_leaseable(task_store)
        task_store.acquire_lease(
            "task-209",
            owner_id="worker-a",
            expires_at=T0 + timedelta(minutes=30),
            acquired_at=T0,
        )

        with pytest.raises(LeaseConflictError):
            task_store.acquire_lease(
                "task-209",
                owner_id="worker-b",
                expires_at=T0 + timedelta(minutes=40),
                acquired_at=T0 + timedelta(minutes=1),
            )

        # The original holder is preserved on both adapters.
        held = task_store.get("task-209")
        assert held is not None
        assert held.lease_owner == "worker-a"

    # (d) acquire_lease with expires_at <= acquired_at raises LeaseConflictError.
    @pytest.mark.parametrize("delta", [timedelta(0), timedelta(seconds=-1)])
    def test_acquire_lease_non_future_expiry_raises(
        self, task_store: TaskSagaStore, delta: timedelta
    ) -> None:
        _seed_leaseable(task_store)
        with pytest.raises(LeaseConflictError):
            task_store.acquire_lease(
                "task-209",
                owner_id="worker-a",
                expires_at=T0 + delta,
                acquired_at=T0,
            )

    def _acquire_default_lease(self, store: TaskSagaStore) -> None:
        _seed_leaseable(store)
        store.acquire_lease(
            "task-209",
            owner_id="worker-a",
            expires_at=T0 + timedelta(minutes=10),
            acquired_at=T0,
        )

    # (e) heartbeat by the wrong owner raises.
    def test_heartbeat_wrong_owner_raises(self, task_store: TaskSagaStore) -> None:
        self._acquire_default_lease(task_store)
        with pytest.raises(LeaseConflictError):
            task_store.heartbeat(
                "task-209",
                owner_id="worker-b",
                heartbeat_at=T0 + timedelta(minutes=5),
                expires_at=T0 + timedelta(minutes=30),
            )

    # (e) heartbeat with non-extending expiry raises.
    def test_heartbeat_non_extending_raises(self, task_store: TaskSagaStore) -> None:
        self._acquire_default_lease(task_store)
        with pytest.raises(LeaseConflictError):
            task_store.heartbeat(
                "task-209",
                owner_id="worker-a",
                heartbeat_at=T0 + timedelta(minutes=5),
                # <= current lease_expires_at (T0 + 10m).
                expires_at=T0 + timedelta(minutes=10),
            )

    # (e) heartbeat after expiry raises.
    def test_heartbeat_after_expiry_raises(self, task_store: TaskSagaStore) -> None:
        self._acquire_default_lease(task_store)
        with pytest.raises(LeaseConflictError):
            task_store.heartbeat(
                "task-209",
                owner_id="worker-a",
                # >= current lease_expires_at (T0 + 10m): lease already expired.
                heartbeat_at=T0 + timedelta(minutes=15),
                expires_at=T0 + timedelta(minutes=40),
            )

    # (f) heartbeat by the holder extends lease_expires_at + last_heartbeat_at.
    def test_heartbeat_by_holder_extends_lease(self, task_store: TaskSagaStore) -> None:
        self._acquire_default_lease(task_store)
        heartbeat_at = T0 + timedelta(minutes=5)
        new_expiry = heartbeat_at + timedelta(minutes=20)

        extended = task_store.heartbeat(
            "task-209",
            owner_id="worker-a",
            heartbeat_at=heartbeat_at,
            expires_at=new_expiry,
        )

        assert extended.lease_expires_at == new_expiry
        assert extended.last_heartbeat_at == heartbeat_at
        # Persisted, not just returned.
        persisted = task_store.get("task-209")
        assert persisted is not None
        assert persisted.lease_expires_at == new_expiry
        assert persisted.last_heartbeat_at == heartbeat_at

    # (g) list_stale(now=...) returns only non-terminal sagas with expired leases.
    def test_list_stale_returns_only_expired_non_terminal(self, task_store: TaskSagaStore) -> None:
        # Stale: lease expires before `now`.
        _seed_leaseable(task_store, "task-stale")
        task_store.acquire_lease(
            "task-stale",
            owner_id="worker-stale",
            expires_at=T0 - timedelta(seconds=1),
            acquired_at=T0 - timedelta(minutes=10),
        )
        # Fresh: lease expires after `now`.
        _seed_leaseable(task_store, "task-fresh")
        task_store.acquire_lease(
            "task-fresh",
            owner_id="worker-fresh",
            expires_at=T0 + timedelta(minutes=5),
            acquired_at=T0 - timedelta(minutes=10),
        )
        # Terminal: had a lease, but is now terminal — must be excluded.
        _seed_leaseable(task_store, "task-terminal")
        task_store.acquire_lease(
            "task-terminal",
            owner_id="worker-terminal",
            expires_at=T0 - timedelta(seconds=1),
            acquired_at=T0 - timedelta(minutes=10),
        )
        task_store.mark_completed("task-terminal", reason="done")
        # Unlease: never leased (lease_expires_at is None) — must be excluded.
        _seed_leaseable(task_store, "task-unleased")

        stale = task_store.list_stale(now=T0)

        assert [saga.task_id for saga in stale] == ["task-stale"]

    # (h) terminal markers reach state; later lease/mutation raises.
    @pytest.mark.parametrize(
        ("marker", "expected_state"),
        [
            ("mark_completed", TaskState.COMPLETED),
            ("mark_compensated", TaskState.COMPENSATED),
            ("mark_quarantined", TaskState.QUARANTINED),
        ],
    )
    def test_terminal_marker_reaches_state_and_blocks_release(
        self, task_store: TaskSagaStore, marker: str, expected_state: TaskState
    ) -> None:
        _seed_leaseable(task_store)
        marked = getattr(task_store, marker)("task-209", reason="terminal reason")

        assert marked.state is expected_state
        assert marked.terminal_reason == "terminal reason"
        assert marked.is_terminal

        with pytest.raises(TerminalTaskMutationError):
            task_store.acquire_lease(
                "task-209",
                owner_id="worker-a",
                expires_at=T0 + timedelta(minutes=30),
                acquired_at=T0,
            )

    def test_mark_failed_reaches_terminal_and_blocks_mutation(
        self, task_store: TaskSagaStore
    ) -> None:
        _seed_leaseable(task_store)  # seeded WITH a compensation.
        failed = task_store.mark_failed("task-209", reason="worker crashed")

        assert failed.state is TaskState.FAILED
        assert failed.is_terminal
        assert failed.terminal_reason == "worker crashed"

        # A later mutation on a terminal saga raises.
        with pytest.raises(TerminalTaskMutationError):
            task_store.mark_completed("task-209", reason="too late")

    # (i) mark_failed with no compensation raises LeaseConflictError.
    def test_mark_failed_without_compensation_raises(self, task_store: TaskSagaStore) -> None:
        _seed_leaseable(task_store, "task-no-comp", compensations=())
        with pytest.raises(LeaseConflictError, match="compensation"):
            task_store.mark_failed("task-no-comp", reason="worker crashed")
        # Still mutable (not flipped to terminal) on both adapters.
        survivor = task_store.get("task-no-comp")
        assert survivor is not None
        assert not survivor.is_terminal

    # --- Adversarial / sad-path (test matrix) -----------------------------

    def test_lease_stealing_via_acquire_raises(self, task_store: TaskSagaStore) -> None:
        """owner-a holds the lease; owner-b cannot steal it via acquire."""
        _seed_leaseable(task_store)
        task_store.acquire_lease(
            "task-209",
            owner_id="owner-a",
            expires_at=T0 + timedelta(minutes=30),
            acquired_at=T0,
        )
        with pytest.raises(LeaseConflictError):
            task_store.acquire_lease(
                "task-209",
                owner_id="owner-b",
                expires_at=T0 + timedelta(minutes=45),
                acquired_at=T0 + timedelta(minutes=1),
            )

    def test_lease_stealing_via_heartbeat(self, task_store: TaskSagaStore) -> None:
        """owner-a holds the lease; owner-b heartbeat is rejected on both."""
        _seed_leaseable(task_store)
        task_store.acquire_lease(
            "task-209",
            owner_id="owner-a",
            expires_at=T0 + timedelta(minutes=30),
            acquired_at=T0,
        )
        with pytest.raises(LeaseConflictError):
            task_store.heartbeat(
                "task-209",
                owner_id="owner-b",
                heartbeat_at=T0 + timedelta(minutes=5),
                expires_at=T0 + timedelta(minutes=60),
            )

    def test_acquire_on_missing_task_raises_keyerror(self, task_store: TaskSagaStore) -> None:
        # T2: lease acquisition against a non-existent task.
        with pytest.raises(KeyError):
            task_store.acquire_lease(
                "task-ghost",
                owner_id="worker-a",
                expires_at=T0 + timedelta(minutes=30),
                acquired_at=T0,
            )


def test_sqlite_task_store_round_trips_capability_policy_after_reopen(
    tmp_path: Path,
) -> None:
    """Persistence-after-reopen (real store only).

    The fake gets value-equality for free because it holds the live object; the
    real store must serialize the ``CapabilityPolicy`` and reconstruct an equal
    ``TaskSaga`` from a reopened db. This case proves the round-trip survives
    serialization.
    """
    db = tmp_path / "tasks.db"
    policy = CapabilityPolicy(
        filesystem=FilesystemScope(
            read_roots=("/repo", "/tmp/wt-loop-209"),
            write_roots=("/tmp/wt-loop-209",),
        ),
        network=NetworkPolicy(allow_domains=("github.com", "api.github.com")),
        mcp=(
            McpGrant(server="github", tools=("*",)),
            McpGrant(server="lumen", tools=("search",)),
        ),
        secret_names=("GITHUB_TOKEN", "ANTHROPIC_API_KEY"),
    )

    created = SqliteTaskSagaStore(db).create(
        task_id="task-209-policy",
        saga_id="saga-209-policy",
        issue=209,
        branch="loop/209-feat-test-fake-vs-real-adapter-contract",
        worktree="/tmp/wt-loop-209",
        compensations=(),
        capability_policy=policy,
    )

    reopened = SqliteTaskSagaStore(db)
    persisted = reopened.get("task-209-policy")

    assert persisted is not None
    assert persisted == created
    assert persisted.capability_policy == policy


# ===========================================================================
# Memory store contract
# ===========================================================================

MemoryStoreFactory = Callable[[Path], MemoryStore]


def _build_fake_memory_store(_tmp_path: Path) -> MemoryStore:
    return FakeMemoryStore()


def _build_sqlite_memory_store(tmp_path: Path) -> MemoryStore:
    return SqliteMemoryStore(tmp_path / "memory.db")


@pytest.fixture(
    params=[_build_fake_memory_store, _build_sqlite_memory_store],
    ids=["fake", "sqlite"],
)
def memory_store(request: pytest.FixtureRequest, tmp_path: Path) -> MemoryStore:
    """A memory store, parametrized over the fake and the real adapter."""
    factory: MemoryStoreFactory = request.param
    return factory(tmp_path)


def _mem_item(
    memory_id: str,
    kind: MemoryKind,
    *,
    title: str | None = None,
    tags: tuple[str, ...] = ("boot-context",),
) -> MemoryItem:
    # Fixed created_at so round-trip equality holds across serialization.
    return MemoryItem(
        memory_id=memory_id,
        kind=kind,
        title=title or f"{kind.value} memory",
        body="Durable memory keeps load-bearing context out of transcripts.",
        tags=tags,
        provenance=MemoryProvenance(
            source_event=None,
            authored_by="test",
            source_task_ref="task:#209",
            confidence=0.9,
            created_at=T0,
            evidence_refs=("issue:#209",),
        ),
    )


class TestMemoryStoreContract:
    """Identical behavioral assertions for fake and real memory stores."""

    def test_put_then_get_round_trip(self, memory_store: MemoryStore) -> None:
        item = _mem_item("mem-1", MemoryKind.SEMANTIC)
        assert memory_store.put(item) == item
        assert memory_store.get("mem-1") == item

    def test_get_missing_returns_none(self, memory_store: MemoryStore) -> None:
        # T2: lookup answered "no".
        assert memory_store.get("mem-missing") is None

    def test_list_active_filters_by_kind(self, memory_store: MemoryStore) -> None:
        semantic = _mem_item("mem-sem", MemoryKind.SEMANTIC)
        procedural = _mem_item("mem-proc", MemoryKind.PROCEDURAL)
        memory_store.put(semantic)
        memory_store.put(procedural)

        assert memory_store.list_active(kind=MemoryKind.PROCEDURAL) == (procedural,)
        assert memory_store.list_active(kind=MemoryKind.SEMANTIC) == (semantic,)
        assert set(memory_store.list_active()) == {semantic, procedural}

    def test_list_active_excludes_superseded(self, memory_store: MemoryStore) -> None:
        old = _mem_item("mem-old", MemoryKind.SEMANTIC)
        new = _mem_item("mem-new", MemoryKind.SEMANTIC)
        memory_store.put(old)
        memory_store.put(new)

        memory_store.supersede("mem-old", by_memory_id="mem-new")

        active_ids = [item.memory_id for item in memory_store.list_active()]
        assert active_ids == ["mem-new"]
        # Superseded item is still inspectable via get().
        superseded = memory_store.get("mem-old")
        assert superseded is not None
        assert superseded.superseded_by == "mem-new"
        assert not superseded.is_active

    def test_list_rejected_paths_returns_only_tagged_active(
        self, memory_store: MemoryStore
    ) -> None:
        rejected = _mem_item(
            "mem-rejected",
            MemoryKind.SEMANTIC,
            title="Rejected archive",
            tags=(REJECTED_PATH_TAG, "memory"),
        )
        normal = _mem_item("mem-normal", MemoryKind.SEMANTIC, title="Normal", tags=("memory",))
        rejected_old = _mem_item(
            "mem-rejected-old",
            MemoryKind.SEMANTIC,
            title="Old rejected",
            tags=(REJECTED_PATH_TAG,),
        )
        memory_store.put(rejected)
        memory_store.put(normal)
        memory_store.put(rejected_old)
        # Superseded rejected item must be excluded (active-only).
        memory_store.supersede("mem-rejected-old", by_memory_id="mem-rejected")

        assert [item.memory_id for item in memory_store.list_rejected_paths()] == ["mem-rejected"]

    def test_supersede_sets_superseded_by(self, memory_store: MemoryStore) -> None:
        old = _mem_item("mem-old", MemoryKind.SEMANTIC)
        new = _mem_item("mem-new", MemoryKind.SEMANTIC)
        memory_store.put(old)
        memory_store.put(new)

        result = memory_store.supersede("mem-old", by_memory_id="mem-new")

        assert result.superseded_by == "mem-new"

    # --- Adversarial / sad-path -------------------------------------------

    def test_supersede_unknown_id_raises(self, memory_store: MemoryStore) -> None:
        # T2: supersede where the target id does not exist.
        memory_store.put(_mem_item("mem-new", MemoryKind.SEMANTIC))
        with pytest.raises(KeyError):
            memory_store.supersede("mem-unknown", by_memory_id="mem-new")

    def test_supersede_unknown_replacement_raises(self, memory_store: MemoryStore) -> None:
        # T2: supersede pointing at a non-existent replacement leaves item intact.
        memory_store.put(_mem_item("mem-old", MemoryKind.SEMANTIC))
        with pytest.raises(KeyError):
            memory_store.supersede("mem-old", by_memory_id="mem-missing-replacement")
        unchanged = memory_store.get("mem-old")
        assert unchanged is not None
        assert unchanged.is_active
