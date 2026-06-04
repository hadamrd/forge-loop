from __future__ import annotations

from pathlib import Path

import pytest

from forge_loop._testing.memory_store import FakeMemoryStore
from forge_loop.eventlog.models import EventId, EventRef
from forge_loop.memory.curator import MemoryCurator, PromotionCandidate
from forge_loop.memory.models import (
    REJECTED_PATH_TAG,
    MemoryItem,
    MemoryKind,
    MemoryProvenance,
    axis_from_tags,
    axis_tag,
    derive_memory_id,
)
from forge_loop.memory.store import SqliteMemoryStore


def _item(
    memory_id: str,
    kind: MemoryKind,
    *,
    title: str | None = None,
    body: str = "Durable memory keeps load-bearing context out of transcripts.",
    tags: tuple[str, ...] = ("boot-context",),
    source_event: EventRef | None = None,
    source_task_ref: str | None = "task:#171",
    supersedes: tuple[str, ...] = (),
) -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        kind=kind,
        title=title or f"{kind.value} memory",
        body=body,
        tags=tags,
        provenance=MemoryProvenance(
            source_event=source_event,
            authored_by="test",
            source_task_ref=source_task_ref,
            confidence=0.9,
            supersedes=supersedes,
            evidence_refs=("issue:#171",),
        ),
    )


def test_active_semantic_episodic_and_procedural_items_round_trip_after_reopen(
    tmp_path: Path,
) -> None:
    db = tmp_path / "memory.db"
    source_ref = EventRef(event_id=EventId("evt-1"), sequence=17)
    first = SqliteMemoryStore(db)
    expected = (
        _item(
            "mem-semantic",
            MemoryKind.SEMANTIC,
            title="SQLite is the durable memory backend",
            source_event=source_ref,
        ),
        _item("mem-episodic", MemoryKind.EPISODIC, title="Issue 171 added memory"),
        _item(
            "mem-procedural",
            MemoryKind.PROCEDURAL,
            title="Run focused memory gates",
            source_task_ref="task:#171",
        ),
    )

    for item in expected:
        first.put(item)

    reopened = SqliteMemoryStore(db)
    active = reopened.list_active()

    assert [item.memory_id for item in active] == [
        "mem-semantic",
        "mem-episodic",
        "mem-procedural",
    ]
    assert {item.kind for item in active} == {
        MemoryKind.SEMANTIC,
        MemoryKind.EPISODIC,
        MemoryKind.PROCEDURAL,
    }
    assert active[0].provenance.source_event == source_ref
    assert active[0].provenance.evidence_refs == ("issue:#171",)
    assert active[2].provenance.source_task_ref == "task:#171"
    assert reopened.list_active(kind=MemoryKind.PROCEDURAL)[0].memory_id == "mem-procedural"


def test_superseding_memory_excludes_old_item_from_active_but_keeps_it_inspectable(
    tmp_path: Path,
) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")
    old = _item(
        "mem-old",
        MemoryKind.SEMANTIC,
        title="Use YAML for memory",
        body="YAML was considered before durable supersession needed transactions.",
        tags=(REJECTED_PATH_TAG, "storage"),
    )
    new = _item(
        "mem-new",
        MemoryKind.SEMANTIC,
        title="Use SQLite for memory",
        body="SQLite preserves durable reopen and supersession state explicitly.",
        tags=("storage",),
        supersedes=("mem-old",),
    )

    store.put(old)
    store.put(new)
    store.supersede("mem-old", by_memory_id="mem-new")

    assert [item.memory_id for item in store.list_active()] == ["mem-new"]
    superseded = store.get("mem-old")
    assert superseded is not None
    assert superseded.superseded_by == "mem-new"
    assert not superseded.is_active
    replacement = store.get("mem-new")
    assert replacement is not None
    assert replacement.provenance.supersedes == ("mem-old",)


def test_rejected_path_query_returns_only_active_rejected_path_memories(
    tmp_path: Path,
) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")
    active_rejected = _item(
        "mem-rejected",
        MemoryKind.SEMANTIC,
        title="Rejected transcript archive",
        body="Raw transcripts were rejected because curated memory must be compact.",
        tags=(REJECTED_PATH_TAG, "memory"),
    )
    active_non_rejected = _item(
        "mem-normal",
        MemoryKind.SEMANTIC,
        title="Curated memory exists",
        tags=("memory",),
    )
    superseded_rejected = _item(
        "mem-rejected-old",
        MemoryKind.SEMANTIC,
        title="Rejected old storage",
        tags=(REJECTED_PATH_TAG,),
    )

    store.put(active_rejected)
    store.put(active_non_rejected)
    store.put(superseded_rejected)
    store.supersede("mem-rejected-old", by_memory_id="mem-rejected")

    assert [item.memory_id for item in store.list_rejected_paths()] == ["mem-rejected"]


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_invalid_confidence_is_rejected(confidence: float) -> None:
    with pytest.raises(ValueError, match="confidence"):
        MemoryProvenance(
            source_event=EventRef(event_id=EventId("evt-1"), sequence=1),
            authored_by="test",
            confidence=confidence,
        )


def test_invalid_provenance_is_rejected() -> None:
    with pytest.raises(ValueError, match="provenance"):
        MemoryProvenance(
            source_event=None,
            authored_by=" ",
            source_task_ref="task:#171",
            confidence=0.7,
        )


def test_missing_source_provenance_is_rejected() -> None:
    with pytest.raises(ValueError, match="source event or source task"):
        MemoryProvenance(source_event=None, authored_by="test", confidence=0.7)

    with pytest.raises(ValueError, match="source event or source task"):
        MemoryProvenance(
            source_event=None,
            authored_by="test",
            source_task_ref=" ",
            confidence=0.7,
        )


def test_curator_does_not_promote_empty_reason_candidates() -> None:
    curator = MemoryCurator()
    candidate = PromotionCandidate(
        title="Command output",
        body="The worker ran ls.",
        reason_to_remember="   ",
        tags=("noise",),
    )

    assert not curator.should_promote(candidate)


def test_curator_promotes_to_configured_store(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")
    curator = MemoryCurator(store)
    item = _item("mem-curated", MemoryKind.SEMANTIC)

    promoted = curator.promote(item)

    assert promoted == item
    assert store.get("mem-curated") == item


def test_sqlite_store_creates_parent_directories(tmp_path: Path) -> None:
    db = tmp_path / ".forge" / "memory.db"

    store = SqliteMemoryStore(db)
    store.put(_item("mem-parent", MemoryKind.SEMANTIC))

    assert db.exists()
    assert SqliteMemoryStore(db).get("mem-parent") is not None


def test_open_memory_store_resolves_canonical_forge_path(tmp_path: Path) -> None:
    """The consolidated helper resolves the single ``.forge/memory.db`` path and
    constructs a working store, so the three CLI factories cannot drift."""
    from forge_loop.memory import memory_db_path, open_memory_store

    assert memory_db_path(tmp_path) == tmp_path / ".forge" / "memory.db"

    store = open_memory_store(tmp_path)
    assert isinstance(store, SqliteMemoryStore)
    assert store.path == tmp_path / ".forge" / "memory.db"
    store.put(_item("mem-helper", MemoryKind.SEMANTIC))
    assert open_memory_store(tmp_path).get("mem-helper") is not None


def test_superseding_missing_memory_raises_keyerror_for_real_and_fake_stores(
    tmp_path: Path,
) -> None:
    real = SqliteMemoryStore(tmp_path / "memory.db")
    fake = FakeMemoryStore()

    for store in (real, fake):
        store.put(_item("mem-new", MemoryKind.SEMANTIC))
        with pytest.raises(KeyError, match="mem-missing"):
            store.supersede("mem-missing", by_memory_id="mem-new")


def test_superseding_to_missing_replacement_raises_without_mutating(
    tmp_path: Path,
) -> None:
    real = SqliteMemoryStore(tmp_path / "memory.db")
    fake = FakeMemoryStore()
    item = _item("mem-old", MemoryKind.SEMANTIC)

    for store in (real, fake):
        store.put(item)
        with pytest.raises(KeyError, match="mem-missing-replacement"):
            store.supersede("mem-old", by_memory_id="mem-missing-replacement")
        unchanged = store.get("mem-old")
        assert unchanged is not None
        assert unchanged.is_active


def test_derive_memory_id_is_stable_and_prefix_scoped() -> None:
    """Idempotency hinge (issue #203): the same source key always maps to the
    same id, and the prefix namespaces reject vs decision memory so a given
    candidate yields exactly one of each."""
    key = "reporthashabc:ticket:throughput:stream worker logs"
    assert derive_memory_id(key, prefix="rejpath") == derive_memory_id(key, prefix="rejpath")
    # Different source keys → different ids.
    assert derive_memory_id(key, prefix="rejpath") != derive_memory_id(
        key + ":other", prefix="rejpath"
    )
    # Same key, different prefix → different (namespaced) ids.
    assert derive_memory_id(key, prefix="rejpath") != derive_memory_id(key, prefix="decision")
    assert derive_memory_id(key, prefix="rejpath").startswith("rejpath-")


def test_derive_memory_id_idempotent_put_creates_no_duplicates(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")
    key = "reporthashabc:ticket:throughput:stream worker logs"

    def _make() -> MemoryItem:
        return MemoryItem(
            memory_id=derive_memory_id(key, prefix="rejpath"),
            kind=MemoryKind.SEMANTIC,
            title="Stream worker logs",
            body="[throughput] rejected: out of axis",
            tags=(REJECTED_PATH_TAG, axis_tag("throughput")),
            provenance=MemoryProvenance(
                source_event=None,
                authored_by="brainstorm-apply",
                source_task_ref="brainstorm-apply:reporthashabc",
            ),
        )

    store.put(_make())
    store.put(_make())  # re-run apply: ON CONFLICT update, not a second row.

    assert len(store.list_rejected_paths()) == 1
    assert len(store.list_active()) == 1


def test_axis_tag_round_trips_and_missing_axis_is_empty() -> None:
    assert axis_from_tags((REJECTED_PATH_TAG, axis_tag("throughput"))) == "throughput"
    assert axis_tag(" throughput ") == "axis:throughput"
    # No axis tag present → empty string (degrade, no crash).
    assert axis_from_tags((REJECTED_PATH_TAG,)) == ""
    assert axis_from_tags(()) == ""


def test_fake_memory_store_matches_real_shape(tmp_path: Path) -> None:
    real = SqliteMemoryStore(tmp_path / "memory.db")
    fake = FakeMemoryStore()
    rejected = _item(
        "mem-rejected",
        MemoryKind.SEMANTIC,
        title="Rejected markdown archive",
        tags=(REJECTED_PATH_TAG,),
    )
    procedure = _item("mem-procedure", MemoryKind.PROCEDURAL)

    assert real.put(rejected) == fake.put(rejected)
    assert real.put(procedure) == fake.put(procedure)
    assert real.list_active(kind=MemoryKind.PROCEDURAL) == fake.list_active(
        kind=MemoryKind.PROCEDURAL
    )
    assert real.supersede("mem-rejected", by_memory_id="mem-procedure") == fake.supersede(
        "mem-rejected", by_memory_id="mem-procedure"
    )
    assert real.get("mem-rejected") == fake.get("mem-rejected")
    assert real.list_rejected_paths() == fake.list_rejected_paths()
