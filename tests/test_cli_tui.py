"""Textual TUI tests (issue #47).

Smoke-test the dashboard TUI launched via ``forge-loop dashboard --tui``.
We use Textual's built-in async test harness (``App.run_test``) so the
test does not require a real terminal.

Covers:
* the app boots and the four panels render (events / queue / workers /
  budget),
* pressing the ``k`` keybinding fires ``WorkerKillRequested`` AND
  appends a JSONL line to the kill-request sink the runner watches,
* the helpers (``_tail_jsonl``, ``_compute_inflight``, ``_compute_budget``)
  behave sensibly on empty / malformed input (adversarial).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

textual = pytest.importorskip("textual")

from forge_loop import cli_tui  # noqa: E402

# ---------------------------------------------------------------------------
# Pure helpers — adversarial inputs.
# ---------------------------------------------------------------------------


def test_tail_jsonl_missing_file(tmp_path: Path) -> None:
    assert cli_tui._tail_jsonl(tmp_path / "nope.jsonl") == []


def test_tail_jsonl_skips_corrupt_lines(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    p.write_text(
        '{"kind": "tick_start", "ts": "2025-01-01T00:00:00Z"}\n'
        "not-json\n"
        '{"kind": "tick_done", "ts": "2025-01-01T00:00:01Z"}\n'
    )
    out = cli_tui._tail_jsonl(p, n=10)
    assert [e["kind"] for e in out] == ["tick_start", "tick_done"]


def test_compute_inflight_pairs_start_with_terminal() -> None:
    events = [
        {"kind": "worker_start", "issue": 1, "ts": "00:00:00"},
        {"kind": "worker_start", "issue": 2, "ts": "00:00:01"},
        {"kind": "worker_done", "issue": 1, "ts": "00:00:02"},
        {"kind": "worker_start", "issue": 3, "ts": "00:00:03"},
    ]
    inflight = cli_tui._compute_inflight(events)
    assert set(inflight) == {2, 3}


def test_compute_inflight_handles_empty_and_bad_payloads() -> None:
    assert cli_tui._compute_inflight([]) == {}
    # Issue field missing or wrong type → silently skipped.
    assert cli_tui._compute_inflight(
        [{"kind": "worker_start"}, {"kind": "worker_start", "issue": "nope"}]
    ) == {}


def test_compute_budget_sums_costs() -> None:
    events = [
        {"kind": "worker_done", "cost_usd": 0.10},
        {"kind": "critic_done", "cost_usd": 0.05},
        {"kind": "po_done", "cost_usd": "0.01"},  # string-cost is OK
        {"kind": "budget_cap", "cap_usd": 5.0},
        {"kind": "worker_done", "cost_usd": "bad"},  # ignored
    ]
    out = cli_tui._compute_budget(events)
    assert out["cap"] == 5.0
    assert out["spent"] == pytest.approx(0.16, rel=1e-3)


def test_compute_queue_depth_missing_cache(tmp_path: Path) -> None:
    assert cli_tui._compute_queue_depth(tmp_path) == -1


def test_compute_queue_depth_reads_cache(tmp_path: Path) -> None:
    (tmp_path / "queue-depth.cache").write_text("7\n")
    assert cli_tui._compute_queue_depth(tmp_path) == 7


# ---------------------------------------------------------------------------
# Textual harness — async smoke + kill keybinding.
# ---------------------------------------------------------------------------


def _run_async(coro):  # type: ignore[no-untyped-def]
    """Tiny sync wrapper so the suite doesn't need pytest-asyncio."""
    import asyncio

    return asyncio.run(coro)


def test_tui_renders_panels_and_kill_fires(tmp_path: Path) -> None:
    """Spin the app up; verify panels render + ``k`` publishes a kill event."""
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps({"kind": "worker_start", "issue": 11, "ts": "00:00:00"}) + "\n"
    )
    sink = tmp_path / "kill-requests.jsonl"

    app = cli_tui.ForgeLoopTUI(
        events_file=events_path,
        state_dir=tmp_path,
        kill_event_sink=sink,
        tick_interval=0.05,
    )

    async def body() -> None:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            assert "worker_start" in app._events_panel.last_text
            assert "#11" in app._workers_panel.last_text
            await pilot.press("k")
            await pilot.pause(0.05)

    _run_async(body())

    assert sink.exists()
    line = sink.read_text().strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload == {"kind": "worker_kill_requested", "issue": 11}


def test_tui_kill_with_no_workers_is_noop(tmp_path: Path) -> None:
    """Adversarial: ``k`` with zero in-flight workers must not crash or write."""
    events_path = tmp_path / "events.jsonl"
    events_path.write_text("")
    sink = tmp_path / "kill-requests.jsonl"

    app = cli_tui.ForgeLoopTUI(
        events_file=events_path,
        state_dir=tmp_path,
        kill_event_sink=sink,
        tick_interval=0.05,
    )

    async def body() -> None:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("k")
            await pilot.pause(0.05)

    _run_async(body())
    assert not sink.exists()
