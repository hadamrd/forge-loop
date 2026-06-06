"""Tests for the memory contradiction predicate and the curator transition gate.

Covers issue #284: a load-bearing decision opposing an already-active
load-bearing decision on the same axis+subject must not be silently admitted as
a second live item; promotion refuses it unless the caller forces an explicit
reopen/amend/retire transition that supersedes the prior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_loop._testing.memory_store import FakeMemoryStore
from forge_loop.memory.curator import (
    ContradictionError,
    ContradictionResolution,
    MemoryCurator,
)
from forge_loop.memory.models import (
    LOAD_BEARING_TAG,
    MemoryItem,
    MemoryKind,
    MemoryProvenance,
    axis_tag,
    contradicts,
    is_load_bearing,
    subject_tag,
)
from forge_loop.memory.store import SqliteMemoryStore


def _item(
    memory_id: str,
    *,
    title: str,
    tags: tuple[str, ...],
    supersedes: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = ("issue:#284",),
) -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id,
        kind=MemoryKind.SEMANTIC,
        title=title,
        body="Durable load-bearing decision for the event log backend.",
        tags=tags,
        provenance=MemoryProvenance(
            source_event=None,
            authored_by="test",
            source_task_ref="task:#284",
            confidence=0.9,
            supersedes=supersedes,
            evidence_refs=evidence_refs,
        ),
    )


def _decision(memory_id: str, *, axis: str, subject: str, title: str, **kw: object) -> MemoryItem:
    return _item(
        memory_id,
        title=title,
        tags=(LOAD_BEARING_TAG, axis_tag(axis), subject_tag(subject)),
        **kw,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# contradicts() predicate
# --------------------------------------------------------------------------- #
def test_contradicts_true_for_same_axis_subject_opposing_stance() -> None:
    postgres = _decision("mem-a", axis="db", subject="event-log", title="use Postgres")
    sqlite = _decision("mem-b", axis="db", subject="event-log", title="use SQLite")
    assert contradicts(sqlite, postgres) is True
    assert contradicts(postgres, sqlite) is True


def test_contradicts_false_for_different_axis() -> None:
    db = _decision("mem-a", axis="db", subject="event-log", title="use Postgres")
    cache = _decision("mem-b", axis="cache", subject="event-log", title="use SQLite")
    assert contradicts(cache, db) is False


def test_contradicts_false_for_unrelated_subject() -> None:
    event_log = _decision("mem-a", axis="db", subject="event-log", title="use Postgres")
    metrics = _decision("mem-b", axis="db", subject="metrics-store", title="use SQLite")
    assert contradicts(metrics, event_log) is False


def test_contradicts_false_when_either_side_not_load_bearing() -> None:
    bearing = _decision("mem-a", axis="db", subject="event-log", title="use Postgres")
    not_bearing = _item(
        "mem-b",
        title="use SQLite",
        tags=(axis_tag("db"), subject_tag("event-log")),  # no LOAD_BEARING_TAG
    )
    assert is_load_bearing(not_bearing) is False
    assert contradicts(not_bearing, bearing) is False
    assert contradicts(bearing, not_bearing) is False


def test_contradicts_false_for_same_stance_and_same_item() -> None:
    a = _decision("mem-a", axis="db", subject="event-log", title="use Postgres")
    same_stance = _decision("mem-b", axis="db", subject="event-log", title="use Postgres")
    assert contradicts(same_stance, a) is False  # agree, not contradict
    assert contradicts(a, a) is False  # identical item never contradicts itself


# --------------------------------------------------------------------------- #
# MemoryCurator.promote() contradiction gate
# --------------------------------------------------------------------------- #
@pytest.fixture(params=["real", "fake"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> object:
    if request.param == "real":
        return SqliteMemoryStore(tmp_path / "memory.db")
    return FakeMemoryStore()


def test_promote_contradicting_without_resolution_raises_and_leaves_store_unchanged(
    store: SqliteMemoryStore,
) -> None:
    curator = MemoryCurator(store)
    prior = _decision("mem-postgres", axis="db", subject="event-log", title="use Postgres")
    curator.promote(prior)

    candidate = _decision(
        "mem-sqlite",
        axis="db",
        subject="event-log",
        title="use SQLite",
        supersedes=("mem-postgres",),
    )
    with pytest.raises(ContradictionError, match="mem-postgres"):
        curator.promote(candidate)

    active = store.list_active(kind=MemoryKind.SEMANTIC)
    assert [i.memory_id for i in active] == ["mem-postgres"]  # prior still sole active
    assert store.get("mem-sqlite") is None  # candidate never admitted


@pytest.mark.parametrize(
    "resolution",
    [
        ContradictionResolution.REOPEN,
        ContradictionResolution.AMEND,
        ContradictionResolution.RETIRE,
    ],
)
def test_promote_with_resolution_supersedes_prior(
    store: SqliteMemoryStore, resolution: ContradictionResolution
) -> None:
    curator = MemoryCurator(store)
    curator.promote(_decision("mem-postgres", axis="db", subject="event-log", title="use Postgres"))

    candidate = _decision(
        "mem-sqlite",
        axis="db",
        subject="event-log",
        title="use SQLite",
        supersedes=("mem-postgres",),
        evidence_refs=("issue:#284", "event:flip"),
    )
    promoted = curator.promote(candidate, resolution=resolution)

    assert promoted.memory_id == "mem-sqlite"
    prior = store.get("mem-postgres")
    assert prior is not None
    assert prior.superseded_by == "mem-sqlite"
    assert not prior.is_active

    survivor = store.get("mem-sqlite")
    assert survivor is not None
    assert survivor.is_active
    assert survivor.provenance.supersedes == ("mem-postgres",)
    assert survivor.provenance.evidence_refs == ("issue:#284", "event:flip")

    active = store.list_active(kind=MemoryKind.SEMANTIC)
    assert [i.memory_id for i in active] == ["mem-sqlite"]  # exactly one for axis+subject


def test_promote_resolution_without_recording_superseded_id_raises(
    store: SqliteMemoryStore,
) -> None:
    curator = MemoryCurator(store)
    curator.promote(_decision("mem-postgres", axis="db", subject="event-log", title="use Postgres"))

    candidate = _decision(
        "mem-sqlite",
        axis="db",
        subject="event-log",
        title="use SQLite",
        supersedes=(),  # forgot to record what it supersedes
    )
    with pytest.raises(ContradictionError, match="provenance.supersedes"):
        curator.promote(candidate, resolution=ContradictionResolution.AMEND)
    assert store.get("mem-sqlite") is None


def test_promote_resolution_without_evidence_raises(store: SqliteMemoryStore) -> None:
    curator = MemoryCurator(store)
    curator.promote(_decision("mem-postgres", axis="db", subject="event-log", title="use Postgres"))

    candidate = _decision(
        "mem-sqlite",
        axis="db",
        subject="event-log",
        title="use SQLite",
        supersedes=("mem-postgres",),
        evidence_refs=(),  # no superseding evidence
    )
    with pytest.raises(ContradictionError, match="evidence"):
        curator.promote(candidate, resolution=ContradictionResolution.RETIRE)


def test_promote_non_contradicting_on_fresh_axis_still_admits(store: SqliteMemoryStore) -> None:
    """Regression guard: a load-bearing decision on a new axis promotes unchanged."""
    curator = MemoryCurator(store)
    curator.promote(_decision("mem-postgres", axis="db", subject="event-log", title="use Postgres"))

    fresh = _decision("mem-runtime", axis="runtime", subject="worker", title="use asyncio")
    promoted = curator.promote(fresh)

    assert promoted.memory_id == "mem-runtime"
    active = {i.memory_id for i in store.list_active(kind=MemoryKind.SEMANTIC)}
    assert active == {"mem-postgres", "mem-runtime"}


def test_promote_non_load_bearing_contradiction_is_exempt(store: SqliteMemoryStore) -> None:
    """Non-load-bearing items skip the gate entirely (only decisions transition)."""
    curator = MemoryCurator(store)
    curator.promote(_decision("mem-postgres", axis="db", subject="event-log", title="use Postgres"))

    note = _item(
        "mem-note",
        title="use SQLite",
        tags=(axis_tag("db"), subject_tag("event-log")),  # not load-bearing
    )
    promoted = curator.promote(note)
    assert promoted.memory_id == "mem-note"
    assert store.get("mem-note") is not None


def test_promote_without_store_returns_item_unchanged() -> None:
    curator = MemoryCurator()
    candidate = _decision("mem-x", axis="db", subject="event-log", title="use SQLite")
    assert curator.promote(candidate) == candidate
