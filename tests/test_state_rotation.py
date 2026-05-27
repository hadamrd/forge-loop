"""Tests for ``rotate_events_file_if_needed`` (issue #59).

Covers the size-threshold cascade, the cap on retained archives, and the
adversarial path where rename() itself raises OSError — boot must keep
going regardless and we expect an ``events_rotation_failed`` line.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from forge_loop import state
from forge_loop.state import (
    DEFAULT_ROTATE_BYTES,
    MAX_ARCHIVES,
    append_event,
    rotate_events_file_if_needed,
)

# ---------------------------------------------------------------------------
# Happy path: below threshold = no-op
# ---------------------------------------------------------------------------


def test_below_threshold_does_not_rotate(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text("x" * (DEFAULT_ROTATE_BYTES - 1))
    result = rotate_events_file_if_needed(events)
    assert result is None
    # File untouched, no archive created.
    assert events.stat().st_size == DEFAULT_ROTATE_BYTES - 1
    assert not events.with_suffix(".jsonl.1").exists()


def test_missing_file_does_not_rotate(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    result = rotate_events_file_if_needed(events)
    assert result is None
    assert not events.exists()


# ---------------------------------------------------------------------------
# At/above threshold = rotate
# ---------------------------------------------------------------------------


def test_at_threshold_rotates(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    # Use a small threshold to keep the test fast.
    threshold = 1024
    payload = b"y" * threshold
    events.write_bytes(payload)
    result = rotate_events_file_if_needed(events, rotate_bytes=threshold)
    assert result is not None
    assert result["rotated"] is True
    assert result["rotated_size"] == threshold
    assert result["archive_count"] == 1

    # Original is fresh — contains exactly the rotation marker line.
    assert events.exists()
    lines = events.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == "events_file_rotated"
    assert rec["rotated_size"] == threshold
    assert rec["archive_count"] == 1

    # Archive holds the original payload.
    archive = tmp_path / "events.jsonl.1"
    assert archive.exists()
    assert archive.read_bytes() == payload


def test_above_threshold_rotates(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    threshold = 100
    events.write_bytes(b"z" * (threshold + 50))
    rotate_events_file_if_needed(events, rotate_bytes=threshold)
    archive = tmp_path / "events.jsonl.1"
    assert archive.exists()
    assert archive.stat().st_size == threshold + 50


# ---------------------------------------------------------------------------
# Cascade: full archive ring shifts and drops the oldest
# ---------------------------------------------------------------------------


def test_full_cascade_drops_oldest(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    threshold = 100
    # Pre-seed all the slots so we can verify the shift.
    events.write_bytes(b"L" * threshold)              # live
    (tmp_path / "events.jsonl.1").write_text("A1")
    (tmp_path / "events.jsonl.2").write_text("A2")
    (tmp_path / "events.jsonl.3").write_text("A3")  # should be discarded

    result = rotate_events_file_if_needed(events, rotate_bytes=threshold)
    assert result is not None and result["rotated"] is True

    # .1 used to be live (now bytes "L"*threshold)
    assert (tmp_path / "events.jsonl.1").read_bytes() == b"L" * threshold
    # .2 used to be .1
    assert (tmp_path / "events.jsonl.2").read_text() == "A1"
    # .3 used to be .2
    assert (tmp_path / "events.jsonl.3").read_text() == "A2"
    # No .4 — old .3 was dropped.
    assert not (tmp_path / "events.jsonl.4").exists()

    # Live file is fresh + rotation marker.
    rec = json.loads(events.read_text().splitlines()[0])
    assert rec["kind"] == "events_file_rotated"
    assert rec["archive_count"] == MAX_ARCHIVES


# ---------------------------------------------------------------------------
# Sad path: OSError during rename is caught
# ---------------------------------------------------------------------------


def test_oserror_during_rotation_emits_failure_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = tmp_path / "events.jsonl"
    threshold = 100
    events.write_bytes(b"q" * threshold)

    real_rename = Path.rename

    def boom(self: Path, target):  # type: ignore[no-untyped-def]
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "rename", boom)

    result = rotate_events_file_if_needed(events, rotate_bytes=threshold)
    assert result is not None
    assert result["rotated"] is False
    assert "read-only" in result["error"]

    # Restore so we can read the events back even after monkeypatch teardown.
    monkeypatch.setattr(Path, "rename", real_rename)

    # The original file still exists (no rename happened) and now contains
    # the failure event appended. The pre-seed bytes did not end with a
    # newline so we don't try to JSON-decode line-by-line; we just verify
    # the failure record sits at the tail.
    assert events.exists()
    contents = events.read_text()
    assert "events_rotation_failed" in contents
    assert "read-only" in contents
    # Extract the trailing JSON object — append_event always writes
    # ``{...}\n`` so the last ``{`` of the file starts a valid record.
    last_brace = contents.rfind("{")
    rec = json.loads(contents[last_brace:].strip())
    assert rec["kind"] == "events_rotation_failed"
    assert "read-only" in rec["error"]


def test_oserror_during_stat_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If even stat() blows up we must not raise — boot has to continue."""
    events = tmp_path / "events.jsonl"
    events.touch()

    def boom_stat(self: Path, *a, **kw):  # type: ignore[no-untyped-def]
        if self == events:
            raise OSError("EIO")
        return os.stat(self)

    monkeypatch.setattr(Path, "stat", boom_stat)
    # Should not raise.
    result = rotate_events_file_if_needed(events, rotate_bytes=10)
    assert result is not None
    assert result["rotated"] is False
    assert "EIO" in (result["error"] or "")


# ---------------------------------------------------------------------------
# Env var override
# ---------------------------------------------------------------------------


def test_env_var_overrides_default_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = tmp_path / "events.jsonl"
    events.write_bytes(b"a" * 500)
    # Smaller threshold via env → should now rotate.
    monkeypatch.setenv("LOOP_EVENTS_ROTATE_BYTES", "300")
    result = rotate_events_file_if_needed(events)
    assert result is not None and result["rotated"] is True
    assert (tmp_path / "events.jsonl.1").exists()


def test_invalid_env_var_falls_back_to_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = tmp_path / "events.jsonl"
    events.write_bytes(b"a" * 500)
    monkeypatch.setenv("LOOP_EVENTS_ROTATE_BYTES", "not-an-int")
    # Default is 10 MiB so 500-byte file should not rotate.
    result = rotate_events_file_if_needed(events)
    assert result is None


# ---------------------------------------------------------------------------
# append_event still works after rotation (regression guard)
# ---------------------------------------------------------------------------


def test_can_append_normally_after_rotation(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_bytes(b"x" * 200)
    rotate_events_file_if_needed(events, rotate_bytes=200)
    append_event(events, "tick_start", tick=1)
    lines = events.read_text().splitlines()
    # Marker + the tick_start we just appended.
    assert len(lines) == 2
    assert json.loads(lines[-1])["kind"] == "tick_start"


# ---------------------------------------------------------------------------
# Module-level constants (sanity)
# ---------------------------------------------------------------------------


def test_default_threshold_is_10_mib() -> None:
    assert DEFAULT_ROTATE_BYTES == 10 * 1024 * 1024


def test_max_archives_is_3() -> None:
    assert MAX_ARCHIVES == 3


# Use the module to suppress unused-import warning on `state`.
def test_module_exposes_helper() -> None:
    assert callable(state.rotate_events_file_if_needed)
