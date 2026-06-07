"""Scorecard projection: fold the durable event log into trend metrics.

This is the FIRST concrete :class:`~forge_loop.eventlog.projections.Projection`
registered on the replay framework (issue #307). It activates dormant
scaffolding: the replay-to-tail machinery in :mod:`forge_loop.control.boot`
already exists and is tested, but no projection consumed its output, so
``projection_cursors`` stayed empty. This class is the consumer.

It folds the per-task event timeline (the same shape
:func:`forge_loop.eventlog.legacy_mirror.replay_task_timeline` rebuilds) into
five trend metrics the maestro can read as a TREND signal.

Honest signal grounding (issue #307 forbids fabricated/zero-filled metrics):

* ``first_pass_critic_acceptance_rate`` — backed by the ``legacy_kind`` field
  on the FIRST ``CRITIQUE_ISSUED`` event per task. A first critique whose
  ``legacy_kind`` is ``critic_verdict_merged`` is a first-pass acceptance.
  ``None`` when no task ever received a critique.
* ``mean_repair_rounds_to_converge`` — backed by the count of non-accepting
  ``CRITIQUE_ISSUED`` events per task that reached ``TASK_COMPLETED``. A task
  accepted on its first critique has zero repair rounds. ``None`` when no task
  converged.
* ``sev2_regeneration_rate`` — **always ``None``**. No ``severity`` / ``sev2``
  signal is carried into mirrored event payloads (critic severity tags live in
  PR-comment text, not in the legacy JSONL runner records the mirror folds, nor
  in any registered ``EventKind`` payload). Reporting it ``None`` is mandated by
  the issue rather than zero-filling a signal that does not exist.
* ``mean_lead_time_seconds`` — backed by ``EventEnvelope.occurred_at`` of the
  ``TASK_PLANNED`` and the terminal ``TASK_COMPLETED`` event for a task. Tasks
  still in flight (no completion) or that ended in failure/compensation are
  excluded. ``None`` when no task has both planned and completed timestamps.
* ``abandonment_rate`` — backed by the terminal ``EventKind`` per task: the
  fraction of terminal tasks that ended in ``TASK_FAILED`` / ``TASK_COMPENSATED``
  rather than ``TASK_COMPLETED``. ``None`` when no task reached a terminal state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from forge_loop.eventlog.legacy_mirror import LegacyRunnerEventKind
from forge_loop.eventlog.models import EventEnvelope, EventKind
from forge_loop.eventlog.projections import ProjectionCursor

#: The registration key used in ``build_boot_sources`` and the cursor table.
SCORECARD_PROJECTION_NAME = "scorecard"

_TERMINAL_KINDS = frozenset(
    {EventKind.TASK_COMPLETED, EventKind.TASK_FAILED, EventKind.TASK_COMPENSATED}
)
_ABANDONMENT_KINDS = frozenset({EventKind.TASK_FAILED, EventKind.TASK_COMPENSATED})


@dataclass
class _TaskAccumulator:
    """Per-task fold state, mirroring ``replay_task_timeline``'s grouping."""

    planned_at: datetime | None = None
    completed_at: datetime | None = None
    terminal_kind: EventKind | None = None
    critique_count: int = 0
    accepting_critique_count: int = 0
    first_critique_accepted: bool | None = None


@dataclass(frozen=True)
class ScorecardMetrics:
    """Trend snapshot derived from the event log.

    Every field is ``float | None``: ``None`` means the backing signal is
    genuinely absent (no data, or — for ``sev2_regeneration_rate`` — no payload
    field carries it). A ``None`` is never silently coerced to ``0.0``.
    """

    first_pass_critic_acceptance_rate: float | None = None
    mean_repair_rounds_to_converge: float | None = None
    sev2_regeneration_rate: float | None = None
    mean_lead_time_seconds: float | None = None
    abandonment_rate: float | None = None


@dataclass
class ScorecardProjection:
    """Concrete projection folding events into :class:`ScorecardMetrics`.

    Structurally satisfies the ``Projection`` protocol: a mutable ``cursor``
    attribute plus ``apply(event)``. ``apply`` is idempotent by sequence — an
    event at or below the current cursor sequence is a no-op — so re-running a
    full replay over an already-tailed projection leaves every metric unchanged.
    """

    cursor: ProjectionCursor = field(default_factory=ProjectionCursor)
    _tasks: dict[str, _TaskAccumulator] = field(default_factory=dict)

    def apply(self, event: EventEnvelope) -> None:
        """Fold one event. Idempotent by sequence (see class docstring)."""
        if event.sequence <= self.cursor.sequence:
            return
        self._fold(event)
        self.cursor = ProjectionCursor(sequence=event.sequence)

    def _fold(self, event: EventEnvelope) -> None:
        task_id = event.task_id
        if task_id is None:
            # Tick/loop-level events (no task identity) carry no scorecard
            # signal; the cursor still advances so the tail is reached.
            return
        task = self._tasks.setdefault(task_id, _TaskAccumulator())
        kind = event.kind
        if kind is EventKind.TASK_PLANNED:
            if task.planned_at is None:
                task.planned_at = event.occurred_at
        elif kind is EventKind.TASK_COMPLETED:
            task.terminal_kind = EventKind.TASK_COMPLETED
            task.completed_at = event.occurred_at
        elif kind in _ABANDONMENT_KINDS:
            # A genuine completion outranks a later failure/compensation record.
            if task.terminal_kind is not EventKind.TASK_COMPLETED:
                task.terminal_kind = kind
        elif kind is EventKind.CRITIQUE_ISSUED:
            verdict = event.payload.get("legacy_kind")
            if verdict is None:
                # No verdict field backs this critique — treat as genuinely
                # unknown rather than fabricating a non-acceptance data point.
                return
            accepted = verdict == LegacyRunnerEventKind.CRITIC_VERDICT_MERGED.value
            task.critique_count += 1
            if accepted:
                task.accepting_critique_count += 1
            if task.first_critique_accepted is None:
                task.first_critique_accepted = accepted

    def metrics(self) -> ScorecardMetrics:
        """Return the current trend snapshot (deterministic, pure)."""
        tasks = tuple(self._tasks.values())

        critiqued = [t for t in tasks if t.first_critique_accepted is not None]
        first_pass = (
            sum(1 for t in critiqued if t.first_critique_accepted) / len(critiqued)
            if critiqued
            else None
        )

        converged = [t for t in tasks if t.terminal_kind is EventKind.TASK_COMPLETED]
        repair_rounds = (
            sum(t.critique_count - t.accepting_critique_count for t in converged) / len(converged)
            if converged
            else None
        )

        lead_samples = [
            (t.completed_at - t.planned_at).total_seconds()
            for t in converged
            if t.planned_at is not None and t.completed_at is not None
        ]
        mean_lead = sum(lead_samples) / len(lead_samples) if lead_samples else None

        terminal = [t for t in tasks if t.terminal_kind is not None]
        abandoned = [t for t in terminal if t.terminal_kind in _ABANDONMENT_KINDS]
        abandonment = len(abandoned) / len(terminal) if terminal else None

        return ScorecardMetrics(
            first_pass_critic_acceptance_rate=first_pass,
            mean_repair_rounds_to_converge=repair_rounds,
            sev2_regeneration_rate=None,  # no backing payload field (see docstring)
            mean_lead_time_seconds=mean_lead,
            abandonment_rate=abandonment,
        )
