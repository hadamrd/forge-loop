"""Close the cognition feedback loop: write memory from real outcomes.

Today the maestro READS curated memory but nothing WRITES it from real
outcomes. When the loop merges PRs, we want to record durable episodic memory
so future ticks and boots know what actually shipped.

This module is deliberately pure and injectable: it takes a :class:`MemoryStore`
and a description of what merged, and records one EPISODIC memory item per
merged issue. It performs no GitHub / network / SDK calls and is fully
deterministic, so it can be unit-tested against a real
:class:`~forge_loop.memory.store.SqliteMemoryStore`.

Idempotency is structural: each merged issue maps to a deterministic
``memory_id`` (``episodic-shipped-{n}``), and the store's :meth:`put` performs
an idempotent upsert, so re-recording the same merged issue never creates a
duplicate row.

When a ticket that previously failed (an active ``episodic-failed-{n}`` written
by the failure-episode track, parent #346) later merges, recording its merged
outcome also supersedes that failure episode with the shipped item, so a
maestro rebuilding its working set from ``list_active`` never sees a stale
"this ticket failed" lesson for a ticket that has since shipped.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

from forge_loop.memory.models import MemoryItem, MemoryKind, MemoryProvenance
from forge_loop.memory.store import MemoryStore

__all__ = ["record_merged_outcomes"]


def _coerce_issue(merged: object) -> tuple[int, str, str | None] | None:
    """Normalise one merged record into ``(issue_number, title, pr_url)``.

    Accepts either a mapping (e.g. ``{"issue": 7, "title": "x"}``) or any
    object exposing ``issue``/``title``/``pr_url`` attributes (e.g. a
    ``WorkerOutcome``). Returns ``None`` for records without a usable issue
    number so callers can skip them rather than crashing the loop.
    """
    number: object
    title: object
    pr_url: object
    if isinstance(merged, Mapping):
        number = merged.get("issue", merged.get("number"))
        title = merged.get("title", "")
        pr_url = merged.get("pr_url", merged.get("pr"))
    else:
        number = getattr(merged, "issue", getattr(merged, "number", None))
        title = getattr(merged, "title", "")
        pr_url = getattr(merged, "pr_url", None)

    if not isinstance(number, (int, float, str)):
        return None
    try:
        n = int(number)
    except (TypeError, ValueError):
        return None

    title_str = str(title or "").strip()
    pr_str = str(pr_url).strip() if pr_url else None
    return n, title_str, (pr_str or None)


def record_merged_outcomes(
    memory_store: MemoryStore,
    merged: Iterable[object],
    *,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Record one EPISODIC memory item per merged issue.

    For each merged issue an EPISODIC :class:`MemoryItem` titled
    ``"shipped #N: <title>"`` is upserted, carrying a
    :class:`MemoryProvenance` with ``source_task_ref="issue:#N"`` and
    ``authored_by="maestro"``. The ``memory_id`` is the deterministic
    ``episodic-shipped-{N}`` so re-runs upsert in place rather than
    duplicating.

    Args:
        memory_store: The durable memory store to write to.
        merged: Merged records — mappings or objects exposing an issue number
            (``issue``/``number``), optional ``title`` and optional ``pr_url``.
        now: Optional fixed timestamp for deterministic provenance; defaults to
            the current UTC time.

    Returns:
        The promoted ``memory_id`` values, in input order, deduplicated by
        issue number (the first record for a given issue wins).
    """
    created_at = now if now is not None else datetime.now(UTC)

    promoted: list[str] = []
    seen: set[int] = set()
    for record in merged:
        coerced = _coerce_issue(record)
        if coerced is None:
            continue
        n, title, pr_url = coerced
        if n in seen:
            continue
        seen.add(n)

        memory_id = f"episodic-shipped-{n}"
        shipped_title = f"shipped #{n}: {title}" if title else f"shipped #{n}"
        body_lines = [shipped_title]
        if pr_url:
            body_lines.append(f"PR: {pr_url}")
        body = "\n".join(body_lines)

        item = MemoryItem(
            memory_id=memory_id,
            kind=MemoryKind.EPISODIC,
            title=shipped_title,
            body=body,
            provenance=MemoryProvenance(
                source_event=None,
                authored_by="maestro",
                source_task_ref=f"issue:#{n}",
                confidence=1.0,
                created_at=created_at,
            ),
            tags=("shipped",),
        )
        # The shipped item MUST be upserted before the supersede call below:
        # ``MemoryStore.supersede`` raises ``KeyError`` if ``by_memory_id`` is
        # absent, so the superseding row has to exist first.
        memory_store.put(item)
        promoted.append(memory_id)

        # Supersede the failure episode (parent #346) for this ticket if one is
        # still active: when #N first fails and later merges, the stale
        # "this ticket failed" lesson must not survive into the working set.
        # We reuse the supersession path (not delete) so provenance stays
        # intact. An already-superseded failure item is left untouched — we
        # only re-point a failure episode that is still active, so we never
        # clobber an existing supersession.
        failure_id = f"episodic-failed-{n}"
        failure_item = memory_store.get(failure_id)
        if failure_item is not None and failure_item.is_active:
            memory_store.supersede(failure_id, by_memory_id=memory_id)

    return tuple(promoted)
