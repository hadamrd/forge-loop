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

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from forge_loop.eventlog.models import EventRef
from forge_loop.memory.models import (
    MemoryItem,
    MemoryKind,
    MemoryProvenance,
    area_tag,
    derive_memory_id,
    derive_skill_key,
    skill_tag,
)
from forge_loop.memory.store import MemoryStore

__all__ = [
    "HarvestedSkill",
    "harvest_skills_from_merge",
    "record_failed_outcomes",
    "record_merged_outcomes",
    "record_procedural_skill",
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


def record_procedural_skill(
    memory_store: MemoryStore,
    *,
    failing_signal: str,
    target: str,
    title: str,
    body: str,
    source_key: str,
    authored_by: str = "maestro",
    source_task_ref: str | None = None,
    source_event: EventRef | None = None,
    extra_tags: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = (),
    confidence: float = 1.0,
    now: datetime | None = None,
) -> str:
    """Record a validated repair as a PROCEDURAL skill, superseding any prior.

    Procedural memory is a *bounded set of current skills*, not an append-only
    log: a repair of the same ``failing_signal`` on the same ``target`` should
    replace the prior recipe, not pile up beside it. Each repair *instance*
    gets its own fresh ``memory_id`` (derived from ``source_key``); the repair
    *signature* is carried as a stable ``skill:<digest>`` tag. We deliberately
    do NOT use the skill-key as the ``memory_id`` — that would collide and let
    ``put``'s ``ON CONFLICT`` overwrite the prior row in place, destroying
    provenance. Instead we look up the active procedural item bearing the same
    skill-key tag and supersede it explicitly, preserving lineage.

    The new item is upserted BEFORE the supersede call, matching the ordering in
    :func:`record_merged_outcomes`: ``MemoryStore.supersede`` raises
    ``KeyError`` if ``by_memory_id`` is absent, so the replacement must exist
    first.

    The lookup is scoped to *active* items of the *same kind*
    (``list_active(kind=PROCEDURAL)``), so an already-superseded skill is left
    untouched (the chain stays linear) and a coincidentally-matching tag on a
    SEMANTIC/EPISODIC item is ignored.

    Args:
        memory_store: The durable memory store to write to.
        failing_signal: The failure signature being repaired (e.g. an error
            message class).
        target: The target identifier the repair applies to (e.g. a file path).
        title: Human-readable skill title.
        body: The reusable recipe body.
        source_key: A key unique to *this* repair instance, used to derive the
            fresh ``memory_id``. Re-running with the same ``source_key`` upserts
            in place (idempotent) rather than self-superseding.
        authored_by: Provenance author; defaults to ``"maestro"``.
        source_task_ref: Optional provenance task reference.
        source_event: Optional provenance event reference.
        now: Optional fixed timestamp for deterministic provenance.

    Returns:
        The ``memory_id`` of the newly-active procedural item.
    """
    created_at = now if now is not None else datetime.now(UTC)
    skill_key = derive_skill_key(failing_signal, target)
    tag = skill_tag(skill_key)
    memory_id = derive_memory_id(source_key, prefix="procedural")

    # The currently-active procedural skill(s) for this exact signature. By the
    # supersession invariant there is at most one, but we iterate defensively.
    # ``memory_id != ...`` guards the idempotent re-run case (same source_key)
    # so the new row never supersedes itself.
    prior = tuple(
        item
        for item in memory_store.list_active(kind=MemoryKind.PROCEDURAL)
        if tag in item.tags and item.memory_id != memory_id
    )

    item = MemoryItem(
        memory_id=memory_id,
        kind=MemoryKind.PROCEDURAL,
        title=title,
        body=body,
        provenance=MemoryProvenance(
            source_event=source_event,
            authored_by=authored_by,
            source_task_ref=source_task_ref,
            confidence=confidence,
            created_at=created_at,
            supersedes=tuple(old.memory_id for old in prior),
            evidence_refs=evidence_refs,
        ),
        tags=(tag, *extra_tags),
    )
    # Persist the replacement first, THEN supersede — see docstring.
    memory_store.put(item)
    for old in prior:
        memory_store.supersede(old.memory_id, by_memory_id=memory_id)

    return memory_id


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


@dataclass(frozen=True)
class HarvestedSkill:
    """Descriptor of one skill harvested from a merge — enough to emit an event."""

    memory_id: str
    issue: int
    area: str
    skill_key: str
    sha: str
    confidence: float
    title: str


def harvest_skills_from_merge(
    memory_store: MemoryStore,
    merged: Iterable[object],
    *,
    fetch_diff: Callable[[int], tuple[str, str]],
    call_llm: Callable[[str], str],
    now: datetime | None = None,
) -> tuple[HarvestedSkill, ...]:
    """Distil and record one PROCEDURAL skill card per merged outcome.

    For each merged issue: fetch its diff + merged SHA via ``fetch_diff(issue)``,
    distil a :class:`~forge_loop.skill_librarian.SkillCard` via the injected
    ``call_llm`` librarian, and record it as a procedural skill tagged with its
    ``area:`` path and stamped with ``commit:<sha>`` provenance. Outcomes whose
    diff is empty, or whose distillation fails to parse, are skipped — only a
    real, proven recipe is harvested. Idempotent per ``(issue, sha)``: the same
    merge re-harvested upserts in place rather than duplicating.

    Both I/O concerns (the diff fetch and the model call) are injected, so the
    harvest control flow is fully unit-testable without git or the SDK. Returns
    one :class:`HarvestedSkill` per recorded card so the caller can emit events.
    """
    from forge_loop.skill_librarian import distill_skill_from_merge

    harvested: list[HarvestedSkill] = []
    seen: set[int] = set()
    for record in merged:
        coerced = _coerce_issue(record)
        if coerced is None:
            continue
        n, title, _pr = coerced
        if n in seen:
            continue
        seen.add(n)

        diff, sha = fetch_diff(n)
        if not diff.strip():
            continue
        card = distill_skill_from_merge(
            diff=diff, issue_title=title, acceptance="", call_llm=call_llm
        )
        if card is None:
            continue

        skill_key = derive_skill_key(card.failing_signal, card.target)
        card_title = f"{card.area}: {card.trigger}" if card.area else card.trigger
        memory_id = record_procedural_skill(
            memory_store,
            failing_signal=card.failing_signal,
            target=card.target,
            title=card_title,
            body=card.to_body(),
            source_key=f"harvest:{n}:{sha}",
            source_task_ref=f"issue:#{n}",
            extra_tags=(area_tag(card.area),) if card.area.strip() else (),
            evidence_refs=(f"commit:{sha}",),
            confidence=card.confidence,
            now=now,
        )
        harvested.append(
            HarvestedSkill(
                memory_id=memory_id,
                issue=n,
                area=card.area,
                skill_key=skill_key,
                sha=sha,
                confidence=card.confidence,
                title=card_title,
            )
        )

    return tuple(harvested)
