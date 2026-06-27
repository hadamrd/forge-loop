"""Harvest a procedural skill card from each critic-clean merged outcome."""

from __future__ import annotations

from pathlib import Path

from forge_loop.memory.models import (
    MemoryKind,
    area_from_tags,
    derive_skill_key,
    skill_tag,
)
from forge_loop.memory.store import SqliteMemoryStore
from forge_loop.runner.learning import (
    harvest_skills_from_merge,
    record_procedural_skill,
)

_VALID_CARD = """
{
  "area": "pulsar-node/http-route",
  "failing_signal": "ledger-only change 500s",
  "target": "bins/pulsar-node/src/ledger.rs",
  "trigger": "adding a GET endpoint",
  "procedure": "add handler; route; cargo test -p pulsar-node --bin",
  "pitfalls": "bare -p filters the bin unittest to zero",
  "confidence": 0.9
}
"""

_PR = "https://github.com/o/r/pull/9"


def _store(tmp_path: Path) -> SqliteMemoryStore:
    return SqliteMemoryStore(tmp_path / "memory.db")


# --- record_procedural_skill: area tag + evidence (provenance) ----------------


def test_record_procedural_skill_persists_area_tag_and_evidence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    memory_id = record_procedural_skill(
        store,
        failing_signal="sig",
        target="file.rs",
        title="a skill",
        body="do it",
        source_key="harvest:1:abc",
        source_task_ref="issue:#1",
        extra_tags=("area:pulsar-node/http-route",),
        evidence_refs=("commit:abc123",),
    )
    item = store.get(memory_id)
    assert item is not None
    assert skill_tag(derive_skill_key("sig", "file.rs")) in item.tags
    assert area_from_tags(item.tags) == "pulsar-node/http-route"
    assert "commit:abc123" in item.provenance.evidence_refs


# --- harvest_skills_from_merge ------------------------------------------------
# fetch_diff is keyed by the PR URL (which the merged outcome carries) and
# returns the unified diff text — matching gh_issues.pr_diff(pr_url, repo),
# forge-loop's githubkit path (the gh CLI is not used and a loop GH_TOKEN
# breaks it). Provenance is the PR URL.


def test_harvest_records_one_card_per_merge_with_area_and_pr(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def fetch_diff(pr_url: str) -> str:
        assert pr_url == _PR  # the PR URL drives the diff fetch, not the issue
        return "diff --git a/ledger.rs b/ledger.rs\n+fn f(){}"

    harvested = harvest_skills_from_merge(
        store,
        [{"issue": 430, "title": "APP4: app-scoped token auth", "pr_url": _PR}],
        fetch_diff=fetch_diff,
        call_llm=lambda _p: _VALID_CARD,
    )

    assert len(harvested) == 1
    assert harvested[0].issue == 430
    assert harvested[0].area == "pulsar-node/http-route"
    assert harvested[0].pr_url == _PR

    active = store.list_active(kind=MemoryKind.PROCEDURAL)
    assert len(active) == 1
    assert area_from_tags(active[0].tags) == "pulsar-node/http-route"
    assert f"pr:{_PR}" in active[0].provenance.evidence_refs
    assert "cargo test" in active[0].body  # the recipe survived


def test_harvest_skips_outcome_without_a_pr_url(tmp_path: Path) -> None:
    store = _store(tmp_path)
    harvested = harvest_skills_from_merge(
        store,
        [{"issue": 1, "title": "x"}],  # no pr_url -> cannot fetch a diff
        fetch_diff=lambda _u: "real diff",
        call_llm=lambda _p: _VALID_CARD,
    )
    assert harvested == ()
    assert store.list_active(kind=MemoryKind.PROCEDURAL) == ()


def test_harvest_skips_outcome_with_empty_diff(tmp_path: Path) -> None:
    store = _store(tmp_path)
    harvested = harvest_skills_from_merge(
        store,
        [{"issue": 1, "title": "x", "pr_url": _PR}],
        fetch_diff=lambda _u: "",
        call_llm=lambda _p: _VALID_CARD,
    )
    assert harvested == ()
    assert store.list_active(kind=MemoryKind.PROCEDURAL) == ()


def test_harvest_skips_when_librarian_returns_garbage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    harvested = harvest_skills_from_merge(
        store,
        [{"issue": 1, "title": "x", "pr_url": _PR}],
        fetch_diff=lambda _u: "real diff",
        call_llm=lambda _p: "sorry, cannot help",
    )
    assert harvested == ()
    assert store.list_active(kind=MemoryKind.PROCEDURAL) == ()


def test_harvest_is_idempotent_for_the_same_merge(tmp_path: Path) -> None:
    store = _store(tmp_path)

    def run() -> None:
        harvest_skills_from_merge(
            store,
            [{"issue": 430, "title": "APP4", "pr_url": _PR}],
            fetch_diff=lambda _u: "diff",
            call_llm=lambda _p: _VALID_CARD,
        )

    run()
    run()
    # same PR URL -> same source_key -> upsert in place, not a duplicate
    assert len(store.list_active(kind=MemoryKind.PROCEDURAL)) == 1
