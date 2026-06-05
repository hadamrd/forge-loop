"""Tests for the issue-closed merge gate (issue #65).

Covers the matrix called out in the issue body:
- gh issue OPEN  → merge proceeds (no-op gate)
- gh issue CLOSED → refuse: auto-merge disabled, PR comment posted,
  event emitted, outcome status flipped merged→open
- gh issue view fails (state=None) → CONSERVATIVE: refuse
- closed-then-reopened mid-flight → final state OPEN → merge proceeds
  (the LAST gh check wins; the gate checks ONCE at merge time)
- multi-outcome integration: only the closed-issue outcome is refused;
  the others proceed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from forge_loop.runner.merge_gate import (
    _refusal_comment,
    apply_issue_closed_gate,
    check_issue_closed_gate,
)
from forge_loop.worker import WorkerOutcome


@dataclass
class _FakeGh:
    """Spy implementing the GhMergeGateClient Protocol slice."""
    # Default: every issue is OPEN. Tests override per-issue with .states.
    states: dict[int, str | None] = field(default_factory=dict)
    default_state: str | None = "OPEN"
    state_calls: list[tuple[int, str | None]] = field(default_factory=list)
    disable_calls: list[tuple] = field(default_factory=list)
    comment_calls: list[dict] = field(default_factory=list)
    # Toggles for adversarial scenarios.
    disable_raises: bool = False
    comment_raises: bool = False

    def get_issue_state(self, issue, repo=None):  # type: ignore[no-untyped-def]
        self.state_calls.append((issue, repo))
        return self.states.get(issue, self.default_state)

    def disable_pr_auto_merge(self, pr, repo=None):  # type: ignore[no-untyped-def]
        if self.disable_raises:
            raise RuntimeError("simulated gh outage on disable")
        self.disable_calls.append((pr, repo))
        return True

    def pr_comment(self, pr, body, repo=None):  # type: ignore[no-untyped-def]
        if self.comment_raises:
            raise RuntimeError("simulated gh outage on comment")
        self.comment_calls.append({"pr": pr, "body": body, "repo": repo})
        return True


def _outcome(issue: int, *, pr: str | None = None,
             status: str = "merged") -> WorkerOutcome:
    return WorkerOutcome(
        issue=issue,
        title=f"#{issue}",
        pr_url=pr,
        status=status,
        duration_s=1.0,
        stdout_tail="",
    )


# ---------------------------------------------------------------------------
# Happy path: OPEN issue → gate is a no-op.
# ---------------------------------------------------------------------------

def test_open_issue_merge_proceeds(tmp_path: Path) -> None:
    gh = _FakeGh(states={47: "OPEN"})
    o = _outcome(47, pr="https://gh/u/r/pull/62", status="merged")
    events: list = []

    refused = check_issue_closed_gate(
        o, gh=gh, repo="o/r",
        events_file=tmp_path / "events.jsonl",
        emit=lambda k, p: events.append((k, p)),
    )

    assert refused is False
    assert gh.disable_calls == []
    assert gh.comment_calls == []
    assert events == []
    assert o.status == "merged"  # untouched
    assert gh.state_calls == [(47, "o/r")]


# ---------------------------------------------------------------------------
# Adversarial: CLOSED issue → gate refuses everything end-to-end.
# ---------------------------------------------------------------------------

def test_closed_issue_refuses_merge_and_flips_status(tmp_path: Path) -> None:
    gh = _FakeGh(states={47: "CLOSED"})
    o = _outcome(47, pr="https://gh/u/r/pull/62", status="merged")
    events_path = tmp_path / "events.jsonl"
    captured: list = []

    refused = check_issue_closed_gate(
        o, gh=gh, repo="o/r",
        events_file=events_path,
        emit=lambda k, p: captured.append((k, p)),
    )

    assert refused is True

    # Side effects on the PR.
    assert gh.disable_calls == [("https://gh/u/r/pull/62", "o/r")]
    assert len(gh.comment_calls) == 1
    body = gh.comment_calls[0]["body"]
    assert "#47" in body
    assert "closed" in body.lower()
    assert "refusing auto-merge" in body.lower()

    # Bus event.
    assert captured == [
        ("merge_refused_issue_closed",
         {"issue": 47, "pr": "https://gh/u/r/pull/62", "issue_state": "CLOSED"}),
    ]

    # File event — JSONL append.
    line = events_path.read_text().strip().splitlines()[-1]
    evt = json.loads(line)
    assert evt["kind"] == "merge_refused_issue_closed"
    assert evt["issue"] == 47
    assert evt["pr"] == "https://gh/u/r/pull/62"
    assert evt["issue_state"] == "CLOSED"

    # Attempts ledger reflects truth: not really merged.
    assert o.status == "open"


# ---------------------------------------------------------------------------
# Network failure: be conservative — refuse rather than risk a bad merge.
# ---------------------------------------------------------------------------

def test_gh_unreachable_is_conservative_refuse(tmp_path: Path) -> None:
    gh = _FakeGh(states={47: None})  # None ≡ gh failed to fetch state
    o = _outcome(47, pr="https://gh/u/r/pull/62", status="merged")
    events: list = []

    refused = check_issue_closed_gate(
        o, gh=gh, repo="o/r",
        events_file=tmp_path / "events.jsonl",
        emit=lambda k, p: events.append((k, p)),
    )

    assert refused is True
    assert gh.disable_calls  # still tried to disable
    assert gh.comment_calls  # still posted comment
    assert events[0][0] == "merge_refused_issue_closed"
    assert events[0][1]["issue_state"] is None
    assert "could not be fetched" in gh.comment_calls[0]["body"]
    assert o.status == "open"


# ---------------------------------------------------------------------------
# Adversarial: closed-then-reopened mid-flight → last gh check wins.
# We model this by configuring the fake to return OPEN at gate time; the
# fact that it was CLOSED earlier is irrelevant (the gate checks ONCE).
# ---------------------------------------------------------------------------

def test_reopened_before_gate_proceeds(tmp_path: Path) -> None:
    gh = _FakeGh(states={47: "OPEN"})  # final state after reopen
    o = _outcome(47, pr="https://gh/u/r/pull/62", status="merged")

    refused = check_issue_closed_gate(
        o, gh=gh, repo="o/r",
        events_file=tmp_path / "events.jsonl",
    )

    assert refused is False
    assert o.status == "merged"
    assert gh.disable_calls == []


# ---------------------------------------------------------------------------
# Sad-path edge: outcome without a PR (worker bailed early) → gate skips it,
# does NOT call gh.get_issue_state, does NOT mutate status.
# ---------------------------------------------------------------------------

def test_no_pr_skipped(tmp_path: Path) -> None:
    gh = _FakeGh(states={47: "CLOSED"})
    o = _outcome(47, pr=None, status="failed")

    refused = check_issue_closed_gate(
        o, gh=gh, repo="o/r",
        events_file=tmp_path / "events.jsonl",
    )

    assert refused is False
    assert gh.state_calls == []  # didn't even bother checking
    assert o.status == "failed"


# ---------------------------------------------------------------------------
# Best-effort: side-effect calls that raise must not kill the gate. We still
# emit the event and flip the status — the operator needs to see the refusal.
# ---------------------------------------------------------------------------

def test_side_effects_swallow_exceptions(tmp_path: Path) -> None:
    gh = _FakeGh(states={47: "CLOSED"}, disable_raises=True, comment_raises=True)
    o = _outcome(47, pr="https://gh/u/r/pull/62", status="merged")
    events: list = []

    refused = check_issue_closed_gate(
        o, gh=gh, repo="o/r",
        events_file=tmp_path / "events.jsonl",
        emit=lambda k, p: events.append((k, p)),
    )

    assert refused is True
    assert events and events[0][0] == "merge_refused_issue_closed"
    assert o.status == "open"


# ---------------------------------------------------------------------------
# Multi-outcome / end-to-end: a tick with three workers, one whose issue
# was closed mid-flight. Only that one is refused; others land.
# ---------------------------------------------------------------------------

def test_apply_issue_closed_gate_only_refuses_closed(tmp_path: Path) -> None:
    gh = _FakeGh(states={
        47: "CLOSED",   # the dogfood scenario
        48: "OPEN",
        49: "OPEN",
    })
    outcomes = [
        _outcome(47, pr="https://gh/u/r/pull/62", status="merged"),
        _outcome(48, pr="https://gh/u/r/pull/63", status="merged"),
        _outcome(49, pr="https://gh/u/r/pull/64", status="open"),
    ]
    emitted: list = []

    refused = apply_issue_closed_gate(
        outcomes,
        gh=gh,
        repo="o/r",
        events_file=tmp_path / "events.jsonl",
        emit=lambda k, p: emitted.append((k, p)),
    )

    assert refused == [47]
    # #47 flipped, #48 / #49 untouched.
    assert [o.status for o in outcomes] == ["open", "merged", "open"]
    # Exactly one auto-merge disable + one comment fired.
    assert len(gh.disable_calls) == 1
    assert gh.disable_calls[0][0] == "https://gh/u/r/pull/62"
    assert len(gh.comment_calls) == 1
    assert "#47" in gh.comment_calls[0]["body"]
    assert [k for k, _ in emitted] == ["merge_refused_issue_closed"]


# ---------------------------------------------------------------------------
# The comment content is operator-facing; assert it's actionable.
# ---------------------------------------------------------------------------

def test_refusal_comment_is_actionable() -> None:
    body = _refusal_comment(47, "CLOSED")
    assert "#47" in body
    assert "closed" in body.lower()
    # Operator needs to know what to DO.
    assert "reopen" in body.lower() or "manually" in body.lower()

    # Unknown state is named explicitly so the operator knows gh failed.
    body_unknown = _refusal_comment(47, None)
    assert "could not be fetched" in body_unknown


# ===========================================================================
# Verify-clean ratchet (issue #241).
#
# Mirrors the matrix in the issue body:
#  - clean repo          → not refused, merge proceeds
#  - non-clean ruff      → refused + outcome flipped + typed event emitted
#  - non-clean pyright   → same
#  - tool missing        → refused (fail-loud, never silent-pass)
#  - runner crash/timeout→ fail-closed (treated non-clean), tick survives
#  - flag OFF            → no-op even when red (dependency-ordering escape hatch)
#  - repo-wide violation → still refused (proves repo-wide, not diff-scoped)
# ===========================================================================

from forge_loop.runner.merge_gate import (  # noqa: E402
    SubprocessVerifyRunner,
    VerifyResult,
    apply_verify_clean_gate,
    run_verify_suite,
)


@dataclass
class _FakeVerifyRunner:
    """Spy implementing the VerifyRunner Protocol slice.

    ``results`` maps a verify command → the VerifyResult to return. A command
    not in the map defaults to clean (returncode 0). ``raises_for`` names a
    command for which the runner blows up (subprocess crash / timeout sim).
    """
    results: dict[str, VerifyResult] = field(default_factory=dict)
    raises_for: str | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)

    def run_verify(self, command, *, cwd, env):  # type: ignore[no-untyped-def]
        self.calls.append((command, cwd))
        if self.raises_for is not None and command == self.raises_for:
            raise RuntimeError("simulated verify subprocess crash")
        return self.results.get(
            command, VerifyResult(command=command, returncode=0, output_tail="")
        )


# A PATH dir that actually contains the tool, so missing_tools() is satisfied
# for the happy/failing-command cases (tool present, but command non-clean).
def _env_with_tools(tmp_path: Path, *tools: str) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for t in tools:
        exe = bindir / t
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
    return {"PATH": str(bindir)}


_RUFF = "ruff check src/ tests/"
_PYRIGHT = "pyright src/forge_loop"


def test_verify_clean_repo_merge_proceeds(tmp_path: Path) -> None:
    """All verify commands clean → gate is a no-op, nothing flipped."""
    runner = _FakeVerifyRunner()  # everything clean
    o = _outcome(241, pr="https://gh/u/r/pull/9", status="merged")
    events: list = []

    refused = apply_verify_clean_gate(
        [o],
        runner=runner,
        gh=_FakeGh(),
        repo="o/r",
        commands=[_RUFF, _PYRIGHT],
        cwd=str(tmp_path),
        env=_env_with_tools(tmp_path, "ruff", "pyright"),
        require=["ruff", "pyright"],
        enabled=True,
        events_file=tmp_path / "events.jsonl",
        emit=lambda k, p: events.append((k, p)),
    )

    assert refused == []
    assert o.status == "merged"  # untouched
    assert events == []
    # Both commands were actually run (repo-wide check ran end-to-end).
    assert [c for c, _ in runner.calls] == [_RUFF, _PYRIGHT]


def test_verify_ruff_unclean_refuses_and_flips(tmp_path: Path) -> None:
    """Non-clean ruff → refuse: auto-merge disabled, comment, typed event,
    outcome flipped merged→open."""
    runner = _FakeVerifyRunner(results={
        _RUFF: VerifyResult(_RUFF, 1, "src/foo.py:1:1: F401 imported but unused"),
    })
    gh = _FakeGh()
    o = _outcome(241, pr="https://gh/u/r/pull/9", status="merged")
    events_path = tmp_path / "events.jsonl"
    captured: list = []

    refused = apply_verify_clean_gate(
        [o],
        runner=runner,
        gh=gh,
        repo="o/r",
        commands=[_RUFF, _PYRIGHT],
        cwd=str(tmp_path),
        env=_env_with_tools(tmp_path, "ruff", "pyright"),
        require=["ruff", "pyright"],
        enabled=True,
        events_file=events_path,
        emit=lambda k, p: captured.append((k, p)),
    )

    assert refused == [241]
    # PR side effects.
    assert gh.disable_calls == [("https://gh/u/r/pull/9", "o/r")]
    assert len(gh.comment_calls) == 1
    body = gh.comment_calls[0]["body"]
    assert "ruff" in body
    assert "verify gate" in body.lower()
    # Typed bus event names which command failed + the tail.
    assert captured[0][0] == "merge_refused_verify_unclean"
    payload = captured[0][1]
    assert payload["command"] == _RUFF
    assert payload["returncode"] == 1
    assert "F401" in payload["output_tail"]
    # File event lands too.
    evt = json.loads(events_path.read_text().strip().splitlines()[-1])
    assert evt["kind"] == "merge_refused_verify_unclean"
    assert evt["command"] == _RUFF
    # Ledger reflects truth.
    assert o.status == "open"
    # Pyright never ran — we short-circuit on the FIRST failure.
    assert [c for c, _ in runner.calls] == [_RUFF]


def test_verify_gh_side_effects_suppressed_but_logged(
    tmp_path: Path, caplog
) -> None:
    """Review nit (#241): a raising gh.disable/comment must be SUPPRESSED so the
    durable record (typed event + merged→open flip) still fires — but the
    suppression must be logged at DEBUG (with traceback) so a silently-failing
    comment/disable is diagnosable, not swallowed in silence."""
    import logging

    runner = _FakeVerifyRunner(results={
        _RUFF: VerifyResult(_RUFF, 1, "src/foo.py:1:1: F401 imported but unused"),
    })
    gh = _FakeGh(disable_raises=True, comment_raises=True)
    o = _outcome(241, pr="https://gh/u/r/pull/9", status="merged")
    events_path = tmp_path / "events.jsonl"
    captured: list = []

    with caplog.at_level(logging.DEBUG, logger="forge_loop.runner.merge_gate"):
        refused = apply_verify_clean_gate(
            [o],
            runner=runner,
            gh=gh,
            repo="o/r",
            commands=[_RUFF],
            cwd=str(tmp_path),
            env=_env_with_tools(tmp_path, "ruff", "pyright"),
            require=["ruff", "pyright"],
            enabled=True,
            events_file=events_path,
            emit=lambda k, p: captured.append((k, p)),
        )

    # Refusal still happened despite BOTH gh side effects raising.
    assert refused == [241]
    assert captured and captured[0][0] == "merge_refused_verify_unclean"
    evt = json.loads(events_path.read_text().strip().splitlines()[-1])
    assert evt["kind"] == "merge_refused_verify_unclean"
    assert o.status == "open"
    # The swallowed gh failures are logged at DEBUG, named per action.
    debug_msgs = [
        r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG
    ]
    assert any("disable_pr_auto_merge" in m for m in debug_msgs)
    assert any("pr_comment" in m for m in debug_msgs)


def test_verify_pyright_unclean_refuses(tmp_path: Path) -> None:
    """Ruff clean but pyright dirty → still refused (second command checked)."""
    runner = _FakeVerifyRunner(results={
        _PYRIGHT: VerifyResult(_PYRIGHT, 1, "src/forge_loop/x.py:3:9 - error: bad"),
    })
    gh = _FakeGh()
    o = _outcome(241, pr="https://gh/u/r/pull/9", status="merged")
    captured: list = []

    refused = apply_verify_clean_gate(
        [o],
        runner=runner,
        gh=gh,
        repo="o/r",
        commands=[_RUFF, _PYRIGHT],
        cwd=str(tmp_path),
        env=_env_with_tools(tmp_path, "ruff", "pyright"),
        require=["ruff", "pyright"],
        enabled=True,
        events_file=tmp_path / "events.jsonl",
        emit=lambda k, p: captured.append((k, p)),
    )

    assert refused == [241]
    assert captured[0][1]["command"] == _PYRIGHT
    assert o.status == "open"
    # Both ran: ruff clean, pyright dirty.
    assert [c for c, _ in runner.calls] == [_RUFF, _PYRIGHT]


def test_verify_tool_missing_fails_loud(tmp_path: Path) -> None:
    """A required tool absent from the DECLARED env PATH → refuse (manifesto
    Q11 fail-loud). Must NOT silently pass even though no command 'failed'."""
    runner = _FakeVerifyRunner()  # would be clean IF it ran
    gh = _FakeGh()
    o = _outcome(241, pr="https://gh/u/r/pull/9", status="merged")
    captured: list = []

    refused = apply_verify_clean_gate(
        [o],
        runner=runner,
        gh=gh,
        repo="o/r",
        commands=[_RUFF, _PYRIGHT],
        cwd=str(tmp_path),
        # PATH has ruff but NOT pyright → pyright is missing.
        env=_env_with_tools(tmp_path, "ruff"),
        require=["ruff", "pyright"],
        enabled=True,
        events_file=tmp_path / "events.jsonl",
        emit=lambda k, p: captured.append((k, p)),
    )

    assert refused == [241]
    assert o.status == "open"
    # We refused on the PREFLIGHT — no verify command was ever run.
    assert runner.calls == []
    payload = captured[0][1]
    assert "pyright" in payload["command"]
    assert "unavailable" in payload["output_tail"].lower()


def test_verify_runner_crash_is_fail_closed(tmp_path: Path) -> None:
    """A verify subprocess that raises (crash / timeout) is treated as
    NON-clean (fail-closed), not a crashed tick."""
    runner = _FakeVerifyRunner(raises_for=_RUFF)
    gh = _FakeGh()
    o = _outcome(241, pr="https://gh/u/r/pull/9", status="merged")
    captured: list = []

    refused = apply_verify_clean_gate(
        [o],
        runner=runner,
        gh=gh,
        repo="o/r",
        commands=[_RUFF],
        cwd=str(tmp_path),
        env=_env_with_tools(tmp_path, "ruff"),
        require=["ruff"],
        enabled=True,
        events_file=tmp_path / "events.jsonl",
        emit=lambda k, p: captured.append((k, p)),
    )

    assert refused == [241]
    assert o.status == "open"
    assert captured[0][1]["returncode"] == -1
    assert "error" in captured[0][1]["output_tail"].lower()


def test_verify_gate_disabled_is_noop_even_when_red(tmp_path: Path) -> None:
    """Flag OFF → gate is a no-op and the merge proceeds even with a dirty
    repo. Proves the dependency-ordering escape hatch."""
    runner = _FakeVerifyRunner(results={
        _RUFF: VerifyResult(_RUFF, 1, "lots of violations"),
    })
    gh = _FakeGh()
    o = _outcome(241, pr="https://gh/u/r/pull/9", status="merged")
    events: list = []

    refused = apply_verify_clean_gate(
        [o],
        runner=runner,
        gh=gh,
        repo="o/r",
        commands=[_RUFF],
        cwd=str(tmp_path),
        env=_env_with_tools(tmp_path, "ruff"),
        require=["ruff"],
        enabled=False,  # <-- the escape hatch
        events_file=tmp_path / "events.jsonl",
        emit=lambda k, p: events.append((k, p)),
    )

    assert refused == []
    assert o.status == "merged"  # NOT flipped
    assert runner.calls == []  # never even ran verify
    assert events == []


def test_verify_repo_wide_violation_still_refuses(tmp_path: Path) -> None:
    """A pre-existing repo-wide violation (the diff itself is clean) must
    STILL refuse — proves the gate is repo-wide, not diff-scoped. We model
    'repo-wide dirty' by the runner reporting the whole-tree command dirty."""
    runner = _FakeVerifyRunner(results={
        _RUFF: VerifyResult(_RUFF, 1, "src/forge_loop/legacy.py:99: E501 line too long"),
    })
    gh = _FakeGh()
    # Outcome's own diff is fine; status merged because the worker + critic
    # were happy. The repo-wide command is what trips the gate.
    o = _outcome(241, pr="https://gh/u/r/pull/9", status="merged")

    refused = apply_verify_clean_gate(
        [o],
        runner=runner,
        gh=gh,
        repo="o/r",
        commands=[_RUFF],
        cwd=str(tmp_path),
        env=_env_with_tools(tmp_path, "ruff"),
        require=["ruff"],
        enabled=True,
        events_file=tmp_path / "events.jsonl",
    )

    assert refused == [241]
    assert o.status == "open"


def test_verify_gate_no_pr_outcomes_skipped(tmp_path: Path) -> None:
    """An outcome without a PR (worker bailed) is not eligible; with no
    eligible outcomes the suite never runs."""
    runner = _FakeVerifyRunner(results={
        _RUFF: VerifyResult(_RUFF, 1, "dirty"),
    })
    o = _outcome(241, pr=None, status="failed")

    refused = apply_verify_clean_gate(
        [o],
        runner=runner,
        gh=_FakeGh(),
        repo="o/r",
        commands=[_RUFF],
        cwd=str(tmp_path),
        env=_env_with_tools(tmp_path, "ruff"),
        require=["ruff"],
        enabled=True,
        events_file=tmp_path / "events.jsonl",
    )

    assert refused == []
    assert o.status == "failed"
    assert runner.calls == []


def test_verify_multi_outcome_all_refused(tmp_path: Path) -> None:
    """One repo-wide failure refuses EVERY merge-eligible outcome (the dirty
    tree blocks the whole train), not just one."""
    runner = _FakeVerifyRunner(results={
        _RUFF: VerifyResult(_RUFF, 1, "dirty"),
    })
    gh = _FakeGh()
    outcomes = [
        _outcome(241, pr="https://gh/u/r/pull/9", status="merged"),
        _outcome(242, pr="https://gh/u/r/pull/10", status="open"),
        _outcome(243, pr=None, status="failed"),  # not eligible
    ]

    refused = apply_verify_clean_gate(
        outcomes,
        runner=runner,
        gh=gh,
        repo="o/r",
        commands=[_RUFF],
        cwd=str(tmp_path),
        env=_env_with_tools(tmp_path, "ruff"),
        require=["ruff"],
        enabled=True,
        events_file=tmp_path / "events.jsonl",
    )

    assert sorted(refused) == [241, 242]
    assert [o.status for o in outcomes] == ["open", "open", "failed"]
    # Suite ran ONCE (repo-wide), not once-per-outcome.
    assert [c for c, _ in runner.calls] == [_RUFF]


# ---------------------------------------------------------------------------
# run_verify_suite direct unit coverage (first-failure short-circuit + clean).
# ---------------------------------------------------------------------------

def test_run_verify_suite_returns_none_when_clean(tmp_path: Path) -> None:
    runner = _FakeVerifyRunner()
    res = run_verify_suite(
        [_RUFF, _PYRIGHT],
        runner=runner,
        cwd=str(tmp_path),
        env=_env_with_tools(tmp_path, "ruff", "pyright"),
        require=["ruff", "pyright"],
    )
    assert res is None


def test_run_verify_suite_skips_blank_commands(tmp_path: Path) -> None:
    runner = _FakeVerifyRunner()
    res = run_verify_suite(
        ["", "   ", _RUFF],
        runner=runner,
        cwd=str(tmp_path),
        env=_env_with_tools(tmp_path, "ruff"),
        require=["ruff"],
    )
    assert res is None
    assert [c for c, _ in runner.calls] == [_RUFF]  # blanks skipped


# ---------------------------------------------------------------------------
# VerifyResult.ok — both branches (T3: returncode ==0 and !=0).
# ---------------------------------------------------------------------------

def test_verify_result_ok_property() -> None:
    assert VerifyResult("x", 0, "").ok is True
    assert VerifyResult("x", 1, "boom").ok is False
    assert VerifyResult("x", 127, "missing").ok is False


# ---------------------------------------------------------------------------
# Contract test (manifesto T4 / T3): the REAL SubprocessVerifyRunner returns
# the same VerifyResult shape the fake does, on representative inputs — clean
# command (exit 0) AND failing command (exit !=0). Drives the current Python
# interpreter as a portable, no-shell argv so it needs no project toolchain on
# PATH (and proves the runner execs argv directly, not via /bin/sh).
# ---------------------------------------------------------------------------

def test_subprocess_verify_runner_real_clean_and_dirty(tmp_path: Path) -> None:
    import os
    import shlex as _shlex
    import sys

    runner = SubprocessVerifyRunner(timeout_s=30.0)
    env = dict(os.environ)
    py = _shlex.quote(sys.executable)

    clean = runner.run_verify(f"{py} -c pass", cwd=str(tmp_path), env=env)
    assert isinstance(clean, VerifyResult)
    assert clean.ok is True
    assert clean.returncode == 0

    script = 'import sys; sys.stderr.write("boom-on-stderr"); sys.exit(3)'
    dirty = runner.run_verify(
        f"{py} -c {_shlex.quote(script)}",
        cwd=str(tmp_path),
        env=env,
    )
    assert dirty.ok is False
    assert dirty.returncode == 3
    assert "boom-on-stderr" in dirty.output_tail


# Regression (sev3/security thread): the runner must NOT interpret shell
# metacharacters — a command string is tokenised with shlex and exec'd as argv,
# never piped through /bin/sh. A trailing ``; rm -rf …`` is therefore passed as
# literal args to the program, not executed as a second shell command.
def test_subprocess_verify_runner_does_not_invoke_shell(tmp_path: Path) -> None:
    import os
    import shlex as _shlex
    import sys

    canary = tmp_path / "canary"
    canary.write_text("intact")
    runner = SubprocessVerifyRunner(timeout_s=30.0)
    env = dict(os.environ)
    py = _shlex.quote(sys.executable)

    # If this ran through a shell, the ``;`` would start a second command and
    # delete the canary. With shell=False the whole string after -c is one arg.
    res = runner.run_verify(
        f'{py} -c pass ; rm {_shlex.quote(str(canary))}',
        cwd=str(tmp_path),
        env=env,
    )
    # ``rm`` is just an argv token to python -c (ignored), so python exits 0
    # and the canary survives — proving no shell ran.
    assert res.returncode == 0
    assert canary.exists()
    assert canary.read_text() == "intact"


# An unparseable command (unbalanced quote) is a config error → fail-closed,
# never a crash and never a silent pass.
def test_subprocess_verify_runner_unparseable_command_fails_closed(
    tmp_path: Path,
) -> None:
    import os

    runner = SubprocessVerifyRunner(timeout_s=30.0)
    res = runner.run_verify(
        'ruff check "unterminated', cwd=str(tmp_path), env=dict(os.environ)
    )
    assert res.ok is False
    assert res.returncode == -1
    assert "not parseable" in res.output_tail


# ===========================================================================
# Integration: drive the tick verify-gate phase (tick._run_verify_gate) with a
# fake gh client and a worktree containing a seeded lint error. Uses the REAL
# SubprocessVerifyRunner + REAL ruff (if present). Asserts the refusal event
# lands and auto-merge is refused (outcome flipped). Mirrors the issue's
# Integration row.
# ===========================================================================

import shutil  # noqa: E402

import pytest  # noqa: E402


def _make_cfg_for_verify(tmp_path: Path, *, enabled: bool, commands):  # type: ignore[no-untyped-def]
    from forge_loop.config import (
        AttemptsConfig,
        Briefs,
        Config,
        CriticConfig,
        Labels,
        LumenConfig,
        POConfig,
        WorkerConfig,
    )

    return Config(
        repo=tmp_path,
        github_repo="o/r",
        parallel=1,
        tick_interval_s=0,
        max_ticks=1,
        worker_timeout_s=60,
        deploy_task="",
        labels=Labels(),
        briefs=Briefs(),
        critic=CriticConfig(enabled=False, timeout_s=10),
        po=POConfig(enabled=False, timeout_s=10, max_to_expand_per_tick=0),
        attempts=AttemptsConfig(enabled=False, max_history_in_brief=5),
        lumen=LumenConfig(),
        worker=WorkerConfig(
            verify_commands=tuple(commands),
            env_require=("ruff",),
            verify_gate_enabled=enabled,
        ),
    )


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not on PATH")
def test_tick_verify_gate_refuses_on_seeded_lint_error(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from forge_loop import gh_issues as _gh
    from forge_loop.runner import tick as _tick

    # Seed a real lint error: an unused import (ruff F401).
    (tmp_path / "bad.py").write_text("import os\n\nx = 1\n")

    disabled: list = []
    comments: list = []
    monkeypatch.setattr(_gh, "disable_pr_auto_merge",
                        lambda pr, repo=None: disabled.append((pr, repo)) or True)
    monkeypatch.setattr(_gh, "pr_comment",
                        lambda pr, body, repo=None: comments.append((pr, body)) or True)

    cfg = _make_cfg_for_verify(
        tmp_path, enabled=True, commands=["ruff check bad.py"]
    )
    o = _outcome(241, pr="https://gh/u/r/pull/9", status="merged")
    emitted: list = []

    refused = _tick._run_verify_gate(cfg, [o], bus_emit=lambda k, p: emitted.append((k, p)))

    assert refused == [241]
    assert o.status == "open"  # auto-merge NOT enabled — flipped back
    assert disabled == [("https://gh/u/r/pull/9", "o/r")]
    assert comments and "ruff" in comments[0][1]
    # Refusal event landed in the event log (file) and on the bus.
    assert emitted and emitted[0][0] == "merge_refused_verify_unclean"
    evt = json.loads(cfg.events_file.read_text().strip().splitlines()[-1])
    assert evt["kind"] == "merge_refused_verify_unclean"
    assert "ruff" in evt["command"]


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not on PATH")
def test_tick_verify_gate_passes_on_clean_repo(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from forge_loop import gh_issues as _gh
    from forge_loop.runner import tick as _tick

    # A clean file — no lint errors.
    (tmp_path / "ok.py").write_text("x = 1\n")
    monkeypatch.setattr(_gh, "disable_pr_auto_merge", lambda pr, repo=None: True)
    monkeypatch.setattr(_gh, "pr_comment", lambda pr, body, repo=None: True)

    cfg = _make_cfg_for_verify(
        tmp_path, enabled=True, commands=["ruff check ok.py"]
    )
    o = _outcome(241, pr="https://gh/u/r/pull/9", status="merged")
    emitted: list = []

    refused = _tick._run_verify_gate(cfg, [o], bus_emit=lambda k, p: emitted.append((k, p)))

    assert refused == []
    assert o.status == "merged"  # proceeds
    assert emitted == []
