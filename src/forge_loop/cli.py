"""CLI entry point — `forge-loop <subcommand>` or `python -m forge_loop <subcommand>`.

Typer-driven (issue #47). Each subcommand is a Typer ``@app.command``
that constructs a ``SimpleNamespace`` and dispatches to the underlying
``_cmd_*`` handler. This keeps the historical Namespace shape every
handler expects (and that's still useful for tests/replay/etc.) while
giving us:

* type-hint driven help and parsing,
* nested subcommands without manual dispatch,
* shell completion for free,
* Rich-formatted output (status/doctor/events) with NO_COLOR support,
* Rich-formatted help panel when invoked with no subcommand
  (instead of an argparse stack trace).

Backward compatibility: every shell invocation that worked under the
argparse era still works — same flag names, same exit codes, same
machine-parseable output where applicable (``status --json``,
``config --json``, ``events --raw``).

Subcommands:
  run             Run the loop in the foreground.
  status          Operator-facing health surface (or ``--json``).
  boot            Reload the maestro reset-recovery context from durable state.
  recover         Reconcile dead-worker sagas (reap worktrees + close them).
  doctor          One-shot health check.
  events          Tail the events log (Rich by default, ``--raw`` for jq).
  pause/resume/stop  Touch the corresponding marker files.
  config          Resolved config (top-level or ``config models``).
  pipeline show   Resolved DAG as ASCII art or JSON.
  repos {list,disable,enable}  Multirepo management.
  retry           Re-dispatch a worker for an issue (``--force`` to bypass).
  dashboard       Operator dashboard — ``--web`` (FastAPI/HTMX) or ``--tui``
                  (Textual). Default keeps the historical web behaviour.
  mcp serve       MCP server on stdio.
  init            Scaffold forge-loop config in a project.
  record-session  Record a real SDK session to a JSONL fixture.
  brief           Render a brief template to stdout.
  replay          Time-travel: re-run a past tick with a modified brief.
  replay diff     Side-by-side report: original tick vs replay tick.
  roles list      List loaded roles + triggers + next firing.
  cluster status  Deprecated stub (multi-host cluster mode removed in #39).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import typer

from forge_loop.cli_commands import CliCommands
from forge_loop.config import load
from forge_loop.log import get_logger
from forge_loop.runner import run as run_loop
from forge_loop.settings import Settings

_log = get_logger("forge_loop.cli")

_STATUS_MARKERS = {
    "green": "[green]✓[/green]",
    "yellow": "[yellow]~[/yellow]",
    "red": "[red]✗[/red]",
}

# ---------------------------------------------------------------------------
# Typer app — Rich-formatted help, no-subcommand prints help (no traceback).
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="forge-loop",
    help="forge-loop sprint-loop runner.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    add_completion=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _local_operator_cfg() -> SimpleNamespace:
    """Best-effort local cfg for commands that only inspect loop files.

    ``config.load()`` intentionally refuses to build a runner config without
    ``repo.github``. Operator visibility should be weaker than runner startup:
    status/events/pause/stop still need to work while repo config is broken.
    """
    try:
        settings = Settings.load()
        repo = settings.repo_path
        ready_label = settings.labels.ready
    except Exception:  # noqa: BLE001 - operator commands must fail soft
        repo = Path.cwd()
        ready_label = "loop:ready"
    state_dir = repo / "docs" / "ops"
    return SimpleNamespace(
        repo=repo,
        github_repo=None,
        labels=SimpleNamespace(ready=ready_label),
        state_dir=state_dir,
        state_file=state_dir / "loop-runner.json",
        events_file=state_dir / "loop-runner-events.jsonl",
        summaries_file=state_dir / "loop-runner-summaries.jsonl",
        pause_file=state_dir / "loop-runner.pause",
        stop_file=state_dir / "loop-runner.stop",
        pid_file=state_dir / "loop-runner.pid",
        logs_dir=state_dir / "loop-runner-logs",
    )


def _operator_cfg() -> tuple[Any, str | None]:
    try:
        return load(), None
    except Exception as exc:  # noqa: BLE001 - surfaced in status payload
        return _local_operator_cfg(), str(exc)


config_app = typer.Typer(help="Resolved config (models, ...)", no_args_is_help=False)
pipeline_app = typer.Typer(help="Inspect the role-chain pipeline.", no_args_is_help=True)
repos_app = typer.Typer(help="Multirepo management.", no_args_is_help=True)
mcp_app = typer.Typer(help="MCP server (expose tools to MCP clients).", no_args_is_help=True)
replay_app = typer.Typer(
    help="Time-travel: re-run a past tick with a modified brief.",
    no_args_is_help=False,
    invoke_without_command=True,
)
roles_app = typer.Typer(help="Pluggable roles.", no_args_is_help=True)
cluster_app = typer.Typer(help="Cluster-mode commands.", no_args_is_help=True)

app.add_typer(config_app, name="config", invoke_without_command=True)
app.add_typer(pipeline_app, name="pipeline")
app.add_typer(repos_app, name="repos")
app.add_typer(mcp_app, name="mcp")
app.add_typer(replay_app, name="replay")
app.add_typer(roles_app, name="roles")
app.add_typer(cluster_app, name="cluster")


# ---------------------------------------------------------------------------
# Handlers — keep the legacy Namespace shape so the actual work is
# unchanged. Tests can still import and call these with a SimpleNamespace.
# ---------------------------------------------------------------------------


def _brainstormer_factory(
    repo_path: Path,
    owner: str,
    repo: str,
    *,
    provider: str = "claude",
    model: str | None = None,
    timeout_s: int = 300,
) -> Any:
    """Construct the default Brainstormer. Tests monkeypatch this.

    Wires the durable memory store from ``.forge/memory.db`` when it exists so
    the generation path can render previously-rejected paths into the prompt and
    filter re-litigations (issue #203). When the db is absent the store is left
    ``None`` and the brainstormer degrades to its pre-memory behaviour.
    """
    from forge_loop.brainstormer import Brainstormer
    from forge_loop.memory import memory_db_path

    memory_store: Any = None
    if memory_db_path(repo_path).exists():
        try:
            memory_store = _memory_store_factory(repo_path)
        except Exception as exc:  # noqa: BLE001 — memory is optional; degrade gracefully
            # Surface before degrading, mirroring the logged degrade in
            # ``Brainstormer._load_rejected_paths`` and the stderr echoes in
            # ``cli_product_commands``. Swallowing this silently would let the
            # whole anti-relitigation feature no-op with zero signal.
            _log.warning("memory_store_unavailable", error=str(exc))
            memory_store = None

    return Brainstormer(
        repo_path=repo_path,
        owner=owner,
        repo=repo,
        provider=provider,
        model=model,
        timeout_s=timeout_s,
        memory_store=memory_store,
    )


def _gh_client_factory() -> Any:
    """Construct the default GhClient. Tests monkeypatch this."""
    from forge_loop.gh_client import GithubkitClient

    return GithubkitClient()


def _memory_store_factory(repo_path: Path) -> Any:
    """Construct the default memory store at ``.forge/memory.db``.

    Tests monkeypatch this to inject a ``FakeMemoryStore``.
    """
    from forge_loop.memory import open_memory_store

    return open_memory_store(repo_path)


def _commands() -> CliCommands:
    return CliCommands(
        load_fn=load,
        run_loop_fn=run_loop,
        operator_cfg_fn=_operator_cfg,
        brainstormer_factory=_brainstormer_factory,
        gh_client_factory=_gh_client_factory,
        memory_store_factory=_memory_store_factory,
        subprocess_module=subprocess,
    )


def _make_cmd(name: str) -> Callable[[SimpleNamespace], int]:
    def _cmd(args: SimpleNamespace) -> int:
        return int(getattr(_commands(), f"_cmd_{name}")(args))

    return _cmd


(
    _cmd_run,
    _cmd_cluster_status,
    _cmd_doctor,
    _cmd_status,
    _cmd_boot,
    _cmd_recover,
    _cmd_events,
    _cmd_pause,
    _cmd_resume,
    _cmd_stop,
    _cmd_dashboard,
    _cmd_mcp_serve,
    _cmd_init,
    _cmd_brainstorm,
    _cmd_audit,
    _cmd_record_session,
    _cmd_retry,
    _cmd_brief,
    _cmd_replay,
    _cmd_replay_diff,
    _cmd_repos_list,
    _cmd_repos_disable,
    _cmd_repos_enable,
    _cmd_pipeline_show,
    _cmd_config,
    _cmd_config_models,
    _cmd_roles_list,
) = (
    _make_cmd("run"),
    _make_cmd("cluster_status"),
    _make_cmd("doctor"),
    _make_cmd("status"),
    _make_cmd("boot"),
    _make_cmd("recover"),
    _make_cmd("events"),
    _make_cmd("pause"),
    _make_cmd("resume"),
    _make_cmd("stop"),
    _make_cmd("dashboard"),
    _make_cmd("mcp_serve"),
    _make_cmd("init"),
    _make_cmd("brainstorm"),
    _make_cmd("audit"),
    _make_cmd("record_session"),
    _make_cmd("retry"),
    _make_cmd("brief"),
    _make_cmd("replay"),
    _make_cmd("replay_diff"),
    _make_cmd("repos_list"),
    _make_cmd("repos_disable"),
    _make_cmd("repos_enable"),
    _make_cmd("pipeline_show"),
    _make_cmd("config"),
    _make_cmd("config_models"),
    _make_cmd("roles_list"),
)

# ---------------------------------------------------------------------------
# Typer commands — thin wrappers that build a SimpleNamespace and dispatch.
# ---------------------------------------------------------------------------


def _exit(rc: int) -> None:
    """Exit by raising typer.Exit so the CliRunner sees the same code path."""
    raise typer.Exit(code=int(rc))


_RUN_AXIS_OPTION = typer.Option(
    [],
    "--axis",
    help=(
        "Narrow dispatch to issues carrying ``axis:<name>`` labels. "
        "Repeatable; values are unioned. Omit to preserve pre-#126 "
        "behaviour (no filter)."
    ),
)

_STATUS_AXIS_OPTION = typer.Option(
    [],
    "--axis",
    help="Narrow the axis-grouped view to these slugs (repeatable).",
)


@app.command("run", help="Run the loop in the foreground.")
def cmd_run(
    orchestrator: str = typer.Option(
        "sync",
        "--orchestrator",
        help="Pipeline orchestrator: 'sync' (default, stable) or 'async'.",
    ),
    queue: str | None = typer.Option(
        None,
        "--queue",
        help="Queue backend URL. Default in-memory; sqlite:///path for durable.",
    ),
    axis: list[str] = _RUN_AXIS_OPTION,
) -> None:
    if orchestrator not in {"sync", "async"}:
        typer.echo(f"run: invalid --orchestrator {orchestrator!r}", err=True)
        raise typer.Exit(code=2)
    _exit(_cmd_run(SimpleNamespace(orchestrator=orchestrator, queue=queue, axis=axis)))


@app.command("status", help="Operator-facing health surface.")
def cmd_status(
    json_: bool = typer.Option(False, "--json", help="Emit raw JSON for scripts."),
    axis: list[str] = _STATUS_AXIS_OPTION,
) -> None:
    _exit(_cmd_status(SimpleNamespace(json=json_, axis=axis)))


@app.command(
    "boot",
    help="Reload the maestro reset-recovery context from durable .forge state.",
)
def cmd_boot(
    json_: bool = typer.Option(False, "--json", help="Emit raw JSON for scripts."),
) -> None:
    _exit(_cmd_boot(SimpleNamespace(json=json_)))


@app.command(
    "recover",
    help="Reconcile dead-worker sagas: reap orphaned worktrees and close them.",
)
def cmd_recover(
    json_: bool = typer.Option(False, "--json", help="Emit raw JSON for scripts."),
) -> None:
    _exit(_cmd_recover(SimpleNamespace(json=json_)))


@app.command("doctor", help="One-shot health check (config-independent checks still run).")
def cmd_doctor() -> None:
    _exit(_cmd_doctor(SimpleNamespace()))


@app.command("events", help="Tail the events log.")
def cmd_events(
    n: int = typer.Option(30, "-n", help="Lines to show."),
    raw: bool = typer.Option(False, "--raw", help="Emit raw JSONL (skip Rich)."),
) -> None:
    _exit(_cmd_events(SimpleNamespace(n=n, raw=raw)))


@app.command("pause")
def cmd_pause() -> None:
    """Touch the pause file."""
    _exit(_cmd_pause(SimpleNamespace()))


@app.command("resume")
def cmd_resume() -> None:
    """Remove the pause file."""
    _exit(_cmd_resume(SimpleNamespace()))


@app.command("stop")
def cmd_stop() -> None:
    """Touch the stop file (graceful)."""
    _exit(_cmd_stop(SimpleNamespace()))


@app.command("retry", help="Re-dispatch a worker for an issue.")
def cmd_retry(
    issue: int = typer.Option(..., "--issue", help="GitHub issue number."),
    force: bool = typer.Option(False, "--force", help="Bypass guards."),
) -> None:
    _exit(_cmd_retry(SimpleNamespace(issue=issue, force=force)))


@app.command("dashboard", help="Operator dashboard: --web (FastAPI) or --tui (Textual).")
def cmd_dashboard(
    web: bool = typer.Option(False, "--web", help="Launch the FastAPI/HTMX dashboard (default)."),
    tui: bool = typer.Option(False, "--tui", help="Launch the Textual TUI."),
    host: str | None = typer.Option(None, "--host", help="Bind host (web only)."),
    port: int | None = typer.Option(None, "--port", help="Bind port (web only)."),
    roles_dir: str | None = typer.Option(None, "--roles-dir", help="Roles dir (web only)."),
) -> None:
    if web and tui:
        typer.echo("dashboard: choose --web or --tui, not both", err=True)
        raise typer.Exit(code=2)
    mode = "tui" if tui else "web"
    _exit(_cmd_dashboard(SimpleNamespace(mode=mode, host=host, port=port, roles_dir=roles_dir)))


@app.command("init", help="Scaffold forge-loop config in a project.")
def cmd_init(
    target: str | None = typer.Option(None, "--target"),
    repo: str | None = typer.Option(None, "--repo"),
    force: bool = typer.Option(False, "--force"),
    create_labels: bool = typer.Option(False, "--create-labels"),
) -> None:
    _exit(
        _cmd_init(
            SimpleNamespace(target=target, repo=repo, force=force, create_labels=create_labels)
        )
    )


@app.command(
    "brainstorm",
    help="Propose axis-aligned epics/tickets from product vision (dry-run by default; --apply files them on GitHub).",
)
def cmd_brainstorm(
    apply: bool = typer.Option(
        False, "--apply", help="Actually file the proposed epics + tickets on GitHub."
    ),
    output: str | None = typer.Option(
        None, "--output", help="Write the dry-run BrainstormReport YAML to this path."
    ),
    report: str | None = typer.Option(
        None, "--report", help="Apply this reviewed BrainstormReport YAML without re-sampling."
    ),
) -> None:
    _exit(_cmd_brainstorm(SimpleNamespace(apply=apply, output=output, report=report)))


@app.command(
    "audit",
    help="Codebase-state audit (issue #156). Dry-run by default; --apply files tickets.",
)
def cmd_audit(
    apply: bool = typer.Option(
        False, "--apply", help="File one ticket per violation (idempotent)."
    ),
    json_: bool = typer.Option(
        False, "--json", help="Emit the report as JSON (for scripts/dashboards)."
    ),
) -> None:
    _exit(_cmd_audit(SimpleNamespace(apply=apply, json=json_)))


@app.command("record-session", help="Record a real SDK session to a JSONL fixture.")
def cmd_record_session(
    issue: int | None = typer.Option(None, "--issue"),
    issue_file: str | None = typer.Option(None, "--issue-file"),
    out: str = typer.Option(..., "--out"),
    worktree: str | None = typer.Option(None, "--worktree"),
    timeout: int = typer.Option(900, "--timeout"),
) -> None:
    if (issue is None) == (issue_file is None):
        typer.echo("record-session: pass exactly one of --issue / --issue-file", err=True)
        raise typer.Exit(code=2)
    _exit(
        _cmd_record_session(
            SimpleNamespace(
                issue=issue,
                issue_file=issue_file,
                out=out,
                worktree=worktree,
                timeout=timeout,
            )
        )
    )


@app.command("brief", help="Render a brief template (worker/po/critic) to stdout.")
def cmd_brief(
    kind: str = typer.Option(..., "--kind"),
    issue: int | None = typer.Option(None, "--issue"),
    issue_file: str | None = typer.Option(None, "--issue-file"),
    worktree: str | None = typer.Option(None, "--worktree"),
    pr: str | None = typer.Option(None, "--pr"),
    repo: str | None = typer.Option(None, "--repo"),
    risk_gated: bool = typer.Option(False, "--risk-gated"),
    raw: bool = typer.Option(False, "--raw"),
) -> None:
    if kind not in {"worker", "po", "critic"}:
        typer.echo(f"brief: --kind must be one of worker|po|critic (got {kind!r})", err=True)
        raise typer.Exit(code=2)
    _exit(
        _cmd_brief(
            SimpleNamespace(
                kind=kind,
                issue=issue,
                issue_file=issue_file,
                worktree=worktree,
                pr=pr,
                repo=repo,
                risk_gated=risk_gated,
                raw=raw,
            )
        )
    )


# ---- config -------------------------------------------------------------


@config_app.callback(invoke_without_command=True)
def cmd_config(
    ctx: typer.Context,
    json_: bool = typer.Option(False, "--json", help="Emit JSON (default also emits JSON)."),
) -> None:
    """Print resolved config (top-level prints the full blob)."""
    if ctx.invoked_subcommand is None:
        _exit(_cmd_config(SimpleNamespace(json=json_)))


@config_app.command("models", help="Print resolved per-role model + thinking-budget.")
def cmd_config_models(
    json_: bool = typer.Option(False, "--json"),
) -> None:
    _exit(_cmd_config_models(SimpleNamespace(json=json_)))


# ---- pipeline -----------------------------------------------------------


@pipeline_app.command("show", help="Print the resolved DAG as ASCII art.")
def cmd_pipeline_show(
    config: str | None = typer.Option(None, "--config", help="Path to pipeline.yaml."),
    json_: bool = typer.Option(False, "--json"),
) -> None:
    _exit(_cmd_pipeline_show(SimpleNamespace(config=config, json=json_)))


# ---- repos --------------------------------------------------------------


@repos_app.command("list", help="List loaded repos + last activity.")
def cmd_repos_list(
    repos_dir: str | None = typer.Option(None, "--repos-dir"),
    json_: bool = typer.Option(False, "--json"),
) -> None:
    _exit(_cmd_repos_list(SimpleNamespace(repos_dir=repos_dir, json=json_)))


@repos_app.command("disable", help="Skip a repo until re-enabled.")
def cmd_repos_disable(
    name: str = typer.Argument(...),
    reason: str | None = typer.Option(None, "--reason"),
    repos_dir: str | None = typer.Option(None, "--repos-dir"),
) -> None:
    _exit(_cmd_repos_disable(SimpleNamespace(name=name, reason=reason, repos_dir=repos_dir)))


@repos_app.command("enable", help="Resume processing a disabled repo.")
def cmd_repos_enable(
    name: str = typer.Argument(...),
    repos_dir: str | None = typer.Option(None, "--repos-dir"),
) -> None:
    _exit(_cmd_repos_enable(SimpleNamespace(name=name, repos_dir=repos_dir)))


# ---- mcp ----------------------------------------------------------------


@mcp_app.command("serve", help="Run MCP server on stdio.")
def cmd_mcp_serve() -> None:
    _exit(_cmd_mcp_serve(SimpleNamespace()))


# ---- replay -------------------------------------------------------------


@replay_app.callback(invoke_without_command=True)
def cmd_replay(
    ctx: typer.Context,
    tick: int | None = typer.Option(None, "--tick"),
    role: str = typer.Option("worker", "--role"),
    brief: str | None = typer.Option(None, "--brief"),
    fixtures_dir: str | None = typer.Option(None, "--fixtures-dir"),
    suffix: str = typer.Option("r", "--suffix"),
    dry_plan: bool = typer.Option(False, "--dry-plan"),
) -> None:
    """Time-travel: re-run a past tick with a modified brief (dry-run)."""
    if ctx.invoked_subcommand is not None:
        return
    if tick is None or not brief:
        typer.echo(
            "replay: --tick and --brief are required (or use `replay diff`)",
            err=True,
        )
        raise typer.Exit(code=2)
    if role != "worker":
        typer.echo(f"replay: --role must be 'worker' (got {role!r})", err=True)
        raise typer.Exit(code=2)
    _exit(
        _cmd_replay(
            SimpleNamespace(
                tick=tick,
                role=role,
                brief=brief,
                fixtures_dir=fixtures_dir,
                suffix=suffix,
                dry_plan=dry_plan,
            )
        )
    )


@replay_app.command("diff", help="Side-by-side: original tick vs replay tick.")
def cmd_replay_diff(
    tick: int = typer.Option(..., "--tick"),
    replay_tick: str = typer.Option(..., "--replay-tick"),
    json_: bool = typer.Option(False, "--json"),
) -> None:
    _exit(_cmd_replay_diff(SimpleNamespace(tick=tick, replay_tick=replay_tick, json=json_)))


# ---- roles --------------------------------------------------------------


@roles_app.command("list", help="List loaded roles + triggers + next firing.")
def cmd_roles_list(
    project_dir: str | None = typer.Option(None, "--project-dir"),
    json_: bool = typer.Option(False, "--json"),
) -> None:
    _exit(_cmd_roles_list(SimpleNamespace(project_dir=project_dir, json=json_)))


# ---- cluster (deprecated stub) ------------------------------------------


@cluster_app.command("status", help="Deprecated: cluster mode removed in #39.")
def cmd_cluster_status(
    queue: str = typer.Option(..., "--queue"),
    json_: bool = typer.Option(False, "--json"),
) -> None:
    _exit(_cmd_cluster_status(SimpleNamespace(queue=queue, json=json_)))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Programmatic entry point — used by ``forge-loop`` and ``python -m forge_loop``.

    Normal subcommands return an integer so tests and replay tools can call
    ``main([...])`` directly. Help keeps the historical CLI shape and raises
    ``SystemExit(0)`` through Typer standalone mode.
    """
    if argv and (
        argv[0] in {"replay", "record-session"} or any(arg in {"-h", "--help"} for arg in argv)
    ):
        app(args=argv, standalone_mode=True)
        return 0  # unreachable in standalone mode — kept for type-checkers
    try:
        result = app(args=argv, standalone_mode=False)
    except typer.Exit as exc:
        return int(exc.exit_code or 0)
    except click.exceptions.Exit as exc:
        return int(exc.exit_code or 0)
    except click.ClickException as exc:
        raise SystemExit(exc.exit_code) from exc
    return int(result or 0)


if __name__ == "__main__":
    sys.exit(main())
