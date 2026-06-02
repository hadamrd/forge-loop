import json
from pathlib import Path

import pytest

from forge_loop.eventlog import EventKind, ProjectionCursor, SqliteEventLog
from forge_loop.eventlog.legacy_mirror import LegacyEventMirror, replay_task_timeline
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
    assert events[4].payload["outcomes"][0]["status"] == "merged"
    assert events[6].task_id == "issue:167"
    assert events[8].payload["legacy_kind"] == "critic_verdict_merged"
    assert events[10].payload["reason"] == "max_ticks_reached"


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
            "last_sequence": 5,
        }
    }
    reopened.set_projection_cursor(
        "legacy-runner-mirror",
        ProjectionCursor(sequence=timeline["issue:167"]["last_sequence"]),
    )
    assert reopened.get_projection_cursor("legacy-runner-mirror").sequence == 5


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
    assert [event.kind for event in durable] == [EventKind.TICK_STARTED]
    assert durable[0].payload == {"legacy_kind": "tick_start", "tick": 2, "issues": [167]}


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
    assert len(durable) == 1
    assert durable[0].kind is EventKind.TICK_STARTED


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


def test_append_event_surfaces_unexpected_durable_mirror_bug(tmp_path: Path) -> None:
    events_file = tmp_path / "loop-runner-events.jsonl"

    class BuggyMirror:
        def mirror_record(self, _record: dict[str, object]) -> None:
            raise RuntimeError("bug in mirror translation")

    with pytest.raises(RuntimeError, match="bug in mirror translation"):
        append_event(events_file, "tick_start", tick=2, issues=[167], durable_mirror=BuggyMirror())

    assert len(events_file.read_text().splitlines()) == 1
