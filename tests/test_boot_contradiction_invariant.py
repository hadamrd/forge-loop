"""Boot-time no-contradiction invariant (issue #284).

A maestro resuming from ``list_active`` assumes the loaded load-bearing
decisions are mutually consistent. ``assert_no_active_contradictions`` makes
that assumption *checked*: if two contradictory load-bearing items were ever
both admitted (e.g. via a direct ``put`` bypassing the curator), boot fails
loudly naming both offenders instead of silently acting on a split frontier.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_loop.control.boot import (
    BootContextError,
    BootSources,
    assemble_boot_context,
    assert_no_active_contradictions,
)
from forge_loop.eventlog import SqliteEventLog
from forge_loop.frontier import FrontierCursor, FrontierStore
from forge_loop.memory import (
    LOAD_BEARING_TAG,
    MemoryItem,
    MemoryKind,
    MemoryProvenance,
    SqliteMemoryStore,
    axis_tag,
    subject_tag,
)


def _frontier() -> FrontierCursor:
    return FrontierCursor(
        product_goal="make long-running agents recoverable",
        current_problem="boot must reject contradictory load-bearing memory",
        next_expansion="force explicit transitions on contradiction",
        why_now="a split frontier silently corrupts maestro strategy",
        active_decisions=("assemble from explicit projections",),
    )


def _decision(memory_id: str, *, title: str, subject: str = "event-log") -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        kind=MemoryKind.SEMANTIC,
        title=title,
        body="Load-bearing decision about the event log backend.",
        tags=(LOAD_BEARING_TAG, axis_tag("db"), subject_tag(subject)),
        provenance=MemoryProvenance(
            source_event=None,
            authored_by="test",
            source_task_ref="task:#284",
            confidence=0.9,
            evidence_refs=("issue:#284",),
        ),
    )


def _boot(memory_path: Path, frontier_path: Path, eventlog_path: Path) -> BootSources:
    return BootSources(
        frontier_store=FrontierStore(frontier_path),
        event_log=SqliteEventLog(eventlog_path),
        memory_store=SqliteMemoryStore(memory_path),
    )


# --------------------------------------------------------------------------- #
# helper-level unit tests
# --------------------------------------------------------------------------- #
def test_assert_no_active_contradictions_passes_for_coherent_set() -> None:
    items = (
        _decision("mem-postgres", title="use Postgres"),
        _decision("mem-runtime", title="use asyncio", subject="worker"),
    )
    assert_no_active_contradictions(items)  # does not raise


def test_assert_no_active_contradictions_raises_naming_both_offenders() -> None:
    items = (
        _decision("mem-postgres", title="use Postgres"),
        _decision("mem-sqlite", title="use SQLite"),
    )
    with pytest.raises(BootContextError) as excinfo:
        assert_no_active_contradictions(items)
    message = str(excinfo.value)
    assert "mem-postgres" in message
    assert "mem-sqlite" in message


# --------------------------------------------------------------------------- #
# integration: assemble_boot_context
# --------------------------------------------------------------------------- #
def test_boot_context_returns_for_coherent_load_bearing_set(tmp_path: Path) -> None:
    frontier_path = tmp_path / "frontier.yaml"
    FrontierStore(frontier_path).save(_frontier())
    memory_path = tmp_path / "memory.db"
    store = SqliteMemoryStore(memory_path)
    store.put(_decision("mem-postgres", title="use Postgres"))
    store.put(_decision("mem-runtime", title="use asyncio", subject="worker"))

    context = assemble_boot_context(_boot(memory_path, frontier_path, tmp_path / "events.db"))

    assert set(context.active_memory_ids) == {"mem-postgres", "mem-runtime"}


def test_boot_context_passes_after_proper_supersede_transition(tmp_path: Path) -> None:
    frontier_path = tmp_path / "frontier.yaml"
    FrontierStore(frontier_path).save(_frontier())
    memory_path = tmp_path / "memory.db"
    store = SqliteMemoryStore(memory_path)
    store.put(_decision("mem-postgres", title="use Postgres"))
    store.put(_decision("mem-sqlite", title="use SQLite"))
    # Proper transition: only the survivor stays active.
    store.supersede("mem-postgres", by_memory_id="mem-sqlite")

    context = assemble_boot_context(_boot(memory_path, frontier_path, tmp_path / "events.db"))

    assert context.active_memory_ids == ("mem-sqlite",)


# --------------------------------------------------------------------------- #
# adversarial / sad path (the invariant's reason to exist)
# --------------------------------------------------------------------------- #
def test_boot_context_refuses_two_active_contradictory_load_bearing_items(tmp_path: Path) -> None:
    frontier_path = tmp_path / "frontier.yaml"
    FrontierStore(frontier_path).save(_frontier())
    memory_path = tmp_path / "memory.db"
    store = SqliteMemoryStore(memory_path)
    # Bypass the curator's transition gate via direct put — both stay active.
    store.put(_decision("mem-postgres", title="use Postgres"))
    store.put(_decision("mem-sqlite", title="use SQLite"))

    with pytest.raises(BootContextError) as excinfo:
        assemble_boot_context(_boot(memory_path, frontier_path, tmp_path / "events.db"))

    message = str(excinfo.value)
    assert "mem-postgres" in message
    assert "mem-sqlite" in message
