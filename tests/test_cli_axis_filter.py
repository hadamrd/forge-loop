"""Axis-label grouping + filtering for ``forge-loop status`` (issue #126).

These tests pin the axis-aware UX:

* ``status --json`` returns ``axes`` + ``unaligned_count`` keys.
* The Rich human surface emits a yellow ``warning`` row iff there is
  at least one unaligned open ready-issue.
* Multi-axis issues appear under every bucket they qualify for, but
  the count of unaligned is unique (an issue with two axes is never
  counted as unaligned).
* ``status --axis dispatch`` narrows the grouped view.

We monkeypatch ``subprocess.run`` rather than hitting GitHub, the same
way the rest of the CLI suite stubs I/O.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from forge_loop import axis as _axis
from forge_loop import cli


# ---------------------------------------------------------------------------
# Pure axis helpers
# ---------------------------------------------------------------------------


def test_extract_axes_picks_lowercased_slugs_only() -> None:
    labels = [
        {"name": "axis:dispatch"},
        {"name": "Axis:CLI"},          # mixed case → lowercase
        {"name": "axis:"},              # empty slug → dropped
        {"name": "loop:ready"},
        "axis:observability",
    ]
    assert _axis.extract_axes(labels) == {"dispatch", "cli", "observability"}


def test_matches_axes_empty_wanted_is_true() -> None:
    # "no filter set" branch — callers gate on len(wanted), but the
    # helper itself must not refuse to match an unfiltered label list.
    assert _axis.matches_axes([{"name": "axis:foo"}], []) is True


def test_matches_axes_intersect() -> None:
    labels = [{"name": "axis:dispatch"}]
    assert _axis.matches_axes(labels, ["dispatch"]) is True
    assert _axis.matches_axes(labels, ["cli"]) is False
    assert _axis.matches_axes(labels, ["cli", "dispatch"]) is True


def test_group_by_axis_buckets_and_dedupes_unaligned() -> None:
    issues = [
        {"number": 1, "labels": [{"name": "axis:dispatch"}]},
        {"number": 2, "labels": [{"name": "axis:cli"}, {"name": "axis:dispatch"}]},
        {"number": 3, "labels": [{"name": "loop:ready"}]},     # unaligned
        {"number": 4, "labels": [{"name": "axis:"}]},          # malformed → unaligned
    ]
    buckets, unaligned = _axis.group_by_axis(issues)

    # #2 appears under BOTH dispatch and cli
    assert {i["number"] for i in buckets["dispatch"]} == {1, 2}
    assert {i["number"] for i in buckets["cli"]} == {2}
    # #3 and #4 land in unaligned; unique count = 2 (not summed across axes)
    assert {i["number"] for i in buckets["unaligned"]} == {3, 4}
    assert unaligned == 2


def test_filter_issues_by_axes_empty_filter_preserves_input() -> None:
    issues = [{"number": 1, "labels": [{"name": "axis:dispatch"}]}]
    assert _axis.filter_issues_by_axes(issues, []) is issues


def test_filter_issues_by_axes_union_semantics() -> None:
    issues = [
        {"number": 1, "labels": [{"name": "axis:dispatch"}]},
        {"number": 2, "labels": [{"name": "axis:cli"}]},
        {"number": 3, "labels": [{"name": "axis:docs"}]},
    ]
    out = _axis.filter_issues_by_axes(issues, ["dispatch", "cli"])
    assert {i["number"] for i in out} == {1, 2}


def test_parse_filter_env_dedupes_and_lowercases() -> None:
    assert _axis.parse_filter_env("dispatch, CLI ,dispatch,, ") == ["dispatch", "cli"]
    assert _axis.parse_filter_env("") == []
    assert _axis.parse_filter_env(None) == []  # reads env; unset → []


# ---------------------------------------------------------------------------
# CLI surface: ``status --json`` + Rich human output
# ---------------------------------------------------------------------------


@pytest.fixture
def _status_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub ``cli.load()`` so ``_cmd_status`` runs against a temp dir."""

    class _Labels:
        ready = "loop:ready"

    cfg = SimpleNamespace(
        github_repo="owner/repo",
        labels=_Labels(),
        state_dir=tmp_path,
        state_file=tmp_path / "state.json",
        events_file=tmp_path / "events.jsonl",
        pid_file=tmp_path / "pid",
    )
    monkeypatch.setattr(cli, "load", lambda: cfg)
    monkeypatch.delenv(_axis.AXIS_FILTER_ENV, raising=False)


def _seed_subprocess(monkeypatch: pytest.MonkeyPatch, payload: list[dict[str, Any]]) -> None:
    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args[0] if args else [],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr("forge_loop.cli.subprocess.run", fake_run)


def test_status_json_groups_by_axis(
    _status_cfg: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_subprocess(monkeypatch, [
        {"number": 1, "title": "a", "labels": [{"name": "axis:dispatch"}]},
        {"number": 2, "title": "b", "labels": [{"name": "axis:cli"}, {"name": "axis:dispatch"}]},
        {"number": 3, "title": "c", "labels": [{"name": "loop:ready"}]},
    ])
    rc = cli._cmd_status(SimpleNamespace(json=True, axis=[]))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["unaligned_count"] == 1
    assert {i["number"] for i in out["axes"]["dispatch"]} == {1, 2}
    assert {i["number"] for i in out["axes"]["cli"]} == {2}
    assert {i["number"] for i in out["axes"]["unaligned"]} == {3}


def test_status_human_warns_on_unaligned(
    _status_cfg: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_subprocess(monkeypatch, [
        {"number": 7, "title": "alone", "labels": [{"name": "loop:ready"}]},
    ])
    cli._cmd_status(SimpleNamespace(json=False, axis=[]))
    out = capsys.readouterr().out
    assert "warning" in out
    assert "no axis" in out


def test_status_human_no_warning_when_aligned(
    _status_cfg: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_subprocess(monkeypatch, [
        {"number": 1, "title": "x", "labels": [{"name": "axis:dispatch"}]},
    ])
    cli._cmd_status(SimpleNamespace(json=False, axis=[]))
    out = capsys.readouterr().out
    assert "no axis" not in out


def test_status_json_axis_filter_narrows_view(
    _status_cfg: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_subprocess(monkeypatch, [
        {"number": 1, "title": "a", "labels": [{"name": "axis:dispatch"}]},
        {"number": 2, "title": "b", "labels": [{"name": "axis:cli"}]},
    ])
    rc = cli._cmd_status(SimpleNamespace(json=True, axis=["dispatch"]))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out["axes"].keys()) == {"dispatch"}
    assert out["axis_filter"] == ["dispatch"]


# ---------------------------------------------------------------------------
# ``run --axis`` sets the env var so the dispatcher can read it.
# ---------------------------------------------------------------------------


def test_cmd_run_axis_flag_sets_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run_loop(cfg: Any) -> int:
        captured["env"] = os.environ.get(_axis.AXIS_FILTER_ENV)
        return 0

    monkeypatch.setattr(cli, "run_loop", fake_run_loop)
    monkeypatch.setattr(cli, "load", lambda: SimpleNamespace())
    monkeypatch.delenv(_axis.AXIS_FILTER_ENV, raising=False)

    cli._cmd_run(SimpleNamespace(orchestrator="sync", queue=None, axis=["Dispatch", "cli"]))
    assert captured["env"] == "dispatch,cli"


def test_cmd_run_no_axis_clears_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_axis.AXIS_FILTER_ENV, "stale")
    monkeypatch.setattr(cli, "run_loop", lambda cfg: 0)
    monkeypatch.setattr(cli, "load", lambda: SimpleNamespace())
    cli._cmd_run(SimpleNamespace(orchestrator="sync", queue=None, axis=[]))
    assert os.environ.get(_axis.AXIS_FILTER_ENV) is None


# ---------------------------------------------------------------------------
# Typer wiring: --axis is repeatable.
# ---------------------------------------------------------------------------


def test_cli_run_accepts_repeated_axis_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {}

    def _stub(args: SimpleNamespace) -> int:
        captured["axis"] = args.axis
        return 0

    monkeypatch.setattr(cli, "_cmd_run", _stub)
    result = runner.invoke(
        cli.app, ["run", "--axis", "dispatch", "--axis", "cli"]
    )
    assert result.exit_code == 0
    assert captured["axis"] == ["dispatch", "cli"]


def test_cli_status_accepts_axis_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = CliRunner()
    captured: dict[str, Any] = {}

    def _stub(args: SimpleNamespace) -> int:
        captured["axis"] = args.axis
        captured["json"] = args.json
        return 0

    monkeypatch.setattr(cli, "_cmd_status", _stub)
    result = runner.invoke(cli.app, ["status", "--json", "--axis", "dispatch"])
    assert result.exit_code == 0
    assert captured["axis"] == ["dispatch"]
    assert captured["json"] is True
