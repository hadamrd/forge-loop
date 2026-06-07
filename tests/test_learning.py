"""Unit tests for the learning loop: episodic memory from merged outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from forge_loop.memory.models import (
    MemoryItem,
    MemoryKind,
    MemoryProvenance,
    derive_skill_key,
    skill_tag,
)
from forge_loop.memory.store import SqliteMemoryStore
from forge_loop.runner.learning import (
    _MAX_REASON_LEN,
    record_failed_outcomes,
    record_merged_outcomes,
    record_procedural_skill,
)

_NOW = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)


@dataclass
class _FakeOutcome:
    issue: int
    title: str
    pr_url: str | None = None


@dataclass
class _FakeFailure:
    issue: int
    title: str
    status: str = "abandoned"
    reason: str | None = None


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


# --- failure-episode memory: record_failed_outcomes --------------------------


def test_record_failed_promotes_episodic_item(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")

    promoted = record_failed_outcomes(
        store,
        [
            _FakeFailure(
                issue=42,
                title="add widget",
                status="abandoned",
                reason="max-iterations cap hit; critic kept blocking sev1",
            )
        ],
        now=_NOW,
    )

    assert promoted == ("episodic-failed-42",)

    item = store.get("episodic-failed-42")
    assert item is not None
    assert item.kind is MemoryKind.EPISODIC
    assert item.title == "failed #42: add widget"
    # body carries the terminal status AND the failure reason as the lesson.
    assert "abandoned" in item.body
    assert "max-iterations cap hit" in item.body
    assert item.tags == ("failed",)
    assert item.provenance.source_task_ref == "issue:#42"
    assert item.provenance.authored_by == "maestro"


def test_record_failed_is_idempotent_on_rerun(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")
    failures = [_FakeFailure(issue=7, title="fix bug", reason="boom")]

    first = record_failed_outcomes(store, failures, now=_NOW)
    second = record_failed_outcomes(store, failures, now=_NOW)

    assert first == second == ("episodic-failed-7",)

    episodic = store.list_active(kind=MemoryKind.EPISODIC)
    assert len(episodic) == 1
    assert episodic[0].memory_id == "episodic-failed-7"


def test_record_failed_accepts_mapping_records(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")

    promoted = record_failed_outcomes(
        store,
        [
            {
                "issue": 9,
                "title": "from dict",
                "status": "abandoned",
                "reason": "no PR pushed",
            }
        ],
        now=_NOW,
    )

    assert promoted == ("episodic-failed-9",)
    item = store.get("episodic-failed-9")
    assert item is not None
    assert item.title == "failed #9: from dict"
    assert "no PR pushed" in item.body


def test_record_failed_truncates_long_reason(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")
    long_reason = "x" * 5000

    record_failed_outcomes(
        store,
        [_FakeFailure(issue=3, title="t", reason=long_reason)],
        now=_NOW,
    )

    item = store.get("episodic-failed-3")
    assert item is not None
    # The reason is truncated to exactly _MAX_REASON_LEN chars: the lesson line
    # carries that many 'x' and no more, pinning the cap rather than the body.
    assert item.body.count("x") == _MAX_REASON_LEN
    assert f"lesson: {'x' * _MAX_REASON_LEN}" in item.body
    assert "x" * (_MAX_REASON_LEN + 1) not in item.body


def test_record_failed_skips_records_without_issue_number(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")

    promoted = record_failed_outcomes(
        store,
        [
            {"title": "no issue", "status": "abandoned", "reason": "x"},
            _FakeFailure(issue=11, title="ok", reason="r"),
        ],
        now=_NOW,
    )

    assert promoted == ("episodic-failed-11",)
    assert len(store.list_active(kind=MemoryKind.EPISODIC)) == 1


def test_record_failed_skips_non_numeric_issue(tmp_path: Path) -> None:
    # A non-numeric issue value cannot form a deterministic id, so the record is
    # skipped by ``_coerce_issue``'s int() guard rather than crashing the loop.
    store = SqliteMemoryStore(tmp_path / "memory.db")

    promoted = record_failed_outcomes(
        store,
        [{"issue": "abc", "status": "abandoned", "reason": "x"}],
        now=_NOW,
    )

    assert promoted == ()
    assert len(store.list_active(kind=MemoryKind.EPISODIC)) == 0


def test_record_failed_handles_missing_reason(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")

    promoted = record_failed_outcomes(
        store,
        [_FakeFailure(issue=15, title="silent", status="abandoned", reason=None)],
        now=_NOW,
    )

    assert promoted == ("episodic-failed-15",)
    item = store.get("episodic-failed-15")
    assert item is not None
    assert "abandoned" in item.body


def test_record_failed_dedupes_within_one_call(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")

    promoted = record_failed_outcomes(
        store,
        [
            _FakeFailure(issue=5, title="a", reason="first"),
            _FakeFailure(issue=5, title="b", reason="second"),
        ],
        now=_NOW,
    )

    assert promoted == ("episodic-failed-5",)
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


# --- Procedural skill-key supersession (issue #359) ---------------------------

_SIGNAL = "ImportError: cannot import name X"
_TARGET = "src/foo/bar.py"


def _write_skill(store: SqliteMemoryStore, *, source_key: str, body: str) -> str:
    """Write one procedural skill for the shared (_SIGNAL, _TARGET) signature."""
    return record_procedural_skill(
        store,
        failing_signal=_SIGNAL,
        target=_TARGET,
        title="repair ImportError in bar.py",
        body=body,
        source_key=source_key,
        source_task_ref="issue:#359",
        now=_NOW,
    )


def test_same_skill_key_leaves_one_active_one_superseded(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")

    old_id = _write_skill(store, source_key="repair:monday", body="monday recipe")
    new_id = _write_skill(store, source_key="repair:thursday", body="thursday recipe")

    assert old_id != new_id

    active = store.list_active(kind=MemoryKind.PROCEDURAL)
    assert len(active) == 1
    assert active[0].memory_id == new_id

    # The superseded item is still retrievable with its lineage intact.
    old = store.get(old_id)
    assert old is not None
    assert old.is_active is False
    assert old.superseded_by == new_id

    # Provenance lineage preserved on the new item.
    new = store.get(new_id)
    assert new is not None
    assert old_id in new.provenance.supersedes


def test_different_skill_keys_keep_both_active(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")

    first = record_procedural_skill(
        store,
        failing_signal=_SIGNAL,
        target=_TARGET,
        title="repair bar.py",
        body="recipe a",
        source_key="repair:a",
        source_task_ref="issue:#359",
        now=_NOW,
    )
    second = record_procedural_skill(
        store,
        failing_signal="TypeError: bad arg",
        target="src/foo/qux.py",
        title="repair qux.py",
        body="recipe b",
        source_key="repair:b",
        source_task_ref="issue:#359",
        now=_NOW,
    )

    active = store.list_active(kind=MemoryKind.PROCEDURAL)
    assert {item.memory_id for item in active} == {first, second}
    assert store.get(first).superseded_by is None  # type: ignore[union-attr]
    assert store.get(second).superseded_by is None  # type: ignore[union-attr]


def test_third_write_supersedes_only_currently_active(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")

    first = _write_skill(store, source_key="repair:1", body="r1")
    second = _write_skill(store, source_key="repair:2", body="r2")
    third = _write_skill(store, source_key="repair:3", body="r3")

    # Chain stays linear: first -> second -> third, no double-supersede.
    assert store.get(first).superseded_by == second  # type: ignore[union-attr]
    assert store.get(second).superseded_by == third  # type: ignore[union-attr]
    third_item = store.get(third)
    assert third_item is not None
    assert third_item.is_active is True

    # Third supersedes ONLY the currently-active (second), never re-points first.
    assert third_item.provenance.supersedes == (second,)

    active = store.list_active(kind=MemoryKind.PROCEDURAL)
    assert len(active) == 1
    assert active[0].memory_id == third


def test_idempotent_rerun_same_source_key_does_not_self_supersede(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")

    first = _write_skill(store, source_key="repair:same", body="recipe")
    again = _write_skill(store, source_key="repair:same", body="recipe v2")

    assert first == again
    active = store.list_active(kind=MemoryKind.PROCEDURAL)
    assert len(active) == 1
    item = store.get(first)
    assert item is not None
    assert item.is_active is True
    assert item.provenance.supersedes == ()  # never superseded itself


def test_skill_key_collision_on_other_kind_is_ignored(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")

    # A SEMANTIC item that coincidentally bears the same skill-key tag.
    skill_key = derive_skill_key(_SIGNAL, _TARGET)
    semantic_id = "semantic-coincidence"
    store.put(
        MemoryItem(
            memory_id=semantic_id,
            kind=MemoryKind.SEMANTIC,
            title="unrelated semantic note",
            body="a fact that happens to share the tag",
            provenance=MemoryProvenance(
                source_event=None,
                authored_by="maestro",
                source_task_ref="issue:#359",
                confidence=1.0,
                created_at=_NOW,
            ),
            tags=(skill_tag(skill_key),),
        )
    )

    new_id = _write_skill(store, source_key="repair:proc", body="recipe")

    # The non-procedural item is untouched (kind-scoped lookup).
    semantic = store.get(semantic_id)
    assert semantic is not None
    assert semantic.is_active is True
    assert semantic.superseded_by is None
    # The procedural item supersedes nothing.
    new = store.get(new_id)
    assert new is not None
    assert new.provenance.supersedes == ()
