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

#: Marks a :class:`MemoryItem` as a *load-bearing* decision — one whose loss or
#: silent contradiction would make the maestro re-litigate a settled choice.
#: Memory has no load-bearing notion of its own (``eventlog.is_load_bearing``
#: classifies *event kinds*, a separate concern per #277), so this tag is the
#: single source of truth for "is this memory item load-bearing?".
LOAD_BEARING_TAG = "load-bearing"

#: Tag prefix used to carry the axis a memory item is filed under, so the
#: brainstormer can re-derive ``normalize_candidate_key(title, axis)`` from a
#: stored rejected-path item without a second normalisation scheme.
AXIS_TAG_PREFIX = "axis:"

#: Tag prefix carrying the *subject* a decision is about (e.g. ``the event
#: log``). Two load-bearing decisions on the same axis+subject answer the same
#: question; only one can be live. Distinct from the title, which carries the
#: *stance* (the answer) and therefore differs between contradicting decisions.
SUBJECT_TAG_PREFIX = "subject:"

#: Tag prefix carrying the *stance* (the position taken). Optional: when absent,
#: the item title is used as the stance, so callers need not double-encode it.
STANCE_TAG_PREFIX = "stance:"


def _tag_value(tags: tuple[str, ...], prefix: str) -> str:
    """Return the value of the first ``<prefix><value>`` tag, or ``""``."""
    for tag in tags:
        if tag.startswith(prefix):
            return tag[len(prefix) :]
    return ""


def axis_tag(axis: str) -> str:
    """Render an ``axis:<name>`` tag for ``axis`` (stripped)."""
    return f"{AXIS_TAG_PREFIX}{axis.strip()}"


def axis_from_tags(tags: tuple[str, ...]) -> str:
    """Extract the axis from an ``axis:<name>`` tag, or ``""`` if absent."""
    return _tag_value(tags, AXIS_TAG_PREFIX)


def subject_tag(subject: str) -> str:
    """Render a ``subject:<name>`` tag for ``subject`` (stripped)."""
    return f"{SUBJECT_TAG_PREFIX}{subject.strip()}"


def subject_from_tags(tags: tuple[str, ...]) -> str:
    """Extract the subject from a ``subject:<name>`` tag, or ``""`` if absent."""
    return _tag_value(tags, SUBJECT_TAG_PREFIX)


def stance_tag(stance: str) -> str:
    """Render a ``stance:<value>`` tag for ``stance`` (stripped)."""
    return f"{STANCE_TAG_PREFIX}{stance.strip()}"


def stance_from_tags(tags: tuple[str, ...]) -> str:
    """Extract the stance from a ``stance:<value>`` tag, or ``""`` if absent."""
    return _tag_value(tags, STANCE_TAG_PREFIX)


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


def is_load_bearing(item: MemoryItem) -> bool:
    """Return whether ``item`` is a load-bearing decision (carries ``LOAD_BEARING_TAG``).

    Single source of truth for memory load-bearing classification — distinct
    from :func:`forge_loop.eventlog.models.is_load_bearing`, which classifies
    *event kinds* (#277). Only load-bearing decisions are forced through an
    explicit transition when contradicted; everything else is exempt.
    """
    return LOAD_BEARING_TAG in item.tags


def _stance(item: MemoryItem) -> str:
    """The position ``item`` takes: its ``stance:`` tag, else its title."""
    return stance_from_tags(item.tags) or item.title


def contradicts(candidate: MemoryItem, active_item: MemoryItem) -> bool:
    """Return whether ``candidate`` contradicts ``active_item``.

    Deterministic — no semantics, no LLM. Two items contradict when both are
    load-bearing, distinct, filed under the *same* non-empty axis and subject,
    and take *opposing* stances. Items on different axes/subjects, or with the
    same stance, or where either side is not load-bearing, do not contradict.
    """
    if not (is_load_bearing(candidate) and is_load_bearing(active_item)):
        return False
    if candidate.memory_id == active_item.memory_id:
        return False
    axis = axis_from_tags(candidate.tags)
    if not axis or axis != axis_from_tags(active_item.tags):
        return False
    subject = subject_from_tags(candidate.tags)
    if not subject or subject != subject_from_tags(active_item.tags):
        return False
    return _stance(candidate) != _stance(active_item)
