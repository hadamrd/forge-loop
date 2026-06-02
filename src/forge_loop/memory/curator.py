"""Memory promotion contracts.

The curator is deliberately separate from workers. Workers propose memory;
the curator decides what becomes durable project cognition.
"""

from __future__ import annotations

from dataclasses import dataclass

from forge_loop.memory.models import MemoryItem
from forge_loop.memory.store import MemoryStore


@dataclass(frozen=True)
class PromotionCandidate:
    """A worker or maestro proposal to promote an observation into memory."""

    title: str
    body: str
    reason_to_remember: str
    tags: tuple[str, ...] = ()


class MemoryCurator:
    """Memory promotion policy with an optional durable store."""

    def __init__(self, store: MemoryStore | None = None) -> None:
        self._store = store

    def should_promote(self, candidate: PromotionCandidate) -> bool:
        """Return whether a candidate should become durable memory.

        The first policy is intentionally strict: remember only observations
        with an explicit reason they change future behavior.
        """
        return bool(candidate.reason_to_remember.strip())

    def promote(self, item: MemoryItem) -> MemoryItem:
        """Persist ``item`` when a durable store is configured."""
        if self._store is not None:
            return self._store.put(item)
        return item
