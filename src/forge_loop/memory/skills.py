"""Retrieve and render learned procedural skills (the skill tree) for a brief.

Retrieval is deliberately deterministic for v1: a card is ranked by token
overlap between the ticket text and the card's *area path* (weighted heavily,
since the area IS the tree address) and its title/body. A clean seam is left for
a future semantic (embedding) ranker — :func:`retrieve_skills_for` is the single
entry point a Lumen-backed implementation would replace, with the same shape.

Cards that overlap nothing with the query are NOT returned: injecting an
irrelevant or stale recipe is worse than injecting none.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace

from forge_loop.memory.models import (
    AREA_NODE_TAG,
    EXPIRED_TAG,
    MemoryItem,
    MemoryKind,
    area_from_tags,
    area_tag,
)
from forge_loop.memory.store import MemoryStore

__all__ = [
    "PromotedNode",
    "expire_stale_skills",
    "promote_internal_nodes",
    "render_skill_section",
    "retrieve_skills_for",
]

_COMMIT_PREFIX = "commit:"

#: An area-path token match counts for more than an incidental body-word match:
#: the tree address is the strongest relevance signal.
_AREA_WEIGHT = 3
_MIN_TOKEN_LEN = 3
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return {tok for tok in _TOKEN_RE.findall(text.lower()) if len(tok) >= _MIN_TOKEN_LEN}


def _area_tokens(area: str) -> set[str]:
    # Split the path on "/" and "-" so "pulsar-node/http-route" contributes
    # {pulsar, node, http, route}.
    return _tokens(area.replace("/", " ").replace("-", " "))


def _score(item: MemoryItem, query_tokens: set[str]) -> int:
    area = area_from_tags(item.tags)
    area_score = _AREA_WEIGHT * len(query_tokens & _area_tokens(area))
    text_score = len(query_tokens & _tokens(f"{item.title} {item.body}"))
    return area_score + text_score


def retrieve_skills_for(store: MemoryStore, query: str, *, k: int = 3) -> tuple[MemoryItem, ...]:
    """Return up to ``k`` active procedural skill cards most relevant to ``query``.

    Ranks active ``PROCEDURAL`` cards by area-weighted token overlap; cards with
    zero overlap are excluded. Ties break by confidence (desc) then insertion
    order (stable). An empty/blank query returns ``()``.
    """
    query_tokens = _tokens(query)
    if not query_tokens:
        return ()
    scored = [
        (score, item)
        for item in store.list_active(kind=MemoryKind.PROCEDURAL)
        if EXPIRED_TAG not in item.tags and (score := _score(item, query_tokens)) > 0
    ]
    # Stable sort: equal (score, confidence) keeps list_active's insertion order.
    scored.sort(key=lambda si: (si[0], si[1].provenance.confidence), reverse=True)
    return tuple(item for _score_value, item in scored[:k])


def render_skill_section(items: tuple[MemoryItem, ...]) -> str:
    """Render retrieved skill cards as a brief section, or ``""`` if none."""
    if not items:
        return ""
    lines = [
        "## Learned skills for this repo (retrieved from past merges)",
        (
            "Distilled from prior successful, reviewed changes. Apply when "
            "relevant, but verify against current code — they can go stale."
        ),
        "",
    ]
    for item in items:
        area = area_from_tags(item.tags) or "general"
        provenance = ", ".join(item.provenance.evidence_refs) or "—"
        lines.append(f"### {area} (confidence {item.provenance.confidence:.2f})")
        lines.append(item.body)
        lines.append(f"_provenance: {provenance}_")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _commit_shas(item: MemoryItem) -> tuple[str, ...]:
    return tuple(
        ref[len(_COMMIT_PREFIX) :]
        for ref in item.provenance.evidence_refs
        if ref.startswith(_COMMIT_PREFIX)
    )


def expire_stale_skills(store: MemoryStore, *, live_shas: set[str]) -> tuple[str, ...]:
    """Retire procedural cards whose proof commit is no longer in history.

    A card carries its proof as ``commit:<sha>`` evidence. When none of a card's
    proof commits are in ``live_shas`` (the set of commits currently reachable in
    the repo), the change it was distilled from has been rebased/squashed away —
    the recipe may no longer match the code, so the card is tagged
    :data:`~forge_loop.memory.models.EXPIRED_TAG` and dropped from retrieval. A
    card with no commit provenance cannot be judged and is left untouched.
    Returns the ``memory_id`` of each card expired this pass.
    """
    expired: list[str] = []
    for item in store.list_active(kind=MemoryKind.PROCEDURAL):
        if EXPIRED_TAG in item.tags:
            continue
        shas = _commit_shas(item)
        if not shas:
            continue
        if any(sha in live_shas for sha in shas):
            continue
        store.put(replace(item, tags=(*item.tags, EXPIRED_TAG)))
        expired.append(item.memory_id)
    return tuple(expired)


@dataclass(frozen=True)
class PromotedNode:
    """Descriptor of one internal node promoted from sibling leaves."""

    memory_id: str
    area: str
    leaf_count: int


def promote_internal_nodes(
    store: MemoryStore,
    *,
    min_leaves: int,
    summarize: Callable[[tuple[MemoryItem, ...]], str],
) -> tuple[PromotedNode, ...]:
    """Generalise sibling leaves into an internal-node card per parent area.

    Groups active *leaf* cards (a leaf has an ``area:`` tag and is neither an
    :data:`~forge_loop.memory.models.AREA_NODE_TAG` node nor expired) by their
    parent area path. When a parent gathers at least ``min_leaves`` leaves, the
    injected ``summarize`` distils their shared pattern into the body of an
    internal-node card filed at the parent path (tagged ``AREA_NODE_TAG``).
    Internal nodes never count as leaves, so promotion does not cascade upward.
    Idempotent: re-running upserts the node in place (stable per parent).
    """
    from forge_loop.runner.learning import record_procedural_skill

    groups: dict[str, list[MemoryItem]] = defaultdict(list)
    for item in store.list_active(kind=MemoryKind.PROCEDURAL):
        if AREA_NODE_TAG in item.tags or EXPIRED_TAG in item.tags:
            continue
        area = area_from_tags(item.tags)
        if not area:
            continue
        parent = "/".join(area.split("/")[:-1])
        if parent:
            groups[parent].append(item)

    promoted: list[PromotedNode] = []
    for parent, leaves in groups.items():
        if len(leaves) < min_leaves:
            continue
        body = summarize(tuple(leaves))
        memory_id = record_procedural_skill(
            store,
            failing_signal="",
            target=parent,
            title=f"{parent} (pattern)",
            body=body,
            source_key=f"node:{parent}",
            source_task_ref=f"area:{parent}",
            extra_tags=(area_tag(parent), AREA_NODE_TAG),
            confidence=0.7,
        )
        promoted.append(PromotedNode(memory_id, parent, len(leaves)))
    return tuple(promoted)
