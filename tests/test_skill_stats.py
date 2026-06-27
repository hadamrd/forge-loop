"""Skill-tree inventory stats for the `forge-loop skill-stats` command."""

from __future__ import annotations

from pathlib import Path

from forge_loop.memory.models import AREA_NODE_TAG, EXPIRED_TAG, area_tag
from forge_loop.memory.store import MemoryStore, SqliteMemoryStore
from forge_loop.runner.learning import record_procedural_skill
from forge_loop.skill_stats import compute_skill_inventory


def _store(tmp_path: Path) -> SqliteMemoryStore:
    return SqliteMemoryStore(tmp_path / "memory.db")


def _leaf(store: MemoryStore, *, area: str, sig: str, extra: tuple[str, ...] = ()) -> str:
    return record_procedural_skill(
        store,
        failing_signal=sig,
        target=f"{area}.rs",
        title=f"{area}: {sig}",
        body="recipe",
        source_key=f"k:{area}:{sig}",
        source_task_ref="issue:#1",
        extra_tags=(area_tag(area), *extra),
    )


def test_inventory_counts_leaves_nodes_areas(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _leaf(store, area="a/b", sig="s1")
    _leaf(store, area="a/c", sig="s2")
    _leaf(store, area="a", sig="node", extra=(AREA_NODE_TAG,))

    inv = compute_skill_inventory(store)

    assert inv.leaves == 2
    assert inv.nodes == 1
    assert inv.expired == 0
    assert inv.areas == {"a/b": 1, "a/c": 1, "a": 1}


def test_inventory_excludes_expired_from_leaves_and_areas(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _leaf(store, area="a/b", sig="live")
    _leaf(store, area="a/d", sig="dead", extra=(EXPIRED_TAG,))

    inv = compute_skill_inventory(store)

    assert inv.leaves == 1  # the expired one is not a live leaf
    assert inv.expired == 1
    assert "a/d" not in inv.areas


def test_inventory_empty_store(tmp_path: Path) -> None:
    inv = compute_skill_inventory(_store(tmp_path))
    assert inv.leaves == 0
    assert inv.nodes == 0
    assert inv.expired == 0
    assert inv.areas == {}


# --- CLI: forge-loop memory skills -------------------------------------------

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from forge_loop import cli  # noqa: E402
from forge_loop._testing.memory_store import FakeMemoryStore  # noqa: E402


@pytest.fixture
def runner() -> CliRunner:
    try:
        return CliRunner(mix_stderr=False)  # type: ignore[call-arg]
    except TypeError:
        return CliRunner()


def test_cli_memory_skills_reports_inventory(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load", lambda: SimpleNamespace(repo=tmp_path, github_repo="acme/x"))
    store = FakeMemoryStore()
    monkeypatch.setattr(cli, "_memory_store_factory", lambda _repo: store)
    _leaf(store, area="pulsar-node/ledger", sig="s1")
    _leaf(store, area="pulsar-node/http", sig="s2")

    result = runner.invoke(cli.app, ["memory", "skills"])

    assert result.exit_code == 0
    assert "2 leaves" in result.stdout
    assert "pulsar-node/ledger" in result.stdout


def test_cli_memory_skills_empty(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load", lambda: SimpleNamespace(repo=tmp_path, github_repo="x/y"))
    monkeypatch.setattr(cli, "_memory_store_factory", lambda _repo: FakeMemoryStore())

    result = runner.invoke(cli.app, ["memory", "skills"])

    assert result.exit_code == 0
    assert "no skills harvested yet" in result.stdout
