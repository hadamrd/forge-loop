"""Tests for the worker_logs MCP tool + parser (#63)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from forge_loop import worker_logs as wl

# ── Helpers ────────────────────────────────────────────────────────────────


def _write_log(path: Path, events: list[dict]) -> Path:
    """Write a stream-json log: one JSON dict per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")
    return path


def _mixed_events(n: int) -> list[dict]:
    """``n`` events of round-robin kinds."""
    kinds = ["tool_use", "assistant_text", "tool_result", "final_result"]
    out: list[dict] = []
    for i in range(n):
        k = kinds[i % len(kinds)]
        ev: dict = {"kind": k, "i": i}
        if k == "tool_use":
            ev["tool"] = "Bash"
            ev["input"] = {"command": f"echo {i}"}
        elif k == "tool_result":
            ev["content"] = f"out-{i}"
        elif k == "assistant_text":
            ev["text"] = f"reply-{i}"
        elif k == "final_result":
            ev["result"] = f"done-{i}"
        out.append(ev)
    return out


# ── Unit: tail slicing ─────────────────────────────────────────────────────


def test_tail_returns_last_n(tmp_path: Path) -> None:
    log = _write_log(tmp_path / "worker-1-1000.log", _mixed_events(100))
    rows = wl.parse_worker_log(log, tail=10)
    assert len(rows) == 10
    # last row's i should be 99 (oldest of tail = 90)
    assert rows[-1]["i"] == 99
    assert rows[0]["i"] == 90


# ── Unit: kind filter ──────────────────────────────────────────────────────


def test_kind_filter_only_matching(tmp_path: Path) -> None:
    log = _write_log(tmp_path / "worker-2-1000.log", _mixed_events(40))
    rows = wl.parse_worker_log(log, kind_filter="tool_use", tail=100)
    assert rows, "expected at least one tool_use row"
    assert all(r["kind"] == "tool_use" for r in rows)
    # round-robin of 4 kinds × 40 events → 10 tool_use
    assert len(rows) == 10


# ── Unit: nonexistent issue → [] (no error dict) ───────────────────────────


def test_nonexistent_issue_returns_empty(tmp_path: Path) -> None:
    out = wl.read_worker_logs(tmp_path, issue=999_999)
    assert out == []


def test_logs_dir_missing_returns_empty(tmp_path: Path) -> None:
    # logs_dir entirely absent — still no error
    out = wl.read_worker_logs(tmp_path / "does-not-exist", issue=1)
    assert out == []


# ── Unit: attempt=2 → second-most-recent ───────────────────────────────────


def test_attempt_picks_nth_most_recent(tmp_path: Path) -> None:
    # Three attempts for issue #7, distinct mtimes.
    older = _write_log(tmp_path / "worker-7-1000.log", [{"kind": "final_result", "i": "older"}])
    middle = _write_log(tmp_path / "worker-7-2000.log", [{"kind": "final_result", "i": "middle"}])
    newer = _write_log(tmp_path / "worker-7-3000.log", [{"kind": "final_result", "i": "newer"}])
    # Force mtimes so the test is deterministic regardless of filesystem speed.
    now = time.time()
    import os
    os.utime(older, (now - 300, now - 300))
    os.utime(middle, (now - 150, now - 150))
    os.utime(newer, (now, now))

    latest = wl.read_worker_logs(tmp_path, issue=7, attempt=1)
    prev = wl.read_worker_logs(tmp_path, issue=7, attempt=2)
    oldest = wl.read_worker_logs(tmp_path, issue=7, attempt=3)
    default = wl.read_worker_logs(tmp_path, issue=7)

    assert latest[0]["i"] == "newer"
    assert prev[0]["i"] == "middle"
    assert oldest[0]["i"] == "older"
    assert default == latest

    # attempt past end → empty (not error)
    assert wl.read_worker_logs(tmp_path, issue=7, attempt=99) == []


# ── Unit: truncation of fat payloads ───────────────────────────────────────


def test_large_tool_use_input_truncated(tmp_path: Path) -> None:
    big = "x" * 5000
    log = _write_log(
        tmp_path / "worker-3-1000.log",
        [{"kind": "tool_use", "tool": "Write", "input": {"file_text": big}}],
    )
    rows = wl.parse_worker_log(log, tail=10)
    assert len(rows) == 1
    val = rows[0]["input"]
    assert isinstance(val, str)
    # 500 chars + ellipsis marker
    assert val.endswith("…[truncated]")
    assert len(val) <= 500 + len("…[truncated]")
    # Original raw value should not leak (5000 chars worth)
    assert len(val) < 5000


def test_large_tool_result_content_truncated(tmp_path: Path) -> None:
    big = "y" * 3000
    log = _write_log(
        tmp_path / "worker-4-1000.log",
        [{"kind": "tool_result", "tool_use_id": "abc", "content": big}],
    )
    rows = wl.parse_worker_log(log, tail=10)
    assert rows[0]["content"].endswith("…[truncated]")
    assert len(rows[0]["content"]) <= 500 + len("…[truncated]")


def test_small_payloads_left_alone(tmp_path: Path) -> None:
    log = _write_log(
        tmp_path / "worker-5-1000.log",
        [
            {"kind": "tool_use", "tool": "Bash", "input": {"command": "ls"}},
            {"kind": "assistant_text", "text": "hi"},
        ],
    )
    rows = wl.parse_worker_log(log, tail=10)
    # input was a dict — small payloads still get JSON-stringified.
    assert "ls" in rows[0]["input"]
    assert "…[truncated]" not in rows[0]["input"]
    # assistant_text is left alone (not on the truncation list)
    assert rows[1]["text"] == "hi"


# ── Adversarial: blank lines + malformed JSON tolerated ────────────────────


def test_malformed_lines_are_skipped(tmp_path: Path) -> None:
    log = tmp_path / "worker-6-1000.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8") as f:
        f.write("\n")
        f.write("not json at all\n")
        f.write(json.dumps({"kind": "final_result", "result": "ok"}) + "\n")
        f.write("{not-quite-json}\n")
        f.write("\n")
    rows = wl.parse_worker_log(log, tail=50)
    assert len(rows) == 1
    assert rows[0]["kind"] == "final_result"


# ── Integration: fixture worker + MCP tool wrapper ─────────────────────────


def test_mcp_tool_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn a fixture log with 3 events of each known kind; call the MCP
    tool with each filter and assert the slicing matches.
    """
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    kinds = ["tool_use", "assistant_text", "tool_result", "final_result", "error"]
    events: list[dict] = []
    for k in kinds:
        for j in range(3):
            ev: dict = {"kind": k, "j": j}
            if k == "tool_use":
                ev["tool"] = "Bash"
                ev["input"] = {"cmd": f"step-{j}"}
            elif k == "tool_result":
                ev["content"] = f"out-{j}"
            elif k == "assistant_text":
                ev["text"] = f"msg-{j}"
            elif k == "final_result":
                ev["result"] = f"r-{j}"
            elif k == "error":
                ev["error_type"] = "x"
                ev["message"] = f"boom-{j}"
            events.append(ev)
    _write_log(logs_dir / "worker-42-12345.log", events)

    # Patch load_config so the MCP tool sees our tmp logs_dir without
    # requiring a real config + repo on disk.
    from forge_loop import mcp_server

    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.logs_dir = logs_dir  # type: ignore[attr-defined]
    monkeypatch.setattr(mcp_server, "load_config", lambda: cfg)

    # FastMCP's `@mcp.tool()` returns the underlying callable unmodified, so
    # we can just call it directly in tests.
    tool = mcp_server.worker_logs

    all_rows = tool(issue=42, tail=100)
    assert len(all_rows) == 15

    for k in kinds:
        rows = tool(issue=42, kind_filter=k, tail=100)
        assert len(rows) == 3, f"expected 3 {k} rows, got {len(rows)}"
        assert all(r["kind"] == k for r in rows)

    # tail caps the count
    tailed = tool(issue=42, tail=5)
    assert len(tailed) == 5
    # last event was the last 'error' row
    assert tailed[-1]["kind"] == "error"
    assert tailed[-1]["j"] == 2

    # nonexistent issue → []
    assert tool(issue=4242) == []
