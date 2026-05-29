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

import json
import os
import subprocess
import sys
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import typer

from forge_loop.config import load
from forge_loop.runner import run as run_loop
from forge_loop.state import tail_events

# ---------------------------------------------------------------------------
# Typer app — Rich-formatted help, no-subcommand prints help (no traceback).
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="forge-loop",
    help="Titan sprint-loop runner.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    add_completion=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

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


def _cmd_run(args: SimpleNamespace) -> int:
    queue_url = getattr(args, "queue", None)
    if queue_url:
        os.environ["LOOP_QUEUE_URL"] = queue_url

    orch = getattr(args, "orchestrator", "sync")
    if orch == "async":
        from forge_loop.runner import run_async as run_async_loop

        return run_async_loop(load())
    return run_loop(load())


def _cmd_cluster_status(args: SimpleNamespace) -> int:
    """Deprecated: multi-host cluster mode was removed in #39."""

    _ = args
    sys.stderr.write(
        "cluster status: multi-host cluster mode was removed in #39 "
        "(premature distribution; one-operator-one-box is the supported "
        "surface). Use 'forge-loop status' and 'forge-loop events' instead.\n"
    )
    return 2


_STATUS_MARKERS = {
    "green": "[green]✓[/green]",
    "yellow": "[yellow]~[/yellow]",
    "red": "[red]✗[/red]",
}


def _cmd_doctor(_args: SimpleNamespace) -> int:
    """One-shot health check with a Rich table."""
    import glob
    import shutil
    import subprocess as _sp

    from rich.console import Console
    from rich.table import Table

    console = Console()

    try:
        cfg = load()
        cfg_ok = True
        cfg_load_error: str | None = None
    except Exception as exc:  # noqa: BLE001
        cfg = None
        cfg_ok = False
        cfg_load_error = str(exc)
    red = not cfg_ok

    table = Table(
        title="[bold]forge-loop doctor[/bold]",
        show_header=True,
        header_style="bold",
        title_justify="left",
        expand=False,
    )
    table.add_column("", width=3, no_wrap=True)
    table.add_column("Check", style="bold")
    table.add_column("Detail", style="dim", overflow="fold")

    def line(status: str, label: str, detail: str = "") -> None:
        nonlocal red
        table.add_row(_STATUS_MARKERS[status], label, detail)
        if status == "red":
            red = True

    if cfg_load_error:
        line("red", "config load failed", cfg_load_error)

    if cfg_ok:
        halt = cfg.state_dir / "loop-runner.HALT"
        stop = cfg.stop_file
        if halt.exists():
            line("red", "halt marker present", f"remove {halt}")
        else:
            line("green", "no halt marker")
        if stop.exists():
            line("yellow", "stop file pending", f"will halt at next tick boundary ({stop})")

    tmux_bin = shutil.which("tmux")
    if tmux_bin is None:
        line("yellow", "tmux not installed", "operator usually runs forge-loop in a tmux session")
    else:
        try:
            r = _sp.run([tmux_bin, "ls"], capture_output=True, text=True, timeout=5)
            sessions = [
                row.split(":", 1)[0]
                for row in r.stdout.splitlines()
                if row.startswith("forge-loop")
            ]
            if sessions:
                line("green", f"tmux sessions: {', '.join(sessions)}")
            else:
                line("yellow", "no forge-loop tmux session", "run with: forge-loop run")
        except _sp.SubprocessError:
            line("yellow", "tmux probe failed")

    orphans = sorted(glob.glob("/tmp/wt-loop-*"))
    if orphans:
        line(
            "yellow",
            f"{len(orphans)} orphan worktree(s) under /tmp/wt-loop-*",
            "the runner reaps these at next boot",
        )
    else:
        line("green", "no orphan worktrees")

    if cfg_ok:
        try:
            local = _sp.run(
                ["git", "rev-parse", "HEAD"],
                cwd=cfg.repo,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            _sp.run(
                ["git", "fetch", "origin", cfg.base_branch, "--quiet"],
                cwd=cfg.repo,
                capture_output=True,
                timeout=10,
            )
            remote = _sp.run(
                ["git", "rev-parse", f"origin/{cfg.base_branch}"],
                cwd=cfg.repo,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            if local and remote and local == remote:
                line("green", f"code matches origin/{cfg.base_branch} @ {local[:8]}")
            elif local and remote:
                line(
                    "yellow",
                    f"local {cfg.base_branch} behind origin",
                    f"local={local[:8]} origin={remote[:8]}",
                )
            else:
                line("yellow", f"could not compare to origin/{cfg.base_branch}")
        except (_sp.SubprocessError, OSError):
            line("yellow", "git probe failed")

    drift_halt_opt_in = os.environ.get("LOOP_DEPLOY_DRIFT_HALT") == "1"
    line(
        "yellow" if drift_halt_opt_in else "green",
        "deploy-drift halt",
        "ENABLED (opt-in)" if drift_halt_opt_in else "disabled (default)",
    )

    console.print(table)
    return 1 if red else 0


def _cmd_status(args: SimpleNamespace) -> int:
    """Operator-facing health surface — Rich Panel + Table by default;
    ``--json`` emits a raw machine-parseable blob for scripts.
    """
    from datetime import datetime

    cfg = load()
    now = datetime.now(UTC)
    today = now.date()

    pid_alive = False
    pid_text = ""
    if cfg.pid_file.exists():
        pid_text = cfg.pid_file.read_text().strip()
        try:
            os.kill(int(pid_text), 0)
            pid_alive = True
        except (OSError, ValueError):
            pid_alive = False

    halt_file = cfg.state_dir / "loop-runner.HALT"
    halt_reason = halt_file.read_text().strip() if halt_file.exists() else None

    state_blob: dict[str, Any] = {}
    if cfg.state_file.exists():
        try:
            state_blob = json.loads(cfg.state_file.read_text())
        except json.JSONDecodeError:
            state_blob = {"_raw": cfg.state_file.read_text()[:200]}

    prs_today: list[int] = []
    last_failure: dict[str, Any] | None = None
    last_5_events: list[dict[str, str]] = []
    if cfg.events_file.exists():
        try:
            with open(cfg.events_file) as f:
                raw = f.readlines()
        except OSError:
            raw = []
        for line in raw[-500:]:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = e.get("ts", "")
            kind = e.get("kind", "")
            try:
                evt_date = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
            except (ValueError, TypeError):
                evt_date = None
            if kind == "tick_done" and evt_date == today:
                for n in e.get("merged", []) or []:
                    prs_today.append(n)
            if "fail" in kind or kind == "watchdog_worker_killed":
                last_failure = {
                    "ts": ts,
                    "kind": kind,
                    "detail": str(e.get("detail") or e.get("err") or "")[:120],
                }
        for line in raw[-5:]:
            try:
                e = json.loads(line)
                last_5_events.append({"ts": str(e.get("ts", "?")), "kind": str(e.get("kind", "?"))})
            except json.JSONDecodeError:
                pass

    queue_depth = 0
    try:
        r = subprocess.run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                cfg.github_repo,
                "--label",
                cfg.labels.ready,
                "--state",
                "open",
                "--json",
                "number",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if r.returncode == 0:
            queue_depth = len(json.loads(r.stdout or "[]"))
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        queue_depth = -1

    payload: dict[str, Any] = {
        "pid": pid_text or None,
        "pid_alive": pid_alive,
        "halt_reason": halt_reason,
        "state": state_blob.get("state"),
        "tick": state_blob.get("tick"),
        "queue_depth": queue_depth,
        "queue_label": cfg.labels.ready,
        "prs_today": prs_today,
        "last_failure": last_failure,
        "last_events": last_5_events,
        "events_file": str(cfg.events_file),
    }

    if getattr(args, "json", False):
        sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")
        return 0

    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    console = Console()

    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("k", style="bold cyan", no_wrap=True)
    table.add_column("v")

    if halt_reason:
        table.add_row("[red]HALTED[/red]", halt_reason)
    pid_render = f"{pid_text or '(no pidfile)'} " + (
        "[green](alive)[/green]" if pid_alive else "[red](NOT running)[/red]"
    )
    table.add_row("pid", pid_render)
    table.add_row(
        "state",
        f"{state_blob.get('state', '?')}  tick={state_blob.get('tick', '?')}",
    )
    qd_str = f"{queue_depth} issues with label '{cfg.labels.ready}'"
    table.add_row("queue", qd_str)
    table.add_row(
        "PRs today",
        f"{len(prs_today)} ({prs_today})" if prs_today else "0",
    )
    if last_failure:
        table.add_row(
            "[red]last fail[/red]",
            f"{last_failure['ts'][-9:-1]}  {last_failure['kind']}  {last_failure['detail']}",
        )
    if last_5_events:
        joined = Text()
        for ev in last_5_events:
            joined.append(f"  {ev['ts'][-9:-1] if ev['ts'] else '?'}  ")
            joined.append(ev["kind"], style="cyan")
            joined.append("\n")
        table.add_row("last 5 events", joined)
    table.add_row("events", str(cfg.events_file))

    console.print(Panel(table, title="[bold]forge-loop status[/bold]", title_align="left"))
    return 0


def _cmd_events(args: SimpleNamespace) -> int:
    """Tail recent events. Rich-formatted by default; ``--raw`` skips colour."""
    cfg = load()

    if getattr(args, "raw", False):
        for line in tail_events(cfg.events_file, n=args.n):
            sys.stdout.write(line)
        return 0

    from rich.console import Console
    from rich.syntax import Syntax
    from rich.text import Text

    _KIND_STYLE = {
        "loop_start": "bold green",
        "loop_stop": "bold red",
        "tick_start": "cyan",
        "tick_done": "bold cyan",
        "tick_idle": "dim",
        "po_start": "blue",
        "po_done": "blue",
        "worker_skip_in_flight": "dim yellow",
        "worker_skip_cooldown": "dim yellow",
        "watchdog_started": "dim",
        "watchdog_stopped": "dim",
        "worktree_reaped": "dim green",
        "orphan_worktrees_reaped": "dim green",
        "redeploy": "magenta",
        "deploy_drift_warn": "bold yellow",
        "deploy_drift_halt": "bold red",
        "loop_drift_halt": "bold red",
        "critic_done": "green",
        "critic_failed": "red",
        "critic_actions_failed": "red",
        "budget_worker_killed": "bold red",
        "signal_stop": "bold red",
        "version_changed_restart": "bold yellow",
    }

    console = Console()
    for raw in tail_events(cfg.events_file, n=args.n):
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            console.print(raw.rstrip(), style="dim red")
            continue
        ts = ev.get("ts", "?")[11:19] if ev.get("ts") else "?"
        kind = ev.get("kind", "?")
        style = _KIND_STYLE.get(kind, "white")
        rest = {k: v for k, v in ev.items() if k not in ("ts", "kind")}
        prefix = Text.assemble(
            (f"{ts} ", "dim"),
            (f"{kind:<26}", style),
            (" ", ""),
        )
        body = json.dumps(rest, default=str)
        if len(body) > 240:
            body = body[:237] + "..."
        console.print(prefix, Syntax(body, "json", theme="ansi_dark", word_wrap=False))
    return 0


def _cmd_pause(_args: SimpleNamespace) -> int:
    cfg = load()
    cfg.pause_file.touch()
    typer.echo(f"[pause] touched {cfg.pause_file}")
    return 0


def _cmd_resume(_args: SimpleNamespace) -> int:
    cfg = load()
    if cfg.pause_file.exists():
        cfg.pause_file.unlink()
    typer.echo(f"[resume] cleared {cfg.pause_file}")
    return 0


def _cmd_stop(_args: SimpleNamespace) -> int:
    cfg = load()
    cfg.stop_file.touch()
    typer.echo(f"[stop] touched {cfg.stop_file}")
    return 0


def _cmd_dashboard(args: SimpleNamespace) -> int:
    """Start the operator dashboard.

    ``--web`` (default for back-compat) launches the FastAPI + HTMX app.
    ``--tui`` launches the new Textual TUI from ``cli_tui.py``.
    """
    mode = getattr(args, "mode", "web")
    if mode == "tui":
        from forge_loop import cli_tui

        cfg = load()
        return cli_tui.run_tui(state_dir=cfg.state_dir, events_file=cfg.events_file)

    from forge_loop.dashboard.app import DashboardBindError
    from forge_loop.dashboard.app import serve as _serve

    cfg = load()
    host = args.host or "127.0.0.1"
    port = int(args.port or os.environ.get("LOOP_DASHBOARD_PORT") or 8765)
    roles_dir = Path(args.roles_dir) if args.roles_dir else cfg.repo / "roles"
    try:
        _serve(
            host=host,
            port=port,
            state_dir=cfg.state_dir,
            roles_dir=roles_dir,
            token=os.environ.get("LOOP_DASHBOARD_TOKEN") or None,
        )
    except DashboardBindError as exc:
        sys.stderr.write(f"dashboard: {exc}\n")
        return 2
    return 0


def _cmd_mcp_serve(_args: SimpleNamespace) -> int:
    from forge_loop.mcp_server import serve_stdio

    return serve_stdio()


def _cmd_init(args: SimpleNamespace) -> int:
    from forge_loop import init as _init_mod

    target = Path(args.target).resolve() if args.target else Path.cwd().resolve()
    repo = args.repo or _init_mod.detect_github_repo(target)

    result = _init_mod.init_project(target, github_repo=repo, force=args.force)

    typer.echo(f"[init] scaffolded forge-loop in {target}")
    typer.echo(f"[init] github repo: {repo}")
    for path in result["created"]:
        typer.echo(f"  + {path}")
    for path in result["skipped"]:
        typer.echo(f"  · skipped (exists; pass --force to overwrite): {path}")

    if args.create_labels:
        created = _init_mod.ensure_labels_via_gh(repo, _init_mod.DEFAULT_LABELS)
        for name in created:
            typer.echo(f"  + label: {name}")
        for name, _, _ in _init_mod.DEFAULT_LABELS:
            if name not in created:
                typer.echo(f"  · label exists: {name}")

    typer.echo("")
    typer.echo("Next:")
    typer.echo("  1. Review forge-loop.yaml")
    typer.echo("  2. Add manual entries under manual/")
    typer.echo("  3. Label issues with `loop:ready` for the loop to attack")
    typer.echo("  4. Run:  forge-loop run        (or: task loop:start)")
    return 0


def _cmd_record_session(args: SimpleNamespace) -> int:
    from forge_loop._testing.recorder import SessionRecorder
    from forge_loop.worker import make_brief

    issue: dict[str, Any]
    if args.issue_file:
        issue = json.loads(Path(args.issue_file).read_text())
    else:
        r = subprocess.run(
            ["gh", "issue", "view", str(args.issue), "--json", "number,title,body"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode != 0:
            sys.stderr.write(f"gh issue view failed: {r.stderr}\n")
            return 2
        issue = json.loads(r.stdout)

    worktree = Path(args.worktree).resolve() if args.worktree else Path.cwd().resolve()
    brief = make_brief(issue, worktree)
    rec = SessionRecorder(issue=issue, worktree=worktree, brief=brief)
    result = rec.record(Path(args.out), timeout_s=args.timeout)
    typer.echo(
        json.dumps(
            {
                "fixture": str(result.fixture_path),
                "events": result.event_count,
                "duration_s": round(result.duration_s, 2),
                "pr": result.pr_url,
                "status": result.status,
            },
            indent=2,
        )
    )
    return 0


def _cmd_retry(args: SimpleNamespace) -> int:
    from forge_loop import attempts as _attempts
    from forge_loop import worker as _worker
    from forge_loop.gh import fetch_issue
    from forge_loop.runner import _force_retry_file

    cfg = load()
    issue = fetch_issue(args.issue, repo=cfg.github_repo)
    if not issue:
        typer.echo(f"[retry] could not fetch issue #{args.issue}", err=True)
        return 2

    brief_hash = _worker.brief_template_hash()
    fp = _attempts.compute_fingerprint(
        issue["number"],
        issue.get("body") or "",
        brief_hash,
    )
    history, corrupt = _attempts.fetch_history_strict(
        issue["number"],
        repo=cfg.github_repo,
    )
    if corrupt:
        typer.echo(f"[retry] warning: {corrupt} corrupt attempt row(s) in history")
    decision = _attempts.classify_skip(
        history,
        fp,
        cooldown_s=_attempts.cooldown_from_env(),
    )
    typer.echo(f"[retry] issue #{args.issue} fingerprint={fp[:12]}")
    if decision.kind == "in_flight":
        typer.echo(f"[retry] guard: in-flight (PR {decision.pr_url})")
    elif decision.kind == "cooldown":
        typer.echo(f"[retry] guard: cooldown ({decision.cooldown_remaining_s}s remaining)")
    else:
        typer.echo("[retry] guard: none — next tick will dispatch normally")

    if not args.force:
        if decision.kind:
            typer.echo("[retry] pass --force to bypass the guard")
        return 0

    marker = _force_retry_file(cfg)
    marker.parent.mkdir(parents=True, exist_ok=True)
    existing: set[int] = set()
    if marker.exists():
        try:
            blob = json.loads(marker.read_text())
            existing = {int(n) for n in (blob.get("issues") or [])}
        except (OSError, ValueError, json.JSONDecodeError):
            existing = set()
    existing.add(int(args.issue))
    marker.write_text(json.dumps({"issues": sorted(existing)}))
    typer.echo(f"[retry] forced: wrote {marker} (issues={sorted(existing)})")
    return 0


def _cmd_brief(args: SimpleNamespace) -> int:
    from forge_loop.briefs import load_template, render_brief

    kind = args.kind

    if args.raw:
        sys.stdout.write(load_template(kind))
        return 0

    issue: dict[str, Any] = {}
    if args.issue_file:
        issue = json.loads(Path(args.issue_file).read_text())
    elif args.issue is not None:
        try:
            r = subprocess.run(
                ["gh", "issue", "view", str(args.issue), "--json", "number,title,body"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if r.returncode == 0:
                issue = json.loads(r.stdout)
            else:
                sys.stderr.write(
                    f"[brief] gh issue view failed ({r.returncode}); "
                    f"falling back to a placeholder issue. stderr={r.stderr.strip()}\n"
                )
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            sys.stderr.write(f"[brief] gh unavailable ({e}); using placeholder.\n")

    if not issue:
        issue = {
            "number": args.issue or 0,
            "title": "<placeholder title>",
            "body": "<placeholder body>",
        }

    if kind == "worker":
        from forge_loop.worker import make_brief

        worktree = Path(args.worktree).resolve() if args.worktree else Path.cwd().resolve()
        out = make_brief(issue, worktree, risk_gated=args.risk_gated)
    elif kind == "po":
        out = render_brief(
            "po",
            issue_number=issue["number"],
            issue_title=issue.get("title", ""),
            issue_body=(issue.get("body") or "")[:4000],
            github_repo=args.repo or "<owner/repo>",
        )
    elif kind == "critic":
        out = render_brief(
            "critic",
            pr_url=args.pr or "<pr-url>",
            issue_number=issue["number"],
        )
    else:
        sys.stderr.write(f"[brief] unknown kind: {kind}\n")
        return 2

    sys.stdout.write(out)
    if not out.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _cmd_replay(args: SimpleNamespace) -> int:
    from forge_loop import replay as _replay

    cfg = load()
    brief_text = Path(args.brief).read_text(encoding="utf-8")
    fixtures_dir = Path(args.fixtures_dir).resolve() if args.fixtures_dir else None

    try:
        plan = _replay.plan_replay(
            cfg.events_file,
            tick=args.tick,
            role=args.role,
            new_brief=brief_text,
            fixtures_dir=fixtures_dir,
            replay_suffix=args.suffix,
        )
    except _replay.ReplayError as exc:
        sys.stderr.write(f"[replay] {exc}\n")
        return 2

    if args.dry_plan:
        typer.echo(
            json.dumps(
                {
                    "original_tick": plan.original_tick,
                    "replay_tick": plan.replay_tick,
                    "role": plan.role,
                    "invocations": [
                        {
                            "issue": inv.issue,
                            "title": inv.title,
                            "fixture": str(inv.fixture_path) if inv.fixture_path else None,
                        }
                        for inv in plan.invocations
                    ],
                },
                indent=2,
            )
        )
        return 0

    try:
        captures = _replay.run_replay_tick(plan, events_path=cfg.events_file)
    except _replay.ReplayError as exc:
        sys.stderr.write(f"[replay] {exc}\n")
        return 3

    typer.echo(
        json.dumps(
            {
                "original_tick": plan.original_tick,
                "replay_tick": plan.replay_tick,
                "captures": [
                    {
                        "issue": c.issue,
                        "status": c.status,
                        "source": c.source,
                        "cost_usd": c.cost_usd,
                        "commit": c.commit_hash,
                        "diff_chars": len(c.diff_text),
                        "error": c.error,
                    }
                    for c in captures
                ],
            },
            indent=2,
        )
    )
    return 0


def _cmd_replay_diff(args: SimpleNamespace) -> int:
    from forge_loop import replay as _replay

    cfg = load()
    try:
        report = _replay.build_diff_report(
            cfg.events_file,
            tick=args.tick,
            replay_tick=args.replay_tick,
        )
    except _replay.ReplayError as exc:
        sys.stderr.write(f"[replay diff] {exc}\n")
        return 2

    if args.json:
        typer.echo(json.dumps(report, indent=2, default=str))
    else:
        sys.stdout.write(_replay.render_diff_report_text(report))
    return 0


def _default_repos_dir() -> Path:
    env = os.environ.get("LOOP_REPOS_DIR")
    return Path(env).expanduser() if env else Path.cwd() / ".forge" / "repos"


def _cmd_repos_list(args: SimpleNamespace) -> int:
    from forge_loop.multirepo import RepoLoadError, is_disabled, load_repos, validate_checkout

    repos_dir = Path(args.repos_dir) if args.repos_dir else _default_repos_dir()
    try:
        specs = load_repos(repos_dir)
    except RepoLoadError as e:
        sys.stderr.write(f"[repos list] {e}\n")
        return 2

    last_activity: dict[str, dict[str, Any]] = {}
    sidecar = (
        repos_dir.parent.parent / ".forge" / "multirepo-events.jsonl"
        if repos_dir.name == "repos"
        else repos_dir.parent / "multirepo-events.jsonl"
    )
    if sidecar.exists():
        try:
            with open(sidecar) as f:
                for line in f.readlines()[-1000:]:
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if e.get("kind") in {
                        "repo_tick_done",
                        "repo_skipped",
                        "repo_tick_start",
                        "repo_tick_error",
                    }:
                        repo = e.get("repo")
                        if repo:
                            last_activity[repo] = {
                                "ts": e.get("ts"),
                                "kind": e.get("kind"),
                                "reason": e.get("reason") or "",
                                "tick": e.get("tick"),
                            }
        except OSError:
            pass

    rows = []
    for spec in specs:
        bad = validate_checkout(spec)
        rows.append(
            {
                "name": spec.name,
                "github": spec.github,
                "checkout": str(spec.checkout),
                "disabled": is_disabled(spec),
                "checkout_invalid": bad,
                "budget_usd_per_day": spec.budget_usd_per_day,
                "source": str(spec.source_path) if spec.source_path else None,
                "last_activity": last_activity.get(spec.name),
            }
        )

    if args.json:
        typer.echo(json.dumps({"repos_dir": str(repos_dir), "repos": rows}, indent=2))
        return 0

    typer.echo(f"== forge-loop repos ({repos_dir}) ==")
    if not rows:
        typer.echo(
            "  (no repo specs loaded — run `forge-loop init` per-repo or "
            "create .forge/repos/*.yaml)"
        )
        return 0
    for r in rows:
        flags = []
        if r["disabled"]:
            flags.append("DISABLED")
        if r["checkout_invalid"]:
            flags.append(f"INVALID({r['checkout_invalid']})")
        flag_s = "  [" + ", ".join(flags) + "]" if flags else ""
        last = r["last_activity"]
        last_s = (
            (f"  last: tick {last['tick']} {last['kind']} ({last['ts']})")
            if last
            else "  last: never"
        )
        typer.echo(f"  - {r['name']:<20} {r['github']:<30}{flag_s}")
        typer.echo(f"    checkout: {r['checkout']}")
        typer.echo(f"    budget/day: ${r['budget_usd_per_day']:.2f}{last_s}")
    return 0


def _cmd_repos_disable(args: SimpleNamespace) -> int:
    from forge_loop.multirepo import RepoLoadError, disable_repo, load_repos

    repos_dir = Path(args.repos_dir) if args.repos_dir else _default_repos_dir()
    try:
        specs = load_repos(repos_dir)
    except RepoLoadError as e:
        sys.stderr.write(f"[repos disable] {e}\n")
        return 2
    match = next((s for s in specs if s.name == args.name), None)
    if not match:
        sys.stderr.write(f"[repos disable] no such repo: {args.name}\n")
        return 2
    flag = disable_repo(match, reason=args.reason or "")
    typer.echo(f"[repos disable] {match.name} → flag at {flag}")
    return 0


def _cmd_repos_enable(args: SimpleNamespace) -> int:
    from forge_loop.multirepo import RepoLoadError, enable_repo, load_repos

    repos_dir = Path(args.repos_dir) if args.repos_dir else _default_repos_dir()
    try:
        specs = load_repos(repos_dir)
    except RepoLoadError as e:
        sys.stderr.write(f"[repos enable] {e}\n")
        return 2
    match = next((s for s in specs if s.name == args.name), None)
    if not match:
        sys.stderr.write(f"[repos enable] no such repo: {args.name}\n")
        return 2
    cleared = enable_repo(match)
    if cleared:
        typer.echo(f"[repos enable] cleared disable flag for {match.name}")
    else:
        typer.echo(f"[repos enable] {match.name} was not disabled (no-op)")
    return 0


def _cmd_pipeline_show(args: SimpleNamespace) -> int:
    from forge_loop.pipeline import (
        PipelineLoadError,
        ValidationError,
        build_dag,
        load_pipeline,
    )

    path = Path(args.config) if args.config else Path.cwd() / ".forge" / "pipeline.yaml"
    if not path.exists():
        sys.stderr.write(f"[pipeline show] config not found: {path}\n")
        return 2
    try:
        spec = load_pipeline(path)
        dag = build_dag(spec)
    except (PipelineLoadError, ValidationError) as e:
        sys.stderr.write(f"[pipeline show] {e}\n")
        return 2

    if args.json:
        out = {
            "source": str(spec.source_path),
            "roots": list(dag.roots),
            "order": list(dag.order),
            "nodes": {
                r: {
                    "parents": list(n.parents),
                    "children": list(n.children),
                    "depth": n.depth,
                    "parallel": n.step.parallel,
                    "on": n.step.on,
                    "condition": {
                        "labels": list(n.step.condition.labels),
                        "all_approve": n.step.condition.all_approve,
                    },
                }
                for r, n in dag.nodes.items()
            },
        }
        typer.echo(json.dumps(out, indent=2))
        return 0

    typer.echo(f"# pipeline: {spec.source_path}")
    typer.echo(f"# roots:    {', '.join(dag.roots)}")
    typer.echo(f"# order:    {' → '.join(dag.order)}")
    typer.echo("")
    typer.echo(dag.render_ascii())
    return 0


def _cmd_config(args: SimpleNamespace) -> int:
    cfg = load()
    out = {
        "repo": str(cfg.repo),
        "base_branch": cfg.base_branch,
        "parallel": cfg.parallel,
        "tick_interval_s": cfg.tick_interval_s,
        "max_ticks": cfg.max_ticks,
        "query_label": cfg.labels.ready,
        "worker_timeout_s": cfg.worker_timeout_s,
        "state_file": str(cfg.state_file),
        "events_file": str(cfg.events_file),
        "worker": {
            "provider": cfg.worker.provider,
            "model": cfg.worker.model,
            "thinking": cfg.worker.thinking,
        },
        "po": {
            "provider": cfg.po.provider,
            "model": cfg.po.model,
            "thinking": cfg.po.thinking,
        },
        "critic": {
            "provider": cfg.critic.provider,
            "model": cfg.critic.model,
            "thinking": cfg.critic.thinking,
        },
    }
    # Historical surface: ``config`` always emits JSON (the ``--json`` flag
    # was a no-op kept for back-compat). Preserve that.
    _ = getattr(args, "json", False)
    typer.echo(json.dumps(out, indent=2))
    return 0


def _cmd_config_models(args: SimpleNamespace) -> int:
    cfg = load()
    rows = [
        ("worker", cfg.worker.provider, cfg.worker.model, cfg.worker.thinking),
        ("po", cfg.po.provider, cfg.po.model, cfg.po.thinking),
        ("critic", cfg.critic.provider, cfg.critic.model, cfg.critic.thinking),
    ]
    if getattr(args, "json", False):
        typer.echo(
            json.dumps(
                {role: {"provider": p, "model": m, "thinking": t} for role, p, m, t in rows},
                indent=2,
            )
        )
        return 0
    typer.echo(f"{'ROLE':<8} {'PROVIDER':<8} {'MODEL':<22} THINKING")
    for role, provider, model, thinking in rows:
        typer.echo(f"{role:<8} {provider:<8} {model or '<default>':<22} {thinking}")
    return 0


def _cmd_roles_list(args: SimpleNamespace) -> int:
    from forge_loop.roles import discover_roles

    project_dir = Path(args.project_dir) if getattr(args, "project_dir", None) else Path.cwd()
    result = discover_roles(project_dir)

    if getattr(args, "json", False):
        payload = {
            "roles": [
                {
                    "name": r.name,
                    "model": r.model,
                    "timeout_s": r.timeout_s,
                    "budget_usd": r.budget_usd,
                    "triggers": [{"on": t.on, "filter": t.filter} for t in r.triggers],
                    "actions": [
                        {"mcp_tools": list(a.mcp_tools), "shell": a.shell} for a in r.actions
                    ],
                    "output_schema": r.output_schema,
                    "source": r.source_path,
                    "next_firing": _next_firing_label(r),
                }
                for r in result.roles
            ],
            "errors": [{"source": e.source, "message": e.message} for e in result.errors],
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return 0

    if not result.roles:
        sys.stdout.write("(no roles discovered)\n")
    for r in result.roles:
        triggers = (
            ", ".join(
                t.on
                + (f"[{','.join(f'{k}={v}' for k, v in t.filter.items())}]" if t.filter else "")
                for t in r.triggers
            )
            or "(none)"
        )
        origin = "builtin"
        if r.source_path and ".forge/roles" in r.source_path:
            origin = "project"
        sys.stdout.write(
            f"- {r.name} ({origin}) model={r.model} timeout={r.timeout_s}s "
            f"budget={'∞' if r.budget_usd is None else f'${r.budget_usd:.2f}'}\n"
            f"    triggers: {triggers}\n"
            f"    next: {_next_firing_label(r)}\n"
        )
    for err in result.errors:
        sys.stderr.write(f"warning: {err}\n")
    return 0


def _next_firing_label(role: Any) -> str:
    if not role.triggers:
        return "manual only"
    on_set = sorted({t.on for t in role.triggers})
    if "tick" in on_set:
        return "every loop tick"
    return "on " + ", ".join(on_set)


# ---------------------------------------------------------------------------
# Typer commands — thin wrappers that build a SimpleNamespace and dispatch.
# ---------------------------------------------------------------------------


def _exit(rc: int) -> None:
    """Exit by raising typer.Exit so the CliRunner sees the same code path."""
    raise typer.Exit(code=int(rc))


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
) -> None:
    if orchestrator not in {"sync", "async"}:
        typer.echo(f"run: invalid --orchestrator {orchestrator!r}", err=True)
        raise typer.Exit(code=2)
    _exit(_cmd_run(SimpleNamespace(orchestrator=orchestrator, queue=queue)))


@app.command("status", help="Operator-facing health surface.")
def cmd_status(
    json_: bool = typer.Option(False, "--json", help="Emit raw JSON for scripts."),
) -> None:
    _exit(_cmd_status(SimpleNamespace(json=json_)))


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

    Runs the Typer app in *standalone* mode, which mirrors the historical
    argparse behaviour: ``--help`` and parse errors raise ``SystemExit``
    with the appropriate exit code, and successful subcommand returns
    raise ``SystemExit(0)``. Callers who want an int back can wrap in
    ``try/except SystemExit``. The ``sys.exit(main())`` idiom at the
    bottom keeps the historical script wrapper happy.
    """
    app(args=argv, standalone_mode=True)
    return 0  # unreachable in standalone mode — kept for type-checkers


if __name__ == "__main__":
    sys.exit(main())
