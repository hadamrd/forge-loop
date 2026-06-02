"""Mirror legacy runner JSONL records into the durable event log."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from forge_loop.eventlog.models import EventEnvelope, EventKind
from forge_loop.eventlog.store import EventLog


class LegacyRunnerEventKind(StrEnum):
    """Legacy JSONL runner milestones that can be mirrored durably."""

    TICK_START = "tick_start"
    TICK_DONE = "tick_done"
    PO_DONE = "po_done"
    WORKER_START = "worker_start"
    WORKER_DONE = "worker_done"
    WORKER_FAILED = "worker_failed"
    WORKER_STARTED = "worker_started"
    WORKER_SESSION_TRANSITION = "worker_session_transition"
    WORKER_ITERATION_ATTEMPT = "worker_iteration_attempt"
    WATCHDOG_WORKER_STUCK = "watchdog_worker_stuck"
    WATCHDOG_WORKER_KILLED = "watchdog_worker_killed"
    STAGE_TIMEOUT = "stage_timeout"
    STAGE_ERROR = "stage_error"
    CRITIC_VERDICT_MERGED = "critic_verdict_merged"
    CRITIC_VERDICT_BLOCKED = "critic_verdict_blocked"
    CRITIC_VERDICT_REVISING = "critic_verdict_revising"
    CRITIC_VERDICT_UNKNOWN = "critic_verdict_unknown"
    CRITIC_DONE = "critic_done"
    MERGE_REFUSED_ISSUE_CLOSED = "merge_refused_issue_closed"
    POST_CRITIC_AUTOMERGE_ENABLED = "post_critic_automerge_enabled"
    POST_CRITIC_AUTOMERGE_FAILED = "post_critic_automerge_failed"
    WORKER_WORK_RESCUED = "worker_work_rescued"
    WORKTREE_REAPED = "worktree_reaped"
    LOOP_DRIFT_HALT = "loop_drift_halt"
    DEPLOY_DRIFT_HALT = "deploy_drift_halt"
    MAX_TICKS_REACHED = "max_ticks_reached"
    LOOP_STOP = "loop_stop"


class LegacyRunnerStage(StrEnum):
    """Legacy async runner stage names carried in stage events."""

    WORKER = "worker"


class LegacyWorkerStatus(StrEnum):
    """Legacy worker outcome labels mirrored from JSONL payloads."""

    MERGED = "merged"
    OPEN = "open"
    FAILED = "failed"
    NO_PR = "no_pr"
    TIMEOUT = "timeout"
    STALE = "stale"
    COMPENSATED = "compensated"


_TERMINAL_STATUS_TO_KIND: dict[LegacyWorkerStatus, EventKind] = {
    LegacyWorkerStatus.MERGED: EventKind.TASK_COMPLETED,
    LegacyWorkerStatus.OPEN: EventKind.TASK_COMPLETED,
    LegacyWorkerStatus.FAILED: EventKind.TASK_FAILED,
    LegacyWorkerStatus.NO_PR: EventKind.TASK_FAILED,
    LegacyWorkerStatus.TIMEOUT: EventKind.TASK_FAILED,
    LegacyWorkerStatus.STALE: EventKind.TASK_FAILED,
    LegacyWorkerStatus.COMPENSATED: EventKind.TASK_COMPENSATED,
}


@dataclass(frozen=True)
class _AppendSpec:
    kind: EventKind
    payload: Mapping[str, Any]
    tick: int | None = None
    issue: int | None = None
    worker: str | None = None
    pr_url: str | None = None
    discriminator: str = ""


class LegacyEventMirror:
    """Translate selected legacy runner records into durable event envelopes."""

    def __init__(self, event_log: EventLog) -> None:
        self._event_log = event_log

    def mirror_record(self, record: Mapping[str, Any]) -> tuple[EventEnvelope, ...] | None:
        """Mirror one legacy JSONL record.

        Unknown legacy kinds are intentionally ignored. The JSONL stream is
        still the operator-facing source during the migration window.
        """

        legacy_kind = _legacy_kind(record)
        if legacy_kind is None:
            return None
        specs = _append_specs(legacy_kind, record)
        if not specs:
            return None
        return tuple(self._append(legacy_kind, spec) for spec in specs)

    def _append(self, legacy_kind: LegacyRunnerEventKind, spec: _AppendSpec) -> EventEnvelope:
        task_id = f"issue:{spec.issue}" if spec.issue is not None else None
        saga_id = f"tick:{spec.tick}" if spec.tick is not None else None
        return self._event_log.append(
            spec.kind,
            spec.payload,
            task_id=task_id,
            saga_id=saga_id,
            idempotency_key=_idempotency_key(legacy_kind, spec),
        )


def replay_task_timeline(events: Iterable[EventEnvelope]) -> dict[str, dict[str, Any]]:
    """Rebuild a compact per-task timeline from durable mirrored events."""

    timeline: dict[str, dict[str, Any]] = {}
    for event in events:
        issue = _issue_from_event(event)
        if issue is None:
            continue
        task_key = f"issue:{issue}"
        task = timeline.setdefault(
            task_key,
            {
                "issue": issue,
                "ticks": [],
                "planned": False,
                "dispatched": False,
                "terminal": None,
                "pr_url": None,
                "last_sequence": 0,
            },
        )
        tick = event.payload.get("tick")
        if isinstance(tick, int) and tick not in task["ticks"]:
            task["ticks"].append(tick)
        if event.kind is EventKind.TASK_PLANNED:
            task["planned"] = True
        elif event.kind is EventKind.TASK_DISPATCHED:
            task["dispatched"] = True
        elif event.kind in {
            EventKind.TASK_COMPLETED,
            EventKind.TASK_FAILED,
            EventKind.TASK_COMPENSATED,
        }:
            task["terminal"] = event.payload.get("status")
        if event.kind is EventKind.PR_OPENED or "pr_url" in event.payload:
            task["pr_url"] = event.payload.get("pr_url")
        task["last_sequence"] = max(task["last_sequence"], event.sequence)
    return timeline


def legacy_runner_event_log_path(events_path: Path) -> Path | None:
    """Return the durable WAL path for the canonical runner JSONL stream."""

    if events_path.name != "loop-runner-events.jsonl":
        return None
    if events_path.parent.name != "ops" or events_path.parent.parent.name != "docs":
        return None
    return events_path.parent.parent.parent / ".forge" / "events.db"


def legacy_runner_mirror_for_events_path(events_path: Path) -> LegacyEventMirror | None:
    """Return a mirror for the canonical runner JSONL file, if any."""

    event_log_path = legacy_runner_event_log_path(events_path)
    if event_log_path is None:
        return None
    from forge_loop.eventlog.sqlite import SqliteEventLog

    event_log_path.parent.mkdir(parents=True, exist_ok=True)
    return LegacyEventMirror(SqliteEventLog(event_log_path))


def _legacy_kind(record: Mapping[str, Any]) -> LegacyRunnerEventKind | None:
    raw_kind = record.get("kind")
    if not isinstance(raw_kind, str):
        return None
    try:
        return LegacyRunnerEventKind(raw_kind)
    except ValueError:
        return None


def _stage(record: Mapping[str, Any]) -> LegacyRunnerStage | None:
    raw_stage = record.get("stage")
    if not isinstance(raw_stage, str):
        return None
    try:
        return LegacyRunnerStage(raw_stage)
    except ValueError:
        return None


def _worker_status(record: Mapping[str, Any]) -> LegacyWorkerStatus | None:
    raw_status = record.get("status")
    if not isinstance(raw_status, str):
        return None
    try:
        return LegacyWorkerStatus(raw_status)
    except ValueError:
        return None


def _append_specs(
    legacy_kind: LegacyRunnerEventKind,
    record: Mapping[str, Any],
) -> tuple[_AppendSpec, ...]:
    tick = _optional_int(record.get("tick"))
    issue = _issue_from_record(record)
    payload = _payload(record)
    worker = _worker_ref(record)

    if legacy_kind is LegacyRunnerEventKind.TICK_START:
        return (_AppendSpec(EventKind.TICK_STARTED, payload, tick=tick),)
    if legacy_kind is LegacyRunnerEventKind.PO_DONE:
        return tuple(
            _AppendSpec(
                EventKind.TASK_PLANNED,
                {**payload, "issue": planned_issue},
                tick=tick,
                issue=planned_issue,
            )
            for planned_issue in _int_list(record.get("expanded"))
        )
    if legacy_kind in {
        LegacyRunnerEventKind.WORKER_START,
        LegacyRunnerEventKind.WORKER_STARTED,
    }:
        return (
            _AppendSpec(
                EventKind.TASK_DISPATCHED,
                payload,
                tick=tick,
                issue=issue,
                worker=worker,
            ),
        )
    if legacy_kind is LegacyRunnerEventKind.WORKER_SESSION_TRANSITION:
        return _worker_session_transition_specs(record, payload, tick, issue, worker)
    if legacy_kind is LegacyRunnerEventKind.WORKER_ITERATION_ATTEMPT:
        return (
            _AppendSpec(
                EventKind.TASK_DISPATCHED,
                payload,
                tick=tick,
                issue=issue,
                worker=worker,
                discriminator=_discriminator(record, "attempt", "state", "status"),
            ),
        )
    if legacy_kind is LegacyRunnerEventKind.WORKER_DONE:
        return _worker_done_specs(record, payload, tick, worker)
    if legacy_kind is LegacyRunnerEventKind.WORKER_FAILED:
        return (
            _AppendSpec(
                EventKind.TASK_FAILED,
                {**payload, "status": "failed"},
                tick=tick,
                issue=issue,
                worker=worker,
                pr_url=_pr_url(record),
            ),
        )
    if legacy_kind in {
        LegacyRunnerEventKind.WATCHDOG_WORKER_STUCK,
        LegacyRunnerEventKind.WATCHDOG_WORKER_KILLED,
    }:
        return (
            _AppendSpec(
                EventKind.TASK_HEARTBEAT,
                payload,
                tick=tick,
                issue=issue,
                worker=worker,
            ),
        )
    if legacy_kind is LegacyRunnerEventKind.STAGE_TIMEOUT:
        if _stage(record) is LegacyRunnerStage.WORKER:
            return (
                _AppendSpec(
                    EventKind.TASK_FAILED,
                    {**payload, "status": "timeout"},
                    tick=tick,
                    issue=issue,
                ),
            )
        return (_AppendSpec(EventKind.WORKER_OBSERVATION, payload, tick=tick, issue=issue),)
    if legacy_kind is LegacyRunnerEventKind.STAGE_ERROR:
        if _stage(record) is LegacyRunnerStage.WORKER:
            return (
                _AppendSpec(
                    EventKind.TASK_FAILED,
                    {**payload, "status": "failed"},
                    tick=tick,
                    issue=issue,
                ),
            )
        return (_AppendSpec(EventKind.WORKER_OBSERVATION, payload, tick=tick, issue=issue),)
    if legacy_kind is LegacyRunnerEventKind.TICK_DONE:
        return _tick_done_specs(record, payload, tick)
    if legacy_kind in {
        LegacyRunnerEventKind.CRITIC_VERDICT_MERGED,
        LegacyRunnerEventKind.CRITIC_VERDICT_BLOCKED,
        LegacyRunnerEventKind.CRITIC_VERDICT_REVISING,
        LegacyRunnerEventKind.CRITIC_VERDICT_UNKNOWN,
        LegacyRunnerEventKind.CRITIC_DONE,
    }:
        if legacy_kind is LegacyRunnerEventKind.CRITIC_VERDICT_BLOCKED:
            return (
                _AppendSpec(
                    EventKind.CRITIQUE_ISSUED,
                    payload,
                    tick=tick,
                    issue=issue,
                    pr_url=_pr_url(record),
                    discriminator=_discriminator(record, "verdict"),
                ),
                _AppendSpec(
                    EventKind.MERGE_BLOCKED,
                    payload,
                    tick=tick,
                    issue=issue,
                    pr_url=_pr_url(record),
                ),
            )
        return (
            _AppendSpec(
                EventKind.CRITIQUE_ISSUED,
                payload,
                tick=tick,
                issue=issue,
                pr_url=_pr_url(record),
                discriminator=_discriminator(record, "verdict"),
            ),
        )
    if legacy_kind is LegacyRunnerEventKind.MERGE_REFUSED_ISSUE_CLOSED:
        return (
            _AppendSpec(
                EventKind.MERGE_BLOCKED,
                payload,
                tick=tick,
                issue=issue,
                pr_url=_pr_url(record),
            ),
        )
    if legacy_kind is LegacyRunnerEventKind.POST_CRITIC_AUTOMERGE_ENABLED:
        return (
            _AppendSpec(
                EventKind.PR_MERGED,
                {**payload, "status": "merged"},
                tick=tick,
                issue=issue,
                pr_url=_pr_url(record),
            ),
        )
    if legacy_kind is LegacyRunnerEventKind.POST_CRITIC_AUTOMERGE_FAILED:
        return (_AppendSpec(EventKind.MERGE_BLOCKED, payload, tick=tick, issue=issue),)
    if legacy_kind is LegacyRunnerEventKind.WORKER_WORK_RESCUED:
        return (
            _AppendSpec(
                EventKind.TASK_COMPENSATED,
                {**payload, "status": "compensated"},
                tick=tick,
                issue=issue,
                pr_url=_pr_url(record),
            ),
        )
    if legacy_kind is LegacyRunnerEventKind.WORKTREE_REAPED:
        return (_AppendSpec(EventKind.WORKTREE_REAPED, payload, tick=tick, issue=issue),)
    if legacy_kind in {
        LegacyRunnerEventKind.LOOP_DRIFT_HALT,
        LegacyRunnerEventKind.DEPLOY_DRIFT_HALT,
        LegacyRunnerEventKind.MAX_TICKS_REACHED,
        LegacyRunnerEventKind.LOOP_STOP,
    }:
        reason = legacy_kind.value
        return (_AppendSpec(EventKind.LOOP_HALTED, {**payload, "reason": reason}, tick=tick),)
    return ()


def _tick_done_specs(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
    tick: int | None,
) -> tuple[_AppendSpec, ...]:
    specs: list[_AppendSpec] = [_AppendSpec(EventKind.TICK_COMPLETED, payload, tick=tick)]
    outcomes = record.get("outcomes")
    if not isinstance(outcomes, list):
        return tuple(specs)
    for raw_outcome in outcomes:
        if not isinstance(raw_outcome, Mapping):
            continue
        outcome = dict(raw_outcome)
        issue = _optional_int(outcome.get("issue"))
        status = _worker_status(outcome)
        status_value = status.value if status is not None else ""
        pr_url = _pr_url(outcome)
        if pr_url is not None:
            specs.append(
                _AppendSpec(
                    EventKind.PR_OPENED,
                    {
                        "legacy_kind": LegacyRunnerEventKind.TICK_DONE.value,
                        "tick": tick,
                        "issue": issue,
                        "status": status_value,
                        "pr_url": pr_url,
                    },
                    tick=tick,
                    issue=issue,
                    pr_url=pr_url,
                )
            )
        terminal_kind = None
        if status is not None:
            terminal_kind = _TERMINAL_STATUS_TO_KIND.get(status)
        if terminal_kind is not None:
            specs.append(
                _AppendSpec(
                    terminal_kind,
                    {
                        "legacy_kind": LegacyRunnerEventKind.TICK_DONE.value,
                        "tick": tick,
                        **outcome,
                    },
                    tick=tick,
                    issue=issue,
                    pr_url=pr_url,
                )
            )
        if status is LegacyWorkerStatus.MERGED and pr_url is not None:
            specs.append(
                _AppendSpec(
                    EventKind.PR_MERGED,
                    {
                        "legacy_kind": LegacyRunnerEventKind.TICK_DONE.value,
                        "tick": tick,
                        "issue": issue,
                        "status": status.value,
                        "pr_url": pr_url,
                    },
                    tick=tick,
                    issue=issue,
                    pr_url=pr_url,
                )
            )
    return tuple(specs)


def _worker_done_specs(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
    tick: int | None,
    worker: str | None,
) -> tuple[_AppendSpec, ...]:
    issue = _issue_from_record(record)
    status = _worker_status(record)
    pr_url = _pr_url(record)
    specs: list[_AppendSpec] = []
    if pr_url is not None:
        specs.append(
            _AppendSpec(
                EventKind.PR_OPENED,
                {
                    "legacy_kind": LegacyRunnerEventKind.WORKER_DONE.value,
                    "tick": tick,
                    "issue": issue,
                    "status": status.value if status is not None else "",
                    "pr_url": pr_url,
                },
                tick=tick,
                issue=issue,
                worker=worker,
                pr_url=pr_url,
            )
        )
    if status is not None and (terminal_kind := _TERMINAL_STATUS_TO_KIND.get(status)) is not None:
        specs.append(
            _AppendSpec(
                terminal_kind,
                payload,
                tick=tick,
                issue=issue,
                worker=worker,
                pr_url=pr_url,
            )
        )
    if not specs:
        specs.append(
            _AppendSpec(
                EventKind.WORKER_OBSERVATION,
                payload,
                tick=tick,
                issue=issue,
                worker=worker,
                pr_url=pr_url,
            )
        )
    return tuple(specs)


def _worker_session_transition_specs(
    record: Mapping[str, Any],
    payload: Mapping[str, Any],
    tick: int | None,
    issue: int | None,
    worker: str | None,
) -> tuple[_AppendSpec, ...]:
    new_state = str(record.get("new_state") or "").lower()
    pr_url = _pr_url(record)

    if new_state == "awaiting_critic":
        return (
            _AppendSpec(
                EventKind.PR_OPENED,
                payload,
                tick=tick,
                issue=issue,
                worker=worker,
                pr_url=pr_url,
                discriminator=_transition_discriminator(record),
            ),
        )
    if new_state == "abandoned":
        return (
            _AppendSpec(
                EventKind.TASK_FAILED,
                {**payload, "status": "failed"},
                tick=tick,
                issue=issue,
                worker=worker,
                pr_url=pr_url,
                discriminator=_transition_discriminator(record),
            ),
        )
    if new_state == "merged":
        specs = [
            _AppendSpec(
                EventKind.TASK_COMPLETED,
                {**payload, "status": "merged"},
                tick=tick,
                issue=issue,
                worker=worker,
                pr_url=pr_url,
                discriminator=_transition_discriminator(record),
            )
        ]
        if pr_url is not None:
            specs.append(
                _AppendSpec(
                    EventKind.PR_MERGED,
                    {**payload, "status": "merged"},
                    tick=tick,
                    issue=issue,
                    worker=worker,
                    pr_url=pr_url,
                    discriminator=_transition_discriminator(record),
                )
            )
        return tuple(specs)
    return (
        _AppendSpec(
            EventKind.TASK_DISPATCHED,
            payload,
            tick=tick,
            issue=issue,
            worker=worker,
            discriminator=_transition_discriminator(record),
        ),
    )


def _payload(record: Mapping[str, Any]) -> dict[str, Any]:
    legacy_kind = _legacy_kind(record)
    payload = {key: value for key, value in record.items() if key not in {"kind", "ts"}}
    if legacy_kind is not None:
        payload = {"legacy_kind": legacy_kind.value, **payload}
    return payload


def _idempotency_key(legacy_kind: LegacyRunnerEventKind, spec: _AppendSpec) -> str:
    return ":".join(
        [
            "legacy-runner",
            spec.kind.value,
            legacy_kind.value,
            f"tick={spec.tick or ''}",
            f"issue={spec.issue or ''}",
            f"worker={spec.worker or ''}",
            f"pr={spec.pr_url or ''}",
            f"step={spec.discriminator}",
        ]
    )


def _transition_discriminator(record: Mapping[str, Any]) -> str:
    prior_state = str(record.get("prior_state") or "")
    new_state = str(record.get("new_state") or "")
    return f"{prior_state}->{new_state}"


def _discriminator(record: Mapping[str, Any], *keys: str) -> str:
    parts = []
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    return "|".join(parts)


def _issue_from_event(event: EventEnvelope) -> int | None:
    if event.task_id and event.task_id.startswith("issue:"):
        return _optional_int(event.task_id.removeprefix("issue:"))
    return _optional_int(event.payload.get("issue"))


def _issue_from_record(record: Mapping[str, Any]) -> int | None:
    issue = _optional_int(record.get("issue"))
    if issue is not None:
        return issue
    return _optional_int(record.get("number"))


def _pr_url(record: Mapping[str, Any]) -> str | None:
    raw = record.get("pr_url") or record.get("pr")
    if raw is None:
        return None
    return str(raw)


def _worker_ref(record: Mapping[str, Any]) -> str | None:
    for key in ("session_id", "worker_id", "worker", "thread_id", "log_path", "worktree", "branch"):
        raw = record.get(key)
        if raw is not None:
            return str(raw)
    return None


def _optional_int(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _int_list(raw: Any) -> tuple[int, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(parsed for item in raw if (parsed := _optional_int(item)) is not None)
