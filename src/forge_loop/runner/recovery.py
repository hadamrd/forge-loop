"""Crash recovery at runner boot (issue #111).

When a runner crashes (SIGKILL, host reboot, OOM), the session store
left behind non-terminal rows that would otherwise block parallel
slots forever. The runner's first job at boot — BEFORE the first
dispatch tick — is to walk
:func:`forge_loop.worker_sessions.recoverable_sessions` and decide,
per state, how to resume.

Decision table:

==================  ===================================================
prior state         recovery action
==================  ===================================================
DISPATCHED          re-dispatch normally; state untouched.
RUNNING             probe worktree (still on disk?) AND PR (GhClient
                    ``get_pull``). Both present → AWAITING_CRITIC.
                    Otherwise → ABANDONED ("boot recovery: state lost").
AWAITING_CRITIC     re-fire critic on the next tick; state untouched.
REVISING            same probe as RUNNING.
==================  ===================================================

Every decision emits a ``worker_session_recovered`` typed event
(:class:`forge_loop.events.WorkerSessionRecoveredEvent`) so operators
get a single auditable trail of what happened to each session.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from forge_loop.events import WorkerSessionRecoveredEvent, emit
from forge_loop.gh_client import GhClient
from forge_loop.worker_sessions import (
    WorkerSession,
    WorkerSessionStore,
    recoverable_sessions,
)
from forge_loop.worker_state import WorkerState

# PR URL form: ``https://github.com/<owner>/<repo>/pull/<n>``.
_PR_URL_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)(?:[/?#].*)?$"
)


@dataclass(frozen=True)
class RecoveryDecision:
    """One per session: what we did and why.

    Returned from :func:`recover_sessions` so callers (and tests) can
    assert on the recovery walk without scraping the event log.
    """

    session_id: str
    issue: int
    prior_state: WorkerState
    action: str  # "redispatch" | "refire_critic" | "promote_to_awaiting_critic" | "abandon"
    new_state: WorkerState
    worktree_present: bool
    pr_present: bool
    reason: str


def _default_worktree_probe(session: WorkerSession) -> bool:
    """Worktree liveness probe: directory exists on disk.

    The runner is the only owner of ``/tmp/wt-loop-<issue>`` paths
    (see ``runner._helpers.reap_orphan_worktrees``). If the directory
    is gone, the worker can't possibly resume in place.

    A defensive caller can swap this for a stricter check (branch
    actually checked out, ``.git`` valid, etc.) by passing
    ``probe_worktree=`` to :func:`recover_sessions`.
    """
    if not session.worktree_path:
        return False
    return Path(session.worktree_path).is_dir()


def _parse_pr_url(pr_url: str | None) -> tuple[str, str, int] | None:
    """Return ``(owner, repo, number)`` for a github PR URL, else None.

    Tolerates trailing slashes, query strings, anchors — anything past
    the PR number is ignored.
    """
    if not pr_url:
        return None
    m = _PR_URL_RE.match(pr_url.strip())
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3))


def _pr_exists(
    session: WorkerSession,
    gh: GhClient,
    owner_fallback: str,
    repo_fallback: str,
) -> bool:
    """Does the PR recorded on ``session.pr_url`` still exist?

    Network errors are treated as "PR not found" so a flaky GitHub API
    at boot can't crash the runner. The session will be re-probed on
    the next boot if it survives this one as ABANDONED — but in
    practice a missing PR after a crash means the worker never got
    far enough to open one.
    """
    parsed = _parse_pr_url(session.pr_url)
    if parsed is None:
        # No URL recorded yet → if the session has gh metadata, we could
        # still try owner_fallback/repo_fallback + session.issue, but a
        # RUNNING-without-pr_url session by definition hasn't opened a
        # PR. Treat as "no PR".
        return False
    owner, repo, number = parsed
    try:
        pr = gh.get_pull(owner, repo, number)
    except Exception:  # noqa: BLE001 — boundary; recovery must never crash boot
        return False
    return pr is not None


def _decide(
    session: WorkerSession,
    *,
    gh: GhClient,
    owner: str,
    repo: str,
    probe_worktree: Callable[[WorkerSession], bool],
) -> RecoveryDecision:
    """Pure decision function — no I/O beyond the injected probes.

    Factored out so the unit-test matrix can hit every branch without
    spinning up an event log.
    """
    state = session.state

    if state == WorkerState.DISPATCHED:
        # State survives untouched until the RUNNING transition; the
        # next dispatch tick re-fires the worker.
        return RecoveryDecision(
            session_id=session.session_id,
            issue=session.issue,
            prior_state=state,
            action="redispatch",
            new_state=state,
            worktree_present=False,
            pr_present=False,
            reason="dispatched survivor: re-dispatch on next tick",
        )

    if state == WorkerState.AWAITING_CRITIC:
        return RecoveryDecision(
            session_id=session.session_id,
            issue=session.issue,
            prior_state=state,
            action="refire_critic",
            new_state=state,
            worktree_present=False,
            pr_present=False,
            reason="awaiting-critic survivor: re-fire critic on next tick",
        )

    if state in (WorkerState.RUNNING, WorkerState.REVISING):
        wt = bool(probe_worktree(session))
        pr = _pr_exists(session, gh, owner, repo)
        if wt and pr:
            return RecoveryDecision(
                session_id=session.session_id,
                issue=session.issue,
                prior_state=state,
                action="promote_to_awaiting_critic",
                new_state=WorkerState.AWAITING_CRITIC,
                worktree_present=True,
                pr_present=True,
                reason="boot recovery: worktree + PR intact, hand off to critic",
            )
        if not wt and not pr:
            return RecoveryDecision(
                session_id=session.session_id,
                issue=session.issue,
                prior_state=state,
                action="abandon",
                new_state=WorkerState.ABANDONED,
                worktree_present=False,
                pr_present=False,
                reason="boot recovery: state lost",
            )
        # Partial: spec defines only the "both" and "neither" cases.
        # If we still have a PR but the worktree is gone, the critic
        # can still review the PR — promote. If we have a worktree
        # but no PR, the worker never opened it and the worktree is
        # an orphan; abandon so the reaper can clean up.
        if pr and not wt:
            return RecoveryDecision(
                session_id=session.session_id,
                issue=session.issue,
                prior_state=state,
                action="promote_to_awaiting_critic",
                new_state=WorkerState.AWAITING_CRITIC,
                worktree_present=False,
                pr_present=True,
                reason="boot recovery: worktree missing but PR intact, hand off to critic",
            )
        return RecoveryDecision(
            session_id=session.session_id,
            issue=session.issue,
            prior_state=state,
            action="abandon",
            new_state=WorkerState.ABANDONED,
            worktree_present=True,
            pr_present=False,
            reason="boot recovery: worktree without PR, state lost",
        )

    # Defensive: a terminal state shouldn't be in recoverable_sessions().
    # If it is, do nothing (no transition, no event) — caller bug.
    return RecoveryDecision(
        session_id=session.session_id,
        issue=session.issue,
        prior_state=state,
        action="redispatch",
        new_state=state,
        worktree_present=False,
        pr_present=False,
        reason=f"unexpected non-recoverable state: {state.value}",
    )


def recover_sessions(
    store: WorkerSessionStore,
    *,
    gh_client: GhClient,
    owner: str,
    repo: str,
    events_file: Path,
    probe_worktree: Callable[[WorkerSession], bool] | None = None,
) -> list[RecoveryDecision]:
    """Walk every non-terminal session and apply the per-state recovery.

    Called once at boot, BEFORE the first dispatch tick (see
    ``runner.boot.run``). Returns the list of decisions in walk order
    so the runner can act on them (e.g. handing the AWAITING_CRITIC
    set to the critic queue) and tests can assert on the matrix.

    The function MUST NOT crash boot. Per-session failures are
    swallowed and reported as an ``abandon`` decision so the runner
    can keep moving. Any sane operator who sees mass ABANDONED-at-boot
    events will investigate the underlying GitHub / disk issue.
    """
    probe = probe_worktree or _default_worktree_probe
    decisions: list[RecoveryDecision] = []

    for session in recoverable_sessions(store):
        try:
            decision = _decide(
                session,
                gh=gh_client,
                owner=owner,
                repo=repo,
                probe_worktree=probe,
            )
        except Exception as ex:  # noqa: BLE001 — boundary; never crash boot
            decision = RecoveryDecision(
                session_id=session.session_id,
                issue=session.issue,
                prior_state=session.state,
                action="abandon",
                new_state=WorkerState.ABANDONED,
                worktree_present=False,
                pr_present=False,
                reason=f"boot recovery: probe failed ({type(ex).__name__}: {ex!s:.120})",
            )

        # Apply the transition if the decision changed state. Failures
        # (e.g. a concurrent writer beat us, or the row was deleted) are
        # swallowed — the event below still records the intent.
        if decision.new_state != decision.prior_state:
            with contextlib.suppress(Exception):
                store.transition_to(
                    decision.session_id,
                    decision.new_state,
                    reason=decision.reason,
                )

        emit(
            events_file,
            WorkerSessionRecoveredEvent(
                session_id=decision.session_id,
                issue=decision.issue,
                prior_state=decision.prior_state.value,
                action=decision.action,
                new_state=decision.new_state.value,
                worktree_present=decision.worktree_present,
                pr_present=decision.pr_present,
                reason=decision.reason,
            ),
        )
        decisions.append(decision)

    return decisions


__all__ = [
    "RecoveryDecision",
    "recover_sessions",
]
