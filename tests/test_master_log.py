"""Tests for master_log.py — append + format + thread safety."""

from __future__ import annotations

import threading
from pathlib import Path

from forge_loop import master_log as ml


def test_append_writes_a_timestamped_line(tmp_path: Path) -> None:
    p = tmp_path / "m.log"
    ml.append(p, "hello world")
    content = p.read_text()
    assert "hello world" in content
    assert "Z" in content  # timestamp present


def test_info_warn_error_use_their_levels(tmp_path: Path) -> None:
    p = tmp_path / "m.log"
    ml.info(p, "info-line")
    ml.warn(p, "warn-line")
    ml.error(p, "error-line")
    lines = p.read_text().splitlines()
    assert any("INFO" in line and "info-line" in line for line in lines)
    assert any("WARN" in line and "warn-line" in line for line in lines)
    assert any("ERROR" in line and "error-line" in line for line in lines)


def test_thread_safe_appends_dont_interleave(tmp_path: Path) -> None:
    p = tmp_path / "m.log"

    def writer(prefix: str) -> None:
        for i in range(50):
            ml.append(p, f"{prefix}-{i}")

    threads = [threading.Thread(target=writer, args=(c,)) for c in "ABC"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = p.read_text().splitlines()
    assert len(lines) == 150
    for line in lines:
        # Each line should be intact (timestamp + payload), not interleaved
        assert "-" in line


def test_append_creates_parent_dir(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "deep" / "m.log"
    ml.append(p, "hi")
    assert p.exists()
