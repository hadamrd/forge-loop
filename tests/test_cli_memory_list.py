"""Tests for ``forge-loop memory list`` — issue #328.

The curated memory store durably records load-bearing decisions, rejected
paths, and episodes with full provenance and an active/superseded lifecycle.
This read-only CLI lets an operator inspect *which* items survived a context
reset and *where they came from*, without opening the SQLite file by hand.

These tests cover the read seam: grouping by ``MemoryKind``, exclusion of
superseded items, ``--kind`` / ``--tag`` filtering, the invalid-kind usage
error, the store-unavailable sad path, and the friendly empty result.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from forge_loop import cli
from forge_loop._testing.memory_store import FakeMemoryStore
from forge_loop.eventlog.models import EventId, EventRef
from forge_loop.memory.models import (
    REJECTED_PATH_TAG,
    MemoryItem,
    MemoryKind,
    MemoryProvenance,
)


@pytest.fixture
def runner() -> CliRunner:
    try:
        return CliRunner(mix_stderr=False)  # type: ignore[call-arg]
    except TypeError:
        return CliRunner()


@pytest.fixture
def cwd_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load", lambda: SimpleNamespace(repo=tmp_path, github_repo="acme/x"))
    return tmp_path


def _install_store(monkeypatch: pytest.MonkeyPatch) -> FakeMemoryStore:
    store = FakeMemoryStore()
    monkeypatch.setattr(cli, "_memory_store_factory", lambda _repo: store)
    return store


def _item(
    memory_id: str,
    kind: MemoryKind,
    title: str,
    *,
    tags: tuple[str, ...] = (),
    authored_by: str = "curator",
    confidence: float = 1.0,
    source_event: EventRef | None = None,
    source_task_ref: str | None = None,
    superseded_by: str | None = None,
) -> MemoryItem:
    # MemoryProvenance requires a source event OR a task ref; default to a task
    # ref so callers can omit both and still build a valid item.
    if source_event is None and source_task_ref is None:
        source_task_ref = "seed-task"
    return MemoryItem(
        memory_id=memory_id,
        kind=kind,
        title=title,
        body="(body)",
        tags=tags,
        provenance=MemoryProvenance(
            source_event=source_event,
            authored_by=authored_by,
            source_task_ref=source_task_ref,
            confidence=confidence,
        ),
        superseded_by=superseded_by,
    )


# ---------------------------------------------------------------------------
# Unit / handler: groups by kind, renders title + tags + provenance
# ---------------------------------------------------------------------------


def test_memory_list_groups_by_kind_with_provenance(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _install_store(monkeypatch)
    store.put(
        _item(
            "sem-1",
            MemoryKind.SEMANTIC,
            "Reject markdown-only architecture",
            tags=(REJECTED_PATH_TAG, "axis:architecture"),
            authored_by="brainstorm-critic",
            confidence=0.8,
            source_event=EventRef(event_id=EventId("evt-123"), sequence=42),
        )
    )
    store.put(_item("epi-1", MemoryKind.EPISODIC, "Sprint 7 retro lesson"))
    store.put(_item("proc-1", MemoryKind.PROCEDURAL, "How to bisect a flaky test"))

    result = runner.invoke(cli.app, ["memory", "list"])

    assert result.exit_code == 0, result.stdout + result.stderr
    out = result.stdout
    # Grouped by kind — every bucket header present.
    assert "semantic" in out
    assert "episodic" in out
    assert "procedural" in out
    # Titles rendered.
    assert "Reject markdown-only architecture" in out
    assert "Sprint 7 retro lesson" in out
    assert "How to bisect a flaky test" in out
    # Tags rendered.
    assert REJECTED_PATH_TAG in out
    assert "axis:architecture" in out
    # Provenance: authored_by, confidence, source-event reference.
    assert "brainstorm-critic" in out
    assert "0.8" in out
    assert "evt-123" in out
    assert "42" in out


def test_memory_list_renders_task_ref_when_no_source_event(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Source-reference fallthrough: an item with no ``source_event`` must fall
    back to rendering ``source_task_ref`` (the else arm of the source formatter)."""
    store = _install_store(monkeypatch)
    store.put(
        _item(
            "epi-1",
            MemoryKind.EPISODIC,
            "Episode without an event",
            source_task_ref="task/abc-789",
        )
    )

    result = runner.invoke(cli.app, ["memory", "list"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "task/abc-789" in result.stdout


# ---------------------------------------------------------------------------
# Unit / filters
# ---------------------------------------------------------------------------


def test_memory_list_kind_filter_shows_only_that_bucket(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _install_store(monkeypatch)
    store.put(_item("sem-1", MemoryKind.SEMANTIC, "Semantic decision"))
    store.put(_item("epi-1", MemoryKind.EPISODIC, "Episodic lesson"))
    store.put(_item("proc-1", MemoryKind.PROCEDURAL, "Procedural recipe"))

    result = runner.invoke(cli.app, ["memory", "list", "--kind", "semantic"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Semantic decision" in result.stdout
    assert "Episodic lesson" not in result.stdout
    assert "Procedural recipe" not in result.stdout


def test_memory_list_tag_filter_shows_only_matching_items(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _install_store(monkeypatch)
    store.put(
        _item(
            "sem-1",
            MemoryKind.SEMANTIC,
            "A rejected path",
            tags=(REJECTED_PATH_TAG,),
        )
    )
    store.put(_item("sem-2", MemoryKind.SEMANTIC, "An accepted fact", tags=("research",)))

    result = runner.invoke(cli.app, ["memory", "list", "--tag", REJECTED_PATH_TAG])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "A rejected path" in result.stdout
    assert "An accepted fact" not in result.stdout


# ---------------------------------------------------------------------------
# Integration (CliRunner): end-to-end exit 0
# ---------------------------------------------------------------------------


def test_memory_list_end_to_end_exit_zero(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _install_store(monkeypatch)
    store.put(_item("sem-1", MemoryKind.SEMANTIC, "Durable decision survives reset"))

    result = runner.invoke(cli.app, ["memory", "list"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Durable decision survives reset" in result.stdout


def test_memory_list_empty_store_is_friendly_and_exits_zero(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_store(monkeypatch)

    result = runner.invoke(cli.app, ["memory", "list"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "no active memory items" in result.stdout.lower()


def test_memory_list_tag_no_match_is_friendly_and_exits_zero(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _install_store(monkeypatch)
    store.put(_item("sem-1", MemoryKind.SEMANTIC, "Has no such tag", tags=("research",)))

    result = runner.invoke(cli.app, ["memory", "list", "--tag", "does-not-exist"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "no active memory items" in result.stdout.lower()
    assert "Has no such tag" not in result.stdout


# ---------------------------------------------------------------------------
# Adversarial / sad path (REQUIRED)
# ---------------------------------------------------------------------------


def test_memory_list_excludes_superseded_items(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A superseded item must NOT appear; only the active one prints."""
    store = _install_store(monkeypatch)
    store.put(_item("new-1", MemoryKind.SEMANTIC, "Active winning decision"))
    store.put(
        _item(
            "old-1",
            MemoryKind.SEMANTIC,
            "Superseded losing decision",
            superseded_by="new-1",
        )
    )

    result = runner.invoke(cli.app, ["memory", "list"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Active winning decision" in result.stdout
    assert "Superseded losing decision" not in result.stdout


def test_memory_list_excludes_superseded_on_real_sqlite_store(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end against a real :class:`SqliteMemoryStore`: ``put`` two items,
    ``supersede`` the old one, and assert only the active title prints. This
    guards the read path against fake/real drift (testing manifesto T4)."""
    from forge_loop.memory.store import SqliteMemoryStore

    store = SqliteMemoryStore(cwd_repo / "memory.db")
    store.put(_item("old-1", MemoryKind.SEMANTIC, "Old decision via real store"))
    store.put(_item("new-1", MemoryKind.SEMANTIC, "New decision via real store"))
    store.supersede("old-1", by_memory_id="new-1")
    monkeypatch.setattr(cli, "_memory_store_factory", lambda _repo: store)

    result = runner.invoke(cli.app, ["memory", "list"])

    assert result.exit_code == 0, result.stdout + result.stderr
    assert "New decision via real store" in result.stdout
    assert "Old decision via real store" not in result.stdout


def test_memory_list_invalid_kind_exits_2_and_prints_no_items(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _install_store(monkeypatch)
    store.put(_item("sem-1", MemoryKind.SEMANTIC, "Should not be printed"))

    result = runner.invoke(cli.app, ["memory", "list", "--kind", "bogus"])

    assert result.exit_code == 2
    assert "Should not be printed" not in result.stdout
    combined = result.stdout + result.stderr
    assert "bogus" in combined or "kind" in combined.lower()


def test_memory_list_store_unavailable_exits_nonzero_without_traceback(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(_repo: Path) -> FakeMemoryStore:
        raise RuntimeError("memory.db missing or unreadable")

    monkeypatch.setattr(cli, "_memory_store_factory", _boom)

    result = runner.invoke(cli.app, ["memory", "list"])

    assert result.exit_code != 0
    assert result.exit_code != 2  # a real failure, not a usage error
    combined = result.stdout + result.stderr
    assert "memory store unavailable" in combined.lower()
    # Fail-soft: no unhandled traceback bubbled to the operator.
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Traceback" not in combined


# ---------------------------------------------------------------------------
# Read-only guarantee
# ---------------------------------------------------------------------------


def test_memory_list_performs_zero_writes(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _install_store(monkeypatch)
    store.put(_item("sem-1", MemoryKind.SEMANTIC, "Read me"))
    snapshot = dict(store.items)

    result = runner.invoke(cli.app, ["memory", "list"])

    assert result.exit_code == 0, result.stdout + result.stderr
    # No mutation: the store's contents are byte-for-byte identical.
    assert store.items == snapshot
