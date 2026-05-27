"""Typer-CliRunner driven CLI tests (issue #55).

The Typer migration kept ``forge_loop.cli.main(argv)`` as the entry
point (consumed by other tests + the ``forge-loop`` console script),
but the canonical way to exercise the CLI is via Typer's
``CliRunner``. These tests cover:

* help rendering for every top-level command (smoke for argparse-equiv
  parity);
* the exact-flag parses the issue's test matrix calls out
  (``run --once --tick-budget 1`` shape, ``repos disable <slug>``,
  ``events --follow --kind worker.start`` shape, ``config models``);
* adversarial / sad-path: unknown command, missing required arg,
  conflicting mutex group, bad choice;
* invariant: the cli module no longer imports argparse.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import forge_loop.cli as cli_mod
from forge_loop.cli import app, main

runner = CliRunner()


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def test_cli_module_does_not_import_argparse() -> None:
    """Acceptance criterion: no `import argparse` remains in cli.py."""
    src = inspect.getsource(cli_mod)
    # Strip docstring & comments before checking — historical references in
    # comments are fine; what matters is the module truly doesn't import or
    # use argparse anymore.
    import re

    code = re.sub(r"#[^\n]*", "", src)
    code = re.sub(r'"""[\s\S]*?"""', "", code)
    assert "import argparse" not in code
    assert "argparse.Namespace" not in code
    assert "argparse.ArgumentParser" not in code


def test_cli_module_uses_typer() -> None:
    """The CLI is a Typer app, not a hand-rolled argparse parser."""
    import typer

    assert isinstance(app, typer.Typer)


# ---------------------------------------------------------------------------
# --help renders for every documented command (smoke)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["run", "--help"],
        ["status", "--help"],
        ["doctor", "--help"],
        ["cluster", "--help"],
        ["cluster", "status", "--help"],
        ["events", "--help"],
        ["pause", "--help"],
        ["resume", "--help"],
        ["stop", "--help"],
        ["config", "--help"],
        ["config", "models", "--help"],
        ["pipeline", "--help"],
        ["pipeline", "show", "--help"],
        ["repos", "--help"],
        ["repos", "list", "--help"],
        ["repos", "disable", "--help"],
        ["repos", "enable", "--help"],
        ["retry", "--help"],
        ["dashboard", "--help"],
        ["mcp", "--help"],
        ["mcp", "serve", "--help"],
        ["init", "--help"],
        ["record-session", "--help"],
        ["brief", "--help"],
        ["replay", "--help"],
        ["replay", "diff", "--help"],
        ["roles", "--help"],
        ["roles", "list", "--help"],
    ],
)
def test_help_renders_for_every_command(argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, (argv, result.output)
    # Typer/Rich uses "Usage:" — argparse used "usage:". Be lenient.
    assert "sage:" in result.output.lower() or "Usage" in result.output


# ---------------------------------------------------------------------------
# Adversarial / sad-path
# ---------------------------------------------------------------------------


def test_unknown_command_exits_nonzero_with_typer_usage_error() -> None:
    result = runner.invoke(app, ["bogus"])
    assert result.exit_code != 0
    # Typer-style "No such command" — emphatically NOT an argparse traceback.
    out = (result.output or "") + (result.stderr or "")
    assert "No such command" in out or "Usage" in out


def test_repos_disable_without_slug_errors() -> None:
    result = runner.invoke(app, ["repos", "disable"])
    assert result.exit_code != 0
    out = (result.output or "") + (result.stderr or "")
    assert "Missing" in out or "missing" in out or "Usage" in out


def test_brief_rejects_bad_kind() -> None:
    result = runner.invoke(app, ["brief", "--kind", "nonsense"])
    assert result.exit_code != 0


def test_record_session_rejects_both_issue_and_issue_file(tmp_path: Path) -> None:
    issue_json = tmp_path / "issue.json"
    issue_json.write_text(json.dumps({"number": 1, "title": "t", "body": "b"}))
    result = runner.invoke(
        app,
        [
            "record-session",
            "--issue",
            "1",
            "--issue-file",
            str(issue_json),
            "--out",
            str(tmp_path / "fix.jsonl"),
        ],
    )
    assert result.exit_code != 0


def test_record_session_requires_one_of_issue_or_issue_file(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["record-session", "--out", str(tmp_path / "fix.jsonl")],
    )
    assert result.exit_code != 0


def test_replay_without_tick_or_brief_errors() -> None:
    result = runner.invoke(app, ["replay"])
    assert result.exit_code != 0
    out = (result.output or "") + (result.stderr or "")
    assert "tick" in out.lower() or "brief" in out.lower() or "Usage" in out


# ---------------------------------------------------------------------------
# Round-trip flag parses → handlers (matches the issue's test matrix)
# ---------------------------------------------------------------------------


def test_repos_disable_then_enable_via_runner(tmp_path: Path) -> None:
    """`repos disable <slug>` and `repos enable <slug>` accept positional repo args."""
    repos_dir = tmp_path / ".forge" / "repos"
    repos_dir.mkdir(parents=True)
    # Set up a fake repo checkout the multirepo loader will accept.
    checkout = tmp_path / "co" / "x"
    checkout.mkdir(parents=True)
    (checkout / ".git").mkdir()
    (repos_dir / "x.yaml").write_text(
        f"name: x\ngithub: o/x\ncheckout: {checkout}\nbudget_usd_per_day: 1\n"
    )

    result = runner.invoke(
        app, ["repos", "disable", "x", "--repos-dir", str(repos_dir)]
    )
    assert result.exit_code == 0, result.output
    assert (checkout / ".forge" / "disabled").exists()

    result = runner.invoke(
        app, ["repos", "enable", "x", "--repos-dir", str(repos_dir)]
    )
    assert result.exit_code == 0, result.output
    assert not (checkout / ".forge" / "disabled").exists()


def test_repos_disable_unknown_slug_exits_2(tmp_path: Path) -> None:
    repos_dir = tmp_path / ".forge" / "repos"
    repos_dir.mkdir(parents=True)
    checkout = tmp_path / "co" / "real"
    checkout.mkdir(parents=True)
    (checkout / ".git").mkdir()
    (repos_dir / "real.yaml").write_text(
        f"name: real\ngithub: o/real\ncheckout: {checkout}\nbudget_usd_per_day: 1\n"
    )
    result = runner.invoke(
        app, ["repos", "disable", "nope", "--repos-dir", str(repos_dir)]
    )
    # The handler returns 2 for "no such repo".
    assert result.exit_code == 2


def test_repos_list_json_dispatches_to_repos_list_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`forge-loop repos list --json` parses + dispatches to the right handler."""
    repos_dir = tmp_path / ".forge" / "repos"
    repos_dir.mkdir(parents=True)
    result = runner.invoke(
        app, ["repos", "list", "--repos-dir", str(repos_dir), "--json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload == {"repos_dir": str(repos_dir), "repos": []}


def test_events_flags_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """`events -n 5 --raw` resolves both flags through to the handler."""
    captured: dict[str, object] = {}

    def fake_cmd_events(args: object) -> int:
        captured["n"] = args.n  # type: ignore[attr-defined]
        captured["raw"] = args.raw  # type: ignore[attr-defined]
        return 0

    monkeypatch.setattr(cli_mod, "_cmd_events", fake_cmd_events)
    result = runner.invoke(app, ["events", "-n", "5", "--raw"])
    assert result.exit_code == 0, result.output
    assert captured == {"n": 5, "raw": True}


def test_config_models_nested_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """`config models --json` dispatches to the models handler, not the bare config one."""
    bare_calls: list[bool] = []
    models_calls: list[bool] = []

    def fake_bare(args: object) -> int:
        bare_calls.append(True)
        return 0

    def fake_models(args: object) -> int:
        models_calls.append(args.json)  # type: ignore[attr-defined]
        return 0

    monkeypatch.setattr(cli_mod, "_cmd_config", fake_bare)
    monkeypatch.setattr(cli_mod, "_cmd_config_models", fake_models)
    result = runner.invoke(app, ["config", "models", "--json"])
    assert result.exit_code == 0, result.output
    assert models_calls == [True]
    assert bare_calls == []  # bare `config` handler must NOT fire


def test_run_orchestrator_and_queue_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run --orchestrator sync --queue sqlite:///x.db` reaches _cmd_run unchanged."""
    captured: dict[str, object] = {}

    def fake_run(args: object) -> int:
        captured["orchestrator"] = args.orchestrator  # type: ignore[attr-defined]
        captured["queue"] = args.queue  # type: ignore[attr-defined]
        return 0

    monkeypatch.setattr(cli_mod, "_cmd_run", fake_run)
    result = runner.invoke(
        app,
        ["run", "--orchestrator", "sync", "--queue", "sqlite:///x.db"],
    )
    assert result.exit_code == 0, result.output
    assert captured == {"orchestrator": "sync", "queue": "sqlite:///x.db"}


# ---------------------------------------------------------------------------
# Back-compat: the legacy `main(argv) -> int` API still works (other tests
# call this directly).
# ---------------------------------------------------------------------------


def test_legacy_main_returns_int_for_handler_success(tmp_path: Path) -> None:
    """A real handler (no monkeypatch) returning 0 propagates through main()."""
    repos_dir = tmp_path / ".forge" / "repos"
    repos_dir.mkdir(parents=True)
    rc = main(["repos", "list", "--repos-dir", str(repos_dir), "--json"])
    assert rc == 0


def test_legacy_main_raises_systemexit_on_help() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0


def test_legacy_main_raises_systemexit_on_missing_required() -> None:
    with pytest.raises(SystemExit):
        main(["repos", "disable"])  # missing positional `name`


def test_legacy_main_propagates_nonzero_exit_code(tmp_path: Path) -> None:
    """A handler returning a non-zero rc (here: 2 for unknown repo slug)
    surfaces as the main() return value — not as a SystemExit.
    """
    repos_dir = tmp_path / ".forge" / "repos"
    repos_dir.mkdir(parents=True)
    rc = main(["repos", "disable", "ghost", "--repos-dir", str(repos_dir)])
    assert rc == 2
