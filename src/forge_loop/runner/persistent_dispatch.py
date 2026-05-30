"""Persistent-worker dispatch glue — wires WorkerSessionStore through dispatch.

Issue #108 (part of epic #95). The runner's legacy dispatch loop is
fire-and-forget: spawn worker → exit → repeat. With
``settings.iteration.persistent_worker=True`` we route every dispatch
through :class:`forge_loop.worker_sessions.WorkerSessionStore` so the
FSM (:mod:`forge_loop.worker_state`) becomes the source of truth for
what's in flight.

This module is intentionally a THIN shim over the store + a small set
of typed events. The worker subprocess itself (``run_worker``) is
unchanged — only the surrounding dispatch wrapper transitions states
and records outcomes. Keeping the surface narrow means the legacy
``persistent_worker=False`` path (no rows touched) is trivially
preserved: callers just don't pass a store.

Public surface:

* :func:`get_or_resume_session` — issue → either a non-terminal row
  found via ``store.by_issue`` or a freshly seeded one in ``DISPATCHED``.
* :func:`mark_running` — DISPATCHED → RUNNING, fired immediately before
  the SDK call.
* :func:`record_outcome` — RUNNING → AWAITING_CRITIC (success+PR) or
  RUNNING → ABANDONED (any failure). Captures ``pr_url`` via
  ``store.set_pr_url`` on the success path.
* :func:`persistent_worker_enabled` — env-aware predicate the dispatch
  loop uses to decide whether to engage the store at all.

All transitions emit a typed
:class:`forge_loop.events.WorkerSessionTransitionEvent` so operators
have a single line per FSM edge in ``events.jsonl``.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from forge_loop.events import WorkerSessionTransitionEvent, emit
from forge_loop.worker_sessions import WorkerSession, WorkerSessionStore
from forge_loop.worker_state import WorkerState

if TYPE_CHECKING:  # pragma: no cover
    from forge_loop.worker import WorkerOutcome


def persistent_worker_enabled() -> bool:
    """Resolve ``settings.iteration.persistent_worker`` defensively.

    A Settings load failure (malformed YAML, missing schema) MUST NOT
    take the runner down — returns ``False`` so the legacy path
    survives. Operators see the underlying error via the normal
    ``Settings.load`` traceback elsewhere.
    """
    try:
        from forge_loop.settings import Settings

        s = Settings.load()
    except Exception:
        return False
    return bool(getattr(s.iteration, "persistent_worker", False))


def open_default_store(state_dir: Path) -> WorkerSessionStore:
    """Open the canonical sessions DB under the runner's state dir.

    The path mirrors ``boot._run_crash_recovery`` so the dispatch loop
    and the recovery walk share one source of truth. Created lazily;
    ``WorkerSessionStore`` handles ``CREATE TABLE IF NOT EXISTS``.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    return WorkerSessionStore(state_dir / "worker-sessions.db")


def get_or_resume_session(
    store: WorkerSessionStore,
    *,
    issue: int,
    branch: str,
    worktree_path: str,
    events_file: Path | None = None,
) -> tuple[WorkerSession, bool]:
    """Find a non-terminal session for ``issue`` or seed a fresh one.

    Returns ``(session, resumed)``. ``resumed=True`` when an existing
    non-terminal row was found (caller can pick up where the previous
    tick left off — the SDK-session-id resumption is a separate ticket,
    #95 follow-up). ``resumed=False`` when we created a fresh
    ``DISPATCHED`` row.

    Tie-break for resume: ``store.by_issue`` returns rows most-recent
    first. We take the first non-terminal hit. Multiple non-terminal
    rows for one issue would be a store bug; we don't try to repair
    that here — the operator sees it via the events log.

    On the create path we emit a typed transition event for the
    ``-> DISPATCHED`` edge so the events log has a complete history.
    """
    existing = [s for s in store.by_issue(issue) if not s.is_terminal]
    if existing:
        return existing[0], True

    sess = store.create(issue=issue, branch=branch, worktree_path=worktree_path)
    if events_file is not None:
        with contextlib.suppress(Exception):
            emit(
                events_file,
                WorkerSessionTransitionEvent(
                    session_id=sess.session_id,
                    issue=issue,
                    prior_state="",
                    new_state=WorkerState.DISPATCHED.value,
                    reason="fresh dispatch",
                    pr_url=None,
                ),
            )
    return sess, False


def mark_running(
    store: WorkerSessionStore,
    *,
    session: WorkerSession,
    events_file: Path | None = None,
    reason: str = "sdk call starting",
) -> WorkerSession:
    """Transition DISPATCHED → RUNNING just before the SDK invocation.

    Idempotent on already-RUNNING rows (e.g. a resumed session that the
    previous tick had already promoted to RUNNING before crashing):
    returns the row unchanged without raising. Any other prior state
    (terminal, AWAITING_CRITIC, REVISING) is left untouched and the
    caller gets the row back as-is — the recovery walk owns those
    transitions.
    """
    if session.state == WorkerState.RUNNING:
        return session
    if session.state != WorkerState.DISPATCHED:
        return session

    updated = store.transition_to(session.session_id, WorkerState.RUNNING, reason=reason)
    if events_file is not None:
        with contextlib.suppress(Exception):
            emit(
                events_file,
                WorkerSessionTransitionEvent(
                    session_id=updated.session_id,
                    issue=updated.issue,
                    prior_state=WorkerState.DISPATCHED.value,
                    new_state=WorkerState.RUNNING.value,
                    reason=reason,
                    pr_url=updated.pr_url,
                ),
            )
    return updated


def record_outcome(
    store: WorkerSessionStore,
    *,
    session: WorkerSession,
    outcome: WorkerOutcome,
    events_file: Path | None = None,
) -> WorkerSession:
    """Apply the FSM edge implied by a worker outcome.

    The decision tree is the acceptance criteria of issue #108:

    * outcome opened a PR (``status in {"open","merged"}`` and a
      ``pr_url``) → RUNNING → AWAITING_CRITIC; capture ``pr_url`` via
      :meth:`WorkerSessionStore.set_pr_url`.
    * anything else (``failed``, ``timeout``, ``no_pr``, ...) →
      RUNNING → ABANDONED with the worker's error/status as the
      transition reason.

    We treat ``merged`` as still requiring a critic pass — the legacy
    dispatch loop runs the critic against open + merged PRs alike (see
    ``_run_critic_for_outcomes``), so AWAITING_CRITIC is the correct
    landing state. A separate edge AWAITING_CRITIC → MERGED is owned
    by the critic-approval ticket, not this one.

    Pre-condition: ``session.state == RUNNING``. If a caller hands us
    a row in any other state we no-op — the recovery walk and the
    critic loop own those edges and double-firing them is a bug.
    """
    if session.state != WorkerState.RUNNING:
        return session

    success = outcome.status in {"open", "merged"} and bool(outcome.pr_url)

    if success:
        # Capture the PR URL BEFORE the transition so a crash between
        # the two leaves a session in RUNNING with the URL set — the
        # recovery walk can then promote correctly.
        assert outcome.pr_url is not None  # narrowed by success guard
        store.set_pr_url(session.session_id, outcome.pr_url)
        updated = store.transition_to(
            session.session_id,
            WorkerState.AWAITING_CRITIC,
            reason="worker opened PR",
        )
        if events_file is not None:
            with contextlib.suppress(Exception):
                emit(
                    events_file,
                    WorkerSessionTransitionEvent(
                        session_id=updated.session_id,
                        issue=updated.issue,
                        prior_state=WorkerState.RUNNING.value,
                        new_state=WorkerState.AWAITING_CRITIC.value,
                        reason="worker opened PR",
                        pr_url=outcome.pr_url,
                    ),
                )
        return updated

    # Failure path: capture status + error in the transition reason so
    # operators can read events.jsonl and understand WHY a session
    # abandoned without spelunking the worker log.
    reason = f"worker {outcome.status}" + (f": {outcome.error[:200]}" if outcome.error else "")
    updated = store.transition_to(session.session_id, WorkerState.ABANDONED, reason=reason)
    if events_file is not None:
        with contextlib.suppress(Exception):
            emit(
                events_file,
                WorkerSessionTransitionEvent(
                    session_id=updated.session_id,
                    issue=updated.issue,
                    prior_state=WorkerState.RUNNING.value,
                    new_state=WorkerState.ABANDONED.value,
                    reason=reason,
                    pr_url=outcome.pr_url,
                ),
            )
    return updated


__all__ = [
    "get_or_resume_session",
    "mark_running",
    "open_default_store",
    "persistent_worker_enabled",
    "record_outcome",
]
