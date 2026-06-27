"""make_brief injects retrieved skill-tree cards (issue #458)."""

from __future__ import annotations

from pathlib import Path

from forge_loop.memory.models import area_tag
from forge_loop.memory.store import SqliteMemoryStore
from forge_loop.runner.learning import record_procedural_skill
from forge_loop.worker_brief import make_brief

_SKILL_HEADER = "Learned skills for this repo"


def _store_with_ledger_card(tmp_path: Path) -> SqliteMemoryStore:
    store = SqliteMemoryStore(tmp_path / "memory.db")
    record_procedural_skill(
        store,
        failing_signal="fold diverges",
        target="ledger.rs",
        title="pulsar-node/ledger: fold",
        body="add part; fold-on-read; cargo test -p pulsar-ledger",
        source_key="k1",
        source_task_ref="issue:#1",
        extra_tags=(area_tag("pulsar-node/ledger"),),
        evidence_refs=("commit:abc",),
    )
    return store


def _issue() -> dict[str, object]:
    return {"number": 7, "title": "fix the ledger fold convergence", "body": "ledger work"}


def test_make_brief_injects_matching_skill(tmp_path: Path) -> None:
    out = make_brief(_issue(), tmp_path, memory_store=_store_with_ledger_card(tmp_path))
    assert _SKILL_HEADER in out
    assert "pulsar-node/ledger" in out
    assert "cargo test -p pulsar-ledger" in out


def test_make_brief_without_store_has_no_skill_section(tmp_path: Path) -> None:
    out = make_brief(_issue(), tmp_path, memory_store=None)
    assert _SKILL_HEADER not in out


def test_make_brief_with_no_matching_card_has_no_section(tmp_path: Path) -> None:
    store = _store_with_ledger_card(tmp_path)
    unrelated = {"number": 9, "title": "vacuum the sqlite database", "body": "compaction"}
    out = make_brief(unrelated, tmp_path, memory_store=store)
    assert _SKILL_HEADER not in out
