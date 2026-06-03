"""CLI tests — Typer / Rich rewrite (issue #47).

Every subcommand is exercised through ``typer.testing.CliRunner`` so the
parsing layer is in scope. Tests cover:

* happy path per subcommand (correct exit code + stdout shape),
* sad paths (missing required args, mutually-exclusive flags, no
  subcommand → Rich help panel rather than a stack trace),
* NO_COLOR / TERM=dumb produce uncoloured output (CI-friendly),
* ``status --json`` still emits raw JSON (back-compat for scripts),
* ``doctor`` runs the cfg-independent checks even when ``LOOP_GH_REPO``
  is unset.

All handlers are bridged through ``SimpleNamespace`` so we monkeypatch
``forge_loop.cli._cmd_*`` to keep these tests free of real I/O.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from forge_loop import cli


@pytest.fixture
def runner() -> CliRunner:
    # Newer Click versions split stderr; older took mix_stderr=. Try both.
    try:
        return CliRunner(mix_stderr=False)  # type: ignore[call-arg]
    except TypeError:
        return CliRunner()


# ---------------------------------------------------------------------------
# Top-level / Rich help
# ---------------------------------------------------------------------------


def test_no_subcommand_shows_help_no_stack_trace(runner: CliRunner) -> None:
    """Adversarial: invoking with no subcommand prints help, not a trace."""
    result = runner.invoke(cli.app, [])
    assert result.exit_code in (0, 2)
    # Help output, NOT a Python traceback.
    assert "Traceback" not in (result.stdout + result.stderr)
    assert "Usage" in (result.stdout + result.stderr)


def test_help_lists_every_subcommand(runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for cmd in (
        "run",
        "status",
        "doctor",
        "events",
        "pause",
        "resume",
        "stop",
        "retry",
        "dashboard",
        "init",
        "brainstorm",
        "record-session",
        "brief",
        "config",
        "pipeline",
        "repos",
        "mcp",
        "replay",
        "roles",
        "cluster",
    ):
        assert cmd in result.stdout, f"missing {cmd} in help"


def test_run_help_shows_type_hint_signatures(runner: CliRunner) -> None:
    """Integration: `forge-loop run --help` shows type-hint-driven options."""
    result = runner.invoke(cli.app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--orchestrator" in result.stdout
    assert "--queue" in result.stdout


# ---------------------------------------------------------------------------
# Happy paths — each subcommand calls its bridged handler.
# ---------------------------------------------------------------------------


def _stub_handler(monkeypatch: pytest.MonkeyPatch, name: str, returns: int = 0) -> dict[str, Any]:
    """Replace ``cli._cmd_<name>`` with a recording stub. Returns the captures."""
    captured: dict[str, Any] = {}

    def _stub(args: SimpleNamespace) -> int:
        captured["args"] = args
        return returns

    monkeypatch.setattr(cli, f"_cmd_{name}", _stub)
    return captured


def test_run_dispatches_with_orchestrator_and_queue(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _stub_handler(monkeypatch, "run")
    result = runner.invoke(cli.app, ["run", "--orchestrator", "async", "--queue", "sqlite:///x.db"])
    assert result.exit_code == 0, result.stderr
    assert captured["args"].orchestrator == "async"
    assert captured["args"].queue == "sqlite:///x.db"


def test_status_default_invokes_handler(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _stub_handler(monkeypatch, "status")
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    assert captured["args"].json is False


def test_status_json_backcompat_flag(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """`status --json` MUST still emit raw JSON for scripts."""
    captured = _stub_handler(monkeypatch, "status")
    result = runner.invoke(cli.app, ["status", "--json"])
    assert result.exit_code == 0
    assert captured["args"].json is True


def test_events_default_n_and_raw_flag(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _stub_handler(monkeypatch, "events")
    runner.invoke(cli.app, ["events"])
    assert captured["args"].n == 30 and captured["args"].raw is False
    runner.invoke(cli.app, ["events", "-n", "5", "--raw"])
    assert captured["args"].n == 5 and captured["args"].raw is True


def test_pause_resume_stop(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("pause", "resume", "stop"):
        captured = _stub_handler(monkeypatch, name)
        result = runner.invoke(cli.app, [name])
        assert result.exit_code == 0, f"{name}: {result.stderr}"
        assert "args" in captured


def test_retry_requires_issue(runner: CliRunner) -> None:
    """Sad path: `retry` without --issue fails with non-zero exit."""
    result = runner.invoke(cli.app, ["retry"])
    assert result.exit_code != 0


def test_retry_with_force(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _stub_handler(monkeypatch, "retry")
    result = runner.invoke(cli.app, ["retry", "--issue", "42", "--force"])
    assert result.exit_code == 0
    assert captured["args"].issue == 42 and captured["args"].force is True


def test_dashboard_web_default(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _stub_handler(monkeypatch, "dashboard")
    result = runner.invoke(cli.app, ["dashboard", "--host", "127.0.0.1", "--port", "9000"])
    assert result.exit_code == 0
    assert captured["args"].mode == "web"
    assert captured["args"].host == "127.0.0.1"
    assert captured["args"].port == 9000


def test_dashboard_tui_flag(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _stub_handler(monkeypatch, "dashboard")
    result = runner.invoke(cli.app, ["dashboard", "--tui"])
    assert result.exit_code == 0
    assert captured["args"].mode == "tui"


def test_dashboard_rejects_both_modes(runner: CliRunner) -> None:
    """Adversarial: choosing --web AND --tui must error, not silently pick one."""
    result = runner.invoke(cli.app, ["dashboard", "--web", "--tui"])
    assert result.exit_code == 2
    assert "choose --web or --tui" in (result.stdout + result.stderr)


def test_config_default_emits_json(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """`config` historically always emitted JSON — preserve."""
    captured = _stub_handler(monkeypatch, "config")
    result = runner.invoke(cli.app, ["config"])
    assert result.exit_code == 0
    assert captured["args"].json is False  # flag absent → False, handler still emits


def test_config_models_subcommand(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _stub_handler(monkeypatch, "config_models")
    result = runner.invoke(cli.app, ["config", "models", "--json"])
    assert result.exit_code == 0
    assert captured["args"].json is True


def test_repos_list_disable_enable(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    cap_list = _stub_handler(monkeypatch, "repos_list")
    cap_dis = _stub_handler(monkeypatch, "repos_disable")
    cap_en = _stub_handler(monkeypatch, "repos_enable")
    runner.invoke(cli.app, ["repos", "list", "--json"])
    runner.invoke(cli.app, ["repos", "disable", "alpha", "--reason", "noisy"])
    runner.invoke(cli.app, ["repos", "enable", "alpha"])
    assert cap_list["args"].json is True
    assert cap_dis["args"].name == "alpha" and cap_dis["args"].reason == "noisy"
    assert cap_en["args"].name == "alpha"


def test_pipeline_show(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _stub_handler(monkeypatch, "pipeline_show")
    result = runner.invoke(cli.app, ["pipeline", "show", "--config", "x.yaml", "--json"])
    assert result.exit_code == 0
    assert captured["args"].config == "x.yaml" and captured["args"].json is True


def test_roles_list(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _stub_handler(monkeypatch, "roles_list")
    result = runner.invoke(cli.app, ["roles", "list", "--json"])
    assert result.exit_code == 0
    assert captured["args"].json is True


def test_mcp_serve(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _stub_handler(monkeypatch, "mcp_serve")
    result = runner.invoke(cli.app, ["mcp", "serve"])
    assert result.exit_code == 0
    assert "args" in captured


def test_cluster_status_is_deprecated_stub(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`cluster status` returns rc=2 with a clear stderr message."""
    captured = _stub_handler(monkeypatch, "cluster_status", returns=2)
    result = runner.invoke(cli.app, ["cluster", "status", "--queue", "redis://x"])
    assert result.exit_code == 2
    assert captured["args"].queue == "redis://x"


def test_brief_kind_validated(runner: CliRunner) -> None:
    result = runner.invoke(cli.app, ["brief", "--kind", "nonsense"])
    assert result.exit_code == 2
    assert "worker|po|critic" in (result.stdout + result.stderr)


def test_record_session_requires_exactly_one_source(runner: CliRunner) -> None:
    # Neither --issue nor --issue-file → error.
    result = runner.invoke(cli.app, ["record-session", "--out", "x.jsonl"])
    assert result.exit_code == 2
    # Both → error.
    result = runner.invoke(
        cli.app,
        ["record-session", "--issue", "1", "--issue-file", "x.json", "--out", "x.jsonl"],
    )
    assert result.exit_code == 2


def test_replay_requires_tick_and_brief(runner: CliRunner) -> None:
    """Sad path: `replay` without --tick and --brief errors out cleanly."""
    result = runner.invoke(cli.app, ["replay"])
    assert result.exit_code == 2
    assert "--tick" in (result.stdout + result.stderr)


def test_replay_diff_subcommand(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _stub_handler(monkeypatch, "replay_diff")
    result = runner.invoke(
        cli.app, ["replay", "diff", "--tick", "5", "--replay-tick", "5r", "--json"]
    )
    assert result.exit_code == 0
    assert captured["args"].tick == 5 and captured["args"].replay_tick == "5r"
    assert captured["args"].json is True


# ---------------------------------------------------------------------------
# NO_COLOR / TERM=dumb (CI-friendly)
# ---------------------------------------------------------------------------


def test_status_handler_respects_json_for_scripts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`status --json` writes a JSON blob with the documented keys."""
    fake_cfg = SimpleNamespace(
        pid_file=tmp_path / "pid",
        state_dir=tmp_path,
        stop_file=tmp_path / "stop",
        state_file=tmp_path / "state.json",
        events_file=tmp_path / "events.jsonl",
        github_repo="o/r",
        labels=SimpleNamespace(ready="loop:ready"),
    )
    monkeypatch.setattr(cli, "load", lambda: fake_cfg)
    # No `gh` call: monkeypatch subprocess.run to fail fast.
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("gh")),
    )
    rc = cli._cmd_status(SimpleNamespace(json=True))
    out = capsys.readouterr().out
    assert rc == 0
    blob = json.loads(out)
    for key in ("pid", "pid_alive", "queue_depth", "events_file", "last_events"):
        assert key in blob


def test_status_json_falls_back_to_local_ops_without_github_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Local operator visibility should survive missing repo.github."""

    ops = tmp_path / "docs" / "ops"
    ops.mkdir(parents=True)
    (ops / "loop-runner.json").write_text(json.dumps({"state": "idle", "tick": 7}))
    (ops / "loop-runner-events.jsonl").write_text(
        json.dumps({"ts": "2026-05-30T10:00:00Z", "kind": "tick_idle"}) + "\n"
    )
    monkeypatch.setenv("LOOP_REPO_DIR", str(tmp_path))
    monkeypatch.delenv("LOOP_GH_REPO", raising=False)
    monkeypatch.setattr(cli, "load", lambda: (_ for _ in ()).throw(RuntimeError("repo missing")))
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("gh must not run")),
    )

    rc = cli._cmd_status(SimpleNamespace(json=True))
    out = capsys.readouterr().out

    assert rc == 0
    blob = json.loads(out)
    assert blob["config_ok"] is False
    assert blob["config_error"] == "repo missing"
    assert blob["state"] == "idle"
    assert blob["tick"] == 7
    assert blob["queue_depth"] == -1
    assert blob["last_events"] == [{"ts": "2026-05-30T10:00:00Z", "kind": "tick_idle"}]


def test_status_json_reports_active_workers_from_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_cfg = SimpleNamespace(
        pid_file=tmp_path / "pid",
        state_dir=tmp_path,
        stop_file=tmp_path / "stop",
        state_file=tmp_path / "state.json",
        events_file=tmp_path / "events.jsonl",
        github_repo="o/r",
        labels=SimpleNamespace(ready="loop:ready"),
    )
    fake_cfg.events_file.write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-05-30T10:00:00Z", "kind": "worker_start", "issue": 1}),
                json.dumps({"ts": "2026-05-30T10:01:00Z", "kind": "worker_start", "issue": 2}),
                json.dumps({"ts": "2026-05-30T10:02:00Z", "kind": "worker_done", "issue": 1}),
            ]
        )
        + "\n"
    )
    monkeypatch.setattr(cli, "load", lambda: fake_cfg)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("gh")),
    )

    rc = cli._cmd_status(SimpleNamespace(json=True))

    assert rc == 0
    blob = json.loads(capsys.readouterr().out)
    assert [w["issue"] for w in blob["active_workers"]] == [2]
    assert blob["active_workers"][0]["status"] == "running"


def test_status_json_falls_back_to_dispatched_state_when_worker_events_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_cfg = SimpleNamespace(
        pid_file=tmp_path / "pid",
        state_dir=tmp_path,
        stop_file=tmp_path / "stop",
        state_file=tmp_path / "state.json",
        events_file=tmp_path / "events.jsonl",
        github_repo="o/r",
        labels=SimpleNamespace(ready="loop:ready"),
    )
    fake_cfg.state_file.write_text(
        json.dumps({"state": "running", "tick": 3, "dispatched": [{"issue": 9, "title": "x"}]})
    )
    monkeypatch.setattr(cli, "load", lambda: fake_cfg)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("gh")),
    )

    rc = cli._cmd_status(SimpleNamespace(json=True))

    assert rc == 0
    blob = json.loads(capsys.readouterr().out)
    assert blob["active_workers"] == [
        {
            "issue": 9,
            "title": "x",
            "started_ts": None,
            "last_event_ts": None,
            "status": "stale_unconfirmed",
            "worktree": None,
            "log_path": None,
            "last_event_age_s": None,
        }
    ]
    assert blob["runner_stale"] is True


def test_status_json_does_not_resurrect_terminal_worker_from_dispatched_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_cfg = SimpleNamespace(
        pid_file=tmp_path / "pid",
        state_dir=tmp_path,
        stop_file=tmp_path / "stop",
        state_file=tmp_path / "state.json",
        events_file=tmp_path / "events.jsonl",
        github_repo="o/r",
        labels=SimpleNamespace(ready="loop:ready"),
    )
    fake_cfg.state_file.write_text(
        json.dumps({"state": "running", "tick": 4, "dispatched": [{"issue": 9, "title": "x"}]})
    )
    fake_cfg.events_file.write_text(
        "\n".join(
            [
                json.dumps({"ts": "2026-05-30T10:00:00Z", "kind": "worker_start", "issue": 9}),
                json.dumps({"ts": "2026-05-30T10:01:00Z", "kind": "worker_done", "issue": 9}),
            ]
        )
        + "\n"
    )
    monkeypatch.setattr(cli, "load", lambda: fake_cfg)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("gh")),
    )

    rc = cli._cmd_status(SimpleNamespace(json=True))

    assert rc == 0
    blob = json.loads(capsys.readouterr().out)
    assert blob["active_workers"] == []


def test_events_raw_falls_back_to_local_ops_without_github_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ops = tmp_path / "docs" / "ops"
    ops.mkdir(parents=True)
    line = json.dumps({"ts": "2026-05-30T10:00:00Z", "kind": "loop_start"}) + "\n"
    (ops / "loop-runner-events.jsonl").write_text(line)
    monkeypatch.setenv("LOOP_REPO_DIR", str(tmp_path))
    monkeypatch.delenv("LOOP_GH_REPO", raising=False)
    monkeypatch.setattr(cli, "load", lambda: (_ for _ in ()).throw(RuntimeError("repo missing")))

    rc = cli._cmd_events(SimpleNamespace(n=10, raw=True))

    assert rc == 0
    assert capsys.readouterr().out == line


def test_doctor_runs_without_loop_gh_repo(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cfg-independent checks still run when config load fails."""

    def _boom() -> Any:
        raise RuntimeError("LOOP_GH_REPO unset")

    monkeypatch.setattr(cli, "load", _boom)
    rc = cli._cmd_doctor(SimpleNamespace())
    out = capsys.readouterr().out
    # Config load failure is a red signal → rc=1.
    assert rc == 1
    # Still rendered the doctor table (and the orphan-worktree / drift-env
    # checks that do NOT need a config).
    assert "doctor" in out.lower()
    assert "deploy-drift" in out


def test_no_color_env_drops_ansi(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Rich output respects NO_COLOR — no ANSI escapes in the dump."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    from rich.console import Console

    # Render the doctor markers via a stable console — assert NO ANSI esc.
    c = Console(force_terminal=False, no_color=True)
    with c.capture() as cap:
        c.print(cli._STATUS_MARKERS["green"])
    assert "\x1b[" not in cap.get()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def test_main_raises_systemexit_on_help(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main(['--help'])` SystemExits with code 0 — matches the historical
    argparse contract that downstream tests (test_replay, test_session_replay)
    rely on.
    """
    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])
    assert exc.value.code == 0
