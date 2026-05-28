"""Tests for the typed events framework (issue #88)."""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest

from forge_loop.events import (
    EVENT_REGISTRY,
    EventBase,
    LoopStartEvent,
    RedeployEvent,
    TickStartEvent,
    WorktreeReapedEvent,
    append_event_with_registry_check,
    emit,
    register_event,
)
from forge_loop.state import append_event


# ---------------------------------------------------------------------------
# Schema validation at construction — typed events refuse bad payloads
# before they reach disk.
# ---------------------------------------------------------------------------


def test_loop_start_event_rejects_negative_parallel() -> None:
    with pytest.raises(Exception) as excinfo:
        LoopStartEvent(parallel=0, tick_interval=60, max_ticks=0)
    assert "parallel" in str(excinfo.value).lower()


def test_redeploy_event_requires_ok() -> None:
    with pytest.raises(Exception) as excinfo:
        RedeployEvent(task="x")  # type: ignore[call-arg]
    assert "ok" in str(excinfo.value).lower()


def test_tick_start_event_rejects_zero_tick() -> None:
    with pytest.raises(Exception):
        TickStartEvent(tick=0)


def test_worktree_reaped_rejects_zero_issue() -> None:
    with pytest.raises(Exception):
        WorktreeReapedEvent(issue=0)


# ---------------------------------------------------------------------------
# Emit path — typed event written round-trips through JSON.
# ---------------------------------------------------------------------------


def test_emit_writes_typed_event_with_kind_and_ts(tmp_path: Path) -> None:
    events_file = tmp_path / "events.jsonl"
    evt = RedeployEvent(ok=True, task="deploy:k3s", detail="")
    emit(events_file, evt)
    line = events_file.read_text().strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["kind"] == "redeploy"
    assert rec["ok"] is True
    assert rec["task"] == "deploy:k3s"
    assert "ts" in rec


def test_emit_rejects_non_event(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="EventBase"):
        emit(tmp_path / "events.jsonl", {"kind": "redeploy", "ok": True})  # type: ignore[arg-type]


def test_emit_appends_does_not_overwrite(tmp_path: Path) -> None:
    events_file = tmp_path / "events.jsonl"
    emit(events_file, TickStartEvent(tick=1, issues=[10, 20]))
    emit(events_file, TickStartEvent(tick=2))
    lines = events_file.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["tick"] == 1
    assert json.loads(lines[0])["issues"] == [10, 20]
    assert json.loads(lines[1])["tick"] == 2


# ---------------------------------------------------------------------------
# Registry contract — duplicate registration is rejected; KIND is required.
# ---------------------------------------------------------------------------


def test_registry_rejects_duplicate_kind() -> None:
    """Catches the silent override bug where two modules each declare a
    typed event with the same KIND and one wins by import order."""

    class _Dup(EventBase):
        KIND = "loop_start"  # already taken by LoopStartEvent

    with pytest.raises(ValueError, match="already registered"):
        register_event(_Dup)


def test_registry_rejects_missing_kind() -> None:
    class _NoKind(EventBase):
        pass  # forgot to set KIND

    with pytest.raises(ValueError, match="must set KIND"):
        register_event(_NoKind)


def test_registry_contains_documented_events() -> None:
    """Locks the public surface — if a future PR drops one of these typed
    schemas, this test breaks loudly so the migration plan stays in view."""
    for kind in ("loop_start", "loop_stop", "tick_start", "redeploy", "worktree_reaped"):
        assert kind in EVENT_REGISTRY, f"event {kind!r} dropped from registry"


# ---------------------------------------------------------------------------
# Back-compat — state.append_event still works for unregistered kinds
# but warns when the kind has a typed model.
# ---------------------------------------------------------------------------


def test_append_event_unregistered_kind_writes_no_warning(tmp_path: Path) -> None:
    events_file = tmp_path / "events.jsonl"
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning would fail
        append_event(events_file, "some_ad_hoc_kind", x=1, y="z")
    rec = json.loads(events_file.read_text().strip())
    assert rec["kind"] == "some_ad_hoc_kind"
    assert rec["x"] == 1
    assert rec["y"] == "z"


def test_append_event_registered_kind_warns_but_still_writes(tmp_path: Path) -> None:
    events_file = tmp_path / "events.jsonl"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        append_event(events_file, "redeploy", ok=True, task="deploy:k3s")
    # The deprecation warning was emitted...
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert deprecations, "expected DeprecationWarning for registered kind"
    assert "RedeployEvent" in str(deprecations[0].message)
    # ...AND the record still hit disk (back-compat).
    rec = json.loads(events_file.read_text().strip())
    assert rec["kind"] == "redeploy"
    assert rec["ok"] is True


def test_loose_path_preserves_extra_fields(tmp_path: Path) -> None:
    """A loose append_event with extra fields beyond the typed model
    must still write all fields — the back-compat contract is 'tolerate
    superset until migration completes'."""
    events_file = tmp_path / "events.jsonl"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        append_event_with_registry_check(
            events_file, "redeploy",
            ok=True, task="deploy:k3s",
            unknown_extra_field="kept",
        )
    rec = json.loads(events_file.read_text().strip())
    assert rec["unknown_extra_field"] == "kept"
