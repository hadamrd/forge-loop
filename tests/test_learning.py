"""Unit tests for the learning loop: episodic memory from merged outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from forge_loop.memory.models import MemoryItem, MemoryKind, MemoryProvenance
from forge_loop.memory.store import SqliteMemoryStore
from forge_loop.runner.learning import record_merged_outcomes

_NOW = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)


@dataclass
class _FakeOutcome:
    issue: int
    title: str
    pr_url: str | None = None


def _seed_failure_episode(store: SqliteMemoryStore, issue: int) -> str:
    """Pre-seed an active ``episodic-failed-{issue}`` item (mirrors #346)."""
    memory_id = f"episodic-failed-{issue}"
    store.put(
        MemoryItem(
            memory_id=memory_id,
            kind=MemoryKind.EPISODIC,
            title=f"failed #{issue}",
            body=f"ticket #{issue} failed to ship",
            provenance=MemoryProvenance(
                source_event=None,
                authored_by="maestro",
                source_task_ref=f"issue:#{issue}",
                confidence=1.0,
                created_at=_NOW,
            ),
            tags=("failed",),
        )
    )
    return memory_id


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


def test_merge_supersedes_existing_failure_episode(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")
    _seed_failure_episode(store, 42)

    record_merged_outcomes(store, [_FakeOutcome(issue=42, title="finally")], now=_NOW)

    failure = store.get("episodic-failed-42")
    assert failure is not None
    assert failure.superseded_by == "episodic-shipped-42"
    assert failure.is_active is False

    shipped = store.get("episodic-shipped-42")
    assert shipped is not None
    assert shipped.superseded_by is None
    assert shipped.is_active is True

    active_ids = {item.memory_id for item in store.list_active(kind=MemoryKind.EPISODIC)}
    assert "episodic-shipped-42" in active_ids
    assert "episodic-failed-42" not in active_ids


def test_merge_without_failure_episode_does_not_supersede(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")

    promoted = record_merged_outcomes(store, [_FakeOutcome(issue=7, title="fresh")], now=_NOW)

    assert promoted == ("episodic-shipped-7",)
    assert store.get("episodic-failed-7") is None
    active = store.list_active(kind=MemoryKind.EPISODIC)
    assert {item.memory_id for item in active} == {"episodic-shipped-7"}


def test_merge_supersedes_failure_for_each_issue_in_batch(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")
    _seed_failure_episode(store, 1)
    _seed_failure_episode(store, 2)

    record_merged_outcomes(
        store,
        [_FakeOutcome(issue=1, title="one"), _FakeOutcome(issue=2, title="two")],
        now=_NOW,
    )

    for issue in (1, 2):
        failure = store.get(f"episodic-failed-{issue}")
        assert failure is not None
        assert failure.superseded_by == f"episodic-shipped-{issue}"
        assert failure.is_active is False

    active_ids = {item.memory_id for item in store.list_active(kind=MemoryKind.EPISODIC)}
    assert active_ids == {"episodic-shipped-1", "episodic-shipped-2"}


def test_already_superseded_failure_is_idempotent_on_rerun(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")
    _seed_failure_episode(store, 13)
    merged = [_FakeOutcome(issue=13, title="ship it")]

    first = record_merged_outcomes(store, merged, now=_NOW)
    # Second run must not raise even though the failure episode is already
    # superseded by ``episodic-shipped-13`` (the guard skips inactive items).
    second = record_merged_outcomes(store, merged, now=_NOW)

    assert first == second == ("episodic-shipped-13",)
    failure = store.get("episodic-failed-13")
    assert failure is not None
    assert failure.superseded_by == "episodic-shipped-13"
    assert failure.is_active is False


def test_merge_when_failure_already_superseded_by_other(tmp_path: Path) -> None:
    """An already-superseded failure (by some other id) is left untouched.

    The merged-outcome path only re-points failure episodes that are still
    active, so a pre-existing supersession is never silently corrupted.
    """
    store = SqliteMemoryStore(tmp_path / "memory.db")
    _seed_failure_episode(store, 99)
    # Some other promoted item already superseded the failure episode.
    store.put(
        MemoryItem(
            memory_id="episodic-other-99",
            kind=MemoryKind.EPISODIC,
            title="other",
            body="other reason the failure no longer applies",
            provenance=MemoryProvenance(
                source_event=None,
                authored_by="maestro",
                source_task_ref="issue:#99",
                confidence=1.0,
                created_at=_NOW,
            ),
        )
    )
    store.supersede("episodic-failed-99", by_memory_id="episodic-other-99")

    record_merged_outcomes(store, [_FakeOutcome(issue=99, title="merged")], now=_NOW)

    failure = store.get("episodic-failed-99")
    assert failure is not None
    # Untouched: still points at the pre-existing superseder, not the shipped id.
    assert failure.superseded_by == "episodic-other-99"
    assert failure.is_active is False
    # The shipped item is still recorded and active.
    shipped = store.get("episodic-shipped-99")
    assert shipped is not None
    assert shipped.is_active is True
