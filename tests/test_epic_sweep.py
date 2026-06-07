"""Tests for the deterministic epic auto-close sweep (issue #367).

Mirrors ``tests/test_stuck_sweep.py``: an in-memory ``MockGhClient`` drives
the pure sweep logic, plus an integration test that exercises the tick wiring
(``tick_checks.run_epic_sweep``) including the maintenance-cadence gate.

Testing-manifesto coverage:

* T1 (state machine ⇒ edge + adversarial default arm): the sweep's per-epic
  decision has three arms — all-closed → close, any-open → skip, no-subs →
  skip; each has a test, plus the cascade adversarial case.
* T2 (external-dep assumption ⇒ adversarial false case): ``sub_issues`` raising
  (API/permission failure) is tested — the epic is recorded under ``errors``
  and the sweep does NOT raise.
* T3/returncode-shape analogue: ``close_issue`` happy path is asserted via the
  recorded calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from forge_loop.config import Config
from forge_loop.epic_sweep import EpicSweepReport, build_close_comment, sweep
from forge_loop.gh_client import GhError, Issue, MockGhClient, SubIssue
from forge_loop.runner.tick_checks import run_epic_sweep

EPIC = "epic"


def _epic(number: int, *, state: str = "open") -> Issue:
    return Issue(number=number, title=f"epic {number}", body="", state=state, labels=[EPIC])


def _sub(number: int, *, state: str = "CLOSED", closing_pr: int | None = None) -> SubIssue:
    return SubIssue(number=number, state=state, title=f"sub {number}", closing_pr=closing_pr)


# ---------------------------------------------------------------------------
# Core close condition
# ---------------------------------------------------------------------------


def test_epic_with_all_subs_closed_is_closed() -> None:
    epic = _epic(312)
    gh = MockGhClient(
        issues_by_label_response=[epic],
        sub_issues_by_epic={312: [_sub(146), _sub(150), _sub(151)]},
    )

    rep = sweep(gh, owner="o", repo="r", epic_label=EPIC)

    assert rep.closed == [312]
    assert rep.skipped_open_subs == []
    assert rep.skipped_no_subs == []
    assert rep.errors == {}
    methods = [m for m, _ in gh.calls]
    assert "add_comment" in methods  # audit comment posted before close
    assert "close_issue" in methods
    close_kw = [kw for m, kw in gh.calls if m == "close_issue"][0]
    assert close_kw["number"] == 312
    assert close_kw["reason"] == "completed"


def test_epic_with_one_open_sub_is_never_closed() -> None:
    """The core regression guard: a single open sub-issue blocks the close."""
    epic = _epic(312)
    gh = MockGhClient(
        issues_by_label_response=[epic],
        sub_issues_by_epic={312: [_sub(146), _sub(150), _sub(151, state="OPEN")]},
    )

    rep = sweep(gh, owner="o", repo="r", epic_label=EPIC)

    assert rep.closed == []
    assert rep.skipped_open_subs == [312]
    methods = [m for m, _ in gh.calls]
    assert "close_issue" not in methods
    assert "add_comment" not in methods


def test_epic_with_zero_subs_is_never_closed() -> None:
    epic = _epic(400)
    gh = MockGhClient(issues_by_label_response=[epic], sub_issues_by_epic={})

    rep = sweep(gh, owner="o", repo="r", epic_label=EPIC)

    assert rep.closed == []
    assert rep.skipped_no_subs == [400]
    assert "close_issue" not in [m for m, _ in gh.calls]


def test_merged_pr_counts_as_closed() -> None:
    """A merged PR closes its sub-issue, so a CLOSED sub-issue with a closing
    PR satisfies the condition — no special 'merged' handling needed."""
    epic = _epic(312)
    gh = MockGhClient(
        issues_by_label_response=[epic],
        sub_issues_by_epic={312: [_sub(146, closing_pr=149), _sub(150, closing_pr=152)]},
    )

    rep = sweep(gh, owner="o", repo="r", epic_label=EPIC)

    assert rep.closed == [312]


# ---------------------------------------------------------------------------
# Comment body
# ---------------------------------------------------------------------------


def test_comment_lists_subs_and_resolving_prs() -> None:
    subs = [_sub(146, closing_pr=149), _sub(150, closing_pr=152), _sub(151)]
    body = build_close_comment(312, subs)
    # Every sub-issue number present.
    assert "#146" in body
    assert "#150" in body
    assert "#151" in body
    # Resolving PRs linked where present.
    assert "PR #149" in body
    assert "PR #152" in body
    # #151 has no closing PR → listed without a PR link.
    assert "#151 (PR" not in body
    assert "all 3 sub-issues resolved" in body


def test_sweep_posts_comment_with_resolving_prs() -> None:
    """The close path actually wires the resolving-PR comment to GhClient."""
    epic = _epic(312)
    gh = MockGhClient(
        issues_by_label_response=[epic],
        sub_issues_by_epic={312: [_sub(146, closing_pr=149), _sub(150, closing_pr=152)]},
    )

    sweep(gh, owner="o", repo="r", epic_label=EPIC)

    comment = [kw for m, kw in gh.calls if m == "add_comment"][0]
    assert "#146 (PR #149)" in comment["body"]
    assert "#150 (PR #152)" in comment["body"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_already_closed_epic_is_a_noop() -> None:
    """Second run: the epic is already closed → state gate makes it a no-op."""
    epic = _epic(312, state="closed")
    gh = MockGhClient(
        issues_by_label_response=[epic],
        sub_issues_by_epic={312: [_sub(146), _sub(150)]},
    )

    rep = sweep(gh, owner="o", repo="r", epic_label=EPIC)

    assert rep.closed == []
    assert rep.skipped_no_subs == []
    assert rep.skipped_open_subs == []
    # Closed epics aren't even probed for sub-issues.
    assert "sub_issues" not in [m for m, _ in gh.calls]
    assert "close_issue" not in [m for m, _ in gh.calls]


# ---------------------------------------------------------------------------
# Adversarial / sad path
# ---------------------------------------------------------------------------


def test_sub_issues_error_is_caught_and_recorded() -> None:
    """GhClient raises on sub_issues → epic recorded under errors, never closed,
    sweep does NOT raise (the tick survives)."""
    epic = _epic(312)
    gh = MockGhClient(
        issues_by_label_response=[epic],
        sub_issues_by_epic={312: [_sub(146)]},
        raise_on={"sub_issues": GhError("sub_issues", 403, "forbidden")},
    )

    rep = sweep(gh, owner="o", repo="r", epic_label=EPIC)

    assert rep.closed == []
    assert 312 in rep.errors
    assert "sub_issues" in rep.errors[312]
    assert "close_issue" not in [m for m, _ in gh.calls]


def test_list_error_returns_empty_report_without_raising() -> None:
    gh = MockGhClient(raise_on={"issues_by_label": GhError("issues_by_label", 500, "boom")})
    rep = sweep(gh, owner="o", repo="r", epic_label=EPIC)
    assert rep == EpicSweepReport()


def test_child_epic_close_does_not_cascade_close_parent() -> None:
    """A parent epic whose only sub-issue is a child epic must NOT be closed in
    the same pass that closes the child — only epics that INDEPENDENTLY meet the
    condition (against the snapshot) close."""
    parent = _epic(500)
    child = _epic(501)
    gh = MockGhClient(
        issues_by_label_response=[parent, child],
        sub_issues_by_epic={
            500: [_sub(501, state="OPEN")],  # child epic still open at snapshot
            501: [_sub(146), _sub(150)],  # child's own subs all closed
        },
    )

    rep = sweep(gh, owner="o", repo="r", epic_label=EPIC)

    assert rep.closed == [501]
    assert rep.skipped_open_subs == [500]
    closed_numbers = [kw["number"] for m, kw in gh.calls if m == "close_issue"]
    assert closed_numbers == [501]


# ---------------------------------------------------------------------------
# Integration — tick wiring + cadence gate
# ---------------------------------------------------------------------------


def _read_events(cfg: Config) -> list[dict[str, Any]]:
    if not cfg.events_file.exists():
        return []
    return [json.loads(ln) for ln in cfg.events_file.read_text().splitlines() if ln.strip()]


def _cfg(tmp_path: Path) -> Config:
    return Config(repo=tmp_path, github_repo="o/r", maintenance_every_n_ticks=5)


def test_run_epic_sweep_writes_event_and_returns_report_on_cadence(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    gh = MockGhClient(
        issues_by_label_response=[_epic(312)],
        sub_issues_by_epic={312: [_sub(146), _sub(150)]},
    )

    rep = run_epic_sweep(cfg, tick=5, client=gh)

    assert rep is not None
    assert rep.closed == [312]
    events = [e for e in _read_events(cfg) if e.get("kind") == "epic_sweep_done"]
    assert len(events) == 1
    assert events[0]["closed"] == [312]
    assert events[0]["skipped_open_subs"] == []
    assert events[0]["skipped_no_subs"] == []
    assert events[0]["errors"] == []
    assert events[0]["tick"] == 5


def test_run_epic_sweep_is_noop_off_cadence(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    gh = MockGhClient(
        issues_by_label_response=[_epic(312)],
        sub_issues_by_epic={312: [_sub(146)]},
    )

    rep = run_epic_sweep(cfg, tick=3, client=gh)  # 3 % 5 != 0

    assert rep is None
    assert gh.calls == []  # GitHub never touched off-cadence
    assert _read_events(cfg) == []


def test_run_epic_sweep_disabled_when_cadence_zero(tmp_path: Path) -> None:
    cfg = Config(repo=tmp_path, github_repo="o/r", maintenance_every_n_ticks=0)
    gh = MockGhClient(issues_by_label_response=[_epic(312)])

    rep = run_epic_sweep(cfg, tick=10, client=gh)

    assert rep is None
    assert gh.calls == []
