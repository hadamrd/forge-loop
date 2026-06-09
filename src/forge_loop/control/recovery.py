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

from forge_loop.tasks import CompensationKind, TaskSagaStore

# Reap a worker's worktree, keyed by issue number (best-effort, idempotent).
ReapWorktree = Callable[[int], None]

# Reap a worker's abandoned branch, keyed by branch name (best-effort,
# idempotent). The branch name is the ``DELETE_BRANCH`` compensation target.
ReapBranch = Callable[[str], None]

# Compensation kinds this engine knows how to run during a recovery sweep. This
# keyset, together with ``_DEFERRED_COMPENSATION_KINDS``, is the load-bearing
# exhaustiveness guard: a contract test asserts their union equals
# ``set(CompensationKind)``, so adding a new enum member without classifying it
# turns that test red (see ``test_recovery``).
_HANDLED_COMPENSATION_KINDS: frozenset[CompensationKind] = frozenset(
    {CompensationKind.REMOVE_WORKTREE, CompensationKind.DELETE_BRANCH}
)

# Kinds dispatch already enqueues but whose recovery handler is a deferred epic
# issue. A stale saga carrying only handled and/or deferred kinds is still
# auto-recovered: the handled compensations run and the saga is driven
# COMPENSATED, while the deferred kinds are knowingly skipped. No kinds are
# deferred today; keep the set so the classification invariant remains explicit.
#
# A kind that is in NEITHER set is a genuine gap: recovery refuses to
# half-compensate such a saga and drives it to the terminal QUARANTINED state
# ("parked for a human, could not auto-compensate") — liveness without lying
# about the side effect. See ``reconcile_stale_sagas``.
_DEFERRED_COMPENSATION_KINDS: frozenset[CompensationKind] = frozenset()


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


def reconcile_stale_sagas(
    saga_store: TaskSagaStore,
    *,
    now: datetime | None = None,
    reap_worktree: ReapWorktree | None = None,
    reap_branch: ReapBranch | None = None,
) -> RecoveryReport:
    """Compensate and close every stale (expired-lease) saga.

    For each stale saga: discharge its compensations by kind. A
    ``remove-worktree`` compensation runs ``reap_worktree(issue)`` and a
    ``delete-branch`` compensation runs ``reap_branch(target)``. A failure on
    one saga is captured and the sweep continues to the next — recovery must
    make as much progress as it can, not abort on the first snag.

    A saga carrying a compensation kind this engine neither handles nor has
    explicitly deferred is NOT marked COMPENSATED: driving it COMPENSATED would
    assert a side effect was undone when it never ran (a wrong-but-green
    integrity hole). Instead the saga is driven to the terminal QUARANTINED
    state ("parked for a human") so it stops being immortal — it drains from the
    in-flight view and is never re-swept — without claiming the side effect was
    undone. No handled compensation is run for such a saga (we refuse to
    half-compensate it), and an entry naming its id + the unhandled kind(s) is
    appended to ``RecoveryReport.errors`` so an operator knows a saga was parked.

    A *deferred* kind (enqueued at dispatch but whose handler has not landed
    yet) does NOT taint the saga: the handled compensations still run and the
    saga is driven COMPENSATED, while the deferred kind is skipped. That keeps
    dead-worker worktrees auto-reaped instead of parked for a human while a
    deferred handler is in flight.

    A saga whose ``delete-branch`` handler raises is driven terminal
    QUARANTINED instead of being left RUNNING. That preserves the integrity of
    COMPENSATED: recovery never claims a branch was deleted when its handler
    failed.
    """
    moment = now or datetime.now(UTC)
    recovered: list[RecoveredSaga] = []
    errors: list[str] = []
    classified = _HANDLED_COMPENSATION_KINDS | _DEFERRED_COMPENSATION_KINDS

    for saga in saga_store.list_stale(now=moment):
        try:
            unhandled = sorted({c.kind for c in saga.compensations if c.kind not in classified})
            if unhandled:
                kinds = ", ".join(unhandled)
                saga_store.mark_quarantined(
                    saga.task_id,
                    reason=(
                        "recovered: parked for human, no recovery handler for "
                        f"compensation kind(s): {kinds}"
                    ),
                )
                errors.append(
                    f"{saga.saga_id}: quarantined, unhandled compensation kind(s): {kinds}"
                )
                continue
            reaped_worktrees: list[str] = []
            failed_compensation = False
            for compensation in saga.compensations:
                # Run only the handled compensations; deferred kinds (no handler
                # yet) are skipped, never falsely claimed undone. Filtering by
                # kind also stops a non-worktree entry from double-firing the
                # reaper or recording its target as a reaped worktree.
                if compensation.kind == CompensationKind.REMOVE_WORKTREE:
                    if reap_worktree is not None and saga.issue is not None:
                        reap_worktree(saga.issue)
                    reaped_worktrees.append(compensation.target)
                elif compensation.kind == CompensationKind.DELETE_BRANCH:
                    if reap_branch is not None:
                        try:
                            reap_branch(compensation.target)
                        except Exception as exc:  # noqa: BLE001 - park this saga, continue sweep
                            reason = (
                                "recovered: parked for human, compensation "
                                f"{CompensationKind.DELETE_BRANCH} for "
                                f"{compensation.target!r} failed: "
                                f"{type(exc).__name__}: {exc}"
                            )
                            saga_store.mark_quarantined(saga.task_id, reason=reason)
                            errors.append(f"{saga.saga_id}: quarantined, {reason}")
                            failed_compensation = True
                            break
            if failed_compensation:
                continue
            saga_store.mark_compensated(
                saga.task_id, reason="recovered: dead-worker lease expired at boot"
            )
            recovered.append(
                RecoveredSaga(
                    task_id=saga.task_id,
                    saga_id=saga.saga_id,
                    issue=saga.issue,
                    worktrees_reaped=tuple(reaped_worktrees),
                )
            )
        except Exception as exc:  # noqa: BLE001 - one bad saga must not abort the sweep
            errors.append(f"{saga.saga_id}: {type(exc).__name__}: {exc}")

    return RecoveryReport(recovered=tuple(recovered), errors=tuple(errors))
