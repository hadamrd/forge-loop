"""Memory models for durable project cognition."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from forge_loop.eventlog.models import EventRef


class MemoryKind(StrEnum):
    """CoALA-inspired memory buckets used by the curator."""

    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"


@dataclass(frozen=True)
class MemoryProvenance:
    """Evidence and lifecycle metadata for a durable memory item."""

    source_event: EventRef | None
    authored_by: str
    confidence: float = 1.0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    supersedes: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("memory confidence must be between 0 and 1")


@dataclass(frozen=True)
class MemoryItem:
    """A promoted memory fact, lesson, or procedure."""

    memory_id: str
    kind: MemoryKind
    title: str
    body: str
    provenance: MemoryProvenance
    tags: tuple[str, ...] = ()
    superseded_by: str | None = None

    @property
    def is_active(self) -> bool:
        return self.superseded_by is None
