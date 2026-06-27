"""Retrieve the most relevant skill cards for a ticket and render them."""

from __future__ import annotations

from pathlib import Path

from forge_loop.memory.models import MemoryKind, area_tag
from forge_loop.memory.skills import render_skill_section, retrieve_skills_for
from forge_loop.memory.store import SqliteMemoryStore
from forge_loop.runner.learning import record_procedural_skill


def _store(tmp_path: Path) -> SqliteMemoryStore:
    return SqliteMemoryStore(tmp_path / "memory.db")


def _add(store: SqliteMemoryStore, *, area: str, signal: str, target: str, body: str) -> str:
    return record_procedural_skill(
        store,
        failing_signal=signal,
        target=target,
        title=f"{area}: {signal}",
        body=body,
        source_key=f"k:{area}:{target}",
        source_task_ref=f"issue:#{abs(hash(target)) % 1000}",
        extra_tags=(area_tag(area),),
        evidence_refs=(f"commit:{target}",),
    )


def test_retrieves_card_matching_an_area_token(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add(
        store,
        area="pulsar-node/ledger",
        signal="fold diverges",
        target="ledger.rs",
        body="fold recipe",
    )
    _add(store, area="ui/page", signal="route missing", target="app.jsx", body="route recipe")

    hits = retrieve_skills_for(store, "fix the ledger fold convergence bug", k=3)

    assert [h.tags for h in hits]  # non-empty
    from forge_loop.memory.models import area_from_tags

    assert area_from_tags(hits[0].tags) == "pulsar-node/ledger"


def test_excludes_cards_with_no_overlap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add(store, area="ui/page", signal="route missing", target="app.jsx", body="route recipe")
    # a query about something totally unrelated must inject NOTHING
    assert retrieve_skills_for(store, "database vacuum compaction schedule", k=3) == ()


def test_respects_the_k_cap(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for i in range(5):
        _add(
            store,
            area=f"pulsar-node/ledger{i}",
            signal=f"ledger sig {i}",
            target=f"l{i}.rs",
            body="ledger",
        )
    hits = retrieve_skills_for(store, "ledger ledger ledger", k=2)
    assert len(hits) == 2


def test_area_match_outranks_incidental_text_match(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add(
        store,
        area="ui/page",
        signal="x",
        target="a.jsx",
        body="this mentions ledger once incidentally",
    )
    _add(store, area="pulsar-node/ledger", signal="y", target="l.rs", body="recipe")
    hits = retrieve_skills_for(store, "ledger endpoint work", k=2)
    from forge_loop.memory.models import area_from_tags

    assert area_from_tags(hits[0].tags) == "pulsar-node/ledger"


def test_only_active_procedural_cards_are_retrieved(tmp_path: Path) -> None:
    store = _store(tmp_path)
    # supersede: same (signal,target) twice -> first becomes superseded
    _add(store, area="pulsar-node/ledger", signal="same", target="l.rs", body="old recipe")
    _add(store, area="pulsar-node/ledger", signal="same", target="l.rs", body="new recipe")
    hits = retrieve_skills_for(store, "ledger work", k=5)
    assert len(hits) == 1
    assert hits[0].kind == MemoryKind.PROCEDURAL
    assert "new recipe" in hits[0].body


def test_render_section_includes_area_recipe_and_provenance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add(
        store,
        area="pulsar-node/ledger",
        signal="fold",
        target="ledger.rs",
        body="step one; step two",
    )
    hits = retrieve_skills_for(store, "ledger fold", k=1)
    section = render_skill_section(hits)
    assert "pulsar-node/ledger" in section
    assert "step one" in section
    assert "commit:ledger.rs" in section  # provenance surfaced


def test_render_empty_when_no_hits() -> None:
    assert render_skill_section(()) == ""
