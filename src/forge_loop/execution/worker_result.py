"""Structured worker result contracts.

Workers may have noisy internal context, but their durable output must be
compact and structured so the maestro and memory curator can reason over it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class WorkerStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class WorkerObservation:
    """A compact observation emitted by a disposable worker."""

    summary: str
    proposed_memory: bool = False
    reason_to_remember: str = ""


@dataclass(frozen=True)
class WorkerResult:
    """Durable result shape returned by a worker runtime."""

    task_id: str
    status: WorkerStatus
    patch_ref: str | None = None
    tests_run: tuple[str, ...] = ()
    observations: tuple[WorkerObservation, ...] = ()
    risks: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()
