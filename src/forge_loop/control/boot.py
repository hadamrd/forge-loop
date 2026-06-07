"""Boot context assembly for maestro reset recovery."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from forge_loop.eventlog import (
    SCORECARD_PROJECTION_NAME,
    ProjectionCursor,
    ScorecardProjection,
    SqliteEventLog,
)
from forge_loop.eventlog.projections import ProjectionReplayError, replay_projection
from forge_loop.frontier import FrontierCursor, FrontierStore
from forge_loop.memory import MemoryItem, MemoryKind, SqliteMemoryStore
from forge_loop.tasks import SqliteTaskSagaStore, TaskSaga

if TYPE_CHECKING:
    from forge_loop.eventlog.projections import Projection, ProjectionEventLog


class BootContextError(RuntimeError):
    """Raised when required durable boot state cannot be loaded."""


class BootFrontierStore(Protocol):
    """Durable frontier source required for maestro boot."""

    def load(self) -> FrontierCursor:
        """Load the current frontier cursor."""
        ...


class BootEventLog(Protocol):
    """Event-log reads needed during boot assembly."""

    def latest_sequence(self) -> int:
        """Return the highest event sequence, or 0 when empty."""
        ...

    def list_projection_cursors(self) -> Mapping[str, ProjectionCursor]:
        """Return saved projection cursors by projection name."""
        ...


class BootMemoryStore(Protocol):
    """Memory reads needed during boot assembly."""

    def list_active(self, *, kind: MemoryKind | None = None) -> tuple[MemoryItem, ...]:
        """Return non-superseded memory items in insertion order."""
        ...

    def list_rejected_paths(self) -> tuple[MemoryItem, ...]:
        """Return active memory items tagged as rejected paths."""
        ...


class BootTaskStore(Protocol):
    """Task saga reads needed during boot assembly."""

    def list_in_flight(self) -> tuple[TaskSaga, ...]:
        """Return non-terminal task sagas in insertion order."""
        ...

    def list_stale(self, *, now: datetime) -> tuple[TaskSaga, ...]:
        """Return non-terminal sagas whose lease has expired by ``now``."""
        ...


@dataclass(frozen=True)
class ProjectionStatus:
    """Boot-time position of one durable projection."""

    sequence: int
    lag: int


@dataclass(frozen=True)
class BootSources:
    """Durable stores used to assemble reset recovery context."""

    frontier_store: BootFrontierStore
    event_log: BootEventLog
    memory_store: BootMemoryStore | None = None
    task_store: BootTaskStore | None = None
    projections: Mapping[str, Projection] = field(default_factory=dict)
    """Live projections to reconcile to the log tail at boot, keyed by name.

    Each registered projection is driven from its saved durable cursor to
    ``event_log.latest_sequence()`` via :func:`replay_projection` before boot
    declares reconstruction complete. Names without a registered projection
    keep their saved-cursor lag (legacy read-only reporting)."""


@dataclass(frozen=True)
class BootContext:
    """Minimum strategic context a maestro needs after a reset."""

    frontier: FrontierCursor
    active_memory_ids: tuple[str, ...] = ()
    rejected_path_memory_ids: tuple[str, ...] = ()
    in_flight_task_ids: tuple[str, ...] = ()
    in_flight_saga_ids: tuple[str, ...] = ()
    stale_saga_ids: tuple[str, ...] = ()
    latest_event_sequence: int = 0
    last_event_sequence: int = 0
    projection_cursors: Mapping[str, ProjectionStatus] = field(default_factory=dict)

    def __post_init__(self) -> None:
        sequence = self.latest_event_sequence or self.last_event_sequence
        if (
            self.latest_event_sequence
            and self.last_event_sequence
            and self.latest_event_sequence != self.last_event_sequence
        ):
            raise ValueError("latest_event_sequence and last_event_sequence differ")
        object.__setattr__(self, "latest_event_sequence", sequence)
        object.__setattr__(self, "last_event_sequence", sequence)

    def summary(self) -> str:
        """Human-readable reset context for logs, prompts, and status views."""
        lines = [self.frontier.boot_summary()]
        if self.active_memory_ids:
            lines.append("memory: " + ", ".join(self.active_memory_ids))
        if self.rejected_path_memory_ids:
            lines.append("rejected_memory: " + ", ".join(self.rejected_path_memory_ids))
        if self.in_flight_task_ids:
            if self.in_flight_saga_ids:
                pairs = (
                    f"{task_id}/{saga_id}"
                    for task_id, saga_id in zip(
                        self.in_flight_task_ids,
                        self.in_flight_saga_ids,
                        strict=True,
                    )
                )
                lines.append("in_flight: " + ", ".join(pairs))
            else:
                lines.append("in_flight: " + ", ".join(self.in_flight_task_ids))
        if self.stale_saga_ids:
            lines.append("stale (dead-worker leases): " + ", ".join(self.stale_saga_ids))
        lines.append(f"event_sequence: {self.latest_event_sequence}")
        if self.projection_cursors:
            projections = [
                f"{name}@{status.sequence} lag={status.lag}"
                for name, status in sorted(self.projection_cursors.items())
            ]
            lines.append("projections: " + ", ".join(projections))
        return "\n".join(lines)


def _drive_projections_to_tail(sources: BootSources, latest_event_sequence: int) -> None:
    """Replay every lagging registered projection to the event-log tail.

    Reuses :func:`replay_projection` (no hand-rolled second replay loop): each
    projection is seeded from its saved durable cursor — so a projection already
    at the tail is a pure no-op — and driven forward. A projection that cannot
    reach the tail (replay raises, or the rebuilt cursor falls short of the head)
    is a HARD boot fault, surfaced as :class:`BootContextError`, never a swallowed
    warning that lets boot proceed on stale state. An empty log skips replay.
    """

    if latest_event_sequence == 0 or not sources.projections:
        return

    # ``BootEventLog`` is structurally a ``ProjectionEventLog`` (the live
    # ``SqliteEventLog`` implements both); cast to the replay contract, mirroring
    # the doctor replay-probe wiring.
    replay_log = cast("ProjectionEventLog", sources.event_log)
    saved = sources.event_log.list_projection_cursors()
    for name, projection in sources.projections.items():
        projection.cursor = saved.get(name, ProjectionCursor(sequence=0))
        try:
            rebuilt = replay_projection(replay_log, name, projection)
        except ProjectionReplayError as exc:
            raise BootContextError(
                f"projection {name!r} could not be replayed to the event-log "
                f"tail at sequence {latest_event_sequence}: {exc}"
            ) from exc
        if rebuilt.sequence != latest_event_sequence:
            raise BootContextError(
                f"projection {name!r} reached sequence {rebuilt.sequence} but the "
                f"event-log head is {latest_event_sequence}; reconstruction is incomplete"
            )


def assemble_boot_context(sources: BootSources, *, now: datetime | None = None) -> BootContext:
    """Assemble compact maestro reset context from durable stores.

    ``now`` anchors stale-lease detection (sagas whose lease has expired are
    the work a dead worker left behind); it defaults to the current UTC time.
    """

    moment = now or datetime.now(UTC)

    # Drive every lagging projection to the log tail BEFORE reading the durable
    # stores, so the frontier/memory/task state and the projection cursors are
    # all observed at the same (head) position — never a head cursor over stale
    # materialised state. A hard fault here aborts boot before any context is
    # returned.
    latest_event_sequence = sources.event_log.latest_sequence()
    _drive_projections_to_tail(sources, latest_event_sequence)

    try:
        frontier = sources.frontier_store.load()
    except FileNotFoundError as exc:
        path = getattr(sources.frontier_store, "path", None)
        location = f" at {Path(path)}" if path is not None else ""
        raise BootContextError(f"frontier state is required{location}") from exc
    except ValueError as exc:
        raise BootContextError(f"frontier state is invalid: {exc}") from exc

    active_memory_ids: tuple[str, ...] = ()
    rejected_path_memory_ids: tuple[str, ...] = ()
    if sources.memory_store is not None:
        active_memory_ids = tuple(item.memory_id for item in sources.memory_store.list_active())
        rejected_path_memory_ids = tuple(
            item.memory_id for item in sources.memory_store.list_rejected_paths()
        )

    in_flight_task_ids: tuple[str, ...] = ()
    in_flight_saga_ids: tuple[str, ...] = ()
    stale_saga_ids: tuple[str, ...] = ()
    if sources.task_store is not None:
        in_flight = sources.task_store.list_in_flight()
        in_flight_task_ids = tuple(saga.task_id for saga in in_flight)
        in_flight_saga_ids = tuple(saga.saga_id for saga in in_flight)
        stale_saga_ids = tuple(saga.saga_id for saga in sources.task_store.list_stale(now=moment))

    projection_cursors = {
        name: ProjectionStatus(
            sequence=cursor.sequence,
            lag=max(latest_event_sequence - cursor.sequence, 0),
        )
        for name, cursor in sources.event_log.list_projection_cursors().items()
    }

    return BootContext(
        frontier=frontier,
        active_memory_ids=active_memory_ids,
        rejected_path_memory_ids=rejected_path_memory_ids,
        in_flight_task_ids=in_flight_task_ids,
        in_flight_saga_ids=in_flight_saga_ids,
        stale_saga_ids=stale_saga_ids,
        latest_event_sequence=latest_event_sequence,
        projection_cursors=projection_cursors,
    )


def canonical_task_saga_path(repo: Path | str) -> Path:
    """The one durable task-saga store ``init`` seeds and ``boot`` reads.

    Single source of truth for the saga store location, shared by ``init``,
    the runner dispatch path, boot assembly, and recovery.
    """
    return Path(repo) / ".forge" / "tasks.db"


def build_boot_sources(repo: Path | str) -> BootSources:
    """Wire the durable boot stores from a repository's ``.forge`` layout.

    Mirrors the canonical paths seeded by ``forge-loop init``. Raises
    :class:`BootContextError` when the frontier cursor is absent, so an
    uninitialised repo fails fast instead of materialising empty event/memory
    stores as a side effect of opening them.
    """
    forge_dir = Path(repo) / ".forge"
    frontier_path = forge_dir / "frontier.yaml"
    if not frontier_path.exists():
        raise BootContextError(
            f"frontier state is required at {frontier_path}; run `forge-loop init` first"
        )
    # Gate the saga store on existence so repos seeded before the task store
    # was canonical (or that never ran a recent `init`) still boot — and so a
    # missing store is not silently materialised on open.
    tasks_path = canonical_task_saga_path(repo)
    task_store = SqliteTaskSagaStore(tasks_path) if tasks_path.exists() else None
    # The ``scorecard`` projection (issue #307) is the first concrete production
    # ``Projection`` registered on this seam: ``assemble_boot_context`` drives it
    # from its saved durable cursor to the event-log tail, materialising trend
    # metrics into ``projection_cursors``. Future projections register the same
    # way — add another keyed entry to this mapping.
    return BootSources(
        frontier_store=FrontierStore(frontier_path),
        event_log=SqliteEventLog(forge_dir / "events.db"),
        memory_store=SqliteMemoryStore(forge_dir / "memory.db"),
        task_store=task_store,
        projections={SCORECARD_PROJECTION_NAME: ScorecardProjection()},
    )
