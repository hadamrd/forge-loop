"""Tests for runner crash-recovery walk (issue #111).

The recovery walk runs once at runner boot, BEFORE the first dispatch
tick. Each non-terminal session left behind by a crashed runner must
map to the correct recovery action per the spec in
:mod:`forge_loop.runner.recovery`.
"""

from __future__ import annotations

import json
from pathlib import Path

from forge_loop.events import WorkerSessionRecoveredEvent
from forge_loop.gh_client import (
    MockGhClient,
    PullRequest,
)
from forge_loop.runner.recovery import (
    recover_sessions,
)
from forge_loop.worker_sessions import WorkerSessionStore
from forge_loop.worker_state import WorkerState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _events(events_file: Path) -> list[dict]:
    if not events_file.exists():
        return []
    return [json.loads(line) for line in events_file.read_text().splitlines() if line]


def _recovered(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("kind") == "worker_session_recovered"]


def _make_pr() -> PullRequest:
    return PullRequest(
        number=1,
        title="t",
        body="b",
        state="open",
        draft=False,
        head_ref="loop/1",
        base_ref="trunk",
        labels=[],
        additions=0,
        deletions=0,
        changed_files=0,
    )


# ---------------------------------------------------------------------------
# Happy path: each of the four non-terminal states gets the right action.
# ---------------------------------------------------------------------------


def test_dispatched_session_marked_for_redispatch(tmp_path: Path) -> None:
    store = WorkerSessionStore(":memory:")
    sess = store.create(issue=1, branch="loop/1")
    events_file = tmp_path / "events.jsonl"

    decisions = recover_sessions(
        store,
        gh_client=MockGhClient(),
        owner="o",
        repo="r",
        events_file=events_file,
    )

    assert len(decisions) == 1
    d = decisions[0]
    assert d.action == "redispatch"
    assert d.new_state == WorkerState.DISPATCHED  # unchanged
    # Store row also unchanged.
    assert store.get(sess.session_id).state == WorkerState.DISPATCHED
    # Event emitted.
    recs = _recovered(_events(events_file))
    assert len(recs) == 1
    assert recs[0]["action"] == "redispatch"
    assert recs[0]["prior_state"] == "dispatched"


def test_awaiting_critic_session_marked_for_refire(tmp_path: Path) -> None:
    store = WorkerSessionStore(":memory:")
    sess = store.create(issue=2, branch="loop/2")
    store.transition_to(sess.session_id, WorkerState.RUNNING)
    store.transition_to(sess.session_id, WorkerState.AWAITING_CRITIC, reason="pr opened")
    events_file = tmp_path / "events.jsonl"

    decisions = recover_sessions(
        store,
        gh_client=MockGhClient(),
        owner="o",
        repo="r",
        events_file=events_file,
    )

    assert decisions[0].action == "refire_critic"
    assert decisions[0].new_state == WorkerState.AWAITING_CRITIC
    assert store.get(sess.session_id).state == WorkerState.AWAITING_CRITIC


def test_running_with_worktree_and_pr_promotes_to_awaiting_critic(
    tmp_path: Path,
) -> None:
    """Spec: RUNNING + worktree on disk + PR exists → AWAITING_CRITIC."""
    wt = tmp_path / "wt-loop-3"
    wt.mkdir()
    pr_url = "https://github.com/o/r/pull/77"

    store = WorkerSessionStore(":memory:")
    sess = store.create(issue=3, branch="loop/3", worktree_path=str(wt))
    store.transition_to(sess.session_id, WorkerState.RUNNING)
    store.set_pr_url(sess.session_id, pr_url)

    gh = MockGhClient(pulls={("o", "r", 77): _make_pr()})
    events_file = tmp_path / "events.jsonl"

    decisions = recover_sessions(
        store, gh_client=gh, owner="o", repo="r", events_file=events_file,
    )
    d = decisions[0]
    assert d.action == "promote_to_awaiting_critic"
    assert d.worktree_present is True
    assert d.pr_present is True
    assert store.get(sess.session_id).state == WorkerState.AWAITING_CRITIC
    # GhClient.get_pull was called with the parsed coordinates.
    assert ("get_pull", {"owner": "o", "repo": "r", "number": 77}) in gh.calls


def test_revising_promotes_same_as_running(tmp_path: Path) -> None:
    """REVISING uses the same probe logic as RUNNING."""
    wt = tmp_path / "wt-loop-4"
    wt.mkdir()
    pr_url = "https://github.com/o/r/pull/4"

    store = WorkerSessionStore(":memory:")
    sess = store.create(issue=4, branch="loop/4", worktree_path=str(wt))
    store.transition_to(sess.session_id, WorkerState.RUNNING)
    store.transition_to(sess.session_id, WorkerState.AWAITING_CRITIC)
    store.transition_to(sess.session_id, WorkerState.REVISING, reason="critic asked changes")
    store.set_pr_url(sess.session_id, pr_url)

    gh = MockGhClient(pulls={("o", "r", 4): _make_pr()})
    events_file = tmp_path / "events.jsonl"

    decisions = recover_sessions(
        store, gh_client=gh, owner="o", repo="r", events_file=events_file,
    )
    assert decisions[0].action == "promote_to_awaiting_critic"
    assert decisions[0].prior_state == WorkerState.REVISING
    assert store.get(sess.session_id).state == WorkerState.AWAITING_CRITIC


# ---------------------------------------------------------------------------
# Adversarial — deleted worktree + no PR → ABANDONED, not crash.
# ---------------------------------------------------------------------------


def test_running_with_no_worktree_and_no_pr_is_abandoned(tmp_path: Path) -> None:
    """The headline adversarial: state lost on both sides → ABANDONED."""
    store = WorkerSessionStore(":memory:")
    sess = store.create(
        issue=99,
        branch="loop/99",
        worktree_path=str(tmp_path / "definitely-not-here"),
    )
    store.transition_to(sess.session_id, WorkerState.RUNNING)
    # No pr_url set.

    gh = MockGhClient()  # empty pulls
    events_file = tmp_path / "events.jsonl"

    decisions = recover_sessions(
        store, gh_client=gh, owner="o", repo="r", events_file=events_file,
    )

    d = decisions[0]
    assert d.action == "abandon"
    assert d.new_state == WorkerState.ABANDONED
    assert d.worktree_present is False
    assert d.pr_present is False
    assert "state lost" in d.reason
    # Row is now terminal.
    assert store.get(sess.session_id).state == WorkerState.ABANDONED
    # Event recorded.
    rec = _recovered(_events(events_file))[0]
    assert rec["action"] == "abandon"
    assert rec["new_state"] == "abandoned"


def test_running_with_unparseable_pr_url_falls_back_to_no_pr(
    tmp_path: Path,
) -> None:
    """An empty/garbage pr_url MUST NOT crash and is treated as no PR."""
    store = WorkerSessionStore(":memory:")
    sess = store.create(issue=5, branch="loop/5", worktree_path="/nope")
    store.transition_to(sess.session_id, WorkerState.RUNNING)
    store.set_pr_url(sess.session_id, "not-a-url")

    decisions = recover_sessions(
        store, gh_client=MockGhClient(),
        owner="o", repo="r", events_file=tmp_path / "events.jsonl",
    )
    assert decisions[0].action == "abandon"


def test_gh_client_exception_does_not_crash_recovery(tmp_path: Path) -> None:
    """A flaky GitHub at boot must not take the runner down."""
    wt = tmp_path / "wt"
    wt.mkdir()

    class BoomGh:
        def get_pull(self, *a, **k):
            raise RuntimeError("network down")

        # Unused protocol methods — must exist for typing only.
        def issues_by_label(self, *a, **k): return []  # noqa: E704
        def get_issue(self, *a, **k): return None  # noqa: E704
        def add_comment(self, *a, **k): return None  # noqa: E704
        def add_labels(self, *a, **k): return None  # noqa: E704
        def remove_label(self, *a, **k): return None  # noqa: E704

    store = WorkerSessionStore(":memory:")
    sess = store.create(issue=6, branch="loop/6", worktree_path=str(wt))
    store.transition_to(sess.session_id, WorkerState.RUNNING)
    store.set_pr_url(sess.session_id, "https://github.com/o/r/pull/6")

    # Must not raise.
    decisions = recover_sessions(
        store, gh_client=BoomGh(),
        owner="o", repo="r", events_file=tmp_path / "events.jsonl",
    )
    # PR probe failed → treated as no PR → worktree-only → abandoned.
    assert decisions[0].action == "abandon"


def test_empty_recovery_walk_emits_nothing(tmp_path: Path) -> None:
    """Empty store at boot → zero decisions, zero events."""
    store = WorkerSessionStore(":memory:")
    events_file = tmp_path / "events.jsonl"
    decisions = recover_sessions(
        store, gh_client=MockGhClient(),
        owner="o", repo="r", events_file=events_file,
    )
    assert decisions == []
    assert _recovered(_events(events_file)) == []


def test_terminal_sessions_are_not_walked(tmp_path: Path) -> None:
    """Sanity: MERGED + ABANDONED stay out of the walk."""
    store = WorkerSessionStore(":memory:")
    s_done = store.create(issue=10, branch="b")
    store.transition_to(s_done.session_id, WorkerState.RUNNING)
    store.transition_to(s_done.session_id, WorkerState.AWAITING_CRITIC)
    store.transition_to(s_done.session_id, WorkerState.MERGED)
    store.create(issue=11, branch="b2")  # DISPATCHED — live; expected to survive walk

    decisions = recover_sessions(
        store, gh_client=MockGhClient(),
        owner="o", repo="r", events_file=tmp_path / "events.jsonl",
    )
    issues = [d.issue for d in decisions]
    assert issues == [11]


# ---------------------------------------------------------------------------
# Integration-flavoured: store survives across two "boots" against the
# same DB file, and the recovery walk on boot #2 leaks no session.
# ---------------------------------------------------------------------------


def test_recovery_walk_does_not_leak_session_across_simulated_restart(
    tmp_path: Path,
) -> None:
    """Simulates: tick crashes mid-flight (we drop store A without
    transitioning), runner restarts (store B), recovery runs, no
    non-terminal row remains in an undecided state."""
    db_file = tmp_path / "sessions.db"
    events_file = tmp_path / "events.jsonl"

    # Boot #1 — dispatch + start running, then "crash" (no graceful close).
    store_a = WorkerSessionStore(db_file)
    s1 = store_a.create(issue=20, branch="loop/20", worktree_path=str(tmp_path / "gone"))
    store_a.transition_to(s1.session_id, WorkerState.RUNNING)
    s2 = store_a.create(issue=21, branch="loop/21")  # never ran
    store_a.close()

    # Boot #2 — recovery walk.
    store_b = WorkerSessionStore(db_file)
    decisions = recover_sessions(
        store_b, gh_client=MockGhClient(),
        owner="o", repo="r", events_file=events_file,
    )
    by_issue = {d.issue: d for d in decisions}
    # s1: lost state → abandoned.
    assert by_issue[20].action == "abandon"
    assert store_b.get(s1.session_id).state == WorkerState.ABANDONED
    # s2: still dispatched → redispatch on next tick.
    assert by_issue[21].action == "redispatch"
    assert store_b.get(s2.session_id).state == WorkerState.DISPATCHED

    # Active-count must NOT include the abandoned row — slot is freed.
    assert store_b.active_count() == 0

    # Each session got exactly one recovery event.
    recs = _recovered(_events(events_file))
    assert len(recs) == 2
    assert {r["issue"] for r in recs} == {20, 21}


# ---------------------------------------------------------------------------
# Restart e2e (#272): an abandoned task-saga that pushed a branch AND opened a
# draft PR is fully compensated on the next boot — branch deleted + PR closed,
# not just the worktree reaped.
# ---------------------------------------------------------------------------


def test_abandoned_saga_with_branch_and_pr_is_fully_compensated_on_boot(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime, timedelta

    from forge_loop.control.recovery import reconcile_stale_sagas
    from forge_loop.tasks import Compensation, SqliteTaskSagaStore, TaskState

    db_file = tmp_path / "tasks.db"

    # Boot #1 — a worker leases issue 30, pushes loop/30, opens draft PR #888,
    # then is hard-killed (no graceful close: we just drop the store handle).
    store_a = SqliteTaskSagaStore(db_file)
    store_a.create(
        task_id="task-30-worker",
        saga_id="saga-30-worker",
        issue=30,
        branch="loop/30-feat",
        worktree="/tmp/wt-loop-30",
        compensations=(
            Compensation(
                kind="remove-worktree", target="/tmp/wt-loop-30", reason="cleanup"
            ),
            Compensation(kind="delete-branch", target="loop/30-feat", reason="branch"),
        ),
    )
    acquired = datetime.now(UTC) - timedelta(minutes=10)
    store_a.acquire_lease(
        "task-30-worker",
        owner_id="worker-30",
        expires_at=acquired + timedelta(minutes=1),  # already expired
        acquired_at=acquired,
    )
    # The worker opened its PR before dying → close-pr appended durably.
    store_a.append_compensation(
        "task-30-worker",
        Compensation(kind="close-pr", target="888", reason="close abandoned PR"),
    )
    store_a.close()

    # Boot #2 — recovery walk with fake gh callbacks wired (the online path).
    gh = MockGhClient()
    reaped: list[int] = []
    store_b = SqliteTaskSagaStore(db_file)
    report = reconcile_stale_sagas(
        store_b,
        reap_worktree=reaped.append,
        delete_branch=lambda b: gh.delete_branch("o", "r", b),
        close_pr=lambda n: gh.close_pull("o", "r", int(n)),
    )

    # Saga is fully compensated and drains from the in-flight view.
    assert store_b.get("task-30-worker").state == TaskState.COMPENSATED
    assert store_b.list_in_flight() == ()
    # Both gh callbacks fired with the right coordinates.
    assert ("delete_branch", {"owner": "o", "repo": "r", "branch": "loop/30-feat"}) in gh.calls
    assert ("close_pull", {"owner": "o", "repo": "r", "number": 888}) in gh.calls
    assert reaped == [30]
    # The report names the reversed side-effects, not just the worktree.
    rec = report.recovered[0]
    assert rec.branches_deleted == ("loop/30-feat",)
    assert rec.prs_closed == ("888",)


# ---------------------------------------------------------------------------
# Event schema — typed model validates required fields.
# ---------------------------------------------------------------------------


def test_worker_session_recovered_event_validates() -> None:
    ev = WorkerSessionRecoveredEvent(
        session_id="abc",
        issue=42,
        prior_state="running",
        action="abandon",
        new_state="abandoned",
        worktree_present=False,
        pr_present=False,
        reason="boot recovery: state lost",
    )
    rec = ev.to_record()
    assert rec["kind"] == "worker_session_recovered"
    assert rec["action"] == "abandon"
    assert rec["session_id"] == "abc"


def test_worker_session_recovered_event_is_registered() -> None:
    from forge_loop.events import EVENT_REGISTRY

    assert "worker_session_recovered" in EVENT_REGISTRY
    assert EVENT_REGISTRY["worker_session_recovered"] is WorkerSessionRecoveredEvent
