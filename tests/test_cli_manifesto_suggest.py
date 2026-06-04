"""CLI integration + e2e tests for `forge-loop manifesto suggest` (#134).

Covers:
  * Subcommand is registered + shows in help.
  * Dry-run prints suggestion, makes ZERO write calls (no PR opener invoked),
    exits 0.
  * ``--apply`` opens exactly one PR targeting ``.forge/*-manifesto.md`` with
    the delta.
  * ``--apply`` without a configured repo errors non-zero, opens nothing.
  * Sad path: insufficient context (unreadable PR) → exit 1, nothing written.
  * Sad path: malformed SDK output → exit 1, no PR opened.

All tests use ``typer.testing.CliRunner`` and stub the suggester + PR opener
so no real SDK / GitHub / git is touched.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from forge_loop import cli
from forge_loop.manifesto_suggest import (
    EditKind,
    InsufficientContextError,
    ManifestoSuggestion,
    ManifestoTarget,
    ProposedManifestoEdit,
    SuggestionParseError,
)
from forge_loop.manifestos import QUALITY_REL


@pytest.fixture
def runner() -> CliRunner:
    try:
        return CliRunner(mix_stderr=False)  # type: ignore[call-arg]
    except TypeError:
        return CliRunner()


@pytest.fixture
def cwd_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    forge = tmp_path / ".forge"
    forge.mkdir(parents=True, exist_ok=True)
    (forge / "quality-manifesto.md").write_text("# Quality\n\nQ6. existing\n", encoding="utf-8")
    (forge / "testing-manifesto.md").write_text("# Testing\n\nT1. existing\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        cli, "load", lambda: SimpleNamespace(repo=tmp_path, github_repo="acme/widgets")
    )
    # Default gh client factory returns a harmless recorder so dry-run doesn't
    # try to build a real githubkit client.
    monkeypatch.setattr(cli, "_gh_client_factory", lambda: SimpleNamespace())
    return tmp_path


def _suggestion() -> ManifestoSuggestion:
    return ManifestoSuggestion(
        summary="guard gh JSON access",
        edits=[
            ProposedManifestoEdit(
                target=ManifestoTarget.QUALITY,
                kind=EditKind.ADD,
                rule_text="Never index gh JSON payloads directly.",
                rationale="Derived from PR #207 / issue #199.",
            )
        ],
    )


class _StubSuggester:
    def __init__(self, *, result: Any = None, exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc
        self.runs: list[int] = []

    def run(self, pr_number: int) -> ManifestoSuggestion:
        self.runs.append(pr_number)
        if self._exc is not None:
            raise self._exc
        return self._result


class _SpyPrOpener:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def __call__(self, plan: Any, **kwargs: Any) -> str:
        self.calls.append((plan, kwargs))
        return "https://github.com/acme/widgets/pull/999"


def _install(
    monkeypatch: pytest.MonkeyPatch,
    suggester: _StubSuggester,
    pr_opener: _SpyPrOpener | None = None,
) -> _SpyPrOpener:
    monkeypatch.setattr(cli, "_manifesto_suggester_factory", lambda *a, **k: suggester)
    opener = pr_opener or _SpyPrOpener()
    monkeypatch.setattr(cli, "_manifesto_pr_opener", opener)
    return opener


def test_subcommand_registered_in_help(runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["manifesto", "--help"])
    assert result.exit_code == 0
    assert "suggest" in result.stdout


def test_dry_run_prints_and_makes_zero_writes(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suggester = _StubSuggester(result=_suggestion())
    opener = _install(monkeypatch, suggester)

    result = runner.invoke(cli.app, ["manifesto", "suggest", "--from-pr", "207"])

    assert result.exit_code == 0, result.stdout + getattr(result, "stderr", "")
    assert "PR #207" in result.stdout
    assert "Never index gh JSON payloads directly" in result.stdout
    # Recoverable structured form present.
    assert "structured (recoverable)" in result.stdout
    # ZERO write calls — the PR opener was never invoked on the dry-run path.
    assert opener.calls == []
    assert suggester.runs == [207]


def test_apply_opens_exactly_one_pr_targeting_manifesto(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suggester = _StubSuggester(result=_suggestion())
    opener = _install(monkeypatch, suggester)

    result = runner.invoke(cli.app, ["manifesto", "suggest", "--from-pr", "207", "--apply"])

    assert result.exit_code == 0, result.stdout + getattr(result, "stderr", "")
    assert len(opener.calls) == 1
    plan, kwargs = opener.calls[0]
    # The PR-create plan targets the quality manifesto file with the delta.
    assert QUALITY_REL in plan.file_contents
    assert "Never index gh JSON payloads directly" in plan.file_contents[QUALITY_REL]
    assert kwargs["github_repo"] == "acme/widgets"
    assert "999" in result.stdout


def test_apply_without_repo_errors_nonzero(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load", lambda: SimpleNamespace(repo=tmp_path, github_repo=""))
    monkeypatch.setattr(cli, "_gh_client_factory", lambda: SimpleNamespace())
    suggester = _StubSuggester(result=_suggestion())
    opener = _install(monkeypatch, suggester)

    result = runner.invoke(cli.app, ["manifesto", "suggest", "--from-pr", "207", "--apply"])

    assert result.exit_code != 0
    # Guard fires BEFORE running the SDK and BEFORE opening anything.
    assert opener.calls == []
    assert suggester.runs == []


def test_insufficient_context_exits_nonzero_and_writes_nothing(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suggester = _StubSuggester(
        exc=InsufficientContextError("could not read PR #207 / insufficient context")
    )
    opener = _install(monkeypatch, suggester)

    result = runner.invoke(cli.app, ["manifesto", "suggest", "--from-pr", "207", "--apply"])

    assert result.exit_code == 1
    assert opener.calls == []
    assert "insufficient context" in (getattr(result, "stderr", "") + result.stdout)


def test_malformed_sdk_output_exits_nonzero_no_pr(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suggester = _StubSuggester(exc=SuggestionParseError("could not parse manifesto suggestion"))
    opener = _install(monkeypatch, suggester)

    result = runner.invoke(cli.app, ["manifesto", "suggest", "--from-pr", "207", "--apply"])

    assert result.exit_code == 1
    assert opener.calls == []


def test_factory_not_passed_dead_provider_kwarg(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for sev3 review: ``provider`` was accepted by the factory but
    silently dropped (``ManifestoSuggester`` has no such field). The command
    must no longer pass it."""
    seen_kwargs: dict[str, Any] = {}
    suggester = _StubSuggester(result=_suggestion())

    def _factory(*_a: Any, **k: Any) -> _StubSuggester:
        seen_kwargs.update(k)
        return suggester

    monkeypatch.setattr(cli, "_manifesto_suggester_factory", _factory)
    monkeypatch.setattr(cli, "_manifesto_pr_opener", _SpyPrOpener())

    result = runner.invoke(cli.app, ["manifesto", "suggest", "--from-pr", "207"])

    assert result.exit_code == 0, result.stdout + getattr(result, "stderr", "")
    assert "provider" not in seen_kwargs
    # The kwargs that survive are the meaningful ones.
    assert set(seen_kwargs) <= {"gh_client", "model", "timeout_s"}


def test_config_load_error_is_logged_not_swallowed(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for sev1 EH-001: a failing ``self.load()`` must be caught
    specifically (ConfigError) and logged with the repo path — not swallowed
    by a bare ``except Exception: pass``. The dry-run still runs on defaults."""
    from forge_loop import cli_product_commands
    from forge_loop.settings import ConfigError

    monkeypatch.chdir(tmp_path)

    def _boom() -> Any:
        raise ConfigError("broken forge-loop.yaml")

    monkeypatch.setattr(cli, "load", _boom)
    monkeypatch.setattr(cli, "_gh_client_factory", lambda: SimpleNamespace())

    warnings: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        cli_product_commands._log,
        "warning",
        lambda event, **kw: warnings.append((event, kw)),
    )

    suggester = _StubSuggester(result=_suggestion())
    opener = _install(monkeypatch, suggester)

    result = runner.invoke(cli.app, ["manifesto", "suggest", "--from-pr", "207"])

    # Command still works on defaults (dry-run), exits 0, writes nothing.
    assert result.exit_code == 0, result.stdout + getattr(result, "stderr", "")
    assert opener.calls == []
    # The failure was logged (not silently swallowed) with the repo path.
    assert warnings, "ConfigError should be logged, not swallowed"
    event, kw = warnings[-1]
    assert "config load failed" in event
    assert "repo_path" in kw
    assert "broken forge-loop.yaml" in kw.get("error", "")


def test_empty_suggestion_apply_opens_nothing(
    runner: CliRunner, cwd_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suggester = _StubSuggester(result=ManifestoSuggestion())
    opener = _install(monkeypatch, suggester)

    result = runner.invoke(cli.app, ["manifesto", "suggest", "--from-pr", "207", "--apply"])

    assert result.exit_code == 0
    assert opener.calls == []
    assert "nothing to apply" in result.stdout
