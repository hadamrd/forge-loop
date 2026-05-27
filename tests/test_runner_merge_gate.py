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
