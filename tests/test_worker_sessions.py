"""Tests for the persistent-worker FSM + session store (issue #95).

Foundation tests: the state machine pins the legal transitions, the
SQLite store pins persistence + crash-recovery semantics. Runner
integration is the follow-up PR; nothing in this PR changes dispatch
behaviour.
"""

from __future__ import annotations

import pytest

from forge_loop.worker_sessions import (
    WorkerSession,
    WorkerSessionStore,
    recoverable_sessions,
)
from forge_loop.worker_state import (
    InvalidTransition,
    WorkerState,
    allowed_next,
    is_allowed,
    transition,
)


# ---------------------------------------------------------------------------
# FSM — legal transitions are exactly the set documented in the module.
# ---------------------------------------------------------------------------


def test_dispatched_can_become_running_or_abandoned() -> None:
    assert is_allowed(WorkerState.DISPATCHED, WorkerState.RUNNING)
    assert is_allowed(WorkerState.DISPATCHED, WorkerState.ABANDONED)
    # But not terminal-success directly without running.
    assert not is_allowed(WorkerState.DISPATCHED, WorkerState.MERGED)


def test_running_to_awaiting_critic_is_the_happy_path_edge() -> None:
    assert is_allowed(WorkerState.RUNNING, WorkerState.AWAITING_CRITIC)


def test_awaiting_critic_can_revise_merge_or_abandon() -> None:
    next_states = allowed_next(WorkerState.AWAITING_CRITIC)
    assert next_states == frozenset({
        WorkerState.REVISING,
        WorkerState.MERGED,
        WorkerState.ABANDONED,
    })


def test_revising_loops_back_to_awaiting_critic() -> None:
    """The ping-pong edge — revision opens a new commit, exits, critic
    re-runs. This is the loop that the persistent-worker design exploits
    to keep prompt cache warm across iterations."""
    assert is_allowed(WorkerState.REVISING, WorkerState.AWAITING_CRITIC)


def test_terminal_states_have_no_outgoing_transitions() -> None:
    assert allowed_next(WorkerState.MERGED) == frozenset()
    assert allowed_next(WorkerState.ABANDONED) == frozenset()


def test_identity_transitions_are_rejected() -> None:
    """RUNNING -> RUNNING would hide a missing state-update bug."""
    with pytest.raises(InvalidTransition):
        transition(WorkerState.RUNNING, WorkerState.RUNNING)


def test_transition_returns_dst_when_valid() -> None:
    assert transition(WorkerState.DISPATCHED, WorkerState.RUNNING) == WorkerState.RUNNING


def test_invalid_transition_carries_src_and_dst_in_message() -> None:
    with pytest.raises(InvalidTransition) as excinfo:
        transition(WorkerState.MERGED, WorkerState.RUNNING)
    msg = str(excinfo.value)
    assert "merged" in msg
    assert "running" in msg


def test_is_active_only_for_running_and_revising() -> None:
    """The parallel-slot accounting contract — AWAITING_CRITIC sessions
    are paused waiting on critic, so they don't burn a worker slot."""
    assert WorkerState.RUNNING.is_active
    assert WorkerState.REVISING.is_active
    assert not WorkerState.AWAITING_CRITIC.is_active
    assert not WorkerState.DISPATCHED.is_active
    assert not WorkerState.MERGED.is_active


def test_is_terminal_only_for_merged_and_abandoned() -> None:
    assert WorkerState.MERGED.is_terminal
    assert WorkerState.ABANDONED.is_terminal
    assert not WorkerState.RUNNING.is_terminal


# ---------------------------------------------------------------------------
# Store — persistence + transitions + recovery surface.
# ---------------------------------------------------------------------------


def test_create_seeds_session_in_dispatched_state() -> None:
    store = WorkerSessionStore(":memory:")
    sess = store.create(issue=42, branch="loop/42-demo", worktree_path="/tmp/wt-loop-42")
    assert sess.state == WorkerState.DISPATCHED
    assert sess.session_id  # non-empty
    assert sess.issue == 42
    assert sess.critic_iterations == 0
    # Round-trip — get(session_id) returns the same row.
    loaded = store.get(sess.session_id)
    assert loaded is not None
    assert loaded.session_id == sess.session_id


def test_transition_to_validates_against_fsm() -> None:
    store = WorkerSessionStore(":memory:")
    sess = store.create(issue=1, branch="loop/1")
    # Illegal: DISPATCHED -> MERGED skips RUNNING + AWAITING_CRITIC.
    with pytest.raises(InvalidTransition):
        store.transition_to(sess.session_id, WorkerState.MERGED)


def test_transition_to_persists_reason() -> None:
    store = WorkerSessionStore(":memory:")
    sess = store.create(issue=1, branch="loop/1")
    after = store.transition_to(
        sess.session_id, WorkerState.RUNNING, reason="worker dispatched at tick 5"
    )
    assert after.state == WorkerState.RUNNING
    assert after.last_transition_reason == "worker dispatched at tick 5"
    assert after.updated_at >= sess.updated_at


def test_transition_to_raises_keyerror_for_unknown_session() -> None:
    store = WorkerSessionStore(":memory:")
    with pytest.raises(KeyError):
        store.transition_to("does-not-exist", WorkerState.RUNNING)


def test_by_state_returns_matching_rows_in_creation_order() -> None:
    store = WorkerSessionStore(":memory:")
    s1 = store.create(issue=1, branch="b1")
    s2 = store.create(issue=2, branch="b2")
    s3 = store.create(issue=3, branch="b3")
    store.transition_to(s1.session_id, WorkerState.RUNNING)
    store.transition_to(s3.session_id, WorkerState.RUNNING)
    # s2 still DISPATCHED
    running = store.by_state(WorkerState.RUNNING)
    assert [s.session_id for s in running] == [s1.session_id, s3.session_id]


def test_active_count_excludes_awaiting_critic() -> None:
    """The parallel-slot accounting contract under load — a paused
    AWAITING_CRITIC session doesn't block a new dispatch."""
    store = WorkerSessionStore(":memory:")
    s1 = store.create(issue=1, branch="b1")
    s2 = store.create(issue=2, branch="b2")
    store.transition_to(s1.session_id, WorkerState.RUNNING)
    store.transition_to(s1.session_id, WorkerState.AWAITING_CRITIC, reason="pr opened")
    store.transition_to(s2.session_id, WorkerState.RUNNING)
    assert store.active_count() == 1  # only s2, s1 is paused waiting


def test_increment_iterations_returns_new_value() -> None:
    store = WorkerSessionStore(":memory:")
    sess = store.create(issue=1, branch="b")
    assert store.increment_iterations(sess.session_id) == 1
    assert store.increment_iterations(sess.session_id) == 2
    assert store.get(sess.session_id).critic_iterations == 2


def test_set_sdk_session_id_persists() -> None:
    store = WorkerSessionStore(":memory:")
    sess = store.create(issue=1, branch="b")
    store.set_sdk_session_id(sess.session_id, "sdk-abc-123")
    loaded = store.get(sess.session_id)
    assert loaded is not None
    assert loaded.sdk_session_id == "sdk-abc-123"


def test_set_pr_url_persists() -> None:
    store = WorkerSessionStore(":memory:")
    sess = store.create(issue=1, branch="b")
    store.set_pr_url(sess.session_id, "https://github.com/o/r/pull/1")
    loaded = store.get(sess.session_id)
    assert loaded is not None
    assert loaded.pr_url == "https://github.com/o/r/pull/1"


def test_recoverable_sessions_returns_non_terminal_only() -> None:
    """Pre-#95 there was no way for the runner to pick up where a
    crashed process left off. This is the crash-recovery surface."""
    store = WorkerSessionStore(":memory:")
    s1 = store.create(issue=1, branch="b1")
    s2 = store.create(issue=2, branch="b2")
    s3 = store.create(issue=3, branch="b3")
    s4 = store.create(issue=4, branch="b4")
    # s1 -> running, s2 -> abandoned, s3 -> awaiting_critic, s4 stays dispatched
    store.transition_to(s1.session_id, WorkerState.RUNNING)
    store.transition_to(s2.session_id, WorkerState.ABANDONED, reason="budget cap")
    store.transition_to(s3.session_id, WorkerState.RUNNING)
    store.transition_to(s3.session_id, WorkerState.AWAITING_CRITIC, reason="pr opened")

    ids = {s.session_id for s in recoverable_sessions(store)}
    # s2 is terminal — excluded.
    assert s1.session_id in ids
    assert s3.session_id in ids
    assert s4.session_id in ids  # still dispatched, never started
    assert s2.session_id not in ids  # terminal — abandoned


def test_persistence_across_connections(tmp_path) -> None:
    """The whole point of SQLite — survive a process restart. Open two
    stores against the same file; rows written by one are readable by
    the other."""
    db_file = tmp_path / "sessions.db"
    store_a = WorkerSessionStore(str(db_file))
    sess = store_a.create(issue=42, branch="loop/42")
    store_a.transition_to(sess.session_id, WorkerState.RUNNING)
    store_a.close()

    store_b = WorkerSessionStore(str(db_file))
    loaded = store_b.get(sess.session_id)
    assert loaded is not None
    assert loaded.state == WorkerState.RUNNING


# ---------------------------------------------------------------------------
# Settings — new fields land in the unified config tree.
# ---------------------------------------------------------------------------


def test_settings_persistent_worker_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default must be OFF — this PR is foundation only; flipping it on
    by default would change runtime behaviour without runner-integration
    code to back it."""
    from forge_loop.settings import IterationSettings

    s = IterationSettings()
    assert s.persistent_worker is False
    assert s.max_critic_iterations == 3


def test_settings_persistent_worker_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from forge_loop.settings import Settings

    monkeypatch.setattr("forge_loop.settings._repo_root", lambda: tmp_path)
    for k in list(__import__("os").environ):
        if k.startswith(("LOOP_", "FORGE_LOOP_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LOOP_PERSISTENT_WORKER", "1")
    monkeypatch.setenv("LOOP_MAX_CRITIC_ITERATIONS", "5")
    s = Settings.load()
    assert s.iteration.persistent_worker is True
    assert s.iteration.max_critic_iterations == 5
