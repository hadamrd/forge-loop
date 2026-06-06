"""Tests for ``forge-loop research add`` — issue #278.

The research-note channel surfaces cited external state-of-art into the
brainstormer's frontier-generation inputs. These tests cover the CLI write
seam: a valid note persists, and the research-channel-specific evidence-ref
guard rejects an uncited note WITHOUT touching the store.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from forge_loop import cli
from forge_loop._testing.memory_store import FakeMemoryStore
from forge_loop.memory.models import RESEARCH_TAG


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


# ---------------------------------------------------------------------------
# Integration: valid note persists
# ---------------------------------------------------------------------------


def test_research_add_persists_research_tagged_item(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _install_store(monkeypatch)

    result = runner.invoke(
        cli.app,
        [
            "research",
            "add",
            "--title",
            "Speculative-decoding critic",
            "--ref",
            "https://arxiv.org/abs/1234.5678",
            "--ref",
            "tool:vllm",
            "--note",
            "cuts critic latency ~2x",
        ],
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    notes = store.list_research_notes()
    assert len(notes) == 1
    item = notes[0]
    assert item.title == "Speculative-decoding critic"
    assert RESEARCH_TAG in item.tags
    assert item.kind.value == "semantic"
    assert item.provenance.evidence_refs == (
        "https://arxiv.org/abs/1234.5678",
        "tool:vllm",
    )
    assert item.body == "cuts critic latency ~2x"


# ---------------------------------------------------------------------------
# Adversarial: no evidence ref → rejected, nothing persisted
# ---------------------------------------------------------------------------


def test_research_add_without_ref_is_rejected_and_persists_nothing(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _install_store(monkeypatch)

    # Typer marks --ref required, so omitting it entirely is a usage error (2).
    # We assert the store stays empty either way — the load-bearing guarantee.
    result = runner.invoke(
        cli.app,
        ["research", "add", "--title", "Uncited claim"],
    )

    assert result.exit_code != 0
    assert store.list_research_notes() == ()


def test_research_add_with_blank_ref_is_rejected_at_write_seam(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whitespace-only --ref must be rejected by the research-channel guard
    (exit 2) and persist nothing — the validation is at the CLI seam, not a
    global ``MemoryProvenance`` change."""
    store = _install_store(monkeypatch)

    result = runner.invoke(
        cli.app,
        ["research", "add", "--title", "Uncited", "--ref", "   "],
    )

    assert result.exit_code == 2
    assert "at least one --ref" in (result.stdout + result.stderr)
    assert store.list_research_notes() == ()


def test_research_add_without_title_is_rejected(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _install_store(monkeypatch)
    result = runner.invoke(
        cli.app,
        ["research", "add", "--title", "   ", "--ref", "url:x"],
    )
    assert result.exit_code == 2
    assert store.list_research_notes() == ()
