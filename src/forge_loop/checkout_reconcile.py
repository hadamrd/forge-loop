"""Deterministic shared-checkout reconcile (operational-convergence axis, #416).

Dispatch borrows the **shared checkout at ``cfg.repo``** and can leave it parked on
a ``loop/<n>`` feature branch after a worker run. Nothing else switches it back, so
the shared repo gets stranded on a stale feature branch (the "drifting-checkout"
failure mode) until a human notices and ``git switch``es it by hand — loop exhaust
the operator must GC.

This reconcile is a **sibling to** :mod:`forge_loop.branch_sweep` /
:mod:`forge_loop.worktree_sweep`. On the maintenance cadence it switches the shared
checkout back to ``base_branch`` — but only when it is **safe** to do so. It is
conservative by construction:

* only ``loop/<n>`` / ``loop/<n>-slug`` branches are ever eligible (reuses
  :func:`forge_loop.branch_sweep.loop_issue_number` — the pattern is NOT reinvented);
* a checkout already on ``base_branch`` or on any non-loop branch is left untouched;
* a **dirty** tree (any ``git status --porcelain`` output) is NEVER switched — we
  never clobber uncommitted work, never stash/discard/``--force``.

The pure decision (:func:`reconcile`) is unit-tested directly with injected deps
(branch reader, dirty reader, switch callable) — no real git required. The runner
adapter (``tick_checks.run_checkout_reconcile``) only supplies the real git probes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from forge_loop.branch_sweep import loop_issue_number

__all__ = [
    "CheckoutReconcileReport",
    "ReconcileOutcome",
    "is_eligible",
    "reconcile",
]


class ReconcileOutcome(StrEnum):
    """Discriminator for one :func:`reconcile` decision.

    A ``str`` Enum (not a string literal) per the manifesto's no-stringly-typed
    cross-module-boundary rule: ``tick_checks.run_checkout_reconcile`` branches on
    this value to decide which typed event to emit, so it crosses a module edge.
    """

    RESTORED = "restored"
    SKIPPED_BASE = "skipped_base"
    SKIPPED_NON_LOOP = "skipped_non_loop"
    SKIPPED_DIRTY = "skipped_dirty"
    ERROR = "error"


@dataclass
class CheckoutReconcileReport:
    """Result of one :func:`reconcile`.

    ``outcome`` — the single decision taken (see :class:`ReconcileOutcome`).
    ``from_branch`` — the branch the checkout was on (``None`` if unreadable).
    ``to_branch`` — the branch switched to (set only on ``RESTORED``).
    ``reason`` — short human reason for a ``SKIPPED_*`` / ``ERROR`` outcome.
    """

    outcome: ReconcileOutcome
    from_branch: str | None = None
    to_branch: str | None = None
    reason: str | None = None


def is_eligible(current_branch: str, base_branch: str) -> bool:
    """True iff ``current_branch`` is a ``loop/<n>`` branch that is NOT the base.

    The only branches this reconcile may ever switch. Reuses
    :func:`forge_loop.branch_sweep.loop_issue_number` so the ``loop/<n>`` /
    ``loop/<n>-slug`` regex lives in exactly one place.
    """
    if not current_branch or current_branch == base_branch:
        return False
    return loop_issue_number(current_branch) is not None


def reconcile(
    *,
    read_branch: Callable[[], str],
    read_dirty: Callable[[], bool],
    switch: Callable[[str], bool],
    base_branch: str,
) -> CheckoutReconcileReport:
    """Decide whether to switch the shared checkout back to ``base_branch``.

    Pure orchestration over three injected deps — no git knowledge of its own:

    * ``read_branch()`` → the checkout's current branch (``""`` if unreadable);
    * ``read_dirty()`` → whether the tree is dirty (called ONLY for an eligible
      branch, so non-loop / already-on-base checkouts never pay for it AND a dirty
      non-loop tree never even reaches the question);
    * ``switch(base)`` → perform the branch switch, returning success.

    Conservative by construction (see module docstring). Never raises: a failure
    reading the branch / dirtiness or performing the switch is captured as an
    ``ERROR`` outcome, so the caller's tick is never crashed.
    """
    try:
        current = read_branch()
    except Exception as ex:  # noqa: BLE001 — belt-and-braces; reconcile never raises
        return CheckoutReconcileReport(
            outcome=ReconcileOutcome.ERROR, reason=f"branch_probe: {type(ex).__name__}"[:200]
        )
    if not current:
        return CheckoutReconcileReport(outcome=ReconcileOutcome.ERROR, reason="head_unreadable")
    if current == base_branch:
        return CheckoutReconcileReport(
            outcome=ReconcileOutcome.SKIPPED_BASE, from_branch=current
        )
    if not is_eligible(current, base_branch):
        return CheckoutReconcileReport(
            outcome=ReconcileOutcome.SKIPPED_NON_LOOP, from_branch=current
        )
    try:
        dirty = read_dirty()
    except Exception as ex:  # noqa: BLE001
        return CheckoutReconcileReport(
            outcome=ReconcileOutcome.ERROR,
            from_branch=current,
            reason=f"status_probe: {type(ex).__name__}"[:200],
        )
    if dirty:
        # Dirty = hands off. Never stash, discard, or --force switch (issue #416).
        return CheckoutReconcileReport(
            outcome=ReconcileOutcome.SKIPPED_DIRTY, from_branch=current, reason="dirty_tree"
        )
    try:
        ok = switch(base_branch)
    except Exception as ex:  # noqa: BLE001
        return CheckoutReconcileReport(
            outcome=ReconcileOutcome.ERROR,
            from_branch=current,
            to_branch=base_branch,
            reason=f"switch: {type(ex).__name__}"[:200],
        )
    if not ok:
        return CheckoutReconcileReport(
            outcome=ReconcileOutcome.ERROR,
            from_branch=current,
            to_branch=base_branch,
            reason="switch returned False",
        )
    return CheckoutReconcileReport(
        outcome=ReconcileOutcome.RESTORED, from_branch=current, to_branch=base_branch
    )
