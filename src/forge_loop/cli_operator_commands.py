from __future__ import annotations

# ruff: noqa: F401
import json
import os
import subprocess
import sys
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import typer

from forge_loop.state import tail_events

_STATUS_MARKERS = {
    "green": "[green]✓[/green]",
    "yellow": "[yellow]~[/yellow]",
    "red": "[red]✗[/red]",
}


class OperatorCommandsMixin:
    load: Any
    run_loop: Any
    operator_cfg: Any

    def _cmd_run(self, args: SimpleNamespace) -> int:
        queue_url = getattr(args, "queue", None)
        if queue_url:
            os.environ["LOOP_QUEUE_URL"] = queue_url

        # Issue #126 — axis-aware dispatch filter. The CLI accepts ``--axis``
        # repeatedly; we serialise to a comma-separated env var so the tick
        # loop (which is a separate function in another module) can read it
        # without us having to thread a new kwarg through ``run_loop`` and
        # every adjacent caller. Empty list -> env var stays unset, and the
        # dispatcher takes the pre-#126 fast path verbatim.
        from forge_loop.axis import AXIS_FILTER_ENV

        axes = [a.strip().lower() for a in (getattr(args, "axis", None) or []) if a and a.strip()]
        previous_axis_filter = os.getenv(AXIS_FILTER_ENV)
        if axes:
            os.environ[AXIS_FILTER_ENV] = ",".join(axes)
        else:
            os.environ.pop(AXIS_FILTER_ENV, None)

        try:
            orch = getattr(args, "orchestrator", "sync")
            if orch == "async":
                from forge_loop.runner import run_async as run_async_loop

                return run_async_loop(self.load())
            return int(self.run_loop(self.load()))
        finally:
            if not axes or previous_axis_filter is None:
                os.environ.pop(AXIS_FILTER_ENV, None)
            else:
                os.environ[AXIS_FILTER_ENV] = previous_axis_filter

    def _cmd_cluster_status(self, args: SimpleNamespace) -> int:
        """Deprecated: multi-host cluster mode was removed in #39."""

        _ = args
        sys.stderr.write(
            "cluster status: multi-host cluster mode was removed in #39 "
            "(premature distribution; one-operator-one-box is the supported "
            "surface). Use 'forge-loop status' and 'forge-loop events' instead.\n"
        )
        return 2

    def _cmd_doctor(self, args: SimpleNamespace) -> int:
        """One-shot health check with a Rich table.

        ``--json`` (``args.json``) emits a machine-readable object whose
        ``control_plane`` key carries the four durable control-plane checks
        (issue #202); the human path appends a control-plane section to the
        Rich table. Either way a ``fail`` control-plane check drives the exit
        code to 1, consistent with the existing red-check behaviour.
        """
        import glob
        import shutil
        import subprocess as _sp
        from datetime import datetime

        from rich.console import Console
        from rich.table import Table

        from forge_loop.control.doctor import (
            FAIL as _CP_FAIL,
        )
        from forge_loop.control.doctor import (
            PASS as _CP_PASS,
        )
        from forge_loop.control.doctor import (
            collect_control_plane_doctor,
            mutation_survivors_check,
            unavailable_checks,
        )

        want_json = bool(getattr(args, "json", False))
        console = Console()

        # ``--json`` accumulates structured rows alongside the Rich table so
        # the two surfaces never drift apart.
        json_checks: list[dict[str, Any]] = []

        try:
            cfg = self.load()
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

        # Map the human marker colour to a machine-readable status word so the
        # JSON and Rich surfaces stay in lock-step.
        _marker_status = {"green": _CP_PASS, "yellow": "warn", "red": _CP_FAIL}

        def line(status: str, label: str, detail: str = "") -> None:
            nonlocal red
            table.add_row(_STATUS_MARKERS[status], label, detail)
            json_checks.append({"name": label, "status": _marker_status[status], "detail": detail})
            if status == "red":
                red = True

        if cfg_load_error:
            line("red", "config load failed", cfg_load_error)

        if cfg is not None:
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
            line(
                "yellow", "tmux not installed", "operator usually runs forge-loop in a tmux session"
            )
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

        if cfg is not None:
            from forge_loop.worker_worktree import worktree_base

            orphan_glob = str(worktree_base(cfg.repo) / "wt-loop-*")
        else:
            orphan_glob = "/tmp/wt-loop-*"
        orphans = sorted(glob.glob(orphan_glob))
        if orphans:
            line(
                "yellow",
                f"{len(orphans)} orphan worktree(s) under {orphan_glob}",
                "the runner reaps these at next boot",
            )
        else:
            line("green", "no orphan worktrees")

        if cfg is not None:
            try:
                local = _sp.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=cfg.repo,
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout.strip()
                _sp.run(
                    ["git", "fetch", "origin", "trunk", "--quiet"],
                    cwd=cfg.repo,
                    capture_output=True,
                    timeout=10,
                )
                remote = _sp.run(
                    ["git", "rev-parse", "origin/trunk"],
                    cwd=cfg.repo,
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout.strip()
                if local and remote and local == remote:
                    line("green", f"code matches origin/trunk @ {local[:8]}")
                elif local and remote:
                    line(
                        "yellow",
                        "local trunk behind origin",
                        f"local={local[:8]} origin={remote[:8]}",
                    )
                else:
                    line("yellow", "could not compare to origin/trunk")
            except (_sp.SubprocessError, OSError):
                line("yellow", "git probe failed")

        from forge_loop.settings import Settings as _Settings  # noqa: PLC0415

        try:
            drift_halt_opt_in = _Settings.load().deploy.drift_halt
        except Exception:  # noqa: BLE001 — doctor must keep running even if cfg fails
            drift_halt_opt_in = False
        line(
            "yellow" if drift_halt_opt_in else "green",
            "deploy-drift halt",
            "ENABLED (opt-in)" if drift_halt_opt_in else "disabled (default)",
        )

        # ---- Durable control-plane checks (issue #202) -------------------
        # These probe the event log / frontier / memory / task state that
        # lives *inside* ``.forge`` — the part ``doctor`` was previously blind
        # to. Read-only: the replay probe re-projects into a throwaway target
        # and never advances the live cursor.
        if cfg_ok and cfg is not None:
            try:
                control_plane = collect_control_plane_doctor(
                    Path(getattr(cfg, "repo", cfg.state_dir)),
                    datetime.now(UTC),
                    state_dir=Path(cfg.state_dir),
                )
            except Exception as exc:  # noqa: BLE001 — doctor must never crash
                control_plane = unavailable_checks(
                    f"control-plane probe errored (treated as not-applicable): {exc}"
                )
        else:
            control_plane = unavailable_checks("config load failed; cannot locate .forge stores")

        # Mutation-survivor probe (issue #380): how many planted faults survive
        # the oracle on the configured high-risk module. The real checker is
        # wired by #379; until then this degrades to ``warn`` (count=None).
        control_plane["mutation_survivors"] = mutation_survivors_check(None)

        _cp_marker = {_CP_PASS: "green", "warn": "yellow", _CP_FAIL: "red"}
        for name, result in control_plane.items():
            detail = result["detail"]
            remediation = result["remediation"]
            if remediation:
                detail = f"{detail}  →  {remediation}"
            label = f"control-plane: {name}"
            table.add_row(_STATUS_MARKERS[_cp_marker[result["status"]]], label, detail)
            if result["status"] == _CP_FAIL:
                red = True

        if want_json:
            import json as _json

            payload = {
                "ok": not red,
                "checks": json_checks,
                "control_plane": control_plane,
            }
            sys.stdout.write(_json.dumps(payload, indent=2, default=str) + "\n")
            return 1 if red else 0

        console.print(table)
        return 1 if red else 0

    def _cmd_pause(self, _args: SimpleNamespace) -> int:
        cfg, _config_error = self.operator_cfg()
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        cfg.pause_file.touch()
        typer.echo(f"[pause] touched {cfg.pause_file}")
        return 0

    def _cmd_resume(self, _args: SimpleNamespace) -> int:
        cfg, _config_error = self.operator_cfg()
        if cfg.pause_file.exists():
            cfg.pause_file.unlink()
        typer.echo(f"[resume] cleared {cfg.pause_file}")
        return 0

    def _cmd_stop(self, _args: SimpleNamespace) -> int:
        cfg, _config_error = self.operator_cfg()
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        cfg.stop_file.touch()
        typer.echo(f"[stop] touched {cfg.stop_file}")
        return 0

    def _cmd_dashboard(self, args: SimpleNamespace) -> int:
        """Start the operator dashboard.

        ``--web`` (default for back-compat) launches the FastAPI + HTMX app.
        ``--tui`` launches the new Textual TUI from ``cli_tui.py``.
        """
        mode = getattr(args, "mode", "web")
        if mode == "tui":
            from forge_loop import cli_tui

            cfg = self.load()
            return cli_tui.run_tui(state_dir=cfg.state_dir, events_file=cfg.events_file)

        from forge_loop.dashboard.app import DashboardBindError
        from forge_loop.dashboard.app import serve as _serve

        cfg = self.load()
        from forge_loop.settings import Settings as _Settings  # noqa: PLC0415

        _dash = _Settings.load().dashboard
        host = args.host or "127.0.0.1"
        port = int(args.port or _dash.port)
        roles_dir = Path(args.roles_dir) if args.roles_dir else cfg.repo / "roles"
        try:
            _serve(
                host=host,
                port=port,
                state_dir=cfg.state_dir,
                roles_dir=roles_dir,
                token=_dash.token or None,
            )
        except DashboardBindError as exc:
            sys.stderr.write(f"dashboard: {exc}\n")
            return 2
        return 0

    def _cmd_mcp_serve(self, _args: SimpleNamespace) -> int:
        from forge_loop.mcp_server import serve_stdio

        return serve_stdio()

    def _cmd_init(self, args: SimpleNamespace) -> int:
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
        for outcome in result.get("precommit", []):
            typer.echo(f"  · {outcome}")
        for hint in result.get("precommit_hint", []):
            typer.echo(f"    hint: {hint}")

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
