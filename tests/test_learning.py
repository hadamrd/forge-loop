"""Unit tests for the learning loop: episodic memory from merged outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from forge_loop.memory.models import MemoryKind
from forge_loop.memory.store import SqliteMemoryStore
from forge_loop.runner.learning import record_merged_outcomes

_NOW = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)


@dataclass
class _FakeOutcome:
    issue: int
    title: str
    pr_url: str | None = None


def test_record_promotes_episodic_item(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")

    promoted = record_merged_outcomes(
        store,
        [_FakeOutcome(issue=42, title="add widget", pr_url="https://x/pr/1")],
        now=_NOW,
    )

    assert promoted == ("episodic-shipped-42",)

    item = store.get("episodic-shipped-42")
    assert item is not None
    assert item.kind is MemoryKind.EPISODIC
    assert item.title == "shipped #42: add widget"
    assert "https://x/pr/1" in item.body
    assert item.provenance.source_task_ref == "issue:#42"
    assert item.provenance.authored_by == "maestro"
    assert 0.0 <= item.provenance.confidence <= 1.0


def test_record_is_idempotent_on_rerun(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")
    merged = [_FakeOutcome(issue=7, title="fix bug")]

    first = record_merged_outcomes(store, merged, now=_NOW)
    second = record_merged_outcomes(store, merged, now=_NOW)

    assert first == second == ("episodic-shipped-7",)

    episodic = store.list_active(kind=MemoryKind.EPISODIC)
    assert len(episodic) == 1
    assert episodic[0].memory_id == "episodic-shipped-7"


def test_recorded_item_is_listable_as_active(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")

    record_merged_outcomes(
        store,
        [
            _FakeOutcome(issue=1, title="first"),
            _FakeOutcome(issue=2, title="second"),
        ],
        now=_NOW,
    )

    active = store.list_active()
    ids = {item.memory_id for item in active}
    assert ids == {"episodic-shipped-1", "episodic-shipped-2"}
    assert all(item.is_active for item in active)


def test_record_accepts_mapping_records(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")

    promoted = record_merged_outcomes(
        store,
        [{"issue": 9, "title": "from dict", "pr_url": "https://x/pr/9"}],
        now=_NOW,
    )

    assert promoted == ("episodic-shipped-9",)
    item = store.get("episodic-shipped-9")
    assert item is not None
    assert item.title == "shipped #9: from dict"


def test_record_dedupes_within_one_call(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")

    promoted = record_merged_outcomes(
        store,
        [
            _FakeOutcome(issue=5, title="a"),
            _FakeOutcome(issue=5, title="b"),
        ],
        now=_NOW,
    )

    assert promoted == ("episodic-shipped-5",)
    assert len(store.list_active(kind=MemoryKind.EPISODIC)) == 1
