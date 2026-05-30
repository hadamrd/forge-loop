"""Property-based tests for :mod:`forge_loop.events` (issue #91).

Hypothesis catches the failure class unit tests don't — unusual-but-valid
input that crashes the parser/emitter. Round-trip is the canonical
property here: anything we ``emit()`` must read back as the same record.
"""

from __future__ import annotations

import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from hypothesis import given, settings
from hypothesis import strategies as st

from forge_loop.events import (
    LoopStartEvent,
    LoopStopEvent,
    RedeployEvent,
    TickStartEvent,
    WorktreeReapedEvent,
    emit,
)


# ---------------------------------------------------------------------------
# Strategies — bounded to the field constraints declared on the EventBase
# subclasses. Hypothesis explores within these envelopes.
# ---------------------------------------------------------------------------


def _loop_start_st() -> st.SearchStrategy:
    return st.builds(
        LoopStartEvent,
        parallel=st.integers(min_value=1, max_value=64),
        tick_interval=st.integers(min_value=1, max_value=3600),
        max_ticks=st.integers(min_value=0, max_value=10_000),
    )


def _loop_stop_st() -> st.SearchStrategy:
    return st.builds(LoopStopEvent, tick=st.integers(min_value=0, max_value=10_000))


def _tick_start_st() -> st.SearchStrategy:
    return st.builds(
        TickStartEvent,
        tick=st.integers(min_value=1, max_value=10_000),
        issues=st.lists(st.integers(min_value=1, max_value=999_999), max_size=20),
    )


def _redeploy_st() -> st.SearchStrategy:
    return st.builds(
        RedeployEvent,
        ok=st.booleans(),
        # task may contain unicode, shell metacharacters, anything yaml/env allows
        task=st.text(max_size=200),
        detail=st.text(max_size=500),
    )


def _worktree_reaped_st() -> st.SearchStrategy:
    return st.builds(
        WorktreeReapedEvent,
        issue=st.integers(min_value=1, max_value=999_999),
        status=st.text(max_size=80),
    )


_ANY_EVENT = st.one_of(
    _loop_start_st(),
    _loop_stop_st(),
    _tick_start_st(),
    _redeploy_st(),
    _worktree_reaped_st(),
)


# ---------------------------------------------------------------------------
# Round-trip property — emit then read back == construction-time payload.
# ---------------------------------------------------------------------------


@contextmanager
def _fresh_events_file() -> Iterator[Path]:
    """Per-iteration temp dir — hypothesis re-runs many times in one test
    function, so a tmp_path fixture would accumulate state across runs.
    A fresh TemporaryDirectory keeps each generated input isolated."""
    with tempfile.TemporaryDirectory() as d:
        yield Path(d) / "events.jsonl"


@given(event=_ANY_EVENT)
@settings(max_examples=200, deadline=None)
def test_emit_round_trips(event) -> None:
    """For any valid typed event, emit() writes a JSON line whose
    payload matches the event's model_dump (modulo the ts stamp).
    """
    with _fresh_events_file() as events_file:
        emit(events_file, event)
        line = events_file.read_text().strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["kind"] == event.KIND
    assert "ts" in rec
    expected = event.model_dump(mode="json")
    for k, v in expected.items():
        assert rec[k] == v, f"field {k!r} round-trip mismatch: emitted={v!r}, read={rec[k]!r}"


# ---------------------------------------------------------------------------
# Append-only property — N events written → N lines in the file, each
# parseable as JSON with a kind matching the original.
# ---------------------------------------------------------------------------


@given(events=st.lists(_ANY_EVENT, min_size=1, max_size=20))
@settings(max_examples=50, deadline=None)
def test_emit_is_append_only(events: list) -> None:
    with _fresh_events_file() as events_file:
        for e in events:
            emit(events_file, e)
        lines = events_file.read_text().strip().splitlines()
    assert len(lines) == len(events), "emit() must append, not overwrite"
    for line, original in zip(lines, events, strict=True):
        rec = json.loads(line)
        assert rec["kind"] == original.KIND


# ---------------------------------------------------------------------------
# Unicode-safety — text fields with arbitrary unicode (emoji, RTL, control
# chars, NUL, surrogates-handled-by-json) must survive the round-trip.
# ---------------------------------------------------------------------------


@given(
    task=st.text(
        # Drop NUL only — json escapes everything else fine.
        alphabet=st.characters(blacklist_characters="\x00"),
        max_size=300,
    ),
    detail=st.text(
        alphabet=st.characters(blacklist_characters="\x00"),
        max_size=600,
    ),
    ok=st.booleans(),
)
@settings(max_examples=200, deadline=None)
def test_redeploy_unicode_safe(task: str, detail: str, ok: bool) -> None:
    evt = RedeployEvent(ok=ok, task=task, detail=detail)
    with _fresh_events_file() as events_file:
        emit(events_file, evt)
        rec = json.loads(events_file.read_text().strip())
    assert rec["task"] == task
    assert rec["detail"] == detail
