"""Tests for the time-travel replay feature (issue #24).

Matrix:
- unit: CLI parser registers `replay` and `replay diff`.
- unit: find_tick_workers reads workers from tick_done; falls back to tick_start.
- unit: assemble_replay_invocations resolves fixtures + rejects unsupported roles.
- unit: apply_dry_run_to_brief is idempotent + carries the no-push banner.
- unit (adversarial): plan_replay raises ReplayError on unknown tick.
- integration: end-to-end fixture-backed replay → emit replay events →
  build_diff_report returns a row per issue with correct costs / statuses.
- adversarial: replay against a corrupted fixture raises ReplayError, no
  side effects on the events log (no replay_tick_done is emitted).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge_loop import replay as r
from forge_loop._testing.recorder import SCHEMA_VERSION

# ---------------------------------------------------------------- helpers

def _write_events(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _make_tick(events_path: Path, tick: int, outcomes: list[dict]) -> None:
    _write_events(events_path, [
        {"ts": "2026-01-01T00:00:00+00:00", "kind": "tick_start",
         "tick": tick, "issues": [o["issue"] for o in outcomes]},
        {"ts": "2026-01-01T00:01:00+00:00", "kind": "tick_done",
         "tick": tick, "merged": [o["issue"] for o in outcomes if o["status"] == "merged"],
         "outcomes": outcomes},
    ])


def _good_fixture(path: Path, issue: int, *, pr_url: str | None, status: str,
                  diff_text: str = "", commit_sha: str | None = None) -> Path:
    """Write a minimal valid recorded SDK session fixture."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "schema": SCHEMA_VERSION,
        "issue": issue,
        "title": f"issue-{issue}",
        "recorded_at": "2026-01-01T00:00:00+00:00",
    }
    events: list[dict] = [
        {"seq": 1, "type": "assistant", "message": {"role": "assistant", "content": []}},
    ]
    # Embed a tool_result carrying a fake diff + SHA so the extractor finds them.
    payload_text = ""
    if diff_text:
        payload_text += diff_text + "\n"
    if commit_sha:
        payload_text += commit_sha + "\n"
    if payload_text:
        events.append({
            "seq": 2, "type": "user",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "x", "content": payload_text}
            ]},
        })
    events.append({
        "seq": len(events) + 1, "type": "result",
        "result": json.dumps({"issue": issue, "pr": pr_url, "status": status}),
    })
    trailer = {"type": "outcome", "pr": pr_url, "status": status, "returncode": 0}
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(header) + "\n")
        for e in events:
            f.write(json.dumps(e) + "\n")
        f.write(json.dumps(trailer) + "\n")
    return path


# ---------------------------------------------------------------- discovery

def test_find_tick_workers_uses_tick_done(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _make_tick(events, 42, [
        {"issue": 101, "title": "feat A", "pr_url": "https://x/pull/1",
         "status": "merged", "cost_usd": 1.5},
        {"issue": 102, "title": "feat B", "pr_url": None,
         "status": "failed", "cost_usd": 0.25, "error": "timeout"},
    ])
    workers = r.find_tick_workers(events, 42)
    assert {w.issue for w in workers} == {101, 102}
    by = {w.issue: w for w in workers}
    assert by[101].original_status == "merged"
    assert by[101].original_pr_url == "https://x/pull/1"
    assert by[101].original_cost_usd == 1.5
    assert by[102].original_error == "timeout"


def test_find_tick_workers_falls_back_to_tick_start(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _write_events(events, [
        {"ts": "t", "kind": "tick_start", "tick": 7, "issues": [200, 201]},
    ])
    workers = r.find_tick_workers(events, 7)
    assert [w.issue for w in workers] == [200, 201]
    assert all(w.original_status == "unknown" for w in workers)


def test_find_tick_workers_empty_when_no_such_tick(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text("")
    assert r.find_tick_workers(events, 99) == []


# ---------------------------------------------------------------- assembly

def test_assemble_rejects_non_worker_role() -> None:
    with pytest.raises(r.ReplayError, match="unsupported replay role"):
        r.assemble_replay_invocations(
            [r.WorkerRecord(1, "t", None, "merged", 0.0)],
            new_brief="b", role="critic",
        )


def test_assemble_resolves_per_tick_fixture(tmp_path: Path) -> None:
    fdir = tmp_path / "fix"
    fdir.mkdir()
    (fdir / "tick-42-issue-101.jsonl").write_text("{}")
    (fdir / "issue-102.jsonl").write_text("{}")
    workers = [
        r.WorkerRecord(101, "t1", None, "merged", 0.0),
        r.WorkerRecord(102, "t2", None, "merged", 0.0),
        r.WorkerRecord(103, "t3", None, "failed", 0.0),
    ]
    invs = r.assemble_replay_invocations(workers, "NEWBRIEF", "worker",
                                         fixtures_dir=fdir, original_tick=42)
    assert invs[0].fixture_path == fdir / "tick-42-issue-101.jsonl"
    assert invs[1].fixture_path == fdir / "issue-102.jsonl"
    assert invs[2].fixture_path is None
    assert all(inv.brief == "NEWBRIEF" for inv in invs)
    assert all(inv.role == "worker" for inv in invs)


def test_assemble_no_fixtures_dir_means_no_fixtures() -> None:
    workers = [r.WorkerRecord(1, "t", None, "merged", 0.0)]
    invs = r.assemble_replay_invocations(workers, "B", "worker", fixtures_dir=None)
    assert invs[0].fixture_path is None


# --------------------------------------------------------------- dry-run brief

def test_apply_dry_run_to_brief_is_idempotent() -> None:
    base = "ORIGINAL BRIEF"
    once = r.apply_dry_run_to_brief(base)
    twice = r.apply_dry_run_to_brief(once)
    assert once == twice
    assert "DO NOT run `git push`" in once
    assert "ORIGINAL BRIEF" in once


def test_worker_make_brief_dry_run_includes_banner(tmp_path: Path) -> None:
    from forge_loop.worker import make_brief
    issue = {"number": 42, "title": "x", "body": "y"}
    plain = make_brief(issue, tmp_path)
    dry = make_brief(issue, tmp_path, dry_run=True)
    assert "DO NOT run `git push`" not in plain
    assert "DO NOT run `git push`" in dry


# --------------------------------------------------------------- plan_replay

def test_plan_replay_unknown_tick_raises(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text("")
    with pytest.raises(r.ReplayError, match="no workers found"):
        r.plan_replay(events, tick=9999, role="worker",
                      new_brief="b", fixtures_dir=None)


def test_plan_replay_assigns_synthetic_tick_id(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _make_tick(events, 5, [{"issue": 1, "title": "t", "pr_url": None,
                            "status": "merged", "cost_usd": 0.0}])
    plan = r.plan_replay(events, tick=5, role="worker", new_brief="b",
                         fixtures_dir=None)
    assert plan.replay_tick == "5r"
    assert plan.original_tick == 5
    assert len(plan.invocations) == 1


# --------------------------------------------------------- end-to-end fixture

def test_end_to_end_fixture_replay_emits_replay_events_and_diff_report(
    tmp_path: Path,
) -> None:
    events = tmp_path / "events.jsonl"
    _make_tick(events, 42, [
        {"issue": 101, "title": "feat A",
         "pr_url": "https://example/pull/101",
         "status": "merged", "cost_usd": 2.50},
        {"issue": 102, "title": "feat B",
         "pr_url": None, "status": "failed", "cost_usd": 0.10},
    ])

    fdir = tmp_path / "fixtures"
    diff_blob = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
    )
    sha = "0" * 40
    _good_fixture(fdir / "tick-42-issue-101.jsonl", 101,
                  pr_url="https://example/pull/101r",
                  status="merged", diff_text=diff_blob, commit_sha=sha)
    _good_fixture(fdir / "tick-42-issue-102.jsonl", 102,
                  pr_url=None, status="failed")

    plan = r.plan_replay(events, tick=42, role="worker",
                         new_brief="NEW BRIEF", fixtures_dir=fdir)
    captures = r.run_replay_tick(plan, events_path=events)

    # Captures are fixture-backed → zero cost.
    by = {c.issue: c for c in captures}
    assert by[101].source == "fixture"
    assert by[101].cost_usd == 0.0
    assert by[101].status == "merged"
    assert by[101].commit_hash == sha
    assert "diff --git a/x.py b/x.py" in by[101].diff_text
    assert by[102].status == "failed"

    # Replay events are stamped with replay=True.
    lines = [json.loads(line) for line in events.read_text().splitlines() if line]
    replay_events = [e for e in lines if e.get("replay") is True]
    kinds = {e["kind"] for e in replay_events}
    assert "replay_tick_start" in kinds
    assert "replay_tick_done" in kinds
    assert "replay_worker_done" in kinds

    # Diff report joins original + replay per issue with correct totals.
    report = r.build_diff_report(events, tick=42, replay_tick="42r")
    assert report["original_tick"] == 42
    assert report["replay_tick"] == "42r"
    rows = {row["issue"]: row for row in report["rows"]}
    assert rows[101]["original_status"] == "merged"
    assert rows[101]["replay_status"] == "merged"
    assert rows[101]["original_cost_usd"] == 2.50
    assert rows[101]["replay_cost_usd"] == 0.0
    assert rows[101]["replay_source"] == "fixture"
    assert rows[101]["replay_diff_chars"] > 0
    assert rows[102]["replay_status"] == "failed"
    assert report["totals"]["issues"] == 2
    assert report["totals"]["savings_usd"] == pytest.approx(2.60)


def test_diff_report_requires_both_anchors(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _make_tick(events, 1, [{"issue": 1, "title": "t", "pr_url": None,
                            "status": "merged", "cost_usd": 0.0}])
    with pytest.raises(r.ReplayError, match="no replay_tick_done"):
        r.build_diff_report(events, tick=1, replay_tick="1r")


def test_render_diff_report_text_is_human_friendly() -> None:
    report = {
        "original_tick": 7, "replay_tick": "7r", "role": "worker",
        "rows": [{
            "issue": 1, "title": "t", "original_status": "merged",
            "replay_status": "merged", "original_pr_url": None,
            "replay_pr_url": None, "original_cost_usd": 1.0,
            "replay_cost_usd": 0.0, "replay_commit_hash": None,
            "replay_diff_chars": 50, "replay_source": "fixture",
        }],
        "totals": {"original_cost_usd": 1.0, "replay_cost_usd": 0.0,
                   "savings_usd": 1.0, "issues": 1},
    }
    out = r.render_diff_report_text(report)
    assert "replay diff: tick 7 → 7r" in out
    assert "savings $1.0000" in out


# -------------------------------------------------- adversarial: corrupted fix

def test_corrupted_fixture_raises_replay_error_no_side_effects(
    tmp_path: Path,
) -> None:
    events = tmp_path / "events.jsonl"
    _make_tick(events, 11, [{"issue": 7, "title": "x", "pr_url": None,
                             "status": "merged", "cost_usd": 0.0}])
    fdir = tmp_path / "fix"
    fdir.mkdir()
    # Corrupted: header is fine but middle line is junk (not JSON)
    bad = fdir / "tick-11-issue-7.jsonl"
    header = {"schema": SCHEMA_VERSION, "issue": 7, "title": "x",
              "recorded_at": "2026-01-01T00:00:00+00:00"}
    bad.write_text(
        json.dumps(header) + "\n"
        + "not a json line at all\n"
        + json.dumps({"type": "outcome", "pr": None, "status": "merged",
                      "returncode": 0}) + "\n"
    )

    plan = r.plan_replay(events, tick=11, role="worker",
                         new_brief="b", fixtures_dir=fdir)
    pre_lines = events.read_text().splitlines()

    with pytest.raises(r.ReplayError, match="corrupt recording"):
        r.run_replay_tick(plan, events_path=events)

    # No replay_tick_done was emitted: events file is unchanged.
    post_lines = events.read_text().splitlines()
    assert post_lines == pre_lines


def test_no_fixture_no_executor_records_skipped_capture(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    _make_tick(events, 3, [{"issue": 9, "title": "z", "pr_url": None,
                            "status": "merged", "cost_usd": 0.0}])
    plan = r.plan_replay(events, tick=3, role="worker",
                         new_brief="b", fixtures_dir=None)
    captures = r.run_replay_tick(plan, events_path=events)
    assert captures[0].status == "skipped_no_fixture"
    assert captures[0].source == "skipped"

    # The replay_tick_done event IS written (the run completed, just with
    # nothing executed) — operators should see it in the diff report.
    report = r.build_diff_report(events, tick=3, replay_tick="3r")
    assert report["rows"][0]["replay_status"] == "skipped_no_fixture"


# --------------------------------------------------------------- CLI parser

def test_cli_replay_subcommand_registered() -> None:
    from forge_loop.cli import main
    with pytest.raises(SystemExit) as exc:
        main(["replay", "--help"])
    assert exc.value.code == 0
    with pytest.raises(SystemExit) as exc2:
        main(["replay", "diff", "--help"])
    assert exc2.value.code == 0


def test_cli_replay_requires_tick_and_brief() -> None:
    from forge_loop.cli import main
    with pytest.raises(SystemExit):
        main(["replay"])  # missing --tick / --brief
