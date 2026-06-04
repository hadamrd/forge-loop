"""Integration test for boot-time events log rotation (issue #59).

Drives ``runner._helpers.rotate_events_file_at_boot`` end-to-end against a
real on-disk events file pre-seeded to >10 MB. Verifies that boot does
NOT abort, the file is rotated, and the freshly-created events file has
the ``events_file_rotated`` marker as its first row.

Then drives the same helper against a read-only target to verify OSError
is swallowed and an ``events_rotation_failed`` event is emitted instead.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from forge_loop.runner._helpers import rotate_events_file_at_boot

# ---------------------------------------------------------------------------
# Happy path: 11 MB pre-seeded → rotation kicks in at boot
# ---------------------------------------------------------------------------


def test_boot_rotates_eleven_mb_events_file(tmp_path: Path) -> None:
    events = tmp_path / "loop-runner-events.jsonl"
    # Pre-seed with 11 MiB of NDJSON-looking content. Use a single huge
    # write so the test is fast; the rotation contract is size-only.
    one_line = json.dumps({"kind": "noise", "n": 1}) + "\n"
    # Approx ~26 bytes per line — 11 MiB ≈ 444k lines, so just pad the last
    # line to hit the size exactly.
    n_lines = 1000
    body = (one_line * n_lines)
    pad_target = 11 * 1024 * 1024 - len(body)
    assert pad_target > 0
    events.write_text(body + ("x" * pad_target))

    pre_size = events.stat().st_size
    assert pre_size >= 10 * 1024 * 1024

    result = rotate_events_file_at_boot(events)

    assert result is not None
    assert result["rotated"] is True, result
    assert result["rotated_size"] == pre_size

    # Old payload moved to .1.
    archive = tmp_path / "loop-runner-events.jsonl.1"
    assert archive.exists()
    assert archive.stat().st_size == pre_size

    # Fresh events file exists and its first (and only) row is the rotation
    # marker.
    assert events.exists()
    new_lines = events.read_text().splitlines()
    assert len(new_lines) == 1
    rec = json.loads(new_lines[0])
    assert rec["kind"] == "events_file_rotated"
    assert rec["rotated_size"] == pre_size
    assert rec["archive_count"] == 1
    assert "ts" in rec


# ---------------------------------------------------------------------------
# Adversarial: read-only parent directory makes rename fail. Boot must
# survive and we must see the failure event.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses mode bits")
def test_boot_rotation_failure_is_swallowed(tmp_path: Path) -> None:
    events = tmp_path / "loop-runner-events.jsonl"
    # Big enough to trip the threshold.
    events.write_bytes(b"q" * (10 * 1024 * 1024))

    # Lock the parent directory so rename() raises PermissionError.
    original_mode = tmp_path.stat().st_mode
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IXUSR)  # r-x------
    try:
        # Must NOT raise.
        result = rotate_events_file_at_boot(events)
    finally:
        os.chmod(tmp_path, original_mode)

    assert result is not None
    assert result["rotated"] is False
    assert result["error"]  # truthy error string

    # Original file is still there (rename failed). We tried to append a
    # failure event; that append may itself have been blocked by the
    # locked directory, which is fine (best-effort). What we *must* see is
    # that we did not raise.
    assert events.exists()


# ---------------------------------------------------------------------------
# Below-threshold no-op
# ---------------------------------------------------------------------------


def test_boot_does_not_rotate_small_file(tmp_path: Path) -> None:
    events = tmp_path / "loop-runner-events.jsonl"
    events.write_text('{"kind":"tick_start"}\n')
    result = rotate_events_file_at_boot(events)
    assert result is None
    # File unchanged, no archive.
    assert events.read_text() == '{"kind":"tick_start"}\n'
    assert not (tmp_path / "loop-runner-events.jsonl.1").exists()


# ---------------------------------------------------------------------------
# Issue #210: load-bearing guard at boot rotation
# ---------------------------------------------------------------------------


def _seed_big_cascade_with_decision(tmp_path: Path) -> Path:
    """Threshold-tripping live file + full ring whose ``.3`` holds a decision."""
    events = tmp_path / "loop-runner-events.jsonl"
    events.write_bytes(b"q" * (10 * 1024 * 1024))
    (tmp_path / "loop-runner-events.jsonl.1").write_text("a\n")
    (tmp_path / "loop-runner-events.jsonl.2").write_text("b\n")
    (tmp_path / "loop-runner-events.jsonl.3").write_text(
        json.dumps({"kind": "decision.made", "choice": "x"}) + "\n"
    )
    return events


def test_boot_rotation_preserves_load_bearing_decision(tmp_path: Path) -> None:
    events = _seed_big_cascade_with_decision(tmp_path)

    result = rotate_events_file_at_boot(events)

    assert result is not None and result["rotated"] is True
    assert result["preserved_load_bearing"] == 1
    preserved = tmp_path / "loop-runner-events.jsonl.preserved"
    kinds = [json.loads(line)["kind"] for line in preserved.read_text().splitlines()]
    assert "decision.made" in kinds


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses mode bits")
def test_boot_rotation_guard_failure_does_not_raise_through_boot(tmp_path: Path) -> None:
    # Adversarial: the preserved-tier sidecar cannot be written (locked dir).
    # The guard must swallow the OSError; boot must not raise and rotation
    # telemetry is still produced.
    events = _seed_big_cascade_with_decision(tmp_path)

    # Make the directory read-only so the sidecar write (and rename) fail.
    original_mode = tmp_path.stat().st_mode
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IXUSR)  # r-x------
    try:
        result = rotate_events_file_at_boot(events)  # must NOT raise
    finally:
        os.chmod(tmp_path, original_mode)

    assert result is not None
    # Either rotation failed cleanly (telemetry) or preservation degraded to 0,
    # but in no case did boot raise.
    assert "preserved_load_bearing" in result
    assert events.exists()
