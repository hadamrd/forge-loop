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
import sqlite3
import warnings
from collections import deque
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Protocol, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field

from forge_loop.precommit import PreCommitInstallMethod

_DEFAULT_DURABLE_MIRROR = object()


class _DurableMirror(Protocol):
    def mirror_record(self, record: Mapping[str, Any]) -> object:
        """Mirror a JSONL record into a durable event stream."""


class DurableMirrorError(RuntimeError):
    """Default durable mirror failed after the JSONL record was written."""


class _BestEffortDurableMirror:
    def __init__(self, mirror: _DurableMirror) -> None:
        self._mirror = mirror

    def mirror_record(self, record: Mapping[str, Any]) -> object:
        try:
            return self._mirror.mirror_record(record)
        except (OSError, sqlite3.Error) as exc:
            raise DurableMirrorError(f"{type(exc).__name__}: {exc!s}") from exc
        except Exception as exc:
            raise DurableMirrorError(f"{type(exc).__name__}: {exc!s}") from exc


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def read_events(path: Path, *, tail: int | None = None) -> Iterator[dict[str, Any]]:
    """Yield decoded event records from a JSONL log — the ONE shared reader.

    This is the single home for the "open → iterate lines → ``json.loads``
    → skip ``JSONDecodeError``" tail-loop that was copy-pasted across ~12
    modules (issue #224). ``events.py`` owns event *emission*; this is the
    matching *consumption* primitive. Consumers MUST use this instead of
    re-rolling their own decode loop.

    Behaviour contract (the union of what the old call sites did):

    * Lines are read in binary and decoded as UTF-8 with
      ``errors="replace"`` so a half-written or non-UTF-8 line can never
      raise mid-stream (the TUI / dashboard must not crash on a line the
      runner is still flushing).
    * Blank lines are skipped.
    * Lines that fail ``json.loads`` (partial flush, corruption) are
      skipped silently.
    * Only JSON objects are yielded; non-dict scalars/arrays are dropped
      (the return type is ``Iterator[dict]``).
    * ``tail`` — when given — bounds the result to the last ``tail``
      yielded records (``tail=0`` yields nothing). Memory stays bounded
      via a ``deque`` so tailing a huge log never materialises it whole.

    ``OSError`` from opening ``path`` is deliberately NOT swallowed:
    callers that treat a missing/unreadable log specially keep their own
    ``path.exists()`` / ``try/except OSError`` guard, exactly as before.
    """
    if tail is not None and tail < 0:
        raise ValueError(f"tail must be >= 0, got {tail!r}")

    def _decoded() -> Iterator[dict[str, Any]]:
        with open(path, "rb") as f:
            for raw in f:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    yield rec

    if tail is None:
        return _decoded()
    return iter(deque(_decoded(), maxlen=tail))


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

_EventT = TypeVar("_EventT", bound=EventBase)


def register_event(cls: type[_EventT]) -> type[_EventT]:
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
class AuditViolationFiledEvent(EventBase):
    """One :func:`forge_loop.codebase_audit.file_violations` filing.

    Emitted per ticket created — operators can see the audit pass
    actually translated a manifesto-state violation into a downstream
    work item without grepping the gh API.
    """

    KIND: ClassVar[str] = "audit_violation_filed"
    probe: str = ""
    target: str = ""
    severity: int = Field(ge=1, le=5, default=2)
    issue_number: int = Field(ge=0, default=0)
    title: str = ""


@register_event
class AuditCleanEvent(EventBase):
    """The audit pass found zero violations.

    Emitted at the end of a clean pass — the explicit "clean" event is
    important because absence-of-violations could otherwise look like
    the auditor never ran. ``probes_run`` lets the operator confirm the
    probe set that actually fired.
    """

    KIND: ClassVar[str] = "audit_clean"
    probes_run: list[str] = Field(default_factory=list)


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


@register_event
class EpicSweepDoneEvent(EventBase):
    """One per-tick epic-auto-close sweep result (issue #367).

    Emitted by ``forge_loop.runner.tick_checks.run_epic_sweep`` on the
    maintenance cadence. Each list holds epic issue NUMBERS: ``closed`` were
    auto-closed (all tracked sub-issues resolved); ``expired`` were closed by
    the TTL pass (issue #435 — zero open sub-issues, aged past
    ``epic_ttl_days``), reported distinctly from ``closed`` because they mean
    an undecomposed epic reaped for staleness, not finished work;
    ``skipped_open_subs`` had ≥ 1 still-open sub-issue; ``skipped_no_subs`` had
    no tracked sub-issues; ``errors`` hit a GhClient failure (sub-issue lookup
    or close) and were left untouched.
    """

    KIND: ClassVar[str] = "epic_sweep_done"
    tick: int = Field(ge=0, default=0)
    closed: list[int] = Field(default_factory=list)
    expired: list[int] = Field(default_factory=list)
    skipped_open_subs: list[int] = Field(default_factory=list)
    skipped_no_subs: list[int] = Field(default_factory=list)
    errors: list[int] = Field(default_factory=list)


@register_event
class CheckoutRestoredEvent(EventBase):
    """The shared checkout at ``cfg.repo`` was switched back to base (issue #416).

    The single, documented event for a drifted-then-restored shared checkout,
    emitted by BOTH operational-convergence return arcs (issue #422):

    * ``run_checkout_reconcile`` on the maintenance cadence, and
    * ``restore_base_branch`` in ``_tick``'s end-of-batch ``finally``-guard,

    each ONLY when a checkout parked on a ``loop/<n>`` branch was actually switched
    back to ``base_branch``. ``from_branch`` is the ``loop/<n>`` branch the checkout
    drifted onto; ``to_branch`` is ``base_branch``. A dirty tree, an already-on-base
    checkout, or a non-loop branch never emits this event (a deliberate no-op).
    """

    KIND: ClassVar[str] = "checkout_restored"
    tick: int = Field(ge=0, default=0)
    from_branch: str = ""
    to_branch: str = ""


@register_event
class WorkerPreCommitInstalledEvent(EventBase):
    """Pre-commit hook propagation result for one worker worktree."""

    KIND: ClassVar[str] = "worker_precommit_installed"
    worktree_path: str = ""
    method: PreCommitInstallMethod
    reason: str | None = None


@register_event
class CriticReviewErroredEvent(EventBase):
    """A critic re-review returned ``verdict=error`` (issue #245).

    An ``error`` verdict is a critic CRASH / timeout / parse-failure — NOT a
    real adjudication. The review has no opinion, so the runner MUST NOT carry
    a prior round's ``critic:blocking`` label forward unevaluated (that froze
    PR #231 for ~2.5h). This typed event makes the crash observable LOUD
    instead of silent: the runner clears the stale block labels and leaves the
    PR in an explicit needs-re-review state that the next tick re-derives from
    the current head.

    ``error`` carries the tail of the underlying failure (timeout / parse /
    SDK transport) so an operator can triage without grepping critic-*.log.
    """

    KIND: ClassVar[str] = "critic_review_errored"
    issue: int = Field(ge=0, default=0)
    pr: str | None = None
    verdict: str = "error"
    error: str = ""


@register_event
class WorkerPolicyEnforcedEvent(EventBase):
    """Deny-by-default worker settings were planted from the saga grant (#200).

    Emitted when ``plant_worker_settings`` writes the rendered
    ``CapabilityPolicy`` into a worktree's ``.claude/settings.json``. The
    effective tool/path/server surface now equals the lease rather than the
    operator's full surface.

    ``policy_hash`` is the stable sha256 over the canonical policy JSON
    (``forge_loop.sandbox.policy_hash``). Boot/replay reads this to confirm
    each worker ran within exactly the grant it was leased.

    ``withheld_secrets`` (issue #283) records the NAMES (never values) of the
    operator's secret-shaped env keys that the lease did NOT grant and were
    therefore withheld from the worker child env. Empty when every secret-shaped
    key was leased (or when no secret-shaped keys were present).
    """

    KIND: ClassVar[str] = "worker_policy_enforced"
    worktree_path: str = ""
    policy_hash: str = ""
    withheld_secrets: list[str] = Field(default_factory=list)


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
    rec = event.to_record()
    _write_record(events_path, rec)


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
    durable_mirror = fields.pop("durable_mirror", _DEFAULT_DURABLE_MIRROR)
    durable_mirror_error = fields.pop("durable_mirror_error", None)
    rec = {"ts": _now_iso(), "kind": kind, **fields}
    _write_record(
        events_path,
        rec,
        durable_mirror=durable_mirror,
        durable_mirror_error=durable_mirror_error,
    )


def _write_record(
    events_path: Path,
    rec: dict[str, Any],
    *,
    durable_mirror: object = _DEFAULT_DURABLE_MIRROR,
    durable_mirror_error: str | None = None,
) -> None:
    if durable_mirror is _DEFAULT_DURABLE_MIRROR:
        durable_mirror, default_error = _default_durable_mirror(events_path)
        if durable_mirror_error is None:
            durable_mirror_error = default_error

    events_path.parent.mkdir(parents=True, exist_ok=True)
    with open(events_path, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    kind = str(rec.get("kind", ""))
    _log_event(kind, rec)
    if durable_mirror_error is not None:
        from forge_loop.log import get_logger

        get_logger().warning(
            "durable_mirror_failed",
            kind=kind,
            events_path=str(events_path),
            error=str(durable_mirror_error)[:300],
        )
    if durable_mirror is not None:
        try:
            cast(_DurableMirror, durable_mirror).mirror_record(rec)
        except (OSError, sqlite3.Error, DurableMirrorError) as exc:
            from forge_loop.log import get_logger

            error = (
                str(exc)
                if isinstance(exc, DurableMirrorError)
                else f"{type(exc).__name__}: {exc!s}"
            )
            get_logger().warning(
                "durable_mirror_failed",
                kind=kind,
                events_path=str(events_path),
                error=error[:300],
            )


def _default_durable_mirror(events_path: Path) -> tuple[object | None, str | None]:
    try:
        from forge_loop.eventlog.legacy_mirror import legacy_runner_mirror_for_events_path

        mirror = legacy_runner_mirror_for_events_path(events_path)
        if mirror is None:
            return None, None
        return _BestEffortDurableMirror(mirror), None
    except (OSError, sqlite3.Error) as exc:
        return None, f"{type(exc).__name__}: {exc!s}"


__all__ = [
    "EVENT_REGISTRY",
    "AuditCleanEvent",
    "AuditViolationFiledEvent",
    "CheckoutRestoredEvent",
    "CriticReviewErroredEvent",
    "EpicSweepDoneEvent",
    "EventBase",
    "LoopStartEvent",
    "LoopStopEvent",
    "RedeployEvent",
    "StuckSweepDemotedEvent",
    "TickStartEvent",
    "WorkerSessionRecoveredEvent",
    "WorkerSessionTransitionEvent",
    "WorkerPreCommitInstalledEvent",
    "WorkerPolicyEnforcedEvent",
    "WorktreeReapedEvent",
    "append_event_with_registry_check",
    "emit",
    "read_events",
    "register_event",
]
