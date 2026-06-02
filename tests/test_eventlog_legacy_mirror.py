import json
from pathlib import Path

from forge_loop.eventlog import EventKind, ProjectionCursor, SqliteEventLog
from forge_loop.eventlog.legacy_mirror import LegacyEventMirror, replay_task_timeline
from forge_loop.state import append_event


def test_legacy_runner_mirror_records_representative_milestones(tmp_path: Path) -> None:
    log = SqliteEventLog(tmp_path / "events.db")
    mirror = LegacyEventMirror(log)

    records = [
        {"kind": "tick_start", "tick": 7, "issues": [167]},
        {"kind": "po_done", "tick": 7, "expanded": [167], "skipped": []},
        {"kind": "worker_started", "tick": 7, "issue": 167, "worktree": "/tmp/wt-loop-167"},
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
