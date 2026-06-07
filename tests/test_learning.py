"""Unit tests for the learning loop: episodic memory from merged outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from forge_loop.memory.models import MemoryItem, MemoryKind, MemoryProvenance
from forge_loop.memory.store import SqliteMemoryStore
from forge_loop.runner.learning import (
    _MAX_REASON_LEN,
    record_failed_outcomes,
    record_merged_outcomes,
    record_repair_recipe,
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


@dataclass
class _FakeRepair:
    issue: int
    title: str
    failing_signal: str
    fix: str
    touched: object
    event_id: str
    event_sequence: int


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


# --- procedural repair recipe: record_repair_recipe (#358) -------------------


def _repair(issue: int = 42, **overrides: object) -> _FakeRepair:
    base: dict[str, object] = {
        "issue": issue,
        "title": "fix flaky retry",
        "failing_signal": "critic sev2: N+1 gh call in repair loop",
        "fix": "hoist GhClient.list_prs out of the per-issue loop",
        "touched": "src/forge_loop/runner/repairs.py",
        "event_id": "evt-repair-001",
        "event_sequence": 7,
    }
    base.update(overrides)
    return _FakeRepair(**base)  # type: ignore[arg-type]


def test_repair_recipe_writes_one_procedural_item(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")

    memory_id = record_repair_recipe(store, _repair(issue=42), now=_NOW)

    assert memory_id == "procedural-repair-42"
    item = store.get("procedural-repair-42")
    assert item is not None
    assert item.kind is MemoryKind.PROCEDURAL
    assert item.title == "repair #42: fix flaky retry"
    # Compact recipe carries failing signal, the named fix, and the file touched.
    assert "critic sev2: N+1 gh call in repair loop" in item.body
    assert "hoist GhClient.list_prs out of the per-issue loop" in item.body
    assert "src/forge_loop/runner/repairs.py" in item.body
    assert item.tags == ("repair",)


def test_repair_recipe_provenance_references_originating_event(tmp_path: Path) -> None:
    # The falsifiable acceptance criterion: after a repair tick lands a passing
    # critic, list_active(kind=PROCEDURAL) returns exactly one new item whose
    # provenance references that tick's event.
    store = SqliteMemoryStore(tmp_path / "memory.db")

    record_repair_recipe(store, _repair(issue=5, event_id="evt-tick-9", event_sequence=12), now=_NOW)

    procedural = store.list_active(kind=MemoryKind.PROCEDURAL)
    assert len(procedural) == 1
    item = procedural[0]
    assert item.provenance.source_event is not None
    assert item.provenance.source_event.event_id == "evt-tick-9"
    assert item.provenance.source_event.sequence == 12
    assert item.provenance.source_task_ref == "issue:#5"
    assert item.provenance.authored_by == "maestro"


def test_repair_recipe_is_idempotent_on_rerun(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")
    repair = _repair(issue=7)

    first = record_repair_recipe(store, repair, now=_NOW)
    second = record_repair_recipe(store, repair, now=_NOW)

    assert first == second == "procedural-repair-7"
    assert len(store.list_active(kind=MemoryKind.PROCEDURAL)) == 1


def test_repair_recipe_accepts_mapping_records(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")

    memory_id = record_repair_recipe(
        store,
        {
            "issue": 9,
            "title": "from dict",
            "failing_signal": "pyright: missing return",
            "fix": "add explicit None return",
            "touched": "src/forge_loop/foo.py",
            "event_id": "evt-dict-1",
            "sequence": 3,
        },
        now=_NOW,
    )

    assert memory_id == "procedural-repair-9"
    item = store.get("procedural-repair-9")
    assert item is not None
    assert item.title == "repair #9: from dict"
    assert item.provenance.source_event is not None
    assert item.provenance.source_event.sequence == 3


def test_repair_recipe_touched_accepts_iterable(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")

    record_repair_recipe(
        store,
        _repair(issue=4, touched=["src/forge_loop/a.py", "tests/test_a.py"]),
        now=_NOW,
    )

    item = store.get("procedural-repair-4")
    assert item is not None
    assert "src/forge_loop/a.py" in item.body
    assert "tests/test_a.py" in item.body


def test_repair_recipe_skips_record_without_issue_number(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")

    memory_id = record_repair_recipe(
        store,
        {
            "title": "no issue",
            "failing_signal": "x",
            "fix": "y",
            "event_id": "evt-1",
            "sequence": 1,
        },
        now=_NOW,
    )

    assert memory_id is None
    assert store.list_active(kind=MemoryKind.PROCEDURAL) == ()


def test_repair_recipe_skips_record_without_event_reference(tmp_path: Path) -> None:
    # Adversarial: provenance MUST reference the originating event. A record with
    # no event_id cannot satisfy the contract, so it is skipped rather than
    # written with empty provenance.
    store = SqliteMemoryStore(tmp_path / "memory.db")

    memory_id = record_repair_recipe(
        store,
        {"issue": 11, "title": "no event", "failing_signal": "x", "fix": "y"},
        now=_NOW,
    )

    assert memory_id is None
    assert store.list_active(kind=MemoryKind.PROCEDURAL) == ()


def test_repair_recipe_skips_non_numeric_event_sequence(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")

    memory_id = record_repair_recipe(
        store,
        {"issue": 12, "event_id": "evt-1", "sequence": "not-a-number"},
        now=_NOW,
    )

    assert memory_id is None
    assert store.list_active(kind=MemoryKind.PROCEDURAL) == ()


def test_repair_recipe_truncates_long_fields(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")
    long_fix = "z" * 5000

    record_repair_recipe(store, _repair(issue=3, fix=long_fix), now=_NOW)

    item = store.get("procedural-repair-3")
    assert item is not None
    # The fix is truncated to exactly _MAX_REASON_LEN chars, pinning the cap.
    assert item.body.count("z") == _MAX_REASON_LEN
    assert "z" * (_MAX_REASON_LEN + 1) not in item.body


def test_repair_recipe_does_not_collide_with_episodic_kinds(tmp_path: Path) -> None:
    # The procedural recipe is a distinct kind; recording one alongside a shipped
    # episode leaves exactly one active item of each kind.
    store = SqliteMemoryStore(tmp_path / "memory.db")

    record_merged_outcomes(store, [_FakeOutcome(issue=21, title="shipped it")], now=_NOW)
    record_repair_recipe(store, _repair(issue=21), now=_NOW)

    assert len(store.list_active(kind=MemoryKind.EPISODIC)) == 1
    assert len(store.list_active(kind=MemoryKind.PROCEDURAL)) == 1
