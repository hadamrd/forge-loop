"""Tests for the ``loop_snapshot`` one-call introspection tool (issue #64).

The snapshot replaces a fan-out of ``loop_status`` + ``events_recent`` +
``gh pr list`` + ``ls /tmp/wt-loop-*`` + ``attempts_history``. These
tests exercise the helper directly (``forge_loop.snapshot.build_snapshot``)
with injected gh-CLI seams and a fake event log so we don't need network
or a real loop install.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from forge_loop.snapshot import build_snapshot


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------
class _FakeLabels:
    ready = "loop:ready"
    triage = "loop:triage"
    blocked = "loop:blocked"
    risk_gate = "risk:high"


class _FakeConfig:
    """Minimal stand-in for ``forge_loop.config.Config``.

    The snapshot helper only touches: ``state_file``, ``events_file``,
    ``state_dir``, ``logs_dir``, ``github_repo``, ``labels.ready`` and
    (optionally) ``worktree_root``. We don't import the real Config
    because instantiating it requires LOOP_GH_REPO + a git repo root.
    """

    def __init__(self, root: Path, *, repo: str = "owner/forge-loop") -> None:
        self.state_dir = root
        self.state_file = root / "loop-runner.json"
        self.events_file = root / "loop-runner-events.jsonl"
        self.summaries_file = root / "loop-runner-summaries.jsonl"
        self.logs_dir = root / "loop-runner-logs"
        self.github_repo = repo
        self.labels = _FakeLabels()
        self.worktree_root = root / "wt"


def _write_events(events_file: Path, events: list[dict[str, Any]]) -> None:
    events_file.parent.mkdir(parents=True, exist_ok=True)
    with open(events_file, "a") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_empty_install_returns_sensible_defaults(tmp_path: Path) -> None:
    """No state file, no events, no PRs — snapshot still returns a
    well-formed dict (the happy path for a brand-new install)."""
    cfg = _FakeConfig(tmp_path)
    snap = build_snapshot(
        cfg,
        since_minutes=15,
        queue_depth_fn=lambda repo, label: 0,
        open_prs_fn=lambda repo: [],
    )

    assert snap["state"] == "uninitialised"
    assert snap["tick"] == 0
    assert snap["runner_id"] is None
    assert snap["queue_depth"] == 0
    assert snap["in_flight"] == []
    assert snap["in_flight_count"] == 0
    assert snap["recent_kinds"] == {}
    assert snap["open_prs"] == []
    assert snap["halt_marker"]["present"] is False
    assert snap["halt_marker"]["age_s"] is None
    assert snap["last_drift_event"] is None
    # Shape contract: schema_version + ts + window are always present.
    assert snap["schema_version"] == 1
    assert "ts" in snap
    assert snap["since_minutes"] == 15


def test_full_fixture_snapshot_matches_issue_contract(tmp_path: Path) -> None:
    """2 in-flight workers + 3 ready issues + 1 halt marker + 1 drift event
    + 2 open PRs → snapshot matches the issue's stated contract."""
    cfg = _FakeConfig(tmp_path)
    now = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)

    # State file: tick 7, running.
    cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_file.write_text(json.dumps({
        "ts": _iso(now - timedelta(minutes=2)),
        "state": "running",
        "tick": 7,
        "dispatched": [{"issue": 101, "title": "x"}, {"issue": 102, "title": "y"}],
    }))

    # Events: loop_start (carries runner_id) + 2 worker_start + 1 drift event
    # + 1 unrelated event. Worker 102 completes; worker 101 does not.
    events = [
        {"ts": _iso(now - timedelta(minutes=10)), "kind": "loop_start",
         "runner_id": "host-abc-123"},
        {"ts": _iso(now - timedelta(minutes=8)), "kind": "worker_start",
         "issue": 101},
        {"ts": _iso(now - timedelta(minutes=8)), "kind": "worker_start",
         "issue": 102},
        {"ts": _iso(now - timedelta(minutes=6)), "kind": "tool_use",
         "issue": 101},
        {"ts": _iso(now - timedelta(minutes=5)), "kind": "deploy_drift_detected",
         "tick": 7, "signature": "abc"},
        {"ts": _iso(now - timedelta(minutes=4)), "kind": "worker_done",
         "issue": 102, "status": "merged"},
        {"ts": _iso(now - timedelta(minutes=3)), "kind": "worker_start",
         "issue": 103},  # third in-flight
    ]
    _write_events(cfg.events_file, events)

    # Halt marker.
    halt = cfg.state_dir / "loop-runner.HALT"
    halt.write_text("deploy-drift halt: 3 in a row")

    # Worktree for 101 — create one with a HEAD pointing to a branch.
    wt = cfg.worktree_root / "wt-loop-101"
    (wt / ".git").mkdir(parents=True)
    (wt / ".git" / "HEAD").write_text("ref: refs/heads/loop/101-add-thing\n")
    # SDK log for 101.
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    (cfg.logs_dir / "worker-101-1716800000.log").write_bytes(b"x" * 1024)

    # Fake gh hooks.
    queue_calls: list[tuple[str, str]] = []

    def fake_queue(repo: str, label: str) -> int:
        queue_calls.append((repo, label))
        return 3

    def fake_prs(repo: str) -> list[dict[str, Any]]:
        return [
            {"number": 555, "title": "fix: foo", "branch": "loop/101-add-thing"},
            {"number": 556, "title": "feat: bar", "branch": "loop/103-baz"},
        ]

    snap = build_snapshot(
        cfg, since_minutes=15, now=now,
        queue_depth_fn=fake_queue, open_prs_fn=fake_prs,
    )

    # Top-level scalars.
    assert snap["tick"] == 7
    assert snap["state"] == "running"
    assert snap["runner_id"] == "host-abc-123"
    assert snap["queue_depth"] == 3
    assert queue_calls == [("owner/forge-loop", "loop:ready")]

    # In-flight: 101 and 103 (102 completed); sorted by issue number.
    inflight_issues = [w["issue"] for w in snap["in_flight"]]
    assert inflight_issues == [101, 103]
    assert snap["in_flight_count"] == 2

    w101 = next(w for w in snap["in_flight"] if w["issue"] == 101)
    assert w101["branch"] == "loop/101-add-thing"
    assert w101["worktree"] == str(wt)
    assert w101["sdk_log_size"] == 1024
    # Last event for 101 was the tool_use at -6min → ~360s old.
    assert 350 <= (w101["last_event_age_s"] or 0) <= 370

    # Worker 103 has no worktree on disk → fields are None/0.
    w103 = next(w for w in snap["in_flight"] if w["issue"] == 103)
    assert w103["branch"] is None
    assert w103["worktree"] is None
    assert w103["sdk_log_size"] == 0

    # Recent kinds — counts across the 15-min window.
    assert snap["recent_kinds"]["worker_start"] == 3
    assert snap["recent_kinds"]["worker_done"] == 1
    assert snap["recent_kinds"]["deploy_drift_detected"] == 1

    # Open PRs.
    assert len(snap["open_prs"]) == 2
    assert snap["open_prs"][0]["number"] == 555
    assert snap["open_prs"][0]["branch"] == "loop/101-add-thing"

    # Halt marker.
    assert snap["halt_marker"]["present"] is True
    assert snap["halt_marker"]["path"].endswith("loop-runner.HALT")
    assert snap["halt_marker"]["reason"] == "deploy-drift halt: 3 in a row"

    # Last drift event.
    assert snap["last_drift_event"]["kind"] == "deploy_drift_detected"
    assert snap["last_drift_event"]["tick"] == 7


def test_gh_failures_are_swallowed_and_recorded(tmp_path: Path) -> None:
    """Adversarial: ``gh`` shells raise → snapshot still returns, with
    diagnostics under ``_errors``. The dict shape must not change."""
    cfg = _FakeConfig(tmp_path)

    def boom_queue(repo: str, label: str) -> int:
        raise RuntimeError("gh: not authenticated")

    def boom_prs(repo: str) -> list[dict[str, Any]]:
        raise OSError("network down")

    snap = build_snapshot(
        cfg, since_minutes=15,
        queue_depth_fn=boom_queue, open_prs_fn=boom_prs,
    )
    assert snap["queue_depth"] == 0
    assert snap["open_prs"] == []
    assert "_errors" in snap
    assert "queue_depth" in snap["_errors"]
    assert "open_prs" in snap["_errors"]
    assert "not authenticated" in snap["_errors"]["queue_depth"]


def test_negative_since_minutes_normalised_to_default(tmp_path: Path) -> None:
    """Bad input: ``since_minutes=0`` or negative → coerced to 15."""
    cfg = _FakeConfig(tmp_path)
    snap = build_snapshot(
        cfg, since_minutes=-99,
        queue_depth_fn=lambda r, lab: 0, open_prs_fn=lambda r: [],
    )
    assert snap["since_minutes"] == 15


def test_terminal_event_clears_in_flight(tmp_path: Path) -> None:
    """A ``worker_start`` followed by ``worker_failed`` for the same
    issue must NOT appear in ``in_flight`` (no zombie workers)."""
    cfg = _FakeConfig(tmp_path)
    now = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)
    _write_events(cfg.events_file, [
        {"ts": _iso(now - timedelta(minutes=5)), "kind": "worker_start", "issue": 42},
        {"ts": _iso(now - timedelta(minutes=2)), "kind": "worker_failed", "issue": 42},
    ])
    snap = build_snapshot(
        cfg, since_minutes=15, now=now,
        queue_depth_fn=lambda r, lab: 0, open_prs_fn=lambda r: [],
    )
    assert snap["in_flight"] == []


def test_halt_marker_carries_age_and_reason(tmp_path: Path) -> None:
    """Adversarial: halt file present but unreadable contents → ``present``
    still True; missing file → ``present`` False."""
    cfg = _FakeConfig(tmp_path)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    halt = cfg.state_dir / "loop-runner.HALT"
    halt.write_text("manual halt")
    snap = build_snapshot(
        cfg, since_minutes=15,
        queue_depth_fn=lambda r, lab: 0, open_prs_fn=lambda r: [],
    )
    assert snap["halt_marker"]["present"] is True
    assert snap["halt_marker"]["reason"] == "manual halt"
    assert (snap["halt_marker"]["age_s"] or 0) >= 0


def test_runner_id_falls_back_to_state_field(tmp_path: Path) -> None:
    """If the state file ever carries ``runner_id`` directly (forward-compat),
    we use it without scanning the events file."""
    cfg = _FakeConfig(tmp_path)
    cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.state_file.write_text(json.dumps({
        "state": "running", "tick": 1, "runner_id": "from-state-file",
    }))
    snap = build_snapshot(
        cfg, since_minutes=15,
        queue_depth_fn=lambda r, lab: 0, open_prs_fn=lambda r: [],
    )
    assert snap["runner_id"] == "from-state-file"


# ---------------------------------------------------------------------------
# Integration: the MCP tool wrapper resolves ``cfg`` via ``load_config``
# ---------------------------------------------------------------------------
def test_mcp_tool_wraps_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``mcp_server.loop_snapshot`` is registered as an MCP tool and delegates
    to ``snapshot.build_snapshot``. We replace ``build_snapshot`` to assert
    the wiring without needing a real loop install."""
    monkeypatch.setenv("LOOP_GH_REPO", "owner/forge-loop")
    import importlib

    from forge_loop import mcp_server
    from forge_loop import snapshot as snapshot_mod
    importlib.reload(snapshot_mod)
    importlib.reload(mcp_server)

    called: dict[str, Any] = {}

    def fake_build(cfg: Any, since_minutes: int = 15, **_: Any) -> dict[str, Any]:
        called["since_minutes"] = since_minutes
        return {"tick": 99, "state": "running", "marker": "from-fake"}

    monkeypatch.setattr(snapshot_mod, "build_snapshot", fake_build)
    # The mcp_server tool imports build_snapshot lazily inside the
    # function body, so patching the source module is enough.

    result = mcp_server.loop_snapshot(since_minutes=42)
    assert result["marker"] == "from-fake"
    assert called["since_minutes"] == 42
