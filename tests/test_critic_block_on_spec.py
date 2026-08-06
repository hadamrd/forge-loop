"""block_on_spec: when the ISSUE is the defect, do NOT dispatch a repair worker.

☠ THE BUG THIS LOCKS DOWN. The critic's rubric makes "missing acceptance criterion" a sev1 that
always blocks, and triage forbids demoting sev1/sev2. The only escape valve demotes COSMETICS. So an
UNSATISFIABLE criterion blocked forever: the worker cannot edit the issue, so it answered with more
code, and the cycle repeated. Measured on a live repo: FIVE repair passes on one PR and ~2h with zero
merges, while the critic itself had already written the correct diagnosis ("escalate to a human to
split the issue") into prose it had nowhere to put.

The load-bearing assertion here is the NEGATIVE one: `dispatch_revision` is never called. Everything
else is bookkeeping.
"""

from __future__ import annotations

from typing import Any

from forge_loop.critic import CriticReport, SpecDefect
from forge_loop.runner.critic_flow import handle_critic_verdict
from forge_loop.worker_sessions import WorkerSessionStore, WorkerState


class _StubGh:
    def __call__(self, *a: Any, **k: Any) -> Any:
        return None

    def __getattr__(self, _name: str) -> Any:
        return lambda *a, **k: None


def _events() -> tuple[list[tuple[str, dict]], Any]:
    seen: list[tuple[str, dict]] = []

    def emit(kind: str, **kw: Any) -> None:
        seen.append((kind, kw))

    return seen, emit


def _seed_awaiting(store: WorkerSessionStore) -> str:
    sess = store.create(issue=160, branch="loop/160", worktree_path="/tmp/wt-loop-160")
    store.transition_to(sess.session_id, WorkerState.RUNNING)
    store.transition_to(sess.session_id, WorkerState.AWAITING_CRITIC, reason="pr opened")
    store.set_pr_url(sess.session_id, "https://github.com/o/r/pull/161")
    return sess.session_id


def _spec_report() -> CriticReport:
    return CriticReport(
        overall="block_on_spec",
        findings=[],
        spec_defects=[
            SpecDefect(
                kind="unsatisfiable_in_one_pr",
                criterion=">=8 of the 36 PLACE rows carry a wire-observed mapId",
                why="requires a code round AND a live data-collection grind; no single PR closes both",
                fix="split into a mechanism ticket and a grind ticket",
                rounds_burned=5,
            )
        ],
    )


def test_block_on_spec_does_not_dispatch_a_revision() -> None:
    """The whole point: no worker is sent at a criterion no diff can satisfy."""
    store = WorkerSessionStore(":memory:")
    sid = _seed_awaiting(store)
    events, emit = _events()
    dispatched: list[dict] = []

    result = handle_critic_verdict(
        store=store,
        session_id=sid,
        report=_spec_report(),
        pr_url="https://github.com/o/r/pull/161",
        gh=_StubGh(),
        emit=emit,
        dispatch_revision=lambda **kw: dispatched.append(kw),
    )

    assert result == "blocked_on_spec"
    assert dispatched == [], "a spec defect must NEVER dispatch a repair worker"

    sess = store.get(sid)
    assert sess is not None
    assert sess.state == WorkerState.ABANDONED
    # Iterations must not be burned on a round the worker could never win.
    assert sess.critic_iterations == 0


def test_block_on_spec_reports_which_criterion_is_broken() -> None:
    """A human is the addressee, so the event must name the criterion and the fix."""
    store = WorkerSessionStore(":memory:")
    sid = _seed_awaiting(store)
    events, emit = _events()

    handle_critic_verdict(
        store=store,
        session_id=sid,
        report=_spec_report(),
        pr_url="https://github.com/o/r/pull/161",
        gh=_StubGh(),
        emit=emit,
    )

    payloads = [p for k, p in events if k == "critic_verdict_blocked_on_spec"]
    assert payloads, "the spec block must be observable as its own typed event"
    defects = payloads[0]["defects"]
    assert defects[0]["kind"] == "unsatisfiable_in_one_pr"
    assert "wire-observed mapId" in defects[0]["criterion"]
    assert defects[0]["rounds_burned"] == 5


def test_request_changes_still_dispatches() -> None:
    """NEV-CTL-04: prove the negative assertion above can fail — the same harness
    with an ordinary verdict MUST dispatch, or the first test proves nothing."""
    from forge_loop.critic import Finding

    store = WorkerSessionStore(":memory:")
    sid = _seed_awaiting(store)
    _, emit = _events()
    dispatched: list[dict] = []

    result = handle_critic_verdict(
        store=store,
        session_id=sid,
        report=CriticReport(
            overall="request_changes",
            findings=[
                Finding(
                    severity="sev1",
                    category="correctness",
                    file="a.py",
                    line=1,
                    message="off-by-one",
                )
            ],
        ),
        pr_url="https://github.com/o/r/pull/161",
        gh=_StubGh(),
        emit=emit,
        dispatch_revision=lambda **kw: dispatched.append(kw),
    )

    assert result == "revising"
    assert len(dispatched) == 1, "the harness can observe a dispatch, so the negative test is real"
