"""Tests for init.py — scaffolding."""

from __future__ import annotations

from pathlib import Path

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


def test_init_appends_to_existing_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("# existing\nfoo/\n")
    result = init_project(tmp_path, github_repo="a/b")
    assert any("gitignore" in p for p in result["created"])
    content = (tmp_path / ".gitignore").read_text()
    assert "# existing" in content
    assert "loop-runner.pid" in content


def test_init_skips_gitignore_when_missing(tmp_path: Path) -> None:
    # When there's no .gitignore at all, init should NOT create one
    # (operators typically already have repo-root .gitignore).
    init_project(tmp_path, github_repo="a/b")
    assert not (tmp_path / ".gitignore").exists()
