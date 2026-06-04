"""Rotation/compaction guard that refuses to drop load-bearing events.

Issue #210. The durable event log is what ``control/boot.py`` reconstructs
project cognition from. Two prune surfaces — NDJSON rotation in
``forge_loop.state`` and SQLite compaction in ``forge_loop.eventlog.sqlite`` —
can silently erase that cognition. This module is the shared, typed guard both
surfaces route through so the classification of "load-bearing" lives in exactly
one place (:func:`forge_loop.eventlog.models.is_load_bearing`).

Two cooperating behaviours (the AC permits either; we expose both because the
two surfaces want different ones):

* :func:`guard_prune` — **option (a)**, refuse: raise
  :class:`LoadBearingGuardError` if asked to drop any load-bearing event. The
  SQLite path uses this as its hard backstop (a forced drop of a
  ``decision.made`` row fails, the row survives).
* :func:`partition_for_prune` — **option (b)**, partition: split a batch into
  the load-bearing remainder to preserve and the noise that is safe to drop,
  mutating nothing. The NDJSON path uses this to force load-bearing lines into a
  preserved sidecar tier before dropping the rest.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from forge_loop.eventlog.models import EventEnvelope, EventKind, is_load_bearing

__all__ = [
    "Classifiable",
    "LoadBearingGuardError",
    "PrunePartition",
    "guard_prune",
    "partition_for_prune",
]

#: Anything :func:`is_load_bearing` accepts.
Classifiable = EventEnvelope | EventKind | str

T = TypeVar("T", EventEnvelope, EventKind, str)


class LoadBearingGuardError(RuntimeError):
    """Raised when a prune would discard one or more load-bearing events.

    The typed guard failure for prune option (a). Carries the offending items so
    callers can log/telemetry exactly what was protected.
    """

    def __init__(self, protected: Sequence[object]) -> None:
        self.protected: tuple[object, ...] = tuple(protected)
        count = len(self.protected)
        super().__init__(
            f"refusing to prune {count} load-bearing event(s); "
            "they must be preserved (see issue #210)"
        )


@dataclass(frozen=True)
class PrunePartition:
    """Result of :func:`partition_for_prune`.

    ``preserve`` holds the load-bearing items (must survive in the live or a
    preserved tier); ``droppable`` holds the telemetry/noise safe to discard.
    The two together are exactly the input, in input order — nothing is mutated.
    """

    preserve: tuple[object, ...]
    droppable: tuple[object, ...]

    @property
    def has_load_bearing(self) -> bool:
        return bool(self.preserve)


def partition_for_prune(events: Iterable[T]) -> PrunePartition:
    """Split ``events`` into load-bearing (preserve) and droppable (noise).

    Pure: never mutates or deletes. Option (b) building block.
    """
    preserve: list[object] = []
    droppable: list[object] = []
    for event in events:
        if is_load_bearing(event):
            preserve.append(event)
        else:
            droppable.append(event)
    return PrunePartition(preserve=tuple(preserve), droppable=tuple(droppable))


def guard_prune(events: Iterable[T]) -> tuple[T, ...]:
    """Return ``events`` unchanged, or raise if any is load-bearing.

    Option (a): a hard refusal. Use to gate a delete — if this returns, every
    item is safe to drop; if it raises :class:`LoadBearingGuardError`, the
    caller must NOT delete anything.
    """
    materialised = tuple(events)
    protected = [event for event in materialised if is_load_bearing(event)]
    if protected:
        raise LoadBearingGuardError(protected)
    return materialised
