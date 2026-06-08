"""Dispatch enqueues a DELETE_BRANCH compensation for the branch it plants (#433).

Epic: "Compensate the branch a failed worker abandons". At dispatch, when a
saga is created for a worker that plants a ``loop/<n>`` branch, the saga must
carry a ``DELETE_BRANCH`` compensation whose target is the *exact* branch name
planted for that worker — alongside the pre-existing ``REMOVE_WORKTREE`` one —
so a failed worker can never leak a branch the control plane doesn't know to
delete.

Primary falsifiable acceptance (``test_dispatch_one_worker_*``): a saga created
by the dispatch path for issue #n carries a DELETE_BRANCH compensation whose
target equals the exact branch name planted for that worker.
"""

from __future__ import annotations

from typing import Any

from forge_loop.runner import dispatch as dispatch_mod
from forge_loop.sandbox import CapabilityPolicy
from forge_loop.tasks import Compensation, CompensationKind, SqliteTaskSagaStore
from forge_loop.worker import WorkerOutcome

# Reuse the Config/issue/meta scaffolding from the dispatch FSM tests.
from tests.test_persistent_dispatch import _issue, _make_cfg, _meta


def _saga_store(cfg: Any) -> SqliteTaskSagaStore:
    return SqliteTaskSagaStore(dispatch_mod.canonical_task_saga_path(cfg.repo))


def _delete_branch(comps: tuple[Compensation, ...]) -> list[Compensation]:
    return [c for c in comps if c.kind == CompensationKind.DELETE_BRANCH]


def _remove_worktree(comps: tuple[Compensation, ...]) -> list[Compensation]:
    return [c for c in comps if c.kind == CompensationKind.REMOVE_WORKTREE]


# --------------------------------------------------------------------------- #
# Unit: the shared compensation builder                                        #
# --------------------------------------------------------------------------- #


def test_worker_compensations_carry_both_kinds_with_correct_targets() -> None:
    comps = dispatch_mod._worker_compensations(
        worktree_path="/tmp/wt-loop-7", branch="loop/7-do-the-thing"
    )

    # Exactly one of each kind — no duplicate, no drift.
    assert len(_remove_worktree(comps)) == 1
    assert len(_delete_branch(comps)) == 1

    rm = _remove_worktree(comps)[0]
    db = _delete_branch(comps)[0]
    assert rm.target == "/tmp/wt-loop-7"
    # The DELETE_BRANCH target is the BRANCH, never the worktree path — the
    # exact falsifiable bug this feature guards against.
    assert db.target == "loop/7-do-the-thing"
    assert db.target != rm.target
    # Worktree is reaped before the branch it planted is deleted.
    assert comps.index(rm) < comps.index(db)


def test_worker_compensations_handles_empty_strings() -> None:
    """Adversarial: empty branch/worktree must not crash or merge the entries."""
    comps = dispatch_mod._worker_compensations(worktree_path="", branch="")

    assert len(comps) == 2
    assert _delete_branch(comps)[0].target == ""
    assert _remove_worktree(comps)[0].target == ""


# --------------------------------------------------------------------------- #
# Unit: both seed paths register the compensation                              #
# --------------------------------------------------------------------------- #


def test_seed_worker_saga_enqueues_delete_branch(tmp_path: Any) -> None:
    store = SqliteTaskSagaStore(":memory:")
    dispatch_mod._seed_worker_saga(
        store,
        repo=tmp_path / "repo",
        issue=_issue(7),
        branch="loop/7-do-the-thing",
        worktree_path="/tmp/wt-loop-7",
        capability_policy=CapabilityPolicy(),
    )

    saga = store.get("task-7-worker")
    assert saga is not None
    db = _delete_branch(saga.compensations)
    assert len(db) == 1
    assert db[0].target == "loop/7-do-the-thing"
    assert len(_remove_worktree(saga.compensations)) == 1


def test_record_worker_task_policy_fallback_enqueues_delete_branch(tmp_path: Any) -> None:
    """The ``task_store is None`` fallback path must also register DELETE_BRANCH."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    saga = dispatch_mod.record_worker_task_policy(
        repo=repo,
        task_id="task-7-worker",
        saga_id="saga-7-worker",
        issue=7,
        branch="loop/7-do-the-thing",
        worktree_path="/tmp/wt-loop-7",
        capability_policy=CapabilityPolicy(),
    )

    db = _delete_branch(saga.compensations)
    assert len(db) == 1
    assert db[0].target == "loop/7-do-the-thing"


# --------------------------------------------------------------------------- #
# Integration: the real dispatch path (primary acceptance)                     #
# --------------------------------------------------------------------------- #


def test_dispatch_one_worker_saga_carries_delete_branch_for_exact_branch(
    monkeypatch: Any, tmp_path: Any
) -> None:
    cfg = _make_cfg(tmp_path)
    issue = _issue(7)
    expected_branch = dispatch_mod._branch_for_issue(issue)
    # Falsifiable anchor: the slug really is the planted branch name.
    assert expected_branch == "loop/7-do-the-thing"

    def ok(*args: Any, **kwargs: Any) -> WorkerOutcome:
        return WorkerOutcome(
            issue=7,
            title="t",
            pr_url="https://x/pull/1",
            status="open",
            duration_s=1.0,
            stdout_tail="",
        )

    monkeypatch.setattr(dispatch_mod, "run_worker", ok)
    dispatch_mod._dispatch_one_worker(
        cfg, issue, _meta(), tick=1, bus_emit=lambda *a, **k: None, store=None
    )

    saga = _saga_store(cfg).get("task-7-worker")
    assert saga is not None
    db = _delete_branch(saga.compensations)
    assert len(db) == 1
    assert db[0].target == expected_branch
