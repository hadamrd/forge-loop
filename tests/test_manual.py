"""Tests for manual.py — filesystem-backed runbook lookups."""

from __future__ import annotations

from pathlib import Path

from forge_loop import manual


def _seed(repo: Path) -> Path:
    d = repo / "dev" / "sprint-loop" / "manual"
    d.mkdir(parents=True)
    (d / "secrets.md").write_text("# Secrets\n\nyour secret manager, etc.")
    (d / "k3s-rig.md").write_text("# k3s rig\n\nLive cluster deployment.")
    (d / "deploy.md").write_text("# Deploy\n\nRun `task deploy:cluster`.")
    return d


def test_list_topics_returns_all_entries_sorted(tmp_path: Path) -> None:
    _seed(tmp_path)
    entries = manual.list_topics(tmp_path)
    topics = {e.topic for e in entries}
    assert {"secrets", "k3s-rig", "deploy"} <= topics


def test_list_topics_title_is_first_non_empty_line(tmp_path: Path) -> None:
    _seed(tmp_path)
    entries = {e.topic: e for e in manual.list_topics(tmp_path)}
    assert entries["secrets"].title == "Secrets"
    assert entries["k3s-rig"].title == "k3s rig"


def test_lookup_exact_match(tmp_path: Path) -> None:
    _seed(tmp_path)
    e = manual.lookup(tmp_path, "deploy")
    assert e is not None
    assert "task deploy:cluster" in e.body


def test_lookup_case_insensitive(tmp_path: Path) -> None:
    _seed(tmp_path)
    e = manual.lookup(tmp_path, "DEPLOY")
    assert e is not None
    assert e.topic == "deploy"


def test_lookup_unknown_topic_returns_none(tmp_path: Path) -> None:
    _seed(tmp_path)
    assert manual.lookup(tmp_path, "nonexistent") is None


def test_search_finds_body_substring(tmp_path: Path) -> None:
    _seed(tmp_path)
    matches = manual.search(tmp_path, "your secret manager")
    assert any(e.topic == "secrets" for e in matches)


def test_search_ranks_topic_match_first(tmp_path: Path) -> None:
    _seed(tmp_path)
    # Search for "deploy": topic-match (deploy.md) should be first
    matches = manual.search(tmp_path, "deploy")
    assert matches[0].topic == "deploy"


def test_search_empty_query_returns_empty(tmp_path: Path) -> None:
    _seed(tmp_path)
    assert manual.search(tmp_path, "") == []


def test_search_respects_limit(tmp_path: Path) -> None:
    _seed(tmp_path)
    matches = manual.search(tmp_path, "e", limit=2)  # 'e' appears in every entry
    assert len(matches) <= 2


def test_repo_root_override_takes_precedence(tmp_path: Path) -> None:
    """A topic defined in the repo-root manual dir wins over the package default."""
    repo_dir = tmp_path / "dev" / "sprint-loop" / "manual"
    repo_dir.mkdir(parents=True)
    (repo_dir / "secrets.md").write_text("# REPO-OVERRIDE\n\nThis wins.")
    e = manual.lookup(tmp_path, "secrets")
    assert e is not None
    assert "REPO-OVERRIDE" in e.body


def test_extract_title_helper() -> None:
    assert manual.extract_title_from_text("# Hello\n\nbody") == "Hello"
    assert manual.extract_title_from_text("no title here") is None
