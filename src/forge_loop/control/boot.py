"""Boot context assembly for maestro reset recovery."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from forge_loop.eventlog import ProjectionCursor, SqliteEventLog
from forge_loop.frontier import FrontierCursor, FrontierStore
from forge_loop.memory import MemoryItem, MemoryKind, SqliteMemoryStore
from forge_loop.tasks import SqliteTaskSagaStore, TaskSaga


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


def assemble_boot_context(sources: BootSources, *, now: datetime | None = None) -> BootContext:
    """Assemble compact maestro reset context from durable stores.

    ``now`` anchors stale-lease detection (sagas whose lease has expired are
    the work a dead worker left behind); it defaults to the current UTC time.
    """

    moment = now or datetime.now(UTC)
    try:
        frontier = sources.frontier_store.load()
    except FileNotFoundError as exc:
        path = getattr(sources.frontier_store, "path", None)
        location = f" at {Path(path)}" if path is not None else ""
        raise BootContextError(f"frontier state is required{location}") from exc
    except ValueError as exc:
        raise BootContextError(f"frontier state is invalid: {exc}") from exc

    latest_event_sequence = sources.event_log.latest_sequence()
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
    tasks_path = forge_dir / "tasks.db"
    task_store = SqliteTaskSagaStore(tasks_path) if tasks_path.exists() else None
    return BootSources(
        frontier_store=FrontierStore(frontier_path),
        event_log=SqliteEventLog(forge_dir / "events.db"),
        memory_store=SqliteMemoryStore(forge_dir / "memory.db"),
        task_store=task_store,
    )
