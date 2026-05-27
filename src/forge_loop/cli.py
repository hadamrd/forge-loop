"""CLI entry point — `forge-loop <subcommand>` or `python -m forge_loop <subcommand>`.

Subcommands:
  run       Run the loop in the foreground (the entry the Taskfile detaches via nohup).
  status    Print the current state file.
  events    Tail the events JSONL.
  pause     Touch the pause file.
  resume    Remove the pause file.
  stop      Touch the stop file (graceful).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC
from typing import Any

from forge_loop.config import load
from forge_loop.runner import run as run_loop
from forge_loop.state import tail_events


def _cmd_run(args: argparse.Namespace) -> int:
    orch = getattr(args, "orchestrator", "sync")
    if orch == "async":
        from forge_loop.runner import run_async as run_async_loop
        return run_async_loop(load())
    return run_loop(load())


def _cmd_status(_args: argparse.Namespace) -> int:
    """Operator-facing health surface — concise + scannable."""
    from datetime import datetime

    cfg = load()
    now = datetime.now(UTC)
    today = now.date()

    # Loop process state
    pid_alive = False
    pid_text = ""
    if cfg.pid_file.exists():
        pid_text = cfg.pid_file.read_text().strip()
        try:
            import os as _os
            _os.kill(int(pid_text), 0)
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

    # Walk recent events for: PRs today, last failure, queue depth, last 5 events
    prs_today: list[int] = []
    last_failure: dict[str, Any] | None = None
    last_5_events: list[str] = []
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
                last_failure = {"ts": ts, "kind": kind,
                                "detail": str(e.get("detail") or e.get("err") or "")[:120]}
        for line in raw[-5:]:
            try:
                e = json.loads(line)
                last_5_events.append(f"  {e.get('ts','?')[-9:-1]}  {e.get('kind','?')}")
            except json.JSONDecodeError:
                pass

    queue_depth = 0
    try:
        r = subprocess.run(
            ["gh", "issue", "list",
             "--repo", cfg.github_repo,
             "--label", cfg.labels.ready,
             "--state", "open",
             "--json", "number"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            queue_depth = len(json.loads(r.stdout or "[]"))
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        queue_depth = -1

    # Render
    print("== forge-loop status ==")
    if halt_reason:
        print(f"  HALTED: {halt_reason}")
    print(f"  pid       : {pid_text or '(no pidfile)'} {'(alive)' if pid_alive else '(NOT running)'}")
    print(f"  state     : {state_blob.get('state', '?')}  tick={state_blob.get('tick', '?')}")
    print(f"  queue     : {queue_depth} issues with label '{cfg.labels.ready}'")
    print(f"  PRs today : {len(prs_today)} ({prs_today})" if prs_today else "  PRs today : 0")
    if last_failure:
        print(f"  last fail : {last_failure['ts'][-9:-1]}  {last_failure['kind']}  {last_failure['detail']}")
    if last_5_events:
        print("  last 5 events:")
        for line in last_5_events:
            print(line)
    print(f"  events    : {cfg.events_file}")
    return 0


def _cmd_events(args: argparse.Namespace) -> int:
    cfg = load()
    for line in tail_events(cfg.events_file, n=args.n):
        sys.stdout.write(line)
    return 0


def _cmd_pause(_args: argparse.Namespace) -> int:
    cfg = load()
    cfg.pause_file.touch()
    print(f"[pause] touched {cfg.pause_file}")
    return 0


def _cmd_resume(_args: argparse.Namespace) -> int:
    cfg = load()
    if cfg.pause_file.exists():
        cfg.pause_file.unlink()
    print(f"[resume] cleared {cfg.pause_file}")
    return 0


def _cmd_stop(_args: argparse.Namespace) -> int:
    cfg = load()
    cfg.stop_file.touch()
    print(f"[stop] touched {cfg.stop_file}")
    return 0


def _cmd_mcp_serve(_args: argparse.Namespace) -> int:
    from forge_loop.mcp_server import serve_stdio
    return serve_stdio()


def _cmd_init(args: argparse.Namespace) -> int:
    from pathlib import Path

    from forge_loop import init as _init_mod

    target = Path(args.target).resolve() if args.target else Path.cwd().resolve()
    repo = args.repo or _init_mod.detect_github_repo(target)

    result = _init_mod.init_project(target, github_repo=repo, force=args.force)

    print(f"[init] scaffolded forge-loop in {target}")
    print(f"[init] github repo: {repo}")
    for path in result["created"]:
        print(f"  + {path}")
    for path in result["skipped"]:
        print(f"  · skipped (exists; pass --force to overwrite): {path}")

    if args.create_labels:
        created = _init_mod.ensure_labels_via_gh(repo, _init_mod.DEFAULT_LABELS)
        for name in created:
            print(f"  + label: {name}")
        for name, _, _ in _init_mod.DEFAULT_LABELS:
            if name not in created:
                print(f"  · label exists: {name}")

    print()
    print("Next:")
    print("  1. Review forge-loop.yaml")
    print("  2. Add manual entries under manual/")
    print("  3. Label issues with `loop:ready` for the loop to attack")
    print("  4. Run:  forge-loop run        (or: task loop:start)")
    return 0


def _cmd_config(_args: argparse.Namespace) -> int:
    cfg = load()
    out = {
        "repo": str(cfg.repo),
        "parallel": cfg.parallel,
        "tick_interval_s": cfg.tick_interval_s,
        "max_ticks": cfg.max_ticks,
        "query_label": cfg.labels.ready,
        "worker_timeout_s": cfg.worker_timeout_s,
        "state_file": str(cfg.state_file),
        "events_file": str(cfg.events_file),
    }
    print(json.dumps(out, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="forge-loop",
        description="Titan sprint-loop runner",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Run the loop in the foreground")
    p_run.add_argument(
        "--orchestrator", choices=("sync", "async"), default="sync",
        help="Pipeline orchestrator. 'sync' (default, stable) ticks PO→workers→critics "
             "sequentially. 'async' runs three independent asyncio queues so a slow PO "
             "or critic does not block other tickets (see runner_async.py).",
    )
    p_run.set_defaults(func=_cmd_run)
    sub.add_parser("status", help="Print current state file").set_defaults(func=_cmd_status)

    p_events = sub.add_parser("events", help="Tail the events log")
    p_events.add_argument("-n", type=int, default=30, help="lines to show (default 30)")
    p_events.set_defaults(func=_cmd_events)

    sub.add_parser("pause", help="Touch pause file").set_defaults(func=_cmd_pause)
    sub.add_parser("resume", help="Remove pause file").set_defaults(func=_cmd_resume)
    sub.add_parser("stop", help="Touch stop file").set_defaults(func=_cmd_stop)
    sub.add_parser("config", help="Print resolved config").set_defaults(func=_cmd_config)

    p_mcp = sub.add_parser("mcp", help="MCP server (expose tools to MCP clients)")
    mcp_sub = p_mcp.add_subparsers(dest="mcp_cmd", required=True)
    mcp_sub.add_parser("serve", help="Run MCP server on stdio").set_defaults(func=_cmd_mcp_serve)

    p_init = sub.add_parser("init", help="Scaffold forge-loop config in a project")
    p_init.add_argument("--target", help="Target directory (default: cwd)")
    p_init.add_argument("--repo", help="GitHub repo owner/name (auto-detected from git remote)")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing files")
    p_init.add_argument("--create-labels", action="store_true",
                        help="Also create the loop's GH labels via gh CLI")
    p_init.set_defaults(func=_cmd_init)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
