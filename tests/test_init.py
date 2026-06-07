"""Tests for init.py — scaffolding."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from forge_loop.control.status import collect_control_plane_status
from forge_loop.init import init_project


def test_init_creates_yaml_and_manual_stub(tmp_path: Path) -> None:
    result = init_project(tmp_path, github_repo="example/foo")
    assert "forge-loop.yaml" in result["created"]
    assert (tmp_path / "forge-loop.yaml").exists()
    body = (tmp_path / "forge-loop.yaml").read_text()
    assert "example/foo" in body
    assert "deploy:" in body
    assert "labels:" in body
    assert "manual/project-quickref.md" in result["created"]
    assert (tmp_path / "manual" / "project-quickref.md").exists()


def test_init_creates_empty_durable_control_plane_stores(tmp_path: Path) -> None:
    result = init_project(tmp_path, github_repo="example/foo")

    assert ".forge/events.db" in result["created"]
    assert ".forge/frontier.yaml" in result["created"]
    assert ".forge/memory.db" in result["created"]
    assert ".forge/tasks.db" in result["created"]
    assert "docs/ops/worker-sessions.db" in result["created"]

    status = collect_control_plane_status(
        tmp_path,
        datetime(2026, 6, 3, tzinfo=UTC),
    )

    assert status["event_log"]["available"] is True
    assert status["event_log"]["last_sequence"] == 0
    assert status["frontier"]["available"] is True
    assert status["memory"] == {
        "available": True,
        "path": str(tmp_path / ".forge" / "memory.db"),
        "active_count": 0,
        "rejected_count": 0,
    }
    assert status["tasks"] == {
        "available": True,
        "path": str(tmp_path / ".forge" / "tasks.db"),
        "in_flight_count": 0,
        "stale_lease_count": 0,
    }
    assert status["boot"]["available"] is True


def test_init_idempotent_skips_existing_without_force(tmp_path: Path) -> None:
    init_project(tmp_path, github_repo="a/b")
    result = init_project(tmp_path, github_repo="c/d")
    assert "forge-loop.yaml" in result["skipped"]
    # Body unchanged — still has a/b not c/d
    assert "a/b" in (tmp_path / "forge-loop.yaml").read_text()


def test_init_force_overwrites(tmp_path: Path) -> None:
    init_project(tmp_path, github_repo="a/b")
    result = init_project(tmp_path, github_repo="c/d", force=True)
    assert "forge-loop.yaml" in result["created"]
    assert "c/d" in (tmp_path / "forge-loop.yaml").read_text()


def test_init_scaffold_does_not_leak_project_specific_deploy_task(tmp_path: Path) -> None:
    """Scaffold MUST NOT default deploy.task to a project-specific value.

    Regression guard for forge-loop#25: the original OSS extraction left
    `task: deploy:k3s:trunk` in the init template, which made every fresh
    `forge-loop init` write a yaml that immediately tripped the deploy_drift
    safety brake on any unrelated repo. The scaffold must ship an empty
    string default (skip redeploy) and let operators opt in explicitly.
    """
    init_project(tmp_path, github_repo="example/foo")
    body = (tmp_path / "forge-loop.yaml").read_text()
    # Scan only the active `task:` assignment line — comments may legitimately
    # mention task names as examples.
    task_lines = [
        line.strip()
        for line in body.splitlines()
        if line.strip().startswith("task:") and not line.lstrip().startswith("#")
    ]
    assert len(task_lines) == 1, f"expected one `task:` line, got {task_lines}"
    actual = task_lines[0]
    # The active default must be empty (`task: \"\"`).
    assert actual in {'task: ""', "task: ''"}, (
        f"Scaffold's deploy.task must default to an empty string; got: {actual}. "
        "A concrete value (e.g. `deploy:k3s:trunk`) would trip the redeploy "
        "drift-detector on every fresh `forge-loop init` against an unrelated repo."
    )


def test_init_appends_to_existing_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("# existing\nfoo/\n")
    result = init_project(tmp_path, github_repo="a/b")
    assert any("gitignore" in p for p in result["created"])
    content = (tmp_path / ".gitignore").read_text()
    assert "# existing" in content
    assert "loop-runner.pid" in content
    assert "docs/ops/worker-sessions.db*" in content
    assert "docs/ops/critic-*.log*" in content


def test_init_skips_gitignore_when_missing(tmp_path: Path) -> None:
    # When there's no .gitignore at all, init should NOT create one
    # (operators typically already have repo-root .gitignore).
    init_project(tmp_path, github_repo="a/b")
    assert not (tmp_path / ".gitignore").exists()
