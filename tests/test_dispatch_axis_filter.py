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
        _make(1, READY, "axis:"),       # empty slug
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
