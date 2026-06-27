"""Skill-tree inventory stats — the data behind ``forge-loop skill-stats``.

Pure read over the procedural-memory store: how many leaf skills and internal
nodes are live, how many are expired, and the per-area distribution. Kept free
of I/O so it is unit-tested against a real
:class:`~forge_loop.memory.store.SqliteMemoryStore`; the CLI layer renders it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from forge_loop.memory.models import (
    AREA_NODE_TAG,
    EXPIRED_TAG,
    MemoryKind,
    area_from_tags,
)
from forge_loop.memory.store import MemoryStore

__all__ = ["SkillInventory", "compute_skill_inventory"]


@dataclass(frozen=True)
class SkillInventory:
    """A snapshot of the live skill tree."""

    leaves: int
    nodes: int
    expired: int
    areas: dict[str, int]


def compute_skill_inventory(store: MemoryStore) -> SkillInventory:
    """Summarise the active procedural skill cards in ``store``.

    Leaves and internal nodes are counted separately; expired cards (tagged
    :data:`~forge_loop.memory.models.EXPIRED_TAG`) are counted on their own and
    excluded from the leaf/node/area tallies — they no longer participate in the
    tree. ``areas`` maps each area path to its live (non-expired) card count.
    """
    leaves = nodes = expired = 0
    areas: Counter[str] = Counter()
    for item in store.list_active(kind=MemoryKind.PROCEDURAL):
        if EXPIRED_TAG in item.tags:
            expired += 1
            continue
        area = area_from_tags(item.tags)
        if area:
            areas[area] += 1
        if AREA_NODE_TAG in item.tags:
            nodes += 1
        else:
            leaves += 1
    return SkillInventory(leaves=leaves, nodes=nodes, expired=expired, areas=dict(areas))
