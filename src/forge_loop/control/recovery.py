"""Boot-time recovery: reconcile dead-worker sagas left by a crashed run.

When the loop process is hard-killed (operator ^C mid-tick, OOM, watchdog
SIGKILL), the workers it had leased die with it. Their task sagas stay
RUNNING with a lease that no longer beats; once the lease lapses they read as
*stale*. This engine is what makes the loop resumable: on the next boot it
walks the stale sagas, runs their compensations (reaping the orphaned
worktree), and drives them to COMPENSATED so they drain from the in-flight
view. The source issue, if still labelled, is re-picked naturally on the next
tick — recovery never touches GitHub, so it is safe to run offline.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from forge_loop.tasks import CompensationKind, TaskSaga, TaskSagaStore

# Reap a worker's worktree, keyed by issue number (best-effort, idempotent).
ReapWorktree = Callable[[int], None]

# Compensation kinds this engine knows how to run during a recovery sweep. A
# stale saga carrying any kind NOT in this set (e.g. the close-pr / delete-branch
# kinds #272 adds) cannot be honestly driven to COMPENSATED here, so it is left
# non-terminal for the next sweep (or that kind's handler) to finish.
_HANDLED_COMPENSATION_KINDS: frozenset[str] = frozenset({CompensationKind.REMOVE_WORKTREE})


@dataclass(frozen=True)
class RecoveredSaga:
    """One dead-worker saga reconciled back to a terminal state."""

    task_id: str
    saga_id: str
    issue: int | None
    worktrees_reaped: tuple[str, ...]


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
            reaped = (
                (" reaped " + ", ".join(item.worktrees_reaped)) if item.worktrees_reaped else ""
            )
            lines.append(f"- {item.saga_id}{issue}{reaped}")
        for error in self.errors:
            lines.append(f"- error: {error}")
        return "\n".join(lines)


def _quarantine_saga_worktree(saga: TaskSaga) -> str | None:
    """Rename a preserve-on-failure saga's worktree out of the way (#357).

    Reuses the existing :func:`quarantine_if_blocking` helper so recovery and
    ``prep_worktree`` share one quarantine mechanism. Best-effort: a saga with
    no worktree, or one whose directory is already gone, yields ``None`` and
    the saga is still driven QUARANTINED.
    """
    from pathlib import Path

    from forge_loop.worker_worktree import quarantine_if_blocking

    if not saga.worktree:
        return None
    moved = quarantine_if_blocking(Path(saga.worktree))
    return str(moved) if moved is not None else None


def reconcile_stale_sagas(
    saga_store: TaskSagaStore,
    *,
    now: datetime | None = None,
    reap_worktree: ReapWorktree | None = None,
) -> RecoveryReport:
    """Compensate and close every stale (expired-lease) saga.

    For each stale saga: run its ``remove-worktree`` compensations via
    ``reap_worktree`` (idempotent, best-effort), then mark it COMPENSATED.
    A failure on one saga is captured and the sweep continues to the next —
    recovery must make as much progress as it can, not abort on the first
    snag.

    A saga carrying a compensation kind this engine has no handler for is NOT
    marked COMPENSATED: driving it terminal would assert a side effect was
    undone when it never ran (a wrong-but-green integrity hole). Instead the
    saga is left non-terminal and an entry naming its id + the unhandled
    kind(s) is appended to ``RecoveryReport.errors`` so the next sweep (or that
    kind's future handler) can finish it.
    """
    moment = now or datetime.now(UTC)
    recovered: list[RecoveredSaga] = []
    errors: list[str] = []

    for saga in saga_store.list_stale(now=moment):
        try:
            if saga.capability_policy.preserve_on_failure:
                # #357: a preserve-on-failure saga is NOT reaped. Quarantine its
                # worktree (rename to ``.stale-<ts>``) and drive it QUARANTINED so
                # the remove-worktree compensation never runs and the crashed
                # checkout survives on disk for the operator to inspect.
                _quarantine_saga_worktree(saga)
                saga_store.mark_quarantined(
                    saga.task_id,
                    reason="recovered: preserve-on-failure dead-worker lease expired at boot",
                )
                recovered.append(
                    RecoveredSaga(
                        task_id=saga.task_id,
                        saga_id=saga.saga_id,
                        issue=saga.issue,
                        worktrees_reaped=(),
                    )
                )
                continue
            unhandled = sorted(
                {c.kind for c in saga.compensations if c.kind not in _HANDLED_COMPENSATION_KINDS}
            )
            if unhandled:
                errors.append(
                    f"{saga.saga_id}: left non-terminal, "
                    f"unhandled compensation kind(s): {', '.join(unhandled)}"
                )
                continue
            reaped: list[str] = []
            for compensation in saga.compensations:
                # Every kind here is handled (unhandled kinds short-circuit above).
                if reap_worktree is not None and saga.issue is not None:
                    reap_worktree(saga.issue)
                reaped.append(compensation.target)
            saga_store.mark_compensated(
                saga.task_id, reason="recovered: dead-worker lease expired at boot"
            )
            recovered.append(
                RecoveredSaga(
                    task_id=saga.task_id,
                    saga_id=saga.saga_id,
                    issue=saga.issue,
                    worktrees_reaped=tuple(reaped),
                )
            )
        except Exception as exc:  # noqa: BLE001 - one bad saga must not abort the sweep
            errors.append(f"{saga.saga_id}: {type(exc).__name__}: {exc}")

    return RecoveryReport(recovered=tuple(recovered), errors=tuple(errors))
