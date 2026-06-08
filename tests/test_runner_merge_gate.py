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

from forge_loop.config import CriticConfig, MutationGateConfig
from forge_loop.runner.merge_gate import (
    MutationCheckResult,
    WorkerLeaseRef,
    _mutation_refusal_comment,
    _refusal_comment,
    apply_issue_closed_gate,
    apply_mutation_survivor_gate,
    apply_write_root_escape_gate,
    check_issue_closed_gate,
    collect_changed_paths,
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


def _outcome(issue: int, *, pr: str | None = None, status: str = "merged") -> WorkerOutcome:
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
        o,
        gh=gh,
        repo="o/r",
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
        o,
        gh=gh,
        repo="o/r",
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
        (
            "merge_refused_issue_closed",
            {"issue": 47, "pr": "https://gh/u/r/pull/62", "issue_state": "CLOSED"},
        ),
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
        o,
        gh=gh,
        repo="o/r",
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
        o,
        gh=gh,
        repo="o/r",
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
        o,
        gh=gh,
        repo="o/r",
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
        o,
        gh=gh,
        repo="o/r",
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
    gh = _FakeGh(
        states={
            47: "CLOSED",  # the dogfood scenario
            48: "OPEN",
            49: "OPEN",
        }
    )
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
# Oracle-strength gate (issue #381): refuse merge when the scoped mutation
# check reports surviving mutants above the configured threshold.
# ===========================================================================

_MODULE = "forge_loop/eventlog/chain.py"


def _mut_cfg(*, enabled: bool = True, threshold: int = 0) -> MutationGateConfig:
    return MutationGateConfig(enabled=enabled, module=_MODULE, survivor_threshold=threshold)


def _result(*survivors: str) -> MutationCheckResult:
    return MutationCheckResult(
        module=_MODULE, survivor_count=len(survivors), survivors=list(survivors)
    )


def test_gate_refuses_when_survivor_exceeds_threshold(tmp_path: Path) -> None:
    gh = _FakeGh()
    o = _outcome(47, pr="https://gh/u/r/pull/62", status="merged")
    events_path = tmp_path / "events.jsonl"
    emitted: list = []

    refused = apply_mutation_survivor_gate(
        [o],
        result=_result("eventlog/chain.py:142 replaced `==` with `!=`"),
        config=_mut_cfg(threshold=0),
        gh=gh,
        repo="o/r",
        events_file=events_path,
        emit=lambda k, p: emitted.append((k, p)),
    )

    assert refused == [47]
    # Auto-merge disabled + survivor-naming comment posted.
    assert gh.disable_calls == [("https://gh/u/r/pull/62", "o/r")]
    assert len(gh.comment_calls) == 1
    assert "eventlog/chain.py:142" in gh.comment_calls[0]["body"]
    # Status flipped so the attempts ledger + memory promotion reflect truth.
    assert o.status == "open"
    # Bus + file event name the surviving mutant.
    assert [k for k, _ in emitted] == ["merge_refused_mutation_survivors"]
    payload = emitted[0][1]
    assert payload["module"] == _MODULE
    assert payload["survivor_count"] == 1
    assert payload["survivors"] == ["eventlog/chain.py:142 replaced `==` with `!=`"]
    assert payload["reason"] == "survivors_exceed_threshold"
    evt = json.loads(events_path.read_text().strip().splitlines()[-1])
    assert evt["kind"] == "merge_refused_mutation_survivors"
    assert evt["survivor_count"] == 1


def test_gate_passes_with_zero_survivors(tmp_path: Path) -> None:
    gh = _FakeGh()
    o = _outcome(48, pr="https://gh/u/r/pull/63", status="merged")
    emitted: list = []

    refused = apply_mutation_survivor_gate(
        [o],
        result=_result(),  # zero survivors
        config=_mut_cfg(threshold=0),
        gh=gh,
        repo="o/r",
        events_file=tmp_path / "events.jsonl",
        emit=lambda k, p: emitted.append((k, p)),
    )

    assert refused == []
    assert gh.disable_calls == []  # no auto-merge disable
    assert gh.comment_calls == []
    assert emitted == []  # no refusal event
    assert o.status == "merged"  # untouched — proceeds as before


def test_threshold_allows_count_at_or_below(tmp_path: Path) -> None:
    # Boundary: threshold=2. Exactly 2 survivors → pass; 3 → refuse.
    at_cap = apply_mutation_survivor_gate(
        [_outcome(50, pr="https://gh/u/r/pull/70", status="merged")],
        result=_result("a", "b"),
        config=_mut_cfg(threshold=2),
        gh=_FakeGh(),
        repo="o/r",
        events_file=tmp_path / "e1.jsonl",
    )
    assert at_cap == []

    over_cap = apply_mutation_survivor_gate(
        [_outcome(51, pr="https://gh/u/r/pull/71", status="merged")],
        result=_result("a", "b", "c"),
        config=_mut_cfg(threshold=2),
        gh=_FakeGh(),
        repo="o/r",
        events_file=tmp_path / "e2.jsonl",
    )
    assert over_cap == [51]


def test_gate_disabled_never_refuses(tmp_path: Path) -> None:
    gh = _FakeGh()
    o = _outcome(52, pr="https://gh/u/r/pull/72", status="merged")
    emitted: list = []

    refused = apply_mutation_survivor_gate(
        [o],
        result=_result("a", "b", "c"),  # would refuse if enabled
        config=_mut_cfg(enabled=False, threshold=0),
        gh=gh,
        repo="o/r",
        events_file=tmp_path / "events.jsonl",
        emit=lambda k, p: emitted.append((k, p)),
    )

    assert refused == []
    assert gh.disable_calls == []
    assert emitted == []
    assert o.status == "merged"  # behaviour preserved when off


def test_refusal_message_names_each_survivor(tmp_path: Path) -> None:
    survivors = [
        "eventlog/chain.py:142 replaced `==` with `!=`",
        "eventlog/chain.py:88 replaced `+` with `-`",
        "eventlog/chain.py:90 removed `return`",
    ]
    gh = _FakeGh()
    refused = apply_mutation_survivor_gate(
        [_outcome(53, pr="https://gh/u/r/pull/73", status="merged")],
        result=_result(*survivors),
        config=_mut_cfg(threshold=0),
        gh=gh,
        repo="o/r",
        events_file=tmp_path / "events.jsonl",
    )
    assert refused == [53]
    body = gh.comment_calls[0]["body"]
    for s in survivors:
        assert s in body
    # Also covered by the comment helper directly.
    helper_body = _mutation_refusal_comment(
        _result(*survivors), _mut_cfg(threshold=0), "survivors_exceed_threshold"
    )
    for s in survivors:
        assert s in helper_body


def test_unavailable_mutation_result_refuses_conservatively(tmp_path: Path) -> None:
    # Adversarial / sad path: result is None (mutation check errored/absent).
    # The gate must NOT green-by-default — it refuses with a DISTINCT reason.
    gh = _FakeGh()
    o = _outcome(54, pr="https://gh/u/r/pull/74", status="merged")
    emitted: list = []

    refused = apply_mutation_survivor_gate(
        [o],
        result=None,
        config=_mut_cfg(threshold=0),
        gh=gh,
        repo="o/r",
        events_file=tmp_path / "events.jsonl",
        emit=lambda k, p: emitted.append((k, p)),
    )

    assert refused == [54]
    assert o.status == "open"  # promotion skipped
    assert gh.disable_calls  # auto-merge disabled
    payload = emitted[0][1]
    assert payload["reason"] == "mutation_result_unavailable"
    assert payload["survivor_count"] is None
    assert "unavailable" in gh.comment_calls[0]["body"].lower()


def test_no_pr_outcome_skipped_by_mutation_gate(tmp_path: Path) -> None:
    # An outcome with no PR has nothing to gate — skip it cleanly.
    gh = _FakeGh()
    o = _outcome(55, pr=None, status="failed")
    refused = apply_mutation_survivor_gate(
        [o],
        result=_result("a"),
        config=_mut_cfg(threshold=0),
        gh=gh,
        repo="o/r",
        events_file=tmp_path / "events.jsonl",
    )
    assert refused == []
    assert gh.disable_calls == []
    assert o.status == "failed"


# ---------------------------------------------------------------------------
# Integration: wire the mutation gate through _run_merge_gate and assert a
# refused issue is (a) excluded from auto-merge and (b) excluded from the
# episodic-memory / frontier promotion path (_record_merged_memory).
# ---------------------------------------------------------------------------


@dataclass
class _MergeResult:
    merged: bool = False
    method: str = "none"


def _int_cfg(tmp_path: Path) -> object:
    from forge_loop.config import Config

    return Config(
        repo=tmp_path,
        github_repo="o/r",
        critic=CriticConfig(enabled=False),
        mutation_gate=MutationGateConfig(enabled=True, module=_MODULE, survivor_threshold=0),
    )


def test_run_merge_gate_skips_promotion_for_refused_issue(tmp_path: Path, monkeypatch) -> None:
    from forge_loop import gh_issues as _gh
    from forge_loop.runner import learning as _learning
    from forge_loop.runner import tick as _tick

    cfg = _int_cfg(tmp_path)

    # gh stubs: issue-closed gate sees OPEN (no-op); track auto-merge attempts.
    monkeypatch.setattr(_gh, "get_issue_state", lambda issue, repo=None: "OPEN")
    monkeypatch.setattr(_gh, "disable_pr_auto_merge", lambda pr, repo=None: True)
    monkeypatch.setattr(_gh, "pr_comment", lambda pr, body, repo=None: True)
    merged_prs: list = []
    monkeypatch.setattr(
        _gh,
        "ensure_pr_merged",
        lambda pr, repo=None: merged_prs.append(pr) or _MergeResult(),
    )
    monkeypatch.setattr(_tick, "_remove_ready_label", lambda *a, **k: None)

    o = _outcome(47, pr="https://gh/u/r/pull/62", status="merged")
    result = MutationCheckResult(
        module=_MODULE,
        survivor_count=1,
        survivors=["eventlog/chain.py:142 replaced `==` with `!=`"],
    )

    refused = _tick._run_merge_gate(
        cfg,
        [o],
        risk_gated_issues=set(),
        used_pipeline=True,  # skips critic → no network
        bus_emit=None,
        master_log_path=tmp_path / "master.log",
        mutation_result=result,
    )

    # (a) refused set carries the issue; auto-merge was NOT enabled for its PR.
    assert 47 in refused
    assert merged_prs == []
    assert o.status == "open"  # flipped → excluded from the merged list

    # (b) promotion seam: a refused issue is filtered out of memory promotion.
    promoted_inputs: list = []
    monkeypatch.setattr(
        _learning,
        "record_merged_outcomes",
        lambda store, merged: promoted_inputs.append([m.issue for m in merged]) or [],
    )
    monkeypatch.setattr("forge_loop.memory.store.SqliteMemoryStore", lambda path: object())
    monkeypatch.setattr(
        "forge_loop.control.boot.canonical_task_saga_path",
        lambda repo: tmp_path / ".forge" / "tasks.db",
    )

    # Even if a refused issue is (wrongly) still flagged merged upstream, the
    # refused_issues filter keeps it out of durable cognition.
    still_merged = _outcome(47, pr="https://gh/u/r/pull/62", status="merged")
    _tick._record_merged_memory(cfg, [still_merged], refused_issues=refused)
    assert promoted_inputs == []  # nothing promoted — refused issue excluded


# ---------------------------------------------------------------------------
# Write-root-escape gate (issue #443).
# ---------------------------------------------------------------------------


def _lease(worktree: str, *, task_id: str = "task-47-worker") -> WorkerLeaseRef:
    """A lease whose only write root is the worktree (the production shape)."""
    return WorkerLeaseRef(task_id=task_id, worktree=worktree, write_roots=(worktree,))


def test_write_root_gate_passes_for_in_bounds_diff(tmp_path: Path) -> None:
    # Happy path: every changed path lives under the leased worktree → the gate
    # is a byte-for-byte pass-through (no refuse, no quarantine, no event).
    gh = _FakeGh()
    wt = str(tmp_path / "wt")
    o = _outcome(47, pr="https://gh/u/r/pull/62", status="merged")
    quarantined: list[Path] = []
    marked: list[tuple[str, str]] = []
    events = tmp_path / "events.jsonl"

    refused = apply_write_root_escape_gate(
        [o],
        lease_for=lambda _o: _lease(wt),
        changed_paths=lambda w: [w + "/src/forge_loop/x.py"],
        gh=gh,
        repo="o/r",
        quarantine=lambda p: quarantined.append(p) or p,
        mark_quarantined=lambda tid, reason: marked.append((tid, reason)),
        events_file=events,
    )

    assert refused == []
    assert o.status == "merged"  # unchanged
    assert gh.disable_calls == []
    assert quarantined == []
    assert marked == []
    assert not events.exists()


def test_write_root_gate_refuses_and_quarantines_on_escape(tmp_path: Path) -> None:
    # Adversarial: a path outside the worktree → refuse, quarantine, event,
    # status flip merged→open, comment posted.
    gh = _FakeGh()
    wt = str(tmp_path / "wt")
    escape = "/home/u/forge-loop/src/forge_loop/runner/tick.py"
    o = _outcome(47, pr="https://gh/u/r/pull/62", status="merged")
    quarantined: list[Path] = []
    marked: list[tuple[str, str]] = []
    events = tmp_path / "events.jsonl"
    emitted: list[tuple[str, dict]] = []

    refused = apply_write_root_escape_gate(
        [o],
        lease_for=lambda _o: _lease(wt),
        changed_paths=lambda w: [w + "/ok.py", escape],
        gh=gh,
        repo="o/r",
        quarantine=lambda p: quarantined.append(p) or p,
        mark_quarantined=lambda tid, reason: marked.append((tid, reason)),
        events_file=events,
        emit=lambda kind, payload: emitted.append((kind, payload)),
    )

    assert refused == [47]
    assert o.status == "open"  # flipped
    assert gh.disable_calls == [("https://gh/u/r/pull/62", "o/r")]
    assert len(gh.comment_calls) == 1
    assert escape in gh.comment_calls[0]["body"]
    # quarantine primitives reused on the worktree.
    assert quarantined == [Path(wt)]
    assert marked == [("task-47-worker", "write-root escape (#443)")]
    # event emitted with the offending path.
    assert emitted == [
        ("merge_refused_write_root_escape", {"issue": 47, "pr": o.pr_url, "paths": [escape]})
    ]
    rows = [json.loads(line) for line in events.read_text().splitlines() if line.strip()]
    assert rows[-1]["kind"] == "merge_refused_write_root_escape"
    assert rows[-1]["paths"] == [escape]


def test_write_root_gate_side_effects_are_best_effort(tmp_path: Path) -> None:
    # A raising pr_comment / disable_pr_auto_merge MUST NOT prevent the event +
    # status flip from firing (mirrors apply_mutation_survivor_gate's contract).
    gh = _FakeGh(disable_raises=True, comment_raises=True)
    wt = str(tmp_path / "wt")
    escape = "/etc/passwd"
    o = _outcome(47, pr="https://gh/u/r/pull/62", status="merged")
    events = tmp_path / "events.jsonl"

    def _raising_quarantine(_p: Path) -> Path | None:
        raise RuntimeError("simulated quarantine failure")

    refused = apply_write_root_escape_gate(
        [o],
        lease_for=lambda _o: _lease(wt),
        changed_paths=lambda w: [escape],
        gh=gh,
        repo="o/r",
        quarantine=_raising_quarantine,
        mark_quarantined=lambda tid, reason: (_ for _ in ()).throw(RuntimeError("boom")),
        events_file=events,
    )

    # Despite EVERY side effect raising, the gate still refused + flipped + emitted.
    assert refused == [47]
    assert o.status == "open"
    rows = [json.loads(line) for line in events.read_text().splitlines() if line.strip()]
    assert rows[-1]["kind"] == "merge_refused_write_root_escape"


def test_write_root_gate_skips_unresolvable_lease(tmp_path: Path) -> None:
    # lease_for → None (no saga / no worktree) → nothing inspectable → skip.
    gh = _FakeGh()
    o = _outcome(47, pr="https://gh/u/r/pull/62", status="merged")
    called: list[str] = []

    refused = apply_write_root_escape_gate(
        [o],
        lease_for=lambda _o: None,
        changed_paths=lambda w: called.append(w) or ["/etc/passwd"],
        gh=gh,
        repo="o/r",
    )

    assert refused == []
    assert o.status == "merged"
    assert called == []  # never even collected a diff


def test_write_root_gate_skips_no_pr_outcome(tmp_path: Path) -> None:
    gh = _FakeGh()
    wt = str(tmp_path / "wt")
    o = _outcome(55, pr=None, status="failed")
    refused = apply_write_root_escape_gate(
        [o],
        lease_for=lambda _o: _lease(wt),
        changed_paths=lambda w: ["/etc/passwd"],
        gh=gh,
        repo="o/r",
    )
    assert refused == []
    assert o.status == "failed"


# ---------------------------------------------------------------------------
# collect_changed_paths: the git seam. Both returncode branches (T3).
# ---------------------------------------------------------------------------


@dataclass
class _FakeCompleted:
    returncode: int
    stdout: str = ""


def test_collect_changed_paths_returncode_zero(tmp_path: Path) -> None:
    wt = str(tmp_path / "wt")

    def _run(cmd, **kw):  # type: ignore[no-untyped-def]
        assert cmd[:3] == ["git", "diff", "--name-only"]
        return _FakeCompleted(returncode=0, stdout="a.py\nsrc/b.py\n\n")

    paths = collect_changed_paths(wt, "trunk", run=_run)
    assert paths == [f"{wt}/a.py", f"{wt}/src/b.py"]


def test_collect_changed_paths_nonzero_returncode_is_empty(tmp_path: Path) -> None:
    wt = str(tmp_path / "wt")

    def _run(cmd, **kw):  # type: ignore[no-untyped-def]
        return _FakeCompleted(returncode=128, stdout="garbage\n")

    # A failed git call yields no paths → the gate is a pass-through, never a
    # false refusal on a transient git failure.
    assert collect_changed_paths(wt, "trunk", run=_run) == []
