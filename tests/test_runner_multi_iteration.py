"""Integration tests for the worker iteration state machine (issue #78).

Drive ``run_iteration_loop`` with a fake ``dispatch_worker`` + a programmable
``run`` shim so we can play out multi-step scenarios:

  * DIRTY_NO_COMMIT → COMMITTED_NOT_PUSHED → PR_OPEN_HEALTHY → merge fires.
  * PR_OPEN_CONFLICT that never resolves → escalates after 3 attempts.
  * PR_OPEN_HEALTHY on attempt 2 → no LLM dispatched, ``gh pr merge --auto``.
  * 3-iteration cap is honoured even if state stays non-terminal.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from forge_loop.runner.iteration import (
    WorkerState,
    run_iteration_loop,
)
from forge_loop.runner.tick import _should_run_worker_iterations


@dataclass
class FakeOutcome:
    issue: int = 78
    title: str = "t"
    pr_url: str | None = None
    status: str = "no_pr"
    duration_s: float = 0.0
    stdout_tail: str = ""
    error: str | None = None
    branch: str = "loop/78-x"


def _cp(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


@pytest.fixture()
def worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    wt.mkdir()
    return wt


@dataclass
class Scripted:
    """Per-attempt scripting of (probe response, dispatch outcome).

    ``stages[attempt]`` is the canned ``gh pr list`` payload + ``git status``
    + ``rev-list`` output the probe will see at the START of that attempt's
    iteration. ``dispatch_results[attempt]`` is the outcome the fake worker
    returns AFTER the brief is rendered.
    """

    stages: list[dict[str, Any]] = field(default_factory=list)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    dispatched: list[str] = field(default_factory=list)
    automerge_calls: list[str] = field(default_factory=list)
    label_calls: list[tuple[int, str]] = field(default_factory=list)

    _attempt_idx: int = 0

    def make_run(self):
        def _run(args, _cwd):
            # gh pr list:
            if args[0] == "gh" and args[1] == "pr" and args[2] == "list":
                stage = self.stages[self._attempt_idx]
                return _cp(json.dumps(stage.get("pr_list", [])))
            if args[0] == "git" and args[1] == "status":
                stage = self.stages[self._attempt_idx]
                return _cp(stage.get("status", ""))
            if args[0] == "git" and args[1] == "rev-list":
                stage = self.stages[self._attempt_idx]
                return _cp(stage.get("rev_list", "0\n"))
            if (
                args[0] == "gh"
                and args[1] == "pr"
                and args[2] == "view"
                and "statusCheckRollup" in args
            ):
                stage = self.stages[self._attempt_idx]
                return _cp(json.dumps({"statusCheckRollup": stage.get("checks", [])}))
            if args[0] == "gh" and args[1] == "pr" and args[2] == "view":
                stage = self.stages[self._attempt_idx]
                return _cp(json.dumps({"comments": stage.get("comments", [])}))
            if args[0] == "gh" and args[1] == "pr" and args[2] == "merge":
                self.automerge_calls.append(args[3])
                return _cp("", 0)
            if args[0] == "gh" and args[1] == "issue" and args[2] == "edit":
                # The escalation hot-fix calls `--add-label X --remove-label Y`
                # in a single invocation. Capture the ADDED label (the one
                # the test cares about) by scanning for --add-label.
                added = ""
                for i, a in enumerate(args):
                    if a == "--add-label" and i + 1 < len(args):
                        added = args[i + 1]
                        break
                self.label_calls.append((int(args[3]), added or args[-1]))
                return _cp("", 0)
            if args[0] == "git" and args[1] == "rev-parse":
                return _cp("loop/78-x\n", 0)
            return _cp("", 0)

        return _run

    def emit(self, kind: str, payload: dict[str, Any]) -> None:
        self.events.append((kind, payload))

    def advance(self) -> None:
        self._attempt_idx += 1

    def dispatch_factory(self, results: list[FakeOutcome]):
        def _dispatch(_issue, brief: str) -> FakeOutcome:
            self.dispatched.append(brief)
            self.advance()  # next probe sees the next stage
            return results[len(self.dispatched) - 1]

        return _dispatch


# ---------------------------------------------------------------------------
# Scenario: dirty → committed → healthy → auto-merge.
# ---------------------------------------------------------------------------


def test_dirty_to_pushed_to_healthy_merges(worktree: Path) -> None:
    s = Scripted(
        stages=[
            # attempt 2 probe: dirty, no PR.
            {"pr_list": [], "status": " M f.py\n", "rev_list": "0\n"},
            # attempt 3 probe: clean, PR open, CI green.
            {
                "pr_list": [
                    {
                        "url": "https://x/78",
                        "number": 78,
                        "state": "OPEN",
                        "mergeable": "MERGEABLE",
                        "mergeStateStatus": "CLEAN",
                    }
                ],
                "status": "",
                "checks": [{"conclusion": "SUCCESS"}],
                "comments": [],
            },
        ]
    )
    # Dispatcher: first follow-up commits & pushes (returns "open" with no
    # PR yet — then on attempt 3 the probe sees PR_OPEN_HEALTHY and we skip
    # the LLM, calling enable_auto_merge instead).
    results = [FakeOutcome(status="open", pr_url=None)]
    issue = {"number": 78, "title": "t", "body": "x"}
    initial = FakeOutcome(status="no_pr", branch="loop/78-x")

    final = run_iteration_loop(
        initial,
        issue,
        repo="o/r",
        base_branch="trunk",
        worktree=worktree,
        max_iterations=3,
        dispatch_worker=s.dispatch_factory(results),
        emit=s.emit,
        run=s.make_run(),
    )

    # First dispatched brief was the commit brief (state was DIRTY_NO_COMMIT).
    assert len(s.dispatched) == 1
    assert "only job" in s.dispatched[0].lower()
    # No LLM on attempt 3 → auto-merge fired.
    assert s.automerge_calls == ["https://x/78"]
    # Final outcome reflects the open PR.
    assert final.pr_url == "https://x/78"
    # worker_iteration events emitted for attempts 2 and 3.
    kinds = [k for k, _ in s.events]
    assert kinds.count("worker_iteration") == 2
    states = [p["state"] for k, p in s.events if k == "worker_iteration"]
    assert states == [WorkerState.DIRTY_NO_COMMIT.value, WorkerState.PR_OPEN_HEALTHY.value]


# ---------------------------------------------------------------------------
# Scenario: 3-iteration cap.
# ---------------------------------------------------------------------------


def test_three_iterations_cap_then_escalates(worktree: Path) -> None:
    """State stays PR_OPEN_CONFLICT across all attempts → escalate."""
    conflict_pr = [
        {
            "url": "https://x/9",
            "number": 9,
            "state": "OPEN",
            "mergeable": "CONFLICTING",
            "mergeStateStatus": "DIRTY",
        }
    ]
    s = Scripted(
        stages=[
            {"pr_list": conflict_pr, "status": "", "rev_list": "0\n"},
            {"pr_list": conflict_pr, "status": "", "rev_list": "0\n"},
            {
                "pr_list": conflict_pr,
                "status": "",
                "rev_list": "0\n",
            },  # for the final probe in escalate
        ]
    )
    results = [
        FakeOutcome(status="open", pr_url="https://x/9"),
        FakeOutcome(status="open", pr_url="https://x/9"),
    ]
    issue = {"number": 9, "title": "t", "body": "x"}
    initial = FakeOutcome(issue=9, status="open", pr_url="https://x/9", branch="loop/9")

    run_iteration_loop(
        initial,
        issue,
        repo="o/r",
        base_branch="trunk",
        worktree=worktree,
        max_iterations=3,
        dispatch_worker=s.dispatch_factory(results),
        emit=s.emit,
        run=s.make_run(),
    )

    # Exactly 2 dispatches (attempts 2 and 3), then exhausted.
    assert len(s.dispatched) == 2, f"expected 2 dispatches, got {len(s.dispatched)}"
    kinds = [k for k, _ in s.events]
    assert "worker_iterations_exhausted" in kinds
    # loop:needs-human label was applied.
    assert s.label_calls and s.label_calls[0][1] == "loop:needs-human"


# ---------------------------------------------------------------------------
# Scenario: healthy PR on attempt 2 → no LLM dispatched.
# ---------------------------------------------------------------------------


def test_healthy_pr_short_circuits_no_llm(worktree: Path) -> None:
    healthy_pr = [
        {
            "url": "https://x/2",
            "number": 2,
            "state": "OPEN",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
    ]
    s = Scripted(
        stages=[
            {
                "pr_list": healthy_pr,
                "status": "",
                "checks": [{"conclusion": "SUCCESS"}],
                "comments": [],
            },
        ]
    )
    results: list[FakeOutcome] = []  # nothing should be dispatched
    issue = {"number": 2, "title": "t", "body": "x"}
    initial = FakeOutcome(issue=2, status="open", pr_url="https://x/2", branch="loop/2")

    run_iteration_loop(
        initial,
        issue,
        repo="o/r",
        base_branch="trunk",
        worktree=worktree,
        max_iterations=3,
        dispatch_worker=s.dispatch_factory(results),
        emit=s.emit,
        run=s.make_run(),
    )

    assert s.dispatched == []  # no LLM
    assert s.automerge_calls == ["https://x/2"]


# ---------------------------------------------------------------------------
# Scenario: terminal DONE_MERGED on first probe — short-circuit.
# ---------------------------------------------------------------------------


def test_terminal_state_short_circuits(worktree: Path) -> None:
    merged_pr = [
        {
            "url": "https://x/3",
            "number": 3,
            "state": "MERGED",
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
        }
    ]
    s = Scripted(
        stages=[
            {"pr_list": merged_pr, "status": ""},
        ]
    )
    issue = {"number": 3, "title": "t", "body": "x"}
    initial = FakeOutcome(issue=3, status="open", pr_url="https://x/3", branch="loop/3")

    run_iteration_loop(
        initial,
        issue,
        repo="o/r",
        base_branch="trunk",
        worktree=worktree,
        max_iterations=3,
        dispatch_worker=s.dispatch_factory([]),
        emit=s.emit,
        run=s.make_run(),
    )

    assert s.dispatched == []
    assert s.automerge_calls == []
    assert any(
        p.get("state") == WorkerState.DONE_MERGED.value
        for k, p in s.events
        if k == "worker_iteration"
    )


def test_stop_marker_suppresses_worker_iterations(tmp_path: Path) -> None:
    stop_file = tmp_path / "loop-runner.stop"
    pause_file = tmp_path / "loop-runner.pause"
    cfg = type(
        "Cfg",
        (),
        {
            "worker_max_iterations": 3,
            "stop_file": stop_file,
            "pause_file": pause_file,
        },
    )()
    outcomes = [FakeOutcome(status="no_pr")]

    assert _should_run_worker_iterations(cfg, outcomes) is True

    stop_file.write_text("stop")

    assert _should_run_worker_iterations(cfg, outcomes) is False


# ---------------------------------------------------------------------------
# Config wiring: LOOP_WORKER_MAX_ITERATIONS overrides default.
# ---------------------------------------------------------------------------


def test_config_max_iterations_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Adversarial: operator sets LOOP_WORKER_MAX_ITERATIONS=5; config picks it up."""
    monkeypatch.setenv("LOOP_REPO_DIR", str(tmp_path))
    monkeypatch.setenv("LOOP_GH_REPO", "o/r")
    monkeypatch.setenv("LOOP_WORKER_MAX_ITERATIONS", "5")
    # Ensure no stale yaml lurks.
    monkeypatch.delenv("LOOP_CONFIG_PATH", raising=False)
    (tmp_path / "forge-loop.yaml").write_text("repo:\n  github: o/r\n")

    from forge_loop import config as cfg_mod

    cfg = cfg_mod.load()
    assert cfg.worker_max_iterations == 5


def test_config_max_iterations_default_is_three(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOOP_REPO_DIR", str(tmp_path))
    monkeypatch.setenv("LOOP_GH_REPO", "o/r")
    monkeypatch.delenv("LOOP_WORKER_MAX_ITERATIONS", raising=False)
    monkeypatch.delenv("LOOP_CONFIG_PATH", raising=False)
    (tmp_path / "forge-loop.yaml").write_text("repo:\n  github: o/r\n")

    from forge_loop import config as cfg_mod

    cfg = cfg_mod.load()
    assert cfg.worker_max_iterations == 3
