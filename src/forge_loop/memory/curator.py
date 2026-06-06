"""Memory promotion contracts.

The curator is deliberately separate from workers. Workers propose memory;
the curator decides what becomes durable project cognition.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from forge_loop.memory.models import (
    MemoryItem,
    axis_from_tags,
    contradicts,
    is_load_bearing,
)
from forge_loop.memory.store import MemoryStore


class ContradictionResolution(StrEnum):
    """How a caller chooses to resolve a contradicted load-bearing decision.

    The maestro must make the transition *explicit* — it cannot silently admit
    a second live decision on the same axis+subject.
    """

    REOPEN = "reopen"
    AMEND = "amend"
    RETIRE = "retire"


class ContradictionError(RuntimeError):
    """Raised when a load-bearing candidate contradicts an active decision.

    Refusal is loud and descriptive (it names the offending memory ids) — it
    never silently no-ops, so the maestro cannot boot a contradictory frontier.
    """


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

    def promote(
        self,
        item: MemoryItem,
        *,
        resolution: ContradictionResolution | None = None,
    ) -> MemoryItem:
        """Persist ``item`` when a durable store is configured.

        A load-bearing candidate that :func:`contradicts` an active load-bearing
        item is refused with :class:`ContradictionError` unless the caller
        supplies an explicit ``resolution`` (reopen/amend/retire). When a
        resolution is given the prior items are superseded via
        ``store.supersede``; the new item's ``provenance.supersedes`` must name
        every contradicted prior and its ``evidence_refs`` must be non-empty, so
        the transition records *why* the decision flipped. Non-load-bearing
        items bypass the gate entirely.
        """
        if self._store is None:
            return item
        if not is_load_bearing(item):
            return self._store.put(item)

        conflicts = tuple(
            active
            for active in self._store.list_active(kind=item.kind)
            if contradicts(item, active)
        )
        if not conflicts:
            return self._store.put(item)

        offenders = ", ".join(c.memory_id for c in conflicts)
        axis = axis_from_tags(item.tags)
        if resolution is None:
            raise ContradictionError(
                f"candidate {item.memory_id!r} contradicts active load-bearing "
                f"decision(s) [{offenders}] on axis {axis!r}; supply an explicit "
                f"resolution (reopen/amend/retire) to force a transition"
            )

        missing = tuple(c.memory_id for c in conflicts if c.memory_id not in item.provenance.supersedes)
        if missing:
            raise ContradictionError(
                f"{resolution} of {item.memory_id!r} must record the superseded "
                f"decision(s) {list(missing)} in provenance.supersedes"
            )
        if not item.provenance.evidence_refs:
            raise ContradictionError(
                f"{resolution} of {item.memory_id!r} must record superseding "
                f"evidence in provenance.evidence_refs"
            )

        stored = self._store.put(item)
        for prior in conflicts:
            self._store.supersede(prior.memory_id, by_memory_id=item.memory_id)
        return stored
