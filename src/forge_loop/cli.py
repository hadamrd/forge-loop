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
                last_failure = {
                    "ts": ts,
                    "kind": kind,
                    "detail": str(e.get("detail") or e.get("err") or "")[:120],
                }
        for line in raw[-5:]:
            try:
                e = json.loads(line)
                last_5_events.append(f"  {e.get('ts', '?')[-9:-1]}  {e.get('kind', '?')}")
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

    # Render
    print("== forge-loop status ==")
    if halt_reason:
        print(f"  HALTED: {halt_reason}")
    print(
        f"  pid       : {pid_text or '(no pidfile)'} {'(alive)' if pid_alive else '(NOT running)'}"
    )
    print(f"  state     : {state_blob.get('state', '?')}  tick={state_blob.get('tick', '?')}")
    print(f"  queue     : {queue_depth} issues with label '{cfg.labels.ready}'")
    print(f"  PRs today : {len(prs_today)} ({prs_today})" if prs_today else "  PRs today : 0")
    if last_failure:
        print(
            f"  last fail : {last_failure['ts'][-9:-1]}  {last_failure['kind']}  {last_failure['detail']}"
        )
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


def _cmd_record_session(args: argparse.Namespace) -> int:
    """Record a real Claude Agent SDK session to a JSONL fixture (test-only).

    Operator-driven counterpart to the test-time SessionReplayer: spawns
    `claude -p` exactly like the loop does, tees stream-json to the fixture,
    writes a trailer with the observed outcome.

    SECRETS are NOT auto-redacted (issue #9 out-of-scope); review before commit.
    """
    from pathlib import Path

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
    print(
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


def _cmd_budget(args: argparse.Namespace) -> int:
    """Print today's spend, current tick spend, top 5 most expensive issues."""
    from forge_loop import budget as _budget

    cfg = load()
    ledger = cfg.spend_ledger

    today = _budget.today_spend(ledger)
    tick_n: int | None = args.tick
    if tick_n is None and cfg.state_file.exists():
        try:
            tick_n = int(json.loads(cfg.state_file.read_text()).get("tick", 0)) or None
        except (json.JSONDecodeError, ValueError, OSError):
            tick_n = None
    tick_total = _budget.tick_spend(ledger, tick_n) if tick_n else 0.0
    top = _budget.top_expensive_issues(ledger, n=5)

    ticket_cap = _budget.ticket_budget_for(None)
    tick_cap = _budget.tick_budget()

    out = {
        "today_usd": round(today, 4),
        "tick": tick_n,
        "tick_usd": round(tick_total, 4),
        "tick_budget_usd": tick_cap,
        "default_ticket_budget_usd": ticket_cap,
        "top_5": [{"issue": n, "cost_usd": round(c, 4)} for n, c in top],
        "ledger": str(ledger),
    }
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    print("== forge-loop budget ==")
    print(f"  today        : ${out['today_usd']:.4f}")
    print(
        f"  tick {out['tick'] or '-':>4}    : ${out['tick_usd']:.4f}"
        f"  (cap ${out['tick_budget_usd']:.2f})"
    )
    print(
        f"  ticket cap   : ${out['default_ticket_budget_usd']:.2f}"
        " (override per-issue with budget:<n> label)"
    )
    if top:
        print("  top 5 issues by spend:")
        for n, c in top:
            print(f"    #{n}  ${c:.4f}")
    else:
        print("  (no spend recorded yet)")
    print(f"  ledger       : {ledger}")
    return 0


def _cmd_retry(args: argparse.Namespace) -> int:
    """Schedule (or force) a re-dispatch for a single issue.

    By default this inspects the fingerprint guards and reports what would
    happen. With ``--force`` it writes a marker the next runner tick consumes
    to bypass both the in-flight and cooldown skips for that issue.
    """
    from forge_loop import attempts as _attempts
    from forge_loop import worker as _worker
    from forge_loop.gh import fetch_issue
    from forge_loop.runner import _force_retry_file

    cfg = load()
    issue = fetch_issue(args.issue, repo=cfg.github_repo)
    if not issue:
        print(f"[retry] could not fetch issue #{args.issue}", file=sys.stderr)
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
        print(f"[retry] warning: {corrupt} corrupt attempt row(s) in history")
    decision = _attempts.classify_skip(
        history,
        fp,
        cooldown_s=_attempts.cooldown_from_env(),
    )
    print(f"[retry] issue #{args.issue} fingerprint={fp[:12]}")
    if decision.kind == "in_flight":
        print(f"[retry] guard: in-flight (PR {decision.pr_url})")
    elif decision.kind == "cooldown":
        print(f"[retry] guard: cooldown ({decision.cooldown_remaining_s}s remaining)")
    else:
        print("[retry] guard: none — next tick will dispatch normally")

    if not args.force:
        if decision.kind:
            print("[retry] pass --force to bypass the guard")
        return 0

    # Write/merge a force-retry marker the runner consumes on its next tick.
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
    print(f"[retry] forced: wrote {marker} (issues={sorted(existing)})")
    return 0


def _cmd_brief(args: argparse.Namespace) -> int:
    """Render a brief template to stdout.

    Lets operators inspect exactly what the loop tells Claude before a
    dispatch, with the same env-overridable loader the runtime uses. The
    rendered output is the literal prompt the subagent would receive.
    """
    from pathlib import Path

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
    else:  # argparse guards this branch
        sys.stderr.write(f"[brief] unknown kind: {kind}\n")
        return 2

    sys.stdout.write(out)
    if not out.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    """`forge-loop replay --tick N --role worker --brief brief.md`

    Re-runs every worker that ran in tick N using the brief loaded from
    ``--brief``. Output is captured as a synthetic replay tick (``Nr``)
    in the events log; ``replay: true`` is stamped on every event so
    downstream consumers can filter.

    Dry-run guarantees: replay NEVER pushes branches or opens PRs —
    fixture-backed invocations replay locally; without a fixture the
    invocation is recorded as ``skipped_no_fixture`` (live worker
    re-dispatch in replay mode is intentionally not wired up here to
    keep the dry-run contract airtight).
    """
    from pathlib import Path

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
        print(json.dumps({
            "original_tick": plan.original_tick,
            "replay_tick": plan.replay_tick,
            "role": plan.role,
            "invocations": [
                {
                    "issue": inv.issue, "title": inv.title,
                    "fixture": str(inv.fixture_path) if inv.fixture_path else None,
                }
                for inv in plan.invocations
            ],
        }, indent=2))
        return 0

    try:
        captures = _replay.run_replay_tick(plan, events_path=cfg.events_file)
    except _replay.ReplayError as exc:
        sys.stderr.write(f"[replay] {exc}\n")
        return 3

    print(json.dumps({
        "original_tick": plan.original_tick,
        "replay_tick": plan.replay_tick,
        "captures": [
            {
                "issue": c.issue, "status": c.status,
                "source": c.source, "cost_usd": c.cost_usd,
                "commit": c.commit_hash, "diff_chars": len(c.diff_text),
                "error": c.error,
            }
            for c in captures
        ],
    }, indent=2))
    return 0


def _cmd_replay_diff(args: argparse.Namespace) -> int:
    """`forge-loop replay diff --tick N --replay-tick Nr` — side-by-side report."""
    from forge_loop import replay as _replay

    cfg = load()
    try:
        report = _replay.build_diff_report(
            cfg.events_file, tick=args.tick, replay_tick=args.replay_tick,
        )
    except _replay.ReplayError as exc:
        sys.stderr.write(f"[replay diff] {exc}\n")
        return 2

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        sys.stdout.write(_replay.render_diff_report_text(report))
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
        "--orchestrator",
        choices=("sync", "async"),
        default="sync",
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

    p_budget = sub.add_parser(
        "budget",
        help="Show today's token spend, current tick spend, top 5 issues",
    )
    p_budget.add_argument(
        "--tick",
        type=int,
        default=None,
        help="Tick number to report (default: current tick from state file)",
    )
    p_budget.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable")
    p_budget.set_defaults(func=_cmd_budget)

    p_retry = sub.add_parser(
        "retry",
        help="Re-dispatch a worker for an issue (use --force to bypass guards)",
    )
    p_retry.add_argument("--issue", type=int, required=True, help="GitHub issue number")
    p_retry.add_argument(
        "--force", action="store_true", help="Bypass in-flight and cooldown fingerprint guards"
    )
    p_retry.set_defaults(func=_cmd_retry)

    p_mcp = sub.add_parser("mcp", help="MCP server (expose tools to MCP clients)")
    mcp_sub = p_mcp.add_subparsers(dest="mcp_cmd", required=True)
    mcp_sub.add_parser("serve", help="Run MCP server on stdio").set_defaults(func=_cmd_mcp_serve)

    p_init = sub.add_parser("init", help="Scaffold forge-loop config in a project")
    p_init.add_argument("--target", help="Target directory (default: cwd)")
    p_init.add_argument("--repo", help="GitHub repo owner/name (auto-detected from git remote)")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing files")
    p_init.add_argument(
        "--create-labels", action="store_true", help="Also create the loop's GH labels via gh CLI"
    )
    p_init.set_defaults(func=_cmd_init)

    p_rec = sub.add_parser(
        "record-session",
        help="Record a real Claude Agent SDK session to a JSONL fixture (test-only)",
    )
    rec_src = p_rec.add_mutually_exclusive_group(required=True)
    rec_src.add_argument("--issue", type=int, help="GitHub issue number to fetch via `gh`")
    rec_src.add_argument("--issue-file", help="Path to a local JSON file with the issue payload")
    p_rec.add_argument("--out", required=True, help="Output fixture path (JSONL)")
    p_rec.add_argument("--worktree", help="Worktree directory (default: cwd)")
    p_rec.add_argument(
        "--timeout", type=int, default=900, help="Subprocess timeout in seconds (default 900)"
    )
    p_rec.set_defaults(func=_cmd_record_session)

    p_brief = sub.add_parser(
        "brief",
        help="Render a brief template (worker/po/critic) to stdout",
    )
    p_brief.add_argument(
        "--kind", required=True, choices=("worker", "po", "critic"), help="Which brief to render"
    )
    p_brief.add_argument(
        "--issue", type=int, default=None, help="GitHub issue number (fetched via `gh issue view`)"
    )
    p_brief.add_argument(
        "--issue-file",
        default=None,
        help="Local JSON file with the issue payload (overrides --issue)",
    )
    p_brief.add_argument(
        "--worktree", default=None, help="Worktree path to use in the worker brief (default: cwd)"
    )
    p_brief.add_argument("--pr", default=None, help="PR URL (critic brief only)")
    p_brief.add_argument("--repo", default=None, help="GitHub repo owner/name (PO brief only)")
    p_brief.add_argument(
        "--risk-gated",
        action="store_true",
        help="Render the risk-gated variant of the worker brief",
    )
    p_brief.add_argument(
        "--raw", action="store_true", help="Print the unrendered template (skip substitution)"
    )
    p_brief.set_defaults(func=_cmd_brief)

    p_replay = sub.add_parser(
        "replay",
        help="Time-travel: re-run a past tick with a modified brief (dry-run, no PRs)",
    )
    replay_sub = p_replay.add_subparsers(dest="replay_cmd")
    # Default action (no subcommand): the actual replay run.
    p_replay.add_argument("--tick", type=int, help="Original tick number to replay")
    p_replay.add_argument(
        "--role", default="worker", choices=("worker",),
        help="Which role to replay (only 'worker' supported per #24 scope)",
    )
    p_replay.add_argument("--brief", help="Path to the new brief file (.md / .tmpl)")
    p_replay.add_argument(
        "--fixtures-dir",
        help="Directory of recorded SDK sessions for zero-cost replay "
             "(expects tick-{N}-issue-{n}.jsonl or issue-{n}.jsonl)",
    )
    p_replay.add_argument(
        "--suffix", default="r",
        help="Suffix appended to the original tick id for replay events (default: r)",
    )
    p_replay.add_argument(
        "--dry-plan", action="store_true",
        help="Print the assembled invocations and exit without running anything",
    )
    p_replay.set_defaults(func=_cmd_replay)

    p_replay_diff = replay_sub.add_parser(
        "diff",
        help="Side-by-side per-issue report: original tick vs replay tick",
    )
    p_replay_diff.add_argument("--tick", type=int, required=True, help="Original tick")
    p_replay_diff.add_argument(
        "--replay-tick", required=True, help="Replay tick id (e.g. '42r')",
    )
    p_replay_diff.add_argument(
        "--json", action="store_true", help="Emit JSON instead of human-readable",
    )
    p_replay_diff.set_defaults(func=_cmd_replay_diff)

    args = parser.parse_args(argv)
    # `replay diff` lands here with replay_cmd="diff"; rewire the func.
    if getattr(args, "cmd", None) == "replay" and getattr(args, "replay_cmd", None) == "diff":
        args.func = _cmd_replay_diff
    elif (
        getattr(args, "cmd", None) == "replay"
        and getattr(args, "replay_cmd", None) is None
        and (args.tick is None or not args.brief)
    ):
        parser.error("replay: --tick and --brief are required (or use `replay diff`)")
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
