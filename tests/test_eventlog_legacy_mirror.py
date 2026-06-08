import json
from pathlib import Path

import pytest

from forge_loop.eventlog import EventKind, ProjectionCursor, SqliteEventLog
from forge_loop.eventlog.legacy_mirror import (
    _TERMINAL_STATUS_TO_KIND,
    LegacyEventMirror,
    LegacyWorkerStatus,
    replay_task_timeline,
)
from forge_loop.events import WorkerSessionTransitionEvent, emit
from forge_loop.state import append_event


def test_legacy_runner_mirror_records_representative_milestones(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    mirror = LegacyEventMirror(log)

    records = [
        {"kind": "tick_start", "tick": 7, "issues": [167]},
        {"kind": "po_done", "tick": 7, "expanded": [167], "skipped": []},
        {"kind": "worker_start", "tick": 7, "issue": 167, "worktree": "/tmp/wt-loop-167"},
        {"kind": "watchdog_worker_stuck", "issue": 167, "idle_s": 901},
        {
            "kind": "tick_done",
            "tick": 7,
            "merged": [167],
            "outcomes": [
                {
                    "issue": 167,
                    "title": "mirror runner milestones",
                    "status": "merged",
                    "pr_url": "https://github.com/acme/forge-loop/pull/167",
                    "duration_s": 42.5,
                }
            ],
        },
        {
            "kind": "critic_verdict_merged",
            "issue": 167,
            "pr": "https://github.com/acme/forge-loop/pull/167",
            "verdict": "approved",
        },
        {"kind": "worktree_reaped", "issue": 167, "status": "merged"},
        {"kind": "max_ticks_reached", "tick": 8},
    ]

    mirrored = [mirror.mirror_record(record) for record in records]

    events = list(log.since(0))
    assert [event.kind for event in events] == [
        EventKind.TICK_STARTED,
        EventKind.TASK_PLANNED,
        EventKind.TASK_PLANNED,
        EventKind.TASK_DISPATCHED,
        EventKind.TASK_HEARTBEAT,
        EventKind.TICK_COMPLETED,
        EventKind.PR_OPENED,
        EventKind.TASK_COMPLETED,
        EventKind.PR_MERGED,
        EventKind.CRITIQUE_ISSUED,
        EventKind.WORKTREE_REAPED,
        EventKind.LOOP_HALTED,
    ]
    assert all(event.idempotency_key for event in events)
    assert mirrored[0] is not None
    assert events[0].payload == {"legacy_kind": "tick_start", "tick": 7, "issues": [167]}
    assert events[5].payload["outcomes"][0]["status"] == "merged"
    assert events[7].task_id == "issue:167"
    assert events[9].payload["legacy_kind"] == "critic_verdict_merged"
    assert events[11].payload["reason"] == "max_ticks_reached"


def test_legacy_runner_mirror_is_idempotent_for_repeated_records(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    mirror = LegacyEventMirror(log)
    record = {
        "kind": "tick_done",
        "tick": 3,
        "merged": [],
        "outcomes": [{"issue": 167, "status": "failed", "error": "tests failed"}],
    }

    first = mirror.mirror_record(record)
    duplicate = mirror.mirror_record(dict(record))

    events = list(log.since(0))
    assert first == duplicate
    assert len(events) == 2
    assert [event.kind for event in events] == [EventKind.TICK_COMPLETED, EventKind.TASK_FAILED]


def test_legacy_runner_mirror_keeps_distinct_worker_records(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    mirror = LegacyEventMirror(log)

    mirror.mirror_record(
        {
            "kind": "worker_iteration_attempt",
            "tick": 3,
            "issue": 167,
            "session_id": "worker-a",
        }
    )
    mirror.mirror_record(
        {
            "kind": "worker_iteration_attempt",
            "tick": 3,
            "issue": 167,
            "session_id": "worker-b",
        }
    )

    events = list(log.since(0))
    assert len(events) == 2
    keys = {event.idempotency_key for event in events}
    assert len(keys) == 2
    assert any(":worker=worker-a:" in str(key) for key in keys)
    assert any(":worker=worker-b:" in str(key) for key in keys)


def test_legacy_runner_mirror_plans_selected_issue_from_tick_start(
    tmp_path: Path,
) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    mirror = LegacyEventMirror(log)

    mirror.mirror_record({"kind": "tick_start", "tick": 7, "issues": [167, 168]})

    events = list(log.since(0))
    assert [event.kind for event in events] == [
        EventKind.TICK_STARTED,
        EventKind.TASK_PLANNED,
        EventKind.TASK_PLANNED,
    ]
    assert [event.task_id for event in events] == [None, "issue:167", "issue:168"]


def test_legacy_runner_mirror_keeps_distinct_same_worker_transitions(
    tmp_path: Path,
) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    mirror = LegacyEventMirror(log)

    first = mirror.mirror_record(
        {
            "kind": "worker_session_transition",
            "issue": 167,
            "session_id": "worker-a",
            "prior_state": "",
            "new_state": "dispatched",
            "reason": "fresh dispatch",
        }
    )
    second = mirror.mirror_record(
        {
            "kind": "worker_session_transition",
            "issue": 167,
            "session_id": "worker-a",
            "prior_state": "dispatched",
            "new_state": "running",
            "reason": "sdk call starting",
        }
    )
    duplicate_second = mirror.mirror_record(
        {
            "kind": "worker_session_transition",
            "issue": 167,
            "session_id": "worker-a",
            "prior_state": "dispatched",
            "new_state": "running",
            "reason": "sdk call starting",
        }
    )

    events = list(log.since(0))
    assert len(events) == 2
    assert first is not None
    assert second == duplicate_second
    assert [event.payload["new_state"] for event in events] == ["dispatched", "running"]
    assert len({event.idempotency_key for event in events}) == 2


def test_legacy_runner_mirror_records_pr_opened_session_transition(
    tmp_path: Path,
) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    mirror = LegacyEventMirror(log)

    mirror.mirror_record(
        {
            "kind": "worker_session_transition",
            "issue": 167,
            "session_id": "worker-a",
            "prior_state": "running",
            "new_state": "awaiting_critic",
            "reason": "worker opened PR",
            "pr_url": "https://github.com/acme/forge-loop/pull/167",
        }
    )

    events = list(log.since(0))
    assert [event.kind for event in events] == [EventKind.PR_OPENED]
    assert events[0].task_id == "issue:167"
    assert events[0].payload["pr_url"] == "https://github.com/acme/forge-loop/pull/167"


def test_legacy_runner_mirror_records_abandoned_session_transition(
    tmp_path: Path,
) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    mirror = LegacyEventMirror(log)

    mirror.mirror_record(
        {
            "kind": "worker_session_transition",
            "issue": 167,
            "session_id": "worker-a",
            "prior_state": "running",
            "new_state": "abandoned",
            "reason": "worker failed: tests failed",
            "pr_url": None,
        }
    )

    events = list(log.since(0))
    assert [event.kind for event in events] == [EventKind.TASK_FAILED]
    assert events[0].task_id == "issue:167"
    assert events[0].payload["status"] == "failed"
    assert events[0].payload["reason"] == "worker failed: tests failed"


def test_legacy_runner_mirror_records_merge_blocked_from_critic_verdict(
    tmp_path: Path,
) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    mirror = LegacyEventMirror(log)

    mirror.mirror_record(
        {
            "kind": "critic_verdict_blocked",
            "issue": 167,
            "pr": "https://github.com/acme/forge-loop/pull/167",
            "verdict": "blocked",
        }
    )

    assert [event.kind for event in log.since(0)] == [
        EventKind.CRITIQUE_ISSUED,
        EventKind.MERGE_BLOCKED,
    ]


def test_critic_done_findings_survive_mirror_to_critique_issued(tmp_path: Path) -> None:
    """Issue #404: findings + minimal_path_to_green added to critic_done flow
    straight through the mirror onto the canonical critique.issued payload."""
    log = SqliteEventLog(tmp_path / "events.db")
    mirror = LegacyEventMirror(log)

    mirror.mirror_record(
        {
            "kind": "critic_done",
            "issue": 4242,
            "pr": "https://github.com/o/r/pull/4242",
            "verdict": "changes_requested",
            "reasons": [],
            "duration_s": 3.0,
            "sev_counts": {"sev1": 0, "sev2": 1, "sev3": 0},
            "parse_retries": 0,
            "findings": [
                {"severity": "sev2", "category": "correctness", "file": "src/a.py", "line": 10, "message": "off-by-one"}
            ],
            "minimal_path_to_green": ["fix off-by-one", "add test"],
        }
    )

    events = list(log.since(0))
    assert [e.kind for e in events] == [EventKind.CRITIQUE_ISSUED]
    payload = events[0].payload
    assert payload["findings"] == [
        {"severity": "sev2", "category": "correctness", "file": "src/a.py", "line": 10, "message": "off-by-one"}
    ]
    assert payload["minimal_path_to_green"] == ["fix off-by-one", "add test"]


def test_critic_done_findings_reach_console_reconstruct_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: real append_event(critic_done, findings=...) → default durable
    mirror → critique.issued → console _reconstruct_prs surfaces the findings."""
    import forge_loop.console_api as capi
    from forge_loop.console_api import _reconstruct_prs
    from forge_loop.critic import CriticReport, Finding, serialize_findings

    # Deterministic: never touch GitHub for issue reconciliation.
    monkeypatch.setattr(capi, "_open_issue_numbers", lambda repo: None)

    report = CriticReport(
        overall="request_changes",
        findings=[
            Finding("sev2", "correctness", "src/a.py", 10, "off-by-one"),
            Finding("sev1", "security", None, None, "no file/line"),
        ],
        minimal_path_to_green=["fix off-by-one"],
    )
    events_file = tmp_path / "docs" / "ops" / "loop-runner-events.jsonl"
    append_event(
        events_file,
        "critic_done",
        issue=4242,
        pr="https://github.com/o/r/pull/4242",
        verdict="changes_requested",
        reasons=[],
        duration_s=3.0,
        sev_counts={"sev1": 1, "sev2": 1, "sev3": 0},
        parse_retries=0,
        findings=serialize_findings(report.findings),
        minimal_path_to_green=list(report.minimal_path_to_green),
    )

    prs = _reconstruct_prs(tmp_path)
    review = next(p for p in prs if p["number"] == 4242)["review"]
    assert review["findings"] == [
        {"severity": "sev2", "category": "correctness", "file": "src/a.py", "line": 10, "message": "off-by-one"},
        {"severity": "sev1", "category": "security", "file": None, "line": None, "message": "no file/line"},
    ]
    assert review["minimal_path_to_green"] == ["fix off-by-one"]


def test_legacy_runner_mirror_records_default_critic_done(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    mirror = LegacyEventMirror(log)

    mirror.mirror_record(
        {
            "kind": "critic_done",
            "issue": 167,
            "pr": "https://github.com/acme/forge-loop/pull/167",
            "verdict": "approved",
            "reasons": [],
            "duration_s": 12.3,
            "sev_counts": {"sev1": 0, "sev2": 0},
            "parse_retries": 0,
        }
    )

    events = list(log.since(0))
    assert [event.kind for event in events] == [EventKind.CRITIQUE_ISSUED]
    assert events[0].task_id == "issue:167"
    assert events[0].payload["legacy_kind"] == "critic_done"
    assert events[0].payload["verdict"] == "approved"


def test_legacy_runner_mirror_idempotency_ignores_incidental_critic_duration(
    tmp_path: Path,
) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    mirror = LegacyEventMirror(log)
    base = {
        "kind": "critic_done",
        "issue": 167,
        "pr": "https://github.com/acme/forge-loop/pull/167",
        "verdict": "approved",
        "reasons": [],
        "sev_counts": {"sev1": 0, "sev2": 0},
        "parse_retries": 0,
    }

    first = mirror.mirror_record({**base, "duration_s": 12.3})
    second = mirror.mirror_record({**base, "duration_s": 99.9})

    events = list(log.since(0))
    assert first == second
    assert len(events) == 1
    assert events[0].payload["duration_s"] == 12.3


def test_legacy_runner_mirror_records_issue_closed_merge_refusal(
    tmp_path: Path,
) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    mirror = LegacyEventMirror(log)

    mirror.mirror_record(
        {
            "kind": "merge_refused_issue_closed",
            "issue": 167,
            "pr": "https://github.com/acme/forge-loop/pull/167",
            "issue_state": "CLOSED",
        }
    )

    events = list(log.since(0))
    assert [event.kind for event in events] == [EventKind.MERGE_BLOCKED]
    assert events[0].task_id == "issue:167"
    assert events[0].payload["legacy_kind"] == "merge_refused_issue_closed"
    assert events[0].payload["issue_state"] == "CLOSED"


def test_legacy_runner_replay_reconstructs_task_timeline_after_sqlite_reopen(
    tmp_path: Path,
) -> None:
    db = tmp_path / "events.db"
    mirror = LegacyEventMirror(SqliteEventLog(db))
    mirror.mirror_record({"kind": "tick_start", "tick": 1, "issues": [167]})
    mirror.mirror_record({"kind": "po_done", "tick": 1, "expanded": [167], "skipped": []})
    mirror.mirror_record({"kind": "worker_started", "tick": 1, "issue": 167})
    mirror.mirror_record(
        {
            "kind": "tick_done",
            "tick": 1,
            "merged": [],
            "outcomes": [{"issue": 167, "status": "timeout", "pr_url": None}],
        }
    )

    reopened = SqliteEventLog(db)
    timeline = replay_task_timeline(reopened.since(0))

    assert timeline == {
        "issue:167": {
            "issue": 167,
            "ticks": [1],
            "planned": True,
            "dispatched": True,
            "terminal": "timeout",
            "pr_url": None,
            "last_sequence": 6,
        }
    }
    reopened.set_projection_cursor(
        "legacy-runner-mirror",
        ProjectionCursor(sequence=timeline["issue:167"]["last_sequence"]),
    )
    assert reopened.get_projection_cursor("legacy-runner-mirror").sequence == 6


def test_legacy_runner_replay_ignores_non_task_and_malformed_payloads(
    tmp_path: Path,
) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    log.append(EventKind.TICK_STARTED, {"tick": "not-an-int"})
    log.append(EventKind.WORKER_OBSERVATION, {"issue": "not-an-int", "tick": 2})

    assert replay_task_timeline(log.since(0)) == {}


def test_runner_jsonl_path_mirrors_to_repo_durable_event_log_by_default(
    tmp_path: Path,
) -> None:
    events_file = tmp_path / "docs" / "ops" / "loop-runner-events.jsonl"

    append_event(events_file, "tick_start", tick=2, issues=[167])

    assert len(events_file.read_text().splitlines()) == 1
    durable = list(SqliteEventLog(tmp_path / ".forge" / "events.db").since(0))
    assert [event.kind for event in durable] == [EventKind.TICK_STARTED, EventKind.TASK_PLANNED]
    assert durable[0].payload == {"legacy_kind": "tick_start", "tick": 2, "issues": [167]}
    assert durable[1].task_id == "issue:167"


def test_runner_jsonl_path_mirrors_typed_emit_to_durable_event_log_by_default(
    tmp_path: Path,
) -> None:
    events_file = tmp_path / "docs" / "ops" / "loop-runner-events.jsonl"

    emit(
        events_file,
        WorkerSessionTransitionEvent(
            session_id="worker-a",
            issue=167,
            prior_state="",
            new_state="dispatched",
            reason="fresh dispatch",
        ),
    )

    assert len(events_file.read_text().splitlines()) == 1
    durable = list(SqliteEventLog(tmp_path / ".forge" / "events.db").since(0))
    assert [event.kind for event in durable] == [EventKind.TASK_DISPATCHED]
    assert durable[0].task_id == "issue:167"
    assert durable[0].payload["legacy_kind"] == "worker_session_transition"
    assert durable[0].payload["session_id"] == "worker-a"


def test_runner_jsonl_path_logs_default_mirror_setup_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    events_file = tmp_path / "docs" / "ops" / "loop-runner-events.jsonl"
    warnings: list[tuple[str, dict[str, object]]] = []

    class FakeLogger:
        def info(self, _event: str, **_payload: object) -> None:
            pass

        def warning(self, event: str, **payload: object) -> None:
            warnings.append((event, payload))

    def fail_mirror_setup(_events_path: Path) -> None:
        raise OSError("durable store unavailable")

    monkeypatch.setattr(
        "forge_loop.eventlog.legacy_mirror.legacy_runner_mirror_for_events_path",
        fail_mirror_setup,
    )
    monkeypatch.setattr("forge_loop.log.get_logger", lambda: FakeLogger())

    append_event(events_file, "tick_start", tick=2, issues=[167])

    lines = events_file.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["kind"] == "tick_start"
    assert "durable_mirror_error" not in record
    assert warnings == [
        (
            "durable_mirror_failed",
            {
                "kind": "tick_start",
                "events_path": str(events_file),
                "error": "OSError: durable store unavailable",
            },
        )
    ]


def test_append_event_preserves_jsonl_when_durable_mirroring_is_enabled(
    tmp_path: Path,
) -> None:
    events_file = tmp_path / "loop-runner-events.jsonl"
    log = SqliteEventLog(tmp_path / "events.db")
    mirror = LegacyEventMirror(log)

    append_event(events_file, "tick_start", tick=2, issues=[167], durable_mirror=mirror)

    lines = events_file.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["kind"] == "tick_start"
    assert record["tick"] == 2
    assert record["issues"] == [167]
    assert "ts" in record
    durable = list(log.since(0))
    assert len(durable) == 2
    assert durable[0].kind is EventKind.TICK_STARTED
    assert durable[1].kind is EventKind.TASK_PLANNED


def test_append_event_logs_expected_durable_mirror_write_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    events_file = tmp_path / "loop-runner-events.jsonl"
    warnings: list[tuple[str, dict[str, object]]] = []

    class FakeLogger:
        def info(self, _event: str, **_payload: object) -> None:
            pass

        def warning(self, event: str, **payload: object) -> None:
            warnings.append((event, payload))

    class FailingMirror:
        def mirror_record(self, _record: dict[str, object]) -> None:
            raise OSError("sqlite unavailable")

    monkeypatch.setattr("forge_loop.log.get_logger", lambda: FakeLogger())

    append_event(events_file, "tick_start", tick=2, issues=[167], durable_mirror=FailingMirror())

    assert len(events_file.read_text().splitlines()) == 1
    assert warnings == [
        (
            "durable_mirror_failed",
            {
                "kind": "tick_start",
                "events_path": str(events_file),
                "error": "OSError: sqlite unavailable",
            },
        )
    ]


def test_runner_jsonl_path_logs_unexpected_default_mirror_bug(
    monkeypatch,
    tmp_path: Path,
) -> None:
    events_file = tmp_path / "docs" / "ops" / "loop-runner-events.jsonl"
    warnings: list[tuple[str, dict[str, object]]] = []

    class FakeLogger:
        def info(self, _event: str, **_payload: object) -> None:
            pass

        def warning(self, event: str, **payload: object) -> None:
            warnings.append((event, payload))

    class BuggyDefaultMirror:
        def mirror_record(self, _record: dict[str, object]) -> None:
            raise RuntimeError("bug in default mirror translation")

    monkeypatch.setattr(
        "forge_loop.eventlog.legacy_mirror.legacy_runner_mirror_for_events_path",
        lambda _events_path: BuggyDefaultMirror(),
    )
    monkeypatch.setattr("forge_loop.log.get_logger", lambda: FakeLogger())

    append_event(events_file, "tick_start", tick=2, issues=[167])

    assert len(events_file.read_text().splitlines()) == 1
    assert warnings == [
        (
            "durable_mirror_failed",
            {
                "kind": "tick_start",
                "events_path": str(events_file),
                "error": "RuntimeError: bug in default mirror translation",
            },
        )
    ]


def test_append_event_surfaces_unexpected_durable_mirror_bug(tmp_path: Path) -> None:
    events_file = tmp_path / "loop-runner-events.jsonl"

    class BuggyMirror:
        def mirror_record(self, _record: dict[str, object]) -> None:
            raise RuntimeError("bug in mirror translation")

    with pytest.raises(RuntimeError, match="bug in mirror translation"):
        append_event(events_file, "tick_start", tick=2, issues=[167], durable_mirror=BuggyMirror())

    assert len(events_file.read_text().splitlines()) == 1


# --- Terminal-mapping coverage gate (issue #304) --------------------------------
#
# WHY these invariants exist: ``_worker_done_specs`` (and ``_tick_done_specs``)
# look up a worker's ``LegacyWorkerStatus`` in ``_TERMINAL_STATUS_TO_KIND`` to
# emit a terminal task event (TASK_COMPLETED / TASK_FAILED / TASK_COMPENSATED).
# If a status is MISSING from that dict, the lookup silently falls through to a
# non-terminal ``WORKER_OBSERVATION``. On restart, ``replay_task_timeline`` never
# sets ``task["terminal"]`` for an observation, so the in-flight view shows the
# task as perpetually running. These tests fail CI the moment a worker outcome
# status loses its terminal mapping, instead of silently dropping the milestone.

# Statuses deliberately excluded from the terminal mapping. Empty today: all 7
# current ``LegacyWorkerStatus`` members are terminal. Adding a member here is a
# DOCUMENTED, intentional act — it asserts the new status is genuinely
# non-terminal and must be handled some other way.
_NON_TERMINAL_STATUSES: set[LegacyWorkerStatus] = set()

_TERMINAL_KINDS = {
    EventKind.TASK_COMPLETED,
    EventKind.TASK_FAILED,
    EventKind.TASK_COMPENSATED,
}


def test_every_worker_status_has_terminal_mapping() -> None:
    """Every LegacyWorkerStatus must resolve to a terminal kind (or be excluded).

    Pins the coverage relationship between the enum and the mapping: adding a
    new ``LegacyWorkerStatus`` member without a ``_TERMINAL_STATUS_TO_KIND``
    entry (and without listing it in ``_NON_TERMINAL_STATUSES``) makes this
    test fail, preventing the silent ``WORKER_OBSERVATION`` fallthrough that
    leaves a task perpetually unfinished on replay.
    """

    mapped = set(_TERMINAL_STATUS_TO_KIND)
    assert set(LegacyWorkerStatus) - _NON_TERMINAL_STATUSES == mapped
    # Excluded statuses must NOT also be mapped — the exclusion is meaningful.
    assert _NON_TERMINAL_STATUSES.isdisjoint(mapped)


def test_terminal_mapping_only_targets_terminal_kinds() -> None:
    """No status may map to a non-terminal EventKind (e.g. WORKER_OBSERVATION)."""

    for status, kind in _TERMINAL_STATUS_TO_KIND.items():
        assert kind in _TERMINAL_KINDS, f"{status} maps to non-terminal {kind}"


def test_unmapped_status_falls_through_to_observation(tmp_path: Path) -> None:
    """Adversarial: an unmapped status mirrors only a WORKER_OBSERVATION.

    This documents the exact regression the coverage tests above guard against.
    A ``worker_done`` record whose status is not recognised by the parser (and
    therefore absent from ``_TERMINAL_STATUS_TO_KIND``) produces a non-terminal
    ``WORKER_OBSERVATION`` — so ``replay_task_timeline`` never marks the task
    terminal and it looks perpetually running. The coverage tests ensure no
    real enum member can ever land in this fallthrough.
    """

    bogus_status = "cancelled"
    assert bogus_status not in {status.value for status in _TERMINAL_STATUS_TO_KIND}

    log = SqliteEventLog(tmp_path / "events.db")
    mirror = LegacyEventMirror(log)
    mirror.mirror_record({"kind": "worker_done", "tick": 1, "issue": 167, "status": bogus_status})

    events = list(log.since(0))
    assert [event.kind for event in events] == [EventKind.WORKER_OBSERVATION]
    assert replay_task_timeline(log.since(0))["issue:167"]["terminal"] is None


# --- #403: per-task cost_usd / tokens land on the pr.merged payload -----------
# WHY: console_api._budget derives real $/merged-PR from cost_usd on pr.merged
# events. The explicit PR_MERGED payload in _tick_done_specs used to DROP the
# worker's cost, so production spend rendered as $0 even though every
# WorkerOutcome carries a real cost_usd. These tests pin the lift so the cost
# signal cannot silently regress to the consumer (manifesto Q10: load-bearing
# data must not be dropped on a cross-component seam).


def _pr_merged_payload(log: SqliteEventLog) -> dict:
    merged = [e for e in log.since(0) if e.kind is EventKind.PR_MERGED]
    assert len(merged) == 1, f"expected exactly one PR_MERGED, got {[e.kind for e in log.since(0)]}"
    return dict(merged[0].payload)


def test_tick_done_merged_outcome_carries_cost_and_tokens(tmp_path: Path) -> None:
    """Happy path: a merged outcome's cost_usd + token usage land on pr.merged."""
    log = SqliteEventLog(tmp_path / "events.db")
    mirror = LegacyEventMirror(log)
    mirror.mirror_record(
        {
            "kind": "tick_done",
            "tick": 3,
            "merged": [167],
            "outcomes": [
                {
                    "issue": 167,
                    "title": "real cost",
                    "status": "merged",
                    "pr_url": "https://github.com/acme/forge-loop/pull/167",
                    "duration_s": 12.0,
                    "cost_usd": 2.75,
                    "usage": {"input_tokens": 1200, "output_tokens": 340},
                }
            ],
        }
    )
    payload = _pr_merged_payload(log)
    assert payload["cost_usd"] == 2.75
    assert payload["input_tokens"] == 1200
    assert payload["output_tokens"] == 340


def test_tick_done_merged_outcome_without_cost_defaults_to_zero(tmp_path: Path) -> None:
    """Adversarial: a merged outcome missing cost_usd/usage must not raise.

    A legacy or codex worker may serialize an outcome with no cost telemetry.
    The lift must default to a real $0.0 / 0 tokens rather than KeyError-ing or
    omitting the field — the consumer reads cost_usd unconditionally.
    """
    log = SqliteEventLog(tmp_path / "events.db")
    mirror = LegacyEventMirror(log)
    mirror.mirror_record(
        {
            "kind": "tick_done",
            "tick": 4,
            "merged": [42],
            "outcomes": [
                {
                    "issue": 42,
                    "title": "no telemetry",
                    "status": "merged",
                    "pr_url": "https://github.com/acme/forge-loop/pull/42",
                }
            ],
        }
    )
    payload = _pr_merged_payload(log)
    assert payload["cost_usd"] == 0.0
    assert payload["input_tokens"] == 0
    assert payload["output_tokens"] == 0
