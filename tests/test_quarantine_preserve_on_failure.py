"""Preserve-on-failure worktree quarantine (#357).

When a saga's ``CapabilityPolicy.preserve_on_failure`` is set, a FAILED worker
must NOT have its worktree reaped: both terminal paths (the dispatch
``_finalize_worker_saga`` wiring and the boot ``reconcile_stale_sagas`` sweep)
rename the checkout to ``.stale-<ts>`` via the existing ``quarantine_if_blocking``
helper and drive the saga to QUARANTINED instead. With the flag off, behaviour
is byte-identical to before.

Falsifiable criterion (issue body): a FAILED saga whose policy sets
``preserve_on_failure`` ends in state QUARANTINED with its worktree directory
still present on disk (renamed, not deleted).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from forge_loop._testing.task_saga_store import FakeTaskSagaStore
from forge_loop.control.recovery import reconcile_stale_sagas
from forge_loop.runner import dispatch as dispatch_mod
from forge_loop.sandbox import CapabilityPolicy
from forge_loop.tasks import Compensation, CompensationKind, TaskState

_REMOVE_WT = (
    Compensation(
        kind=CompensationKind.REMOVE_WORKTREE,
        target="/tmp/wt",
        reason="cleanup",
    ),
)


def _make_worktree(tmp_path: Path, name: str = "wt-loop-7") -> Path:
    wt = tmp_path / name
    wt.mkdir()
    (wt / "evidence.txt").write_text("crash trace", encoding="utf-8")
    return wt


def _seed(
    store: FakeTaskSagaStore,
    *,
    worktree: Path,
    preserve: bool,
    task_id: str = "task-7-worker",
) -> None:
    store.create(
        task_id=task_id,
        saga_id="saga-7-worker",
        issue=7,
        branch="loop/7",
        worktree=str(worktree),
        compensations=_REMOVE_WT,
        capability_policy=CapabilityPolicy(preserve_on_failure=preserve),
    )


def _assert_quarantined_on_disk(tmp_path: Path, wt: Path) -> None:
    """Original gone; a sibling ``<name>.stale-<ts>`` dir survives with contents."""
    assert not wt.exists()
    stale = list(tmp_path.glob(f"{wt.name}.stale-*"))
    assert len(stale) == 1, f"expected one quarantined dir, found {stale}"
    assert (stale[0] / "evidence.txt").read_text(encoding="utf-8") == "crash trace"


# --------------------------------------------------------------------------- #
# Serialization round-trip (the field must survive store persistence).
# --------------------------------------------------------------------------- #


def test_preserve_on_failure_round_trips_through_json() -> None:
    policy = CapabilityPolicy(preserve_on_failure=True)
    restored = CapabilityPolicy.from_json_obj(policy.to_json_obj())
    assert restored.preserve_on_failure is True


def test_preserve_on_failure_defaults_false_for_legacy_json() -> None:
    # A saga written by an older loop has no key — it must default to False,
    # not crash, so legacy worktrees keep their reap-on-failure behaviour.
    restored = CapabilityPolicy.from_json_obj({"secret_names": []})
    assert restored.preserve_on_failure is False


# --------------------------------------------------------------------------- #
# Dispatch path: _finalize_worker_saga state machine (T1: one test per edge).
# --------------------------------------------------------------------------- #


def test_finalize_failed_with_preserve_quarantines(tmp_path: Path) -> None:
    wt = _make_worktree(tmp_path)
    store = FakeTaskSagaStore()
    _seed(store, worktree=wt, preserve=True)

    dispatch_mod._finalize_worker_saga(store, task_id="task-7-worker", status="failed")

    saga = store.get("task-7-worker")
    assert saga is not None
    assert saga.state is TaskState.QUARANTINED
    _assert_quarantined_on_disk(tmp_path, wt)


def test_finalize_failed_without_preserve_marks_failed(tmp_path: Path) -> None:
    # Flag off: unchanged behaviour — saga FAILED, worktree left in place for
    # the remove-worktree compensation (recovery reaps it later).
    wt = _make_worktree(tmp_path)
    store = FakeTaskSagaStore()
    _seed(store, worktree=wt, preserve=False)

    dispatch_mod._finalize_worker_saga(store, task_id="task-7-worker", status="failed")

    saga = store.get("task-7-worker")
    assert saga is not None
    assert saga.state is TaskState.FAILED
    assert wt.exists()  # NOT quarantined
    assert list(tmp_path.glob(f"{wt.name}.stale-*")) == []


def test_finalize_success_completes_even_with_preserve(tmp_path: Path) -> None:
    # Adversarial: the preserve flag must ONLY bite on failure. A successful
    # worker (open/merged) still COMPLETES and the worktree is untouched here.
    wt = _make_worktree(tmp_path)
    store = FakeTaskSagaStore()
    _seed(store, worktree=wt, preserve=True)

    dispatch_mod._finalize_worker_saga(store, task_id="task-7-worker", status="open")

    saga = store.get("task-7-worker")
    assert saga is not None
    assert saga.state is TaskState.COMPLETED
    assert wt.exists()


def test_finalize_none_store_is_noop() -> None:
    # Sad path: no saga store wired (legacy/unit callers) must not raise.
    dispatch_mod._finalize_worker_saga(None, task_id="task-7-worker", status="failed")


# --------------------------------------------------------------------------- #
# Recovery path: reconcile_stale_sagas (T1 edge + T2 negative branch).
# --------------------------------------------------------------------------- #


def _stale(store: FakeTaskSagaStore, task_id: str = "task-7-worker") -> None:
    acquired = datetime.now(UTC) - timedelta(minutes=10)
    store.acquire_lease(
        task_id,
        owner_id="worker-7-tick-1",
        expires_at=acquired + timedelta(minutes=1),  # expired
        acquired_at=acquired,
    )


def test_reconcile_preserve_quarantines_without_reaping(tmp_path: Path) -> None:
    wt = _make_worktree(tmp_path)
    store = FakeTaskSagaStore()
    _seed(store, worktree=wt, preserve=True)
    _stale(store)

    reaped: list[int] = []
    report = reconcile_stale_sagas(store, reap_worktree=reaped.append)

    saga = store.get("task-7-worker")
    assert saga is not None
    assert saga.state is TaskState.QUARANTINED
    assert reaped == []  # the remove-worktree reap MUST NOT fire
    assert report.recovered_count == 1
    _assert_quarantined_on_disk(tmp_path, wt)


def test_reconcile_without_preserve_reaps_and_compensates(tmp_path: Path) -> None:
    # Flag off: unchanged — saga COMPENSATED and reap_worktree fires.
    wt = _make_worktree(tmp_path)
    store = FakeTaskSagaStore()
    _seed(store, worktree=wt, preserve=False)
    _stale(store)

    reaped: list[int] = []
    report = reconcile_stale_sagas(store, reap_worktree=reaped.append)

    saga = store.get("task-7-worker")
    assert saga is not None
    assert saga.state is TaskState.COMPENSATED
    assert reaped == [7]
    assert report.recovered_count == 1


def test_reconcile_preserve_with_missing_worktree_still_quarantines(tmp_path: Path) -> None:
    # T2 adversarial: the worktree dir is already gone (a prior partial reap).
    # quarantine_if_blocking returns None; the saga must STILL go QUARANTINED.
    missing = tmp_path / "vanished-wt"
    store = FakeTaskSagaStore()
    _seed(store, worktree=missing, preserve=True)
    _stale(store)

    report = reconcile_stale_sagas(store, reap_worktree=lambda _n: None)

    saga = store.get("task-7-worker")
    assert saga is not None
    assert saga.state is TaskState.QUARANTINED
    assert report.recovered_count == 1
