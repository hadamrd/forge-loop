"""Boot-time recovery: reconcile dead-worker sagas left by a crashed run.

When the loop process is hard-killed (operator ^C mid-tick, OOM, watchdog
SIGKILL), the workers it had leased die with it. Their task sagas stay
RUNNING with a lease that no longer beats; once the lease lapses they read as
*stale*. This engine is what makes the loop resumable: on the next boot it
walks the stale sagas, runs their compensations (reaping the orphaned
worktree, and — when a gh callback is wired — deleting the abandoned remote
branch and closing the never-merged draft PR, #272), and drives them to
COMPENSATED so they drain from the in-flight view. The source issue, if still
labelled, is re-picked naturally on the next tick.

Offline-safe by construction: the gh-backed ``delete_branch`` / ``close_pr``
callbacks are optional and default ``None``. Absent a callback (an offline
boot), that compensation is skipped without error — exactly like a missing
``reap_worktree`` today — so recovery still runs and still drives every stale
saga to COMPENSATED. Each compensation is best-effort and idempotent: one
failure (or absent callback) aborts neither the other compensations, nor the
saga, nor the sweep.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from forge_loop.tasks import CompensationKind, TaskSagaStore

# Reap a worker's worktree, keyed by issue number (best-effort, idempotent).
ReapWorktree = Callable[[int], None]
# Delete an abandoned remote branch by ref (best-effort, idempotent, #272).
DeleteBranch = Callable[[str], object]
# Close an abandoned never-merged PR by number string (best-effort, #272).
ClosePull = Callable[[str], object]


@dataclass(frozen=True)
class RecoveredSaga:
    """One dead-worker saga reconciled back to a terminal state."""

    task_id: str
    saga_id: str
    issue: int | None
    worktrees_reaped: tuple[str, ...]
    branches_deleted: tuple[str, ...] = ()
    prs_closed: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecoveryReport:
    """Outcome of a recovery sweep."""

    recovered: tuple[RecoveredSaga, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def recovered_count(self) -> int:
        return len(self.recovered)

    def summary(self) -> str:
        if not self.recovered and not self.errors:
            return "recovery: nothing to reconcile"
        lines = [f"recovery: reconciled {len(self.recovered)} dead-worker saga(s)"]
        for item in self.recovered:
            issue = f" (issue #{item.issue})" if item.issue is not None else ""
            parts: list[str] = []
            if item.worktrees_reaped:
                parts.append("reaped " + ", ".join(item.worktrees_reaped))
            if item.branches_deleted:
                parts.append("deleted-branch " + ", ".join(item.branches_deleted))
            if item.prs_closed:
                parts.append("closed-pr " + ", ".join(item.prs_closed))
            suffix = (" " + "; ".join(parts)) if parts else ""
            lines.append(f"- {item.saga_id}{issue}{suffix}")
        for error in self.errors:
            lines.append(f"- error: {error}")
        return "\n".join(lines)


def reconcile_stale_sagas(
    saga_store: TaskSagaStore,
    *,
    now: datetime | None = None,
    reap_worktree: ReapWorktree | None = None,
    delete_branch: DeleteBranch | None = None,
    close_pr: ClosePull | None = None,
) -> RecoveryReport:
    """Compensate and close every stale (expired-lease) saga.

    For each stale saga, run its **full** compensation list in registered
    order — ``remove-worktree`` via ``reap_worktree``, ``delete-branch`` via
    ``delete_branch``, ``close-pr`` via ``close_pr`` — then mark it COMPENSATED.
    Every compensation is idempotent and best-effort: a failing one (or an
    absent callback, i.e. an offline boot) is captured in
    :attr:`RecoveryReport.errors` and skipped, but the saga still reaches
    COMPENSATED and the sweep still continues to the next saga. Recovery must
    make as much progress as it can, not abort on the first snag.
    """
    moment = now or datetime.now(UTC)
    recovered: list[RecoveredSaga] = []
    errors: list[str] = []

    for saga in saga_store.list_stale(now=moment):
        reaped: list[str] = []
        branches: list[str] = []
        prs: list[str] = []
        for compensation in saga.compensations:
            try:
                if compensation.kind == CompensationKind.REMOVE_WORKTREE:
                    if reap_worktree is not None and saga.issue is not None:
                        reap_worktree(saga.issue)
                    reaped.append(compensation.target)
                elif compensation.kind == CompensationKind.DELETE_BRANCH:
                    if delete_branch is not None:
                        delete_branch(compensation.target)
                        branches.append(compensation.target)
                elif compensation.kind == CompensationKind.CLOSE_PR:
                    if close_pr is not None:
                        close_pr(compensation.target)
                        prs.append(compensation.target)
            except Exception as exc:  # noqa: BLE001 - one bad compensation isn't fatal
                errors.append(
                    f"{saga.saga_id}: {compensation.kind}: {type(exc).__name__}: {exc}"
                )
        try:
            saga_store.mark_compensated(
                saga.task_id, reason="recovered: dead-worker lease expired at boot"
            )
        except Exception as exc:  # noqa: BLE001 - one bad saga must not abort the sweep
            errors.append(f"{saga.saga_id}: {type(exc).__name__}: {exc}")
            continue
        recovered.append(
            RecoveredSaga(
                task_id=saga.task_id,
                saga_id=saga.saga_id,
                issue=saga.issue,
                worktrees_reaped=tuple(reaped),
                branches_deleted=tuple(branches),
                prs_closed=tuple(prs),
            )
        )

    return RecoveryReport(recovered=tuple(recovered), errors=tuple(errors))
