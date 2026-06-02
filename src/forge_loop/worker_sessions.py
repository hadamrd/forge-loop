"""SQLite-backed session store for the persistent-worker epic (issue #95).

One row per worker session. Tracks state machine position, SDK
session_id (for prompt-cache-preserving resumption), worktree path,
PR URL, critic-iteration counter, and audit timestamps. The runner
queries this store on every tick to drive its dispatch decisions
without losing context across the operator process boundary
(restart, crash, redeploy).

The store is intentionally small + sync — one connection per
instance, no async, no connection pool. Tick rate is O(seconds);
session-row updates per tick are O(workers) = single digits. SQLite
WAL mode is the durability + concurrency story; nothing fancier is
needed at this scale.

Public surface mirrors the legacy worker outcome shape so existing
runner code can adopt it incrementally — see
:class:`forge_loop.worker_state.WorkerState` for the FSM.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from forge_loop.worker_state import (
    InvalidTransition,
    WorkerState,
    is_allowed,
)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _new_id() -> str:
    """Generate a session id. ``urn:uuid`` form, short enough to log."""
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Row dataclass — what the store hands back to callers.
# ---------------------------------------------------------------------------


@dataclass
class WorkerSession:
    """One session row.

    Identifiers split into two layers:
    - ``session_id`` is our internal opaque id; persists across restarts.
    - ``sdk_session_id`` is the Claude Agent SDK's id; resumable via the
      SDK's ``resume`` arg on the next dispatch. Populated lazily once
      the SDK returns one.
    """

    session_id: str
    issue: int
    branch: str
    state: WorkerState
    worktree_path: str = ""
    sdk_session_id: str | None = None
    pr_url: str | None = None
    lease_expires_at: str | None = None
    critic_iterations: int = 0
    last_transition_reason: str = ""
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    @property
    def is_active(self) -> bool:
        """Mirror of :attr:`WorkerState.is_active` — does this session
        count against the parallel-slot budget?"""
        return self.state.is_active


# ---------------------------------------------------------------------------
# Store — thin sync wrapper around sqlite3.
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS worker_sessions (
    session_id          TEXT PRIMARY KEY,
    issue               INTEGER NOT NULL,
    branch              TEXT NOT NULL,
    state               TEXT NOT NULL,
    worktree_path       TEXT NOT NULL DEFAULT '',
    sdk_session_id      TEXT,
    pr_url              TEXT,
    lease_expires_at    TEXT,
    critic_iterations   INTEGER NOT NULL DEFAULT 0,
    last_transition_reason TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_worker_sessions_issue ON worker_sessions(issue);
CREATE INDEX IF NOT EXISTS idx_worker_sessions_state ON worker_sessions(state);
"""


class WorkerSessionStore:
    """Sync SQLite store for :class:`WorkerSession` rows.

    Use ``":memory:"`` for tests; an absolute path otherwise. The
    process holds one open connection; SQLite's WAL journal is the
    durability mechanism so crashes between transitions don't lose
    rows (each ``transition_to`` is wrapped in a transaction).
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # WAL for concurrent reads + better crash recovery. No-op on
        # ":memory:" but harmless.
        if self._db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._ensure_lease_column()

    # ------------------------------------------------------------------
    # Create / read
    # ------------------------------------------------------------------

    def create(
        self,
        *,
        issue: int,
        branch: str,
        worktree_path: str = "",
    ) -> WorkerSession:
        """Insert a fresh session in :attr:`WorkerState.DISPATCHED`."""
        sess = WorkerSession(
            session_id=_new_id(),
            issue=issue,
            branch=branch,
            worktree_path=worktree_path,
            state=WorkerState.DISPATCHED,
        )
        self._conn.execute(
            "INSERT INTO worker_sessions ("
            " session_id, issue, branch, state, worktree_path,"
            " sdk_session_id, pr_url, lease_expires_at, critic_iterations,"
            " last_transition_reason, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sess.session_id,
                sess.issue,
                sess.branch,
                sess.state.value,
                sess.worktree_path,
                sess.sdk_session_id,
                sess.pr_url,
                sess.lease_expires_at,
                sess.critic_iterations,
                sess.last_transition_reason,
                sess.created_at,
                sess.updated_at,
            ),
        )
        return sess

    def get(self, session_id: str) -> WorkerSession | None:
        row = self._conn.execute(
            "SELECT * FROM worker_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return self._row_to_session(row) if row else None

    def by_issue(self, issue: int) -> list[WorkerSession]:
        """Every session for a given issue — most recent first."""
        rows = self._conn.execute(
            "SELECT * FROM worker_sessions WHERE issue = ? ORDER BY created_at DESC",
            (issue,),
        ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def by_state(self, *states: WorkerState) -> list[WorkerSession]:
        """All sessions matching any of the given states.

        Used by the runner to find resume-candidates on crash recovery
        (scan non-terminal states) and to count parallel-slot consumers
        (``RUNNING`` + ``REVISING``).
        """
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        rows = self._conn.execute(
            f"SELECT * FROM worker_sessions WHERE state IN ({placeholders}) ORDER BY created_at",
            tuple(s.value for s in states),
        ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def active_count(self) -> int:
        """How many sessions count against the parallel budget right now?"""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM worker_sessions WHERE state IN (?, ?)",
            (WorkerState.RUNNING.value, WorkerState.REVISING.value),
        ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # Mutate — every mutator validates the transition via worker_state.
    # ------------------------------------------------------------------

    def transition_to(
        self,
        session_id: str,
        new_state: WorkerState,
        *,
        reason: str = "",
    ) -> WorkerSession:
        """Move ``session_id`` to ``new_state``. Raises if illegal.

        Wrapped in a transaction so a process death mid-transition
        leaves the row in its pre-transition state, never half-updated.
        """
        sess = self.get(session_id)
        if sess is None:
            raise KeyError(f"unknown session_id: {session_id}")
        if not is_allowed(sess.state, new_state):
            raise InvalidTransition(sess.state, new_state)
        now = _now_iso()
        with self._conn:
            self._conn.execute(
                "UPDATE worker_sessions"
                " SET state = ?, updated_at = ?, last_transition_reason = ?"
                " WHERE session_id = ?",
                (new_state.value, now, reason, session_id),
            )
        sess.state = new_state
        sess.updated_at = now
        sess.last_transition_reason = reason
        return sess

    def set_sdk_session_id(self, session_id: str, sdk_id: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE worker_sessions SET sdk_session_id = ?, updated_at = ?"
                " WHERE session_id = ?",
                (sdk_id, _now_iso(), session_id),
            )

    def set_pr_url(self, session_id: str, pr_url: str) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE worker_sessions SET pr_url = ?, updated_at = ? WHERE session_id = ?",
                (pr_url, _now_iso(), session_id),
            )

    def set_lease_expires_at(self, session_id: str, lease_expires_at: str | None) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE worker_sessions SET lease_expires_at = ?, updated_at = ?"
                " WHERE session_id = ?",
                (lease_expires_at, _now_iso(), session_id),
            )

    def increment_iterations(self, session_id: str) -> int:
        """Bump the critic-iteration counter; return the new value.

        The runner reads this against
        ``Settings.iteration.max_critic_iterations`` to decide when to
        abandon a stuck ping-pong.
        """
        with self._conn:
            cur = self._conn.execute(
                "UPDATE worker_sessions"
                " SET critic_iterations = critic_iterations + 1, updated_at = ?"
                " WHERE session_id = ? RETURNING critic_iterations",
                (_now_iso(), session_id),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(f"unknown session_id: {session_id}")
            return int(row["critic_iterations"])

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def _ensure_lease_column(self) -> None:
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(worker_sessions)").fetchall()
        }
        if "lease_expires_at" not in columns:
            self._conn.execute("ALTER TABLE worker_sessions ADD COLUMN lease_expires_at TEXT")

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> WorkerSession:
        return WorkerSession(
            session_id=row["session_id"],
            issue=int(row["issue"]),
            branch=row["branch"],
            state=WorkerState(row["state"]),
            worktree_path=row["worktree_path"] or "",
            sdk_session_id=row["sdk_session_id"],
            pr_url=row["pr_url"],
            lease_expires_at=row["lease_expires_at"],
            critic_iterations=int(row["critic_iterations"]),
            last_transition_reason=row["last_transition_reason"] or "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# ---------------------------------------------------------------------------
# Crash recovery — scan for non-terminal sessions on runner boot.
# ---------------------------------------------------------------------------


def recoverable_sessions(store: WorkerSessionStore) -> Iterable[WorkerSession]:
    """Return every session in a non-terminal state.

    Called by the runner at boot. Each session needs a per-state
    recovery decision:
    - ``DISPATCHED``: re-dispatch
    - ``RUNNING``: probe worktree + PR; if PR exists, move to
      ``AWAITING_CRITIC``; else ABANDONED
    - ``AWAITING_CRITIC``: re-fire critic
    - ``REVISING``: same as RUNNING

    The decision tree lives in the runner integration (follow-up PR);
    this helper only surfaces the set.
    """
    return store.by_state(
        WorkerState.DISPATCHED,
        WorkerState.RUNNING,
        WorkerState.AWAITING_CRITIC,
        WorkerState.REVISING,
    )


__all__ = [
    "WorkerSession",
    "WorkerSessionStore",
    "recoverable_sessions",
]
