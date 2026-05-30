"""Typed events for the forge-loop event log (issue #88).

Today every event is written via ``state.append_event(events_file, kind, **fields)``
— a loose ``str`` + ``**kwargs`` shape. That's fine for ad-hoc emissions
but means:

* No schema validation: a typo like ``count=`` instead of ``n=`` ships
  silently and breaks downstream consumers.
* No discoverability: operators / dashboards / typed-events epic #95
  can't know what fields a given ``kind`` carries without grepping every
  call site.
* No discriminated-union shape: ``events.jsonl`` is a soup of mixed
  payloads keyed only by ``kind``, which thwarts schema-aware tooling.

This module introduces a typed, registry-based ``Event`` abstraction.
Each known kind is declared once as a Pydantic model; ``emit(events_file,
event)`` writes it after validation. The legacy ``append_event`` path
keeps working (untyped tail), but emits a deprecation warning when the
kind is one we've already declared a typed model for — so the long-tail
migration from ad-hoc to typed is gated by adoption pressure, not a
single big-bang rewrite.

Migration pattern for a call site:

    # before
    append_event(events_file, "redeploy", ok=True, dur_s=12.3)

    # after
    emit(events_file, RedeployEvent(ok=True, dur_s=12.3))
"""

from __future__ import annotations

import json
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class EventBase(BaseModel):
    """Discriminated-union base for every typed event.

    Subclasses declare ``KIND: ClassVar[str]`` — the discriminator — plus
    their payload fields. Pydantic enforces field presence + types at
    ``emit()`` time, before the JSON line hits disk.

    ``extra="allow"`` is intentional during the migration window: legacy
    call sites pass a superset of fields (e.g. ``loop_start`` carries
    historical knobs like ``runner_id`` / ``queue_backend`` that aren't
    in the core schema). Once all call sites migrate we'll flip this to
    ``forbid`` and the registry will be canonical.
    """

    model_config = ConfigDict(extra="allow")

    # Subclasses MUST override KIND; the base value is sentinel-only.
    KIND: ClassVar[str] = "_unset"

    def to_record(self) -> dict[str, Any]:
        """Serialise to the on-disk shape — ``{ts, kind, ...payload}``.

        ``ts`` and ``kind`` are stamped here so subclasses can't shadow
        them with their own field declarations.
        """
        payload = self.model_dump(mode="json")
        return {"ts": _now_iso(), "kind": self.KIND, **payload}


# ---------------------------------------------------------------------------
# Registry of known kinds. Populated via :func:`register_event`. Used by
# :func:`append_event_with_registry_check` to warn when a loose-shape call
# site emits a ``kind`` for which a typed model already exists.
# ---------------------------------------------------------------------------


EVENT_REGISTRY: dict[str, type[EventBase]] = {}


def register_event(cls: type[EventBase]) -> type[EventBase]:
    """Class decorator: register a typed event so the loose-shape path
    can warn when callers emit it without going through ``emit()``.
    """
    kind = getattr(cls, "KIND", "_unset")
    if kind == "_unset":
        raise ValueError(f"{cls.__name__} must set KIND ClassVar")
    if kind in EVENT_REGISTRY:
        raise ValueError(
            f"event kind {kind!r} already registered to {EVENT_REGISTRY[kind].__name__}"
        )
    EVENT_REGISTRY[kind] = cls
    return cls


# ---------------------------------------------------------------------------
# Typed schemas for the high-value events. The long tail of ad-hoc kinds
# stays loose for now — this PR ships the framework + the first wave;
# follow-up PRs migrate per-domain (deploy, worker, drift, ...).
# ---------------------------------------------------------------------------


@register_event
class LoopStartEvent(EventBase):
    """Runner boot. Emitted once per process startup."""

    KIND: ClassVar[str] = "loop_start"
    parallel: int = Field(ge=1)
    tick_interval: int = Field(ge=1)
    max_ticks: int = Field(ge=0)


@register_event
class LoopStopEvent(EventBase):
    """Runner shutdown. Emitted from the signal handler or max-ticks exit."""

    KIND: ClassVar[str] = "loop_stop"
    tick: int = Field(ge=0)


@register_event
class TickStartEvent(EventBase):
    """One tick of the dispatch loop is starting.

    ``issues`` is the list of issue numbers we plan to dispatch this tick
    (post-filter, post-cooldown). Empty list = no work this tick.
    """

    KIND: ClassVar[str] = "tick_start"
    tick: int = Field(ge=1)
    issues: list[int] = Field(default_factory=list)


@register_event
class RedeployEvent(EventBase):
    """Result of one ``deploy_task`` invocation after a worker merge.

    Tracked by drift detection — three consecutive failures with the
    same error signature halt the loop (opt-in via ``deploy.drift_halt``).
    """

    KIND: ClassVar[str] = "redeploy"
    task: str = ""
    ok: bool
    detail: str = ""


@register_event
class WorkerSessionRecoveredEvent(EventBase):
    """A non-terminal session was rediscovered at runner boot (issue #111).

    Emitted once per session encountered during the crash-recovery walk.
    ``action`` is one of:

    - ``redispatch``: ``DISPATCHED`` survivor — re-dispatch normally.
    - ``refire_critic``: ``AWAITING_CRITIC`` survivor — re-run critic.
    - ``promote_to_awaiting_critic``: ``RUNNING`` / ``REVISING`` survivor
      whose worktree + PR both still exist; promoted so critic picks up.
    - ``abandon``: state lost (no worktree, no PR) — moved to ABANDONED.
    """

    KIND: ClassVar[str] = "worker_session_recovered"
    session_id: str = ""
    issue: int = 0
    prior_state: str = ""
    action: str = ""
    new_state: str = ""
    worktree_present: bool = False
    pr_present: bool = False
    reason: str = ""


@register_event
class WorkerSessionTransitionEvent(EventBase):
    """One FSM edge in the persistent-worker store (issue #108).

    Emitted on every transition the dispatch loop drives:
    ``-> DISPATCHED`` (fresh seed), ``DISPATCHED -> RUNNING`` (SDK
    starting), ``RUNNING -> AWAITING_CRITIC`` (PR opened),
    ``RUNNING -> ABANDONED`` (worker failed).

    ``prior_state`` is the empty string on the initial seed
    (``-> DISPATCHED``) because there is no prior state — every other
    edge carries both endpoints.
    """

    KIND: ClassVar[str] = "worker_session_transition"
    session_id: str = ""
    issue: int = 0
    prior_state: str = ""
    new_state: str = ""
    reason: str = ""
    pr_url: str | None = None


@register_event
class WorktreeReapedEvent(EventBase):
    """Per-issue worktree cleanup after a worker outcome that doesn't
    need the directory preserved for inspection (merged/open path)."""

    KIND: ClassVar[str] = "worktree_reaped"
    issue: int = Field(ge=1)
    status: str = ""


@register_event
class StuckSweepDemotedEvent(EventBase):
    """A per-tick stuck-sweep decision (issue #129).

    Emitted by ``forge_loop.stuck_sweep.sweep`` whenever it touches an
    issue — successful demotions carry ``ok=True``; gh API failures
    carry ``ok=False`` plus a ``reason`` so the operator can see what
    blew up without grepping structlog.

    Idempotency skips (issue already lost ``loop:ready``) are NOT
    emitted — there's nothing operationally interesting about them.
    """

    KIND: ClassVar[str] = "stuck_sweep_demoted"
    issue: int = Field(ge=1)
    attempts: int = Field(ge=1)
    last_state: str = ""
    pr_url: str | None = None
    ok: bool = True
    reason: str = ""


# ---------------------------------------------------------------------------
# Emit + back-compat shim. ``emit`` is the typed path; ``append_event_with_
# registry_check`` is the back-compat wrapper called by state.append_event.
# ---------------------------------------------------------------------------


def emit(events_path: Path, event: EventBase) -> None:
    """Append a typed event to the events log AND mirror it to structlog.

    Validates the event at construction (Pydantic) and at write time
    (this function) so a malformed payload can't reach disk.

    Logging level heuristic (issue #89): events whose KIND includes
    ``fail``/``error``/``halt``/``drift``/``refused`` emit at WARNING;
    everything else is INFO. This gives operators a useful default
    on the live log stream without per-kind classification.
    """
    if not isinstance(event, EventBase):
        raise TypeError(f"emit expected EventBase, got {type(event).__name__}")
    events_path.parent.mkdir(parents=True, exist_ok=True)
    rec = event.to_record()
    with open(events_path, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    _log_event(event.KIND, rec)


def _log_event(kind: str, rec: dict[str, Any]) -> None:
    """Mirror an event to structlog at the appropriate level."""
    from forge_loop.log import get_logger

    logger = get_logger()
    # Drop the timestamp + kind from the payload — structlog stamps its own
    # timestamp, and ``kind`` is the message.
    payload = {k: v for k, v in rec.items() if k not in ("ts", "kind")}
    severity_markers = ("fail", "error", "halt", "drift", "refused", "stop")
    if any(m in kind.lower() for m in severity_markers):
        logger.warning(kind, **payload)
    else:
        logger.info(kind, **payload)


def append_event_with_registry_check(events_path: Path, kind: str, **fields: Any) -> None:
    """Loose-shape emission with registry-aware deprecation hinting.

    Called by :func:`forge_loop.state.append_event` to keep every legacy
    call site working. If ``kind`` is in EVENT_REGISTRY, emit a
    ``DeprecationWarning`` pointing the operator at the typed model — but
    still write the record so behaviour stays identical until callers
    migrate.

    NOTE: We do NOT validate the loose ``**fields`` against the registered
    schema. Validation would break call sites that pass a superset of
    fields (legitimate during the migration window). The migration plan:
    issue #88 ships the framework + 5 typed events; per-domain follow-ups
    migrate the long tail and then flip a CI gate that rejects unregistered
    kinds.
    """
    if kind in EVENT_REGISTRY:
        warnings.warn(
            f"event kind {kind!r} has a typed model "
            f"({EVENT_REGISTRY[kind].__name__}); prefer "
            f"`emit(events_file, {EVENT_REGISTRY[kind].__name__}(...))` "
            "over the loose append_event() path.",
            DeprecationWarning,
            stacklevel=3,
        )
    events_path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": _now_iso(), "kind": kind, **fields}
    with open(events_path, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    _log_event(kind, rec)


__all__ = [
    "EVENT_REGISTRY",
    "EventBase",
    "LoopStartEvent",
    "LoopStopEvent",
    "RedeployEvent",
    "StuckSweepDemotedEvent",
    "TickStartEvent",
    "WorkerSessionRecoveredEvent",
    "WorkerSessionTransitionEvent",
    "WorktreeReapedEvent",
    "append_event_with_registry_check",
    "emit",
    "register_event",
]
