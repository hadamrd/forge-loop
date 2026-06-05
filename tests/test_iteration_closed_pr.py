"""Tests for the ``CLOSED_PR_ABANDONED`` state + stale-tracking-ref fix.

The bug: when a branch has a CLOSED-but-not-merged PR from a prior
attempt, ``probe_worker_state`` used to fall through to the
"no PR" path and inspect ``origin/<branch>..HEAD``. With a stale
tracking ref the count showed commits "ahead" forever, putting the
iteration loop into a push-forever cycle. Caught dogfooding the loop
on a downstream project (#1104).

Fixes:
- CLOSED PR (state != MERGED, != OPEN) returns the new terminal
  ``WorkerState.CLOSED_PR_ABANDONED`` so the iteration loop bails
  immediately and escalates to operator (loop:needs-human).
- A ``git fetch --quiet origin <branch>`` runs before the ahead-count
  to refresh the tracking ref. Without it, a successful push from a
  prior session still reads as "local commits ahead".
"""

from __future__ import annotations

import subprocess
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from forge_loop import gh_issues
from forge_loop.gh_client import MockGhClient
from forge_loop.runner.iteration import (
    TERMINAL_STATES,
    WorkerState,
    brief_kind_for,
    is_terminal,
    probe_worker_state,
)


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _make_fake_run(plan, default=None):
    """git-only ``run`` shim (gh state is seeded on the client now)."""
    default = default if default is not None else _completed("", 0)

    def _run(args, _cwd):
        for prefix, out in plan.items():
            if tuple(args[: len(prefix)]) == prefix:
                return out
        return default

    return _run


def _seed_pr(
    head: str,
    pr: dict[str, Any] | None,
    *,
    status_failed: bool = False,
    critic_report: str = "",
) -> MockGhClient:
    """Seed the GhClient with a PR (or none) for ``head`` + CI / critic state."""
    client = MockGhClient(
        pr_by_head={head: pr} if pr else {},
        pr_status_failed_by_pr={int(pr["number"]): status_failed} if pr else {},
        critic_report_by_pr={int(pr["number"]): critic_report} if pr else {},
    )
    gh_issues.set_client(client)
    return client


@pytest.fixture(autouse=True)
def _reset_client() -> Generator[None, None, None]:
    gh_issues.set_client(None)
    yield
    gh_issues.set_client(None)


@pytest.fixture()
def worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").mkdir()
    return wt


# ---------------------------------------------------------------------------
# Core regression — CLOSED PR short-circuits to terminal state.
# ---------------------------------------------------------------------------


def test_closed_pr_returns_abandoned_terminal_state(worktree: Path) -> None:
    """The headline regression pin — a CLOSED-but-not-merged PR no
    longer triggers the COMMITTED_NOT_PUSHED push-forever loop."""
    _seed_pr(
        "loop/1-demo",
        {
            "url": "https://github.com/o/r/pull/42",
            "number": 42,
            "state": "CLOSED",
            "mergeable": "",
            "mergeStateStatus": "",
            "isDraft": False,
        },
    )
    state, ctx = probe_worker_state(worktree, "loop/1-demo", "o/r", 1, run=_make_fake_run({}))
    assert state == WorkerState.CLOSED_PR_ABANDONED
    assert ctx.pr_url == "https://github.com/o/r/pull/42"


def test_closed_pr_abandoned_is_terminal() -> None:
    """The iteration loop bails on terminal states; this assertion pins
    that CLOSED_PR_ABANDONED is included in the terminal set."""
    assert WorkerState.CLOSED_PR_ABANDONED in TERMINAL_STATES
    assert is_terminal(WorkerState.CLOSED_PR_ABANDONED)


def test_closed_pr_abandoned_brief_kind_is_none() -> None:
    """Terminal states have no follow-up brief — confirms the iteration
    loop won't try to dispatch yet another worker on a closed PR."""
    assert brief_kind_for(WorkerState.CLOSED_PR_ABANDONED) is None


def test_merged_pr_still_takes_precedence_over_closed_branch(worktree: Path) -> None:
    """Defensive: when the PR is MERGED (vs CLOSED-without-merge) we
    return DONE_MERGED, not CLOSED_PR_ABANDONED."""
    _seed_pr(
        "loop/1-demo",
        {
            "url": "https://github.com/o/r/pull/99",
            "number": 99,
            "state": "MERGED",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "isDraft": False,
        },
    )
    state, _ctx = probe_worker_state(worktree, "loop/1-demo", "o/r", 1, run=_make_fake_run({}))
    assert state == WorkerState.DONE_MERGED


def test_open_pr_still_classified_normally_not_closed(worktree: Path) -> None:
    """Defensive: OPEN PR continues to flow through the normal
    PR_OPEN_* dispatch."""
    _seed_pr(
        "loop/5",
        {
            "url": "https://github.com/o/r/pull/5",
            "number": 5,
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "isDraft": False,
        },
    )
    # No CI failure, no critic block — healthy path (client defaults).
    state, _ctx = probe_worker_state(worktree, "loop/5", "o/r", 5, run=_make_fake_run({}))
    # Anything in the PR_OPEN_* family is fine for this test — we're
    # just pinning that CLOSED handling didn't break the OPEN path.
    assert state != WorkerState.CLOSED_PR_ABANDONED
    assert state != WorkerState.DONE_MERGED


# ---------------------------------------------------------------------------
# Stale-tracking-ref fix — probe must fetch before ahead-counting.
# ---------------------------------------------------------------------------


def test_probe_fetches_origin_branch_before_ahead_count(worktree: Path) -> None:
    """The probe must refresh ``origin/<branch>`` before measuring
    ``origin/branch..HEAD`` — otherwise a successful push from a prior
    session still shows as 'local commits ahead'."""
    _seed_pr("loop/1-demo", None)  # no PR for this branch
    calls: list[list[str]] = []

    def fake_run(args, _cwd):
        calls.append(list(args))
        if args[:2] == ["git", "fetch"]:
            return _completed("")
        if args[:1] == ["git"] and "status" in args:
            return _completed("")  # clean
        if args[:1] == ["git"] and "rev-list" in args:
            return _completed("0")  # nothing ahead → PUSHED_NO_PR
        return _completed("")

    probe_worker_state(worktree, "loop/1-demo", "o/r", 1, run=fake_run)

    # The fetch must run AND it must precede the rev-list ahead-count.
    fetch_idx = next(
        (i for i, c in enumerate(calls) if c[:2] == ["git", "fetch"]),
        None,
    )
    revlist_idx = next(
        (i for i, c in enumerate(calls) if c[:2] == ["git", "rev-list"]),
        None,
    )
    assert fetch_idx is not None, (
        f"probe must call `git fetch` before computing ahead-count. calls={calls!r}"
    )
    if revlist_idx is not None:
        assert fetch_idx < revlist_idx, (
            "fetch must precede rev-list, otherwise the tracking ref is stale"
        )


def test_iteration_loop_escalates_to_human_on_closed_pr(
    worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end pin: when probe returns CLOSED_PR_ABANDONED at iteration
    start, the loop must NOT dispatch a follow-up worker AND must call
    escalate_to_human (label loop:needs-human + diagnostic comment) so
    the dispatcher doesn't keep picking up the issue every tick."""
    from forge_loop.runner.iteration import run_iteration_loop

    _seed_pr(
        "loop/1-demo",
        {
            "url": "https://github.com/o/r/pull/42",
            "number": 42,
            "state": "CLOSED",
            "mergeable": "",
            "mergeStateStatus": "",
            "isDraft": False,
        },
    )
    run = _make_fake_run({})

    dispatches: list = []

    def dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))
        # Sentinel — should never be called.
        raise AssertionError("dispatch_worker must not be called when CLOSED_PR_ABANDONED")

    escalations: list = []
    monkeypatch.setattr(
        "forge_loop.runner.iteration.escalate_to_human",
        lambda *a, **kw: escalations.append((a, kw)),
    )

    emits: list[tuple[str, dict]] = []

    class _Outcome:
        branch = "loop/1-demo"
        status = "open"
        pr_url = None

    run_iteration_loop(
        _Outcome(),
        {"number": 1, "title": "demo", "body": ""},
        repo="o/r",
        base_branch="trunk",
        worktree=worktree,
        max_iterations=3,
        dispatch_worker=dispatch,
        emit=lambda k, p: emits.append((k, p)),
        run=run,
    )

    assert dispatches == [], "no follow-up worker should be dispatched"
    assert escalations, "escalate_to_human must fire on CLOSED_PR_ABANDONED"
    kinds = [k for k, _ in emits]
    assert "worker_iteration" in kinds
    # The first emitted iteration event must carry the abandoned state.
    first = next(p for k, p in emits if k == "worker_iteration")
    assert first["state"] == WorkerState.CLOSED_PR_ABANDONED.value


def test_probe_fetch_failure_is_non_fatal(worktree: Path) -> None:
    """Adversarial: a fetch failure (network down, branch already
    deleted on origin) must NOT crash the probe — we degrade to the
    best-effort ahead-count just like before."""
    _seed_pr("loop/1-demo", None)  # no PR for this branch

    def fake_run(args, _cwd):
        if args[:2] == ["git", "fetch"]:
            # Simulate offline / remote-gone — fetch fails hard.
            raise subprocess.SubprocessError("network unreachable")
        if args[:1] == ["git"] and "status" in args:
            return _completed("")
        if args[:1] == ["git"] and "rev-list" in args:
            return _completed("0")
        if args[:1] == ["git"] and "log" in args:
            return _completed("")
        return _completed("")

    # Must not raise — degrades to whatever state the stale refs imply.
    state, _ctx = probe_worker_state(worktree, "loop/1-demo", "o/r", 1, run=fake_run)
    # Any non-crash outcome is fine; the contract is "fetch failure
    # never escapes the probe boundary".
    assert isinstance(state, WorkerState)
