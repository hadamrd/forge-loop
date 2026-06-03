from __future__ import annotations

# ruff: noqa: F401
import json
import os
import sys
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import typer

from forge_loop.control.status import collect_control_plane_status
from forge_loop.state import tail_events


class StatusCommandsMixin:
    subprocess: Any
    operator_cfg: Any

    def _cmd_status(self, args: SimpleNamespace) -> int:
        """Operator-facing health surface — Rich Panel + Table by default;
        ``--json`` emits a raw machine-parseable blob for scripts.
        """
        from datetime import datetime

        cfg, config_error = self.operator_cfg()
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
        active_workers_by_issue: dict[int, dict[str, Any]] = {}
        terminal_worker_issues: set[int] = set()
        worker_terminal_kinds = {
            "worker_done",
            "worker_failed",
            "worker_skip_in_flight",
            "worker_skip_cooldown",
            "budget_worker_killed",
            "watchdog_worker_killed",
            "worker_completed",
            "worker_merged",
        }
        raw: list[str] = []
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
                issue = e.get("issue") or e.get("issue_number")
                if not isinstance(issue, int):
                    try:
                        issue = int(issue)
                    except (TypeError, ValueError):
                        issue = None
                if isinstance(issue, int):
                    if kind == "worker_start":
                        active_workers_by_issue[issue] = {
                            "issue": issue,
                            "title": e.get("title"),
                            "started_ts": ts,
                            "last_event_ts": ts,
                            "status": "running",
                            "worktree": e.get("worktree"),
                            "log_path": e.get("log_path"),
                        }
                    elif kind in worker_terminal_kinds:
                        terminal_worker_issues.add(issue)
                        active_workers_by_issue.pop(issue, None)
                    elif issue in active_workers_by_issue:
                        active_workers_by_issue[issue]["last_event_ts"] = ts
            for line in raw[-5:]:
                try:
                    e = json.loads(line)
                    last_5_events.append(
                        {"ts": str(e.get("ts", "?")), "kind": str(e.get("kind", "?"))}
                    )
                except json.JSONDecodeError:
                    pass
        active_workers = list(active_workers_by_issue.values())
        runner_stale = state_blob.get("state") == "running" and not pid_alive and not active_workers
        if not active_workers and state_blob.get("state") == "running":
            for entry in state_blob.get("dispatched") or []:
                if (
                    isinstance(entry, dict)
                    and isinstance(entry.get("issue"), int)
                    and entry["issue"] not in terminal_worker_issues
                ):
                    active_workers.append(
                        {
                            "issue": entry["issue"],
                            "title": entry.get("title"),
                            "started_ts": None,
                            "last_event_ts": None,
                            "status": "stale_unconfirmed"
                            if runner_stale
                            else "running_unconfirmed",
                            "worktree": None,
                            "log_path": None,
                        }
                    )
        for worker in active_workers:
            last_ts = worker.get("last_event_ts") or worker.get("started_ts")
            try:
                last_dt = datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                worker["last_event_age_s"] = None
            else:
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=UTC)
                worker["last_event_age_s"] = max(0, int((now - last_dt).total_seconds()))

        # Issue #126 — fetch labels + title alongside number so we can group
        # the open ready-queue by ``axis:*`` label below. Cheap: same call,
        # one additional JSON field.
        queue_depth = 0
        ready_issues: list[dict[str, Any]] = []
        if cfg.github_repo:
            try:
                r = self.subprocess.run(
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
                        "--limit",
                        "200",
                        "--json",
                        "number,title,labels",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if r.returncode == 0:
                    ready_issues = json.loads(r.stdout or "[]")
                    queue_depth = len(ready_issues)
            except (self.subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
                queue_depth = -1
        else:
            queue_depth = -1

        # Group the open ready-queue by axis. Axis filter (--axis) narrows
        # the bucketed view to just the requested slugs — this is the
        # "sanity check before running" surface called out in the spec.
        from forge_loop.axis import UNALIGNED_BUCKET, group_by_axis

        axis_filter = [
            a.strip().lower() for a in (getattr(args, "axis", None) or []) if a and a.strip()
        ]
        grouped_all, unaligned_count = group_by_axis(ready_issues)
        if axis_filter:
            axes_view: dict[str, list[dict[str, Any]]] = {
                k: v for k, v in grouped_all.items() if k in set(axis_filter)
            }
        else:
            axes_view = grouped_all
        # Render shape: {axis_slug: [{number, title}], ...} (drop the heavy
        # ``labels`` blob from the per-issue payload).
        axes_payload: dict[str, list[dict[str, Any]]] = {
            k: [{"number": i.get("number"), "title": i.get("title", "")} for i in v]
            for k, v in axes_view.items()
        }

        payload: dict[str, Any] = {
            "pid": pid_text or None,
            "pid_alive": pid_alive,
            "halt_reason": halt_reason,
            "state": state_blob.get("state"),
            "tick": state_blob.get("tick"),
            "runner_stale": runner_stale,
            "queue_depth": queue_depth,
            "queue_label": cfg.labels.ready,
            "prs_today": prs_today,
            "last_failure": last_failure,
            "last_events": last_5_events,
            "active_workers": sorted(active_workers, key=lambda w: int(w.get("issue") or 0)),
            "events_file": str(cfg.events_file),
            "axes": axes_payload,
            "unaligned_count": unaligned_count,
            "axis_filter": axis_filter,
            "config_ok": config_error is None,
            "config_error": config_error,
            "control_plane": collect_control_plane_status(
                Path(getattr(cfg, "repo", cfg.state_dir)),
                now,
                state_dir=Path(cfg.state_dir),
            ),
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
        if config_error:
            table.add_row("[yellow]config[/yellow]", f"[yellow]{config_error}[/yellow]")
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
        if active_workers:
            workers_text = Text()
            for worker in sorted(active_workers, key=lambda w: int(w.get("issue") or 0)):
                workers_text.append(f"  #{worker.get('issue')} ", style="cyan")
                workers_text.append(str(worker.get("status") or "running"))
                age = worker.get("last_event_age_s")
                if age is not None:
                    workers_text.append(f"  quiet={age}s")
                workers_text.append("\n")
            table.add_row("active workers", workers_text)
        table.add_row("events", str(cfg.events_file))

        # Issue #126 — axis breakdown. Render one row per axis (sorted for
        # deterministic output) showing the issue count, plus a yellow
        # "unaligned" warning iff any open ready-issue has no axis label.
        if axis_filter:
            table.add_row("axis filter", ", ".join(sorted(set(axis_filter))))
        if axes_view:
            axis_lines = Text()
            for ax in sorted(k for k in axes_view if k != UNALIGNED_BUCKET):
                nums = ", ".join(f"#{i.get('number')}" for i in axes_view[ax])
                axis_lines.append(f"  {ax}", style="cyan")
                axis_lines.append(f" ({len(axes_view[ax])})  {nums}\n")
            if UNALIGNED_BUCKET in axes_view:
                unaligned_nums = ", ".join(
                    f"#{i.get('number')}" for i in axes_view[UNALIGNED_BUCKET]
                )
                axis_lines.append(f"  {UNALIGNED_BUCKET}", style="yellow")
                axis_lines.append(f" ({len(axes_view[UNALIGNED_BUCKET])})  {unaligned_nums}\n")
            table.add_row("axes", axis_lines)
        if unaligned_count > 0 and not axis_filter:
            table.add_row(
                "[yellow]warning[/yellow]",
                f"[yellow]{unaligned_count} open issue(s) carry no axis:* label[/yellow]",
            )

        console.print(Panel(table, title="[bold]forge-loop status[/bold]", title_align="left"))
        return 0

    def _cmd_boot(self, args: SimpleNamespace) -> int:
        """Reload the maestro reset-recovery context from durable ``.forge`` state.

        This is the operator (and future maestro) entrypoint for booting from
        explicit durable stores instead of transcript memory: it assembles the
        frontier cursor, curated memory ids, in-flight tasks, and event-log
        position into one compact summary.
        """
        from forge_loop.control.boot import (
            BootContextError,
            assemble_boot_context,
            build_boot_sources,
        )

        cfg, _config_error = self.operator_cfg()
        repo = Path(getattr(cfg, "repo", cfg.state_dir))
        try:
            context = assemble_boot_context(build_boot_sources(repo))
        except BootContextError as exc:
            sys.stderr.write(f"{exc}\n")
            return 1

        if getattr(args, "json", False):
            payload = {
                "frontier": {
                    "product_goal": context.frontier.product_goal,
                    "current_problem": context.frontier.current_problem,
                    "next_expansion": context.frontier.next_expansion,
                    "why_now": context.frontier.why_now,
                },
                "active_memory_ids": list(context.active_memory_ids),
                "rejected_path_memory_ids": list(context.rejected_path_memory_ids),
                "in_flight_task_ids": list(context.in_flight_task_ids),
                "in_flight_saga_ids": list(context.in_flight_saga_ids),
                "stale_saga_ids": list(context.stale_saga_ids),
                "latest_event_sequence": context.latest_event_sequence,
                "projection_cursors": {
                    name: {"sequence": status.sequence, "lag": status.lag}
                    for name, status in context.projection_cursors.items()
                },
            }
            sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")
            return 0

        sys.stdout.write(context.summary() + "\n")
        return 0

    def _cmd_events(self, args: SimpleNamespace) -> int:
        """Tail recent events. Rich-formatted by default; ``--raw`` skips colour."""
        cfg, _config_error = self.operator_cfg()

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
