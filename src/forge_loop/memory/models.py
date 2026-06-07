"""Memory models for durable project cognition."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256

from forge_loop.eventlog.models import EventRef


class MemoryKind(StrEnum):
    """CoALA-inspired memory buckets used by the curator."""

    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"


REJECTED_PATH_TAG = "rejected-path"

#: Tag marking a durable *research note* — cited external state-of-art surfaced
#: into the brainstormer's frontier-generation inputs (issue #278). Mirrors
#: ``REJECTED_PATH_TAG``: a plain tag on a ``SEMANTIC`` :class:`MemoryItem`, no
#: new persistence layer. Distinct from ``rejected-path`` (filters inputs) — a
#: research note *adds* a new input source.
RESEARCH_TAG = "research"

#: Tag prefix used to carry the axis a memory item is filed under, so the
#: brainstormer can re-derive ``normalize_candidate_key(title, axis)`` from a
#: stored rejected-path item without a second normalisation scheme.
AXIS_TAG_PREFIX = "axis:"


def axis_tag(axis: str) -> str:
    """Render an ``axis:<name>`` tag for ``axis`` (stripped)."""
    return f"{AXIS_TAG_PREFIX}{axis.strip()}"


def axis_from_tags(tags: tuple[str, ...]) -> str:
    """Extract the axis from an ``axis:<name>`` tag, or ``""`` if absent."""
    for tag in tags:
        if tag.startswith(AXIS_TAG_PREFIX):
            return tag[len(AXIS_TAG_PREFIX) :]
    return ""


#: Tag prefix carrying a procedural item's stable *skill-key* — the digest of
#: its repair signature (failing-signal + target). Procedural memory is a
#: bounded set of *current* skills, not an append-only log, so the producer uses
#: this tag (not the ``memory_id``) to find the active skill for a given repair
#: signature and supersede it, preserving lineage instead of overwriting in
#: place. Mirrors ``AXIS_TAG_PREFIX`` / ``axis_tag`` / ``axis_from_tags``.
SKILL_TAG_PREFIX = "skill:"


def derive_skill_key(failing_signal: str, target: str) -> str:
    """Derive a deterministic skill-key from a repair signature.

    The skill-key is the ``sha256`` digest of the ``(failing-signal, target)``
    pair — same signature → same key, different signature → different key —
    reusing the digest pattern from :func:`derive_memory_id`. A NUL separator
    keeps ``("ab", "c")`` distinct from ``("a", "bc")``. Inputs are stripped so
    incidental whitespace does not fork the key.
    """
    source = f"{failing_signal.strip()}\x00{target.strip()}"
    return sha256(source.encode("utf-8")).hexdigest()[:16]


def skill_tag(skill_key: str) -> str:
    """Render a ``skill:<digest>`` tag for ``skill_key`` (stripped)."""
    return f"{SKILL_TAG_PREFIX}{skill_key.strip()}"


def skill_from_tags(tags: tuple[str, ...]) -> str:
    """Extract the skill-key from a ``skill:<digest>`` tag, or ``""`` if absent."""
    for tag in tags:
        if tag.startswith(SKILL_TAG_PREFIX):
            return tag[len(SKILL_TAG_PREFIX) :]
    return ""


def derive_memory_id(source_key: str, *, prefix: str) -> str:
    """Derive a stable ``memory_id`` from a source key.

    The same ``source_key`` always maps to the same id, so re-running an apply
    over the same report (same ``source_report_hash``) overwrites the existing
    item via ``put``'s ``ON CONFLICT`` clause instead of creating a duplicate —
    mirroring how the frontier ledger dedupes on ``source_key``.
    """
    digest = sha256(source_key.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


@dataclass(frozen=True)
class MemoryProvenance:
    """Evidence and lifecycle metadata for a durable memory item."""

    source_event: EventRef | None
    authored_by: str
    source_task_ref: str | None = None
    confidence: float = 1.0
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    supersedes: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0 <= self.confidence <= 1:
            raise ValueError("memory confidence must be between 0 and 1")
        if not self.authored_by.strip():
            raise ValueError("memory provenance authored_by must be non-empty")
        if self.source_event is None and (
            self.source_task_ref is None or not self.source_task_ref.strip()
        ):
            raise ValueError(
                "memory provenance must include a source event or source task reference"
            )


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
