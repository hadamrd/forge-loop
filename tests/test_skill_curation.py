"""Curation: expire stale skill cards; promote leaves into internal nodes."""

from __future__ import annotations

from pathlib import Path

from forge_loop.memory.models import AREA_NODE_TAG, MemoryKind, area_from_tags, area_tag
from forge_loop.memory.skills import (
    expire_stale_skills,
    promote_internal_nodes,
    retrieve_skills_for,
)
from forge_loop.memory.store import SqliteMemoryStore
from forge_loop.runner.learning import record_procedural_skill


def _store(tmp_path: Path) -> SqliteMemoryStore:
    return SqliteMemoryStore(tmp_path / "memory.db")


def _leaf(store: SqliteMemoryStore, *, area: str, signal: str, target: str, sha: str) -> str:
    return record_procedural_skill(
        store,
        failing_signal=signal,
        target=target,
        title=f"{area}: {signal}",
        body="recipe with ledger keyword",
        source_key=f"k:{target}",
        source_task_ref=f"issue:#{abs(hash(target)) % 1000}",
        extra_tags=(area_tag(area),),
        evidence_refs=(f"commit:{sha}",),
    )


# --- expiry -------------------------------------------------------------------


def test_expire_tags_card_whose_proof_sha_is_gone(tmp_path: Path) -> None:
    store = _store(tmp_path)
    dead = _leaf(store, area="pulsar-node/ledger", signal="x", target="a.rs", sha="deadsha")
    expired = expire_stale_skills(store, live_shas={"livesha"})
    assert dead in expired
    # adversarial: an expired card must NOT be retrieved even on a matching query
    assert retrieve_skills_for(store, "ledger recipe", k=5) == ()


def test_expire_keeps_card_with_a_live_sha(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _leaf(store, area="pulsar-node/ledger", signal="x", target="a.rs", sha="livesha")
    expired = expire_stale_skills(store, live_shas={"livesha"})
    assert expired == ()
    assert len(retrieve_skills_for(store, "ledger recipe", k=5)) == 1


def test_expire_ignores_card_without_commit_provenance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    record_procedural_skill(
        store,
        failing_signal="x",
        target="a.rs",
        title="no-provenance",
        body="ledger recipe",
        source_key="k",
        source_task_ref="issue:#1",
        extra_tags=(area_tag("pulsar-node/ledger"),),
    )
    assert expire_stale_skills(store, live_shas=set()) == ()


# --- internal-node promotion --------------------------------------------------


def _fake_summarize(leaves: tuple) -> str:
    return "pattern: " + "; ".join(leaf.title for leaf in leaves)


def test_promote_creates_internal_node_when_enough_sibling_leaves(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _leaf(store, area="pulsar-node/ledger", signal="a", target="a.rs", sha="s1")
    _leaf(store, area="pulsar-node/http", signal="b", target="b.rs", sha="s2")

    promoted = promote_internal_nodes(store, min_leaves=2, summarize=_fake_summarize)

    assert len(promoted) == 1
    assert promoted[0].area == "pulsar-node"
    assert promoted[0].leaf_count == 2
    nodes = [i for i in store.list_active(kind=MemoryKind.PROCEDURAL) if AREA_NODE_TAG in i.tags]
    assert len(nodes) == 1
    assert area_from_tags(nodes[0].tags) == "pulsar-node"
    assert "pattern:" in nodes[0].body


def test_promote_skips_area_below_threshold(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _leaf(store, area="pulsar-node/ledger", signal="a", target="a.rs", sha="s1")
    assert promote_internal_nodes(store, min_leaves=2, summarize=_fake_summarize) == ()


def test_promote_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _leaf(store, area="pulsar-node/ledger", signal="a", target="a.rs", sha="s1")
    _leaf(store, area="pulsar-node/http", signal="b", target="b.rs", sha="s2")

    def run() -> None:
        promote_internal_nodes(store, min_leaves=2, summarize=_fake_summarize)

    run()
    run()
    nodes = [i for i in store.list_active(kind=MemoryKind.PROCEDURAL) if AREA_NODE_TAG in i.tags]
    assert len(nodes) == 1


def test_internal_node_does_not_count_itself_as_a_leaf(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _leaf(store, area="pulsar-node/ledger", signal="a", target="a.rs", sha="s1")
    _leaf(store, area="pulsar-node/http", signal="b", target="b.rs", sha="s2")
    promote_internal_nodes(store, min_leaves=2, summarize=_fake_summarize)
    # a second pass must not cascade the node into a grandparent ("" / root) node
    promote_internal_nodes(store, min_leaves=2, summarize=_fake_summarize)
    nodes = [i for i in store.list_active(kind=MemoryKind.PROCEDURAL) if AREA_NODE_TAG in i.tags]
    assert len(nodes) == 1
    assert area_from_tags(nodes[0].tags) == "pulsar-node"
