"""Test fake for curated memory storage."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from forge_loop.memory.models import REJECTED_PATH_TAG, MemoryItem, MemoryKind


@dataclass
class FakeMemoryStore:
    items: dict[str, MemoryItem] = field(default_factory=dict)

    def put(self, item: MemoryItem) -> MemoryItem:
        self.items[item.memory_id] = item
        return item

    def get(self, memory_id: str) -> MemoryItem | None:
        return self.items.get(memory_id)

    def list_active(self, *, kind: MemoryKind | None = None) -> tuple[MemoryItem, ...]:
        return tuple(
            item
            for item in self.items.values()
            if item.is_active and (kind is None or item.kind is kind)
        )

    def list_rejected_paths(self) -> tuple[MemoryItem, ...]:
        return tuple(item for item in self.list_active() if REJECTED_PATH_TAG in item.tags)

    def supersede(self, memory_id: str, *, by_memory_id: str) -> MemoryItem:
        if by_memory_id not in self.items:
            raise KeyError(by_memory_id)
        item = self.items.get(memory_id)
        if item is None:
            raise KeyError(memory_id)
        superseded = replace(item, superseded_by=by_memory_id)
        self.items[memory_id] = superseded
        return superseded
