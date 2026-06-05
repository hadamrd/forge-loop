"""Tests for the typed events framework (issue #88)."""

from __future__ import annotations

import json
import warnings
from pathlib import Path

import pytest
from pydantic import ValidationError

from forge_loop.events import (
    EVENT_REGISTRY,
    EventBase,
    LoopStartEvent,
    RedeployEvent,
    TickStartEvent,
    WorktreeReapedEvent,
    append_event_with_registry_check,
    emit,
    read_events,
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
    with pytest.raises(ValidationError):
        TickStartEvent(tick=0)


def test_worktree_reaped_rejects_zero_issue() -> None:
    with pytest.raises(ValidationError):
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


# ---------------------------------------------------------------------------
# read_events — the ONE shared JSONL reader (issue #224). Covers the
# malformed-line skip + tail-bound logic that used to be copy-pasted across
# ~12 modules.
# ---------------------------------------------------------------------------


def _write_lines(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_read_events_happy_path_yields_dicts_in_order(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    _write_lines(p, ['{"kind": "a", "n": 1}', '{"kind": "b", "n": 2}'])
    out = list(read_events(p))
    assert out == [{"kind": "a", "n": 1}, {"kind": "b", "n": 2}]


def test_read_events_skips_malformed_and_blank_lines(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    _write_lines(
        p,
        [
            '{"kind": "ok1"}',
            "",  # blank
            "{not valid json",  # half-written / corrupt
            "   ",  # whitespace-only
            '{"kind": "ok2"}',
        ],
    )
    out = list(read_events(p))
    assert out == [{"kind": "ok1"}, {"kind": "ok2"}]


def test_read_events_skips_non_dict_json(tmp_path: Path) -> None:
    """A valid-JSON scalar/array is not an event record — the contract is
    Iterator[dict], so non-objects are dropped."""
    p = tmp_path / "events.jsonl"
    _write_lines(p, ["123", '"a string"', "[1, 2, 3]", '{"kind": "real"}'])
    assert list(read_events(p)) == [{"kind": "real"}]


def test_read_events_tail_bounds_to_last_n_records(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    _write_lines(p, [f'{{"n": {i}}}' for i in range(10)])
    out = list(read_events(p, tail=3))
    assert [e["n"] for e in out] == [7, 8, 9]


def test_read_events_tail_counts_records_not_lines(tmp_path: Path) -> None:
    """tail bounds *yielded records*, so malformed lines interleaved with
    the tail window don't eat into the count."""
    p = tmp_path / "events.jsonl"
    _write_lines(
        p,
        ['{"n": 0}', "GARBAGE", '{"n": 1}', "", '{"n": 2}'],
    )
    out = list(read_events(p, tail=2))
    assert [e["n"] for e in out] == [1, 2]


def test_read_events_tail_zero_yields_nothing(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    _write_lines(p, ['{"n": 1}', '{"n": 2}'])
    assert list(read_events(p, tail=0)) == []


def test_read_events_tail_larger_than_file_yields_all(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    _write_lines(p, ['{"n": 1}', '{"n": 2}'])
    assert [e["n"] for e in read_events(p, tail=999)] == [1, 2]


def test_read_events_negative_tail_raises(tmp_path: Path) -> None:
    """Adversarial: a negative tail is a programming error, not a silent
    no-op — it must raise rather than guess."""
    p = tmp_path / "events.jsonl"
    _write_lines(p, ['{"n": 1}'])
    with pytest.raises(ValueError, match="tail must be >= 0"):
        list(read_events(p, tail=-1))


def test_read_events_missing_file_raises_oserror(tmp_path: Path) -> None:
    """Adversarial / T2: read_events does NOT swallow OSError — callers
    keep their own exists()/try-except guard. A missing path must raise
    when iterated, not silently yield nothing."""
    missing = tmp_path / "nope.jsonl"
    with pytest.raises(OSError):
        list(read_events(missing))


def test_read_events_tolerates_non_utf8_bytes(tmp_path: Path) -> None:
    """A non-UTF-8 / half-flushed byte sequence must not crash the reader
    (errors='replace'); valid records around it still come through."""
    p = tmp_path / "events.jsonl"
    p.write_bytes(b'{"kind": "before"}\n\xff\xfe not utf8\n{"kind": "after"}\n')
    out = list(read_events(p))
    assert out == [{"kind": "before"}, {"kind": "after"}]


def test_read_events_returns_lazy_iterator_when_untailed(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    _write_lines(p, ['{"n": 1}', '{"n": 2}'])
    it = read_events(p)
    assert iter(it) is iter(it)  # it's an iterator, not a re-iterable list
    assert next(iter(it))["n"] == 1
