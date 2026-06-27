"""Area-path tags that give a procedural skill its position in the tree."""

from __future__ import annotations

from forge_loop.memory.models import (
    AREA_NODE_TAG,
    area_ancestors,
    area_from_tags,
    area_tag,
)


def test_area_tag_renders_prefixed_stripped_path() -> None:
    assert area_tag("  pulsar-node/http-route ") == "area:pulsar-node/http-route"


def test_area_from_tags_extracts_the_path() -> None:
    tags = ("skill:abc123", "area:pulsar-node/ledger")
    assert area_from_tags(tags) == "pulsar-node/ledger"


def test_area_from_tags_absent_returns_empty() -> None:
    assert area_from_tags(("skill:abc123",)) == ""


def test_area_ancestors_walks_most_specific_first_including_self() -> None:
    assert area_ancestors("a/b/c") == ("a/b/c", "a/b", "a")


def test_area_ancestors_single_segment_is_just_itself() -> None:
    assert area_ancestors("a") == ("a",)


def test_area_ancestors_blank_is_empty() -> None:
    assert area_ancestors("  ") == ()


def test_area_node_marker_tag_is_stable() -> None:
    assert AREA_NODE_TAG == "area-node"
