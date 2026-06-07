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

from forge_loop.eventlog.models import EventId, EventRef
from forge_loop.memory.models import MemoryItem, MemoryKind, MemoryProvenance
from forge_loop.memory.store import MemoryStore

__all__ = [
    "record_failed_outcomes",
    "record_merged_outcomes",
    "record_repair_recipe",
]

#: Upper bound on how many characters of a failure reason are persisted as the
#: lesson body. Mirrors the 2000-char cap used by ``runner/_helpers.py`` when it
#: scans worker error text, so a runaway traceback never bloats the memory row.
_MAX_REASON_LEN = 2000


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


def _coerce_failure(record: object) -> tuple[int, str, str, str] | None:
    """Normalise one failed record into ``(issue_number, title, status, reason)``.

    Reuses :func:`_coerce_issue` for the issue-number/title extraction (so the
    two paths share one normalisation scheme) and additionally pulls the
    terminal ``status`` and the ``reason``/``error`` text. Returns ``None`` for
    records without a usable issue number so callers skip them rather than
    crashing the loop.
    """
    coerced = _coerce_issue(record)
    if coerced is None:
        return None
    n, title, _pr = coerced

    status: object
    reason: object
    if isinstance(record, Mapping):
        status = record.get("status", "")
        reason = record.get("reason", record.get("error"))
    else:
        status = getattr(record, "status", "")
        reason = getattr(record, "reason", getattr(record, "error", None))

    status_str = str(status or "").strip()
    reason_str = str(reason or "").strip()[:_MAX_REASON_LEN]
    return n, title, status_str, reason_str


def record_failed_outcomes(
    memory_store: MemoryStore,
    failures: Iterable[object],
    *,
    now: datetime | None = None,
) -> tuple[str, ...]:
    """Record one EPISODIC failure memory per abandoned/failed issue.

    Sibling of :func:`record_merged_outcomes` for the *did-not-ship* path: when
    a worker saga reaches a terminal failure (e.g. ``ABANDONED``), this upserts
    one EPISODIC :class:`MemoryItem` titled ``"failed #N: <title>"`` whose
    structured body carries the terminal ``status`` and the (truncated) failure
    reason as the durable lesson. Provenance uses ``source_task_ref="issue:#N"``
    and ``authored_by="maestro"``; the item is tagged ``("failed",)``.

    The ``memory_id`` is the deterministic ``episodic-failed-{N}`` so re-runs
    upsert in place rather than duplicating — the lesson survives a maestro
    context reset instead of vanishing with the worker.

    Args:
        memory_store: The durable memory store to write to.
        failures: Failed/abandoned records — mappings or objects exposing an
            issue number (``issue``/``number``), optional ``title``, a terminal
            ``status``, and a failure ``reason`` (or ``error``).
        now: Optional fixed timestamp for deterministic provenance; defaults to
            the current UTC time.

    Returns:
        The promoted ``memory_id`` values, in input order, deduplicated by
        issue number (the first record for a given issue wins).
    """
    created_at = now if now is not None else datetime.now(UTC)

    promoted: list[str] = []
    seen: set[int] = set()
    for record in failures:
        coerced = _coerce_failure(record)
        if coerced is None:
            continue
        n, title, status, reason = coerced
        if n in seen:
            continue
        seen.add(n)

        memory_id = f"episodic-failed-{n}"
        failed_title = f"failed #{n}: {title}" if title else f"failed #{n}"
        body_lines = [failed_title, f"status: {status or 'failed'}"]
        if reason:
            body_lines.append(f"lesson: {reason}")
        body = "\n".join(body_lines)

        item = MemoryItem(
            memory_id=memory_id,
            kind=MemoryKind.EPISODIC,
            title=failed_title,
            body=body,
            provenance=MemoryProvenance(
                source_event=None,
                authored_by="maestro",
                source_task_ref=f"issue:#{n}",
                confidence=1.0,
                created_at=created_at,
            ),
            tags=("failed",),
        )
        memory_store.put(item)
        promoted.append(memory_id)

    return tuple(promoted)


def _coerce_touched(touched: object) -> str:
    """Render the file(s)/test(s) a repair touched into a compact string.

    Accepts a single path string or an iterable of paths. The result is capped
    at :data:`_MAX_REASON_LEN` so a sprawling change list never bloats the row.
    """
    if touched is None:
        return ""
    if isinstance(touched, str):
        return touched.strip()[:_MAX_REASON_LEN]
    if isinstance(touched, (list, tuple)):
        joined = ", ".join(str(part).strip() for part in touched if str(part).strip())
        return joined[:_MAX_REASON_LEN]
    return str(touched).strip()[:_MAX_REASON_LEN]


def _coerce_repair(
    record: object,
) -> tuple[int, str, str, str, str, EventRef] | None:
    """Normalise one repair record into a structured recipe tuple.

    Returns ``(issue_number, title, failing_signal, fix, touched, event_ref)`` or
    ``None`` when the record lacks a usable issue number OR an originating event
    reference. Both are required: the recipe's ``memory_id`` is keyed on the
    issue, and the acceptance contract demands the provenance point at the
    repair tick's event, so a record without an ``event_id``/``sequence`` pair
    cannot satisfy it and is skipped rather than written with empty provenance.

    Reuses :func:`_coerce_issue` for the issue-number/title extraction so the
    repair path shares the one normalisation scheme used by the other producers.
    """
    coerced = _coerce_issue(record)
    if coerced is None:
        return None
    n, title, _pr = coerced

    signal: object
    fix: object
    touched: object
    event_id: object
    sequence: object
    if isinstance(record, Mapping):
        signal = record.get("failing_signal", record.get("signal"))
        fix = record.get("fix")
        touched = record.get("touched")
        event_id = record.get("event_id")
        sequence = record.get("event_sequence", record.get("sequence"))
    else:
        signal = getattr(record, "failing_signal", getattr(record, "signal", None))
        fix = getattr(record, "fix", None)
        touched = getattr(record, "touched", None)
        event_id = getattr(record, "event_id", None)
        sequence = getattr(record, "event_sequence", getattr(record, "sequence", None))

    if not event_id or not isinstance(sequence, (int, float, str)):
        return None
    try:
        seq = int(sequence)
    except (TypeError, ValueError):
        return None
    event_ref = EventRef(event_id=EventId(str(event_id)), sequence=seq)

    signal_str = str(signal or "").strip()[:_MAX_REASON_LEN]
    fix_str = str(fix or "").strip()[:_MAX_REASON_LEN]
    touched_str = _coerce_touched(touched)
    return n, title, signal_str, fix_str, touched_str, event_ref


def record_repair_recipe(
    memory_store: MemoryStore,
    repair: object,
    *,
    now: datetime | None = None,
) -> str | None:
    """Record one PROCEDURAL repair-recipe memory item for a validated repair.

    Sibling of :func:`record_merged_outcomes` for the *repair* path (epic #355):
    when a repair tick lands a passing critic (an approved / merged PR produced
    by the repair loop), this upserts ONE PROCEDURAL :class:`MemoryItem`
    capturing a compact, reusable repair recipe — the failing signal that
    triggered the repair, the named fix that was applied, and the file/test it
    touched. It is a *recipe*, NOT a transcript.

    The item's :class:`MemoryProvenance` points at the ORIGINATING event id (the
    repair tick's event) via ``source_event``, so a maestro rebuilding its
    working set after a context loss can trace the recipe back to the concrete
    validated outcome instead of re-deriving a fix that already worked.

    The ``memory_id`` is the deterministic ``procedural-repair-{N}`` so re-runs
    upsert in place rather than duplicating; cross-fix dedup is a separate slice.

    Args:
        memory_store: The durable memory store to write to.
        repair: A repair record — a mapping or object exposing an issue number
            (``issue``/``number``), the ``failing_signal`` (or ``signal``), the
            ``fix`` applied, the ``touched`` file/test (str or iterable), and the
            originating ``event_id`` plus ``event_sequence`` (or ``sequence``).
        now: Optional fixed timestamp for deterministic provenance; defaults to
            the current UTC time.

    Returns:
        The promoted ``memory_id``, or ``None`` when ``repair`` lacks a usable
        issue number or originating event reference.
    """
    created_at = now if now is not None else datetime.now(UTC)

    coerced = _coerce_repair(repair)
    if coerced is None:
        return None
    n, title, signal, fix, touched, event_ref = coerced

    memory_id = f"procedural-repair-{n}"
    recipe_title = f"repair #{n}: {title}" if title else f"repair #{n}"
    body_lines = [recipe_title]
    if signal:
        body_lines.append(f"failing signal: {signal}")
    if fix:
        body_lines.append(f"fix: {fix}")
    if touched:
        body_lines.append(f"touched: {touched}")
    body = "\n".join(body_lines)

    item = MemoryItem(
        memory_id=memory_id,
        kind=MemoryKind.PROCEDURAL,
        title=recipe_title,
        body=body,
        provenance=MemoryProvenance(
            source_event=event_ref,
            authored_by="maestro",
            source_task_ref=f"issue:#{n}",
            confidence=1.0,
            created_at=created_at,
        ),
        tags=("repair",),
    )
    memory_store.put(item)
    return memory_id


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
