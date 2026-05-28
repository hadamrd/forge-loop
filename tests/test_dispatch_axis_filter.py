"""Dispatch-side axis filter (issue #126).

The dispatcher MUST honour ``LOOP_AXIS_FILTER`` when it pulls the open
ready-queue. These tests pin:

* Issues labelled ``axis:dispatch`` are picked when the filter is
  ``--axis dispatch``.
* Union semantics for multiple ``--axis`` values.
* No filter → no change vs today (regression guard).
* An ``axis:dispatch`` issue WITHOUT ``loop:ready`` is still skipped
  (the ready-gate is the prior, higher-priority filter — we never
  override it).
* Malformed ``axis:`` labels are treated as unaligned (skipped under
  a filter, included otherwise).
"""

from __future__ import annotations

from typing import Any

import pytest

from forge_loop.axis import (
    AXIS_FILTER_ENV,
    filter_issues_by_axes,
    parse_filter_env,
)

READY = "loop:ready"


def _make(num: int, *labels: str) -> dict[str, Any]:
    return {
        "number": num,
        "title": f"issue-{num}",
        "labels": [{"name": name} for name in labels],
    }


# ---------------------------------------------------------------------------
# Filtering against a synthetic ready-queue (the ``top_issues`` payload).
# ---------------------------------------------------------------------------


def test_single_axis_picks_only_matching_issues() -> None:
    queue = [
        _make(1, READY, "axis:dispatch"),
        _make(2, READY, "axis:cli"),
        _make(3, READY, "axis:dispatch", "axis:cli"),
        _make(4, READY),  # unaligned
    ]
    picked = filter_issues_by_axes(queue, ["dispatch"])
    assert [i["number"] for i in picked] == [1, 3]


def test_multi_axis_unions() -> None:
    queue = [
        _make(1, READY, "axis:dispatch"),
        _make(2, READY, "axis:cli"),
        _make(3, READY, "axis:docs"),
    ]
    picked = filter_issues_by_axes(queue, ["dispatch", "cli"])
    assert {i["number"] for i in picked} == {1, 2}


def test_no_filter_is_byte_identical_to_today() -> None:
    """Regression guard: empty filter MUST return the input list as-is."""
    queue = [
        _make(1, READY, "axis:dispatch"),
        _make(2, READY),
    ]
    # Identity-preserving (no copy, no reorder) keeps any callsite that
    # relies on list identity (e.g. ``is``-based caches) safe.
    assert filter_issues_by_axes(queue, []) is queue


def test_axis_match_alone_does_not_bypass_ready_gate() -> None:
    """The ready-gate is upstream: `top_issues(loop:ready, …)` only
    returns issues already carrying the ready label. The axis filter is
    a *narrower* filter on top, never a wider one. We simulate that here
    by only feeding ready-labelled issues into the filter — an issue
    that has ``axis:dispatch`` but lacks ``loop:ready`` would never be
    in the input list at all, and the filter is incapable of
    re-introducing it."""
    queue_from_ready_gate = [
        _make(1, READY, "axis:dispatch"),
        # #99 (axis:dispatch but no loop:ready) is NOT in this list —
        # `top_issues` filtered it out upstream.
    ]
    picked = filter_issues_by_axes(queue_from_ready_gate, ["dispatch"])
    assert [i["number"] for i in picked] == [1]


def test_malformed_axis_label_does_not_crash_and_excludes_under_filter() -> None:
    queue = [
        _make(1, READY, "axis:"),  # empty slug
        _make(2, READY, "axis:dispatch"),
    ]
    picked = filter_issues_by_axes(queue, ["dispatch"])
    # #1 (malformed) is NOT in the dispatch bucket.
    assert [i["number"] for i in picked] == [2]


def test_mixed_case_axis_label_normalises() -> None:
    queue = [_make(1, READY, "Axis:Dispatch")]
    picked = filter_issues_by_axes(queue, ["dispatch"])
    assert [i["number"] for i in picked] == [1]


# ---------------------------------------------------------------------------
# Env-var contract: how the CLI tells the dispatcher what to filter.
# ---------------------------------------------------------------------------


def test_parse_filter_env_returns_empty_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(AXIS_FILTER_ENV, raising=False)
    assert parse_filter_env() == []


def test_parse_filter_env_reads_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(AXIS_FILTER_ENV, "dispatch,cli")
    assert parse_filter_env() == ["dispatch", "cli"]


def test_unknown_axis_filter_produces_empty_result_not_error() -> None:
    """Sad path: ``--axis nonexistent`` against a queue with zero
    matching issues is NOT an error — it's a clean idle tick. The
    dispatcher logs a one-liner and moves on (see ``axis_filter_empty``
    event)."""
    queue = [_make(1, READY, "axis:dispatch")]
    picked = filter_issues_by_axes(queue, ["nonexistent"])
    assert picked == []


def test_blocked_pr_repair_respects_axis_filter(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from forge_loop.config import Config
    from forge_loop.runner import tick as tick_mod

    cfg = Config(repo=tmp_path, github_repo="acme/widgets")
    monkeypatch.setenv(AXIS_FILTER_ENV, "dispatch")
    monkeypatch.setattr(
        tick_mod,
        "prs_requiring_repair",
        lambda *_a, **_k: [
            {
                "number": 10,
                "url": "https://github.com/acme/widgets/pull/10",
                "headRefName": "loop/99-old",
            }
        ],
    )
    monkeypatch.setattr(
        tick_mod,
        "fetch_issue",
        lambda *_a, **_k: _make(99, READY, "axis:docs"),
    )
    monkeypatch.setattr(
        tick_mod,
        "pr_review_context",
        lambda *_a, **_k: "should not be called",
    )

    repairs = tick_mod._blocking_pr_repairs(cfg)

    assert repairs == []
    assert "axis_filter_mismatch" in cfg.events_file.read_text()


def test_unresolved_review_thread_pr_is_selected_for_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from forge_loop.config import Config
    from forge_loop.runner import tick as tick_mod

    cfg = Config(repo=tmp_path, github_repo="acme/widgets")
    monkeypatch.setattr(
        tick_mod,
        "prs_requiring_repair",
        lambda *_a, **_k: [
            {
                "number": 10,
                "url": "https://github.com/acme/widgets/pull/10",
                "headRefName": "loop/99-fix-review",
                "repairReasons": ["unresolved_review_threads"],
            }
        ],
    )
    monkeypatch.setattr(tick_mod, "fetch_issue", lambda *_a, **_k: _make(99, READY))
    monkeypatch.setattr(tick_mod, "pr_review_context", lambda *_a, **_k: "thread context")

    repairs = tick_mod._blocking_pr_repairs(cfg)

    assert len(repairs) == 1
    issue, pr, ctx = repairs[0]
    assert issue["number"] == 99
    assert pr["repairReasons"] == ["unresolved_review_threads"]
    assert ctx == "thread context"
    assert "repair_pr_selected" in cfg.events_file.read_text()


def test_repaired_pr_gets_automerge_after_threads_are_clear(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from forge_loop import gh
    from forge_loop.config import Config
    from forge_loop.runner import merge_gate
    from forge_loop.runner import tick as tick_mod
    from forge_loop.worker import WorkerOutcome

    cfg = Config(repo=tmp_path, github_repo="acme/widgets")
    gate_calls: list[str] = []
    merge_calls: list[str] = []
    monkeypatch.setattr(
        merge_gate,
        "apply_issue_closed_gate",
        lambda outcomes, **_kwargs: gate_calls.extend(o.pr_url or "" for o in outcomes) or [],
    )
    monkeypatch.setattr(gh, "unresolved_review_threads", lambda *_a, **_k: [])
    monkeypatch.setattr(
        gh,
        "enable_pr_auto_merge",
        lambda pr, **_kwargs: merge_calls.append(str(pr)) or True,
    )
    outcome = WorkerOutcome(
        issue=99,
        title="fix review",
        pr_url="https://github.com/acme/widgets/pull/10",
        status="open",
        duration_s=1.0,
        stdout_tail="",
    )

    tick_mod._enable_automerge_for_repaired_prs(cfg, [outcome], lambda *_a, **_k: None)

    assert gate_calls == ["https://github.com/acme/widgets/pull/10"]
    assert merge_calls == ["https://github.com/acme/widgets/pull/10"]
    assert outcome.status == "merged"
    assert "repair_automerge_enabled" in cfg.events_file.read_text()
