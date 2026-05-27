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
import os
import subprocess
import sys
from datetime import UTC
from pathlib import Path
from typing import Any

from forge_loop.config import load
from forge_loop.runner import run as run_loop
from forge_loop.state import tail_events


def _cmd_run(args: argparse.Namespace) -> int:
    import os as _os

    # Propagate --queue to the runner via env var so the wiring stays
    # localized (runner.py reads LOOP_QUEUE_URL and bootstraps the
    # cluster coordinator). Default of None keeps the historical
    # in-memory behaviour.
    queue_url = getattr(args, "queue", None)
    if queue_url:
        _os.environ["LOOP_QUEUE_URL"] = queue_url

    orch = getattr(args, "orchestrator", "sync")
    if orch == "async":
        from forge_loop.runner import run_async as run_async_loop

        return run_async_loop(load())
    return run_loop(load())


def _cmd_cluster_status(args: argparse.Namespace) -> int:
    """Deprecated: multi-host cluster mode was removed in #39.

    The subcommand is kept as a stub so old scripts get a clear, actionable
    error instead of a silent no-op or AttributeError.
    """

    _ = args
    sys.stderr.write(
        "cluster status: multi-host cluster mode was removed in #39 "
        "(premature distribution; one-operator-one-box is the supported "
        "surface). Use 'forge-loop status' and 'forge-loop events' instead.\n"
    )
    return 2


def _cmd_doctor(_args: argparse.Namespace) -> int:
    """One-shot health check.

    Aggregates the checks an operator typically runs by hand after a
    surprise (a worker stalled, the loop seems quiet, a recent merge).
    Prints a green/yellow/red line per check and exits 0 if all green,
    1 if any red. Yellow is informational and does not affect exit code.
    """
    import glob
    import shutil
    import subprocess as _sp

    # Doctor must run even when config is broken — that's the whole
    # point of running it. Fall back to a minimal stub so the checks
    # that don't need a real cfg still execute.
    try:
        cfg = load()
        cfg_ok = True
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ config load failed: {exc}")
        cfg = None
        cfg_ok = False
    red = not cfg_ok  # config-broken counts as a red signal

    def line(status: str, label: str, detail: str = "") -> None:
        nonlocal red
        marker = {"green": "✓", "yellow": "~", "red": "✗"}[status]
        print(f"  {marker} {label}" + (f" — {detail}" if detail else ""))
        if status == "red":
            red = True

    print("forge-loop doctor:")

    # 1. Halt markers — should NOT exist on a healthy install
    if cfg_ok:
        halt = cfg.state_dir / "loop-runner.HALT"
        stop = cfg.stop_file
        if halt.exists():
            line("red", "halt marker present", f"remove {halt}")
        else:
            line("green", "no halt marker")
        if stop.exists():
            line("yellow", "stop file pending", f"will halt at next tick boundary ({stop})")

    # 2. tmux session — try to find a forge-loop-named one
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

    # 3. Orphan worktrees — anything under /tmp/wt-loop-*
    orphans = sorted(glob.glob("/tmp/wt-loop-*"))
    if orphans:
        line(
            "yellow",
            f"{len(orphans)} orphan worktree(s) under /tmp/wt-loop-*",
            "the runner reaps these at next boot",
        )
    else:
        line("green", "no orphan worktrees")

    # 4. Code freshness — does the local checkout match origin/trunk?
    if cfg_ok:
        try:
            local = _sp.run(
                ["git", "rev-parse", "HEAD"],
                cwd=cfg.repo, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            # Best-effort fetch with a short timeout; offline → just skip.
            _sp.run(
                ["git", "fetch", "origin", "trunk", "--quiet"],
                cwd=cfg.repo, capture_output=True, timeout=10,
            )
            remote = _sp.run(
                ["git", "rev-parse", "origin/trunk"],
                cwd=cfg.repo, capture_output=True, text=True, timeout=5,
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

    # 5. Halt-causing env vars — surface them so the operator knows
    drift_halt_opt_in = os.environ.get("LOOP_DEPLOY_DRIFT_HALT") == "1"
    line(
        "yellow" if drift_halt_opt_in else "green",
        "deploy-drift halt",
        "ENABLED (opt-in)" if drift_halt_opt_in else "disabled (default)",
    )

    return 1 if red else 0


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


def _cmd_dashboard(args: argparse.Namespace) -> int:
    """Start the operator dashboard (FastAPI + HTMX).

    Defaults to ``127.0.0.1`` to avoid accidentally exposing an unauthed
    surface. Override with ``--host 0.0.0.0`` only when a token is set
    via ``LOOP_DASHBOARD_TOKEN`` — the server hard-refuses otherwise.
    """
    import os as _os

    from forge_loop.dashboard.app import DashboardBindError
    from forge_loop.dashboard.app import serve as _serve

    cfg = load()
    host = args.host or "127.0.0.1"
    port = int(args.port or _os.environ.get("LOOP_DASHBOARD_PORT") or 8765)
    roles_dir = Path(args.roles_dir) if args.roles_dir else cfg.repo / "roles"
    try:
        _serve(
            host=host,
            port=port,
            state_dir=cfg.state_dir,
            roles_dir=roles_dir,
            token=_os.environ.get("LOOP_DASHBOARD_TOKEN") or None,
        )
    except DashboardBindError as exc:
        sys.stderr.write(f"dashboard: {exc}\n")
        return 2
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
        print(
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

    print(
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


def _cmd_replay_diff(args: argparse.Namespace) -> int:
    """`forge-loop replay diff --tick N --replay-tick Nr` — side-by-side report."""
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
        print(json.dumps(report, indent=2, default=str))
    else:
        sys.stdout.write(_replay.render_diff_report_text(report))
    return 0


def _default_repos_dir() -> Path:
    """Where the loop expects ``.forge/repos/*.yaml`` to live.

    Defaults to ``<cwd>/.forge/repos`` so the loop home is wherever the
    operator invokes ``forge-loop`` from; override with ``LOOP_REPOS_DIR``.
    """
    import os
    from pathlib import Path

    env = os.environ.get("LOOP_REPOS_DIR")
    return Path(env).expanduser() if env else Path.cwd() / ".forge" / "repos"


def _cmd_repos_list(args: argparse.Namespace) -> int:
    """`forge-loop repos list` — print loaded repos + last tick activity."""
    from forge_loop.multirepo import RepoLoadError, is_disabled, load_repos, validate_checkout

    repos_dir = Path(args.repos_dir) if args.repos_dir else _default_repos_dir()
    try:
        specs = load_repos(repos_dir)
    except RepoLoadError as e:
        sys.stderr.write(f"[repos list] {e}\n")
        return 2

    # Last-activity is read from the sidecar events log so the operator
    # can see the most recent global tick that touched each repo, even
    # across loop restarts.
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
        print(json.dumps({"repos_dir": str(repos_dir), "repos": rows}, indent=2))
        return 0

    print(f"== forge-loop repos ({repos_dir}) ==")
    if not rows:
        print(
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
        print(f"  - {r['name']:<20} {r['github']:<30}{flag_s}")
        print(f"    checkout: {r['checkout']}")
        print(f"    budget/day: ${r['budget_usd_per_day']:.2f}{last_s}")
    return 0


def _cmd_repos_disable(args: argparse.Namespace) -> int:
    from forge_loop.multirepo import (
        RepoLoadError,
        disable_repo,
        load_repos,
    )

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
    print(f"[repos disable] {match.name} → flag at {flag}")
    return 0


def _cmd_repos_enable(args: argparse.Namespace) -> int:
    from forge_loop.multirepo import (
        RepoLoadError,
        enable_repo,
        load_repos,
    )

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
        print(f"[repos enable] cleared disable flag for {match.name}")
    else:
        print(f"[repos enable] {match.name} was not disabled (no-op)")
    return 0


def _cmd_pipeline_show(args: argparse.Namespace) -> int:
    """`forge-loop pipeline show` — print the resolved DAG as ASCII art."""
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
        print(json.dumps(out, indent=2))
        return 0

    print(f"# pipeline: {spec.source_path}")
    print(f"# roots:    {', '.join(dag.roots)}")
    print(f"# order:    {' → '.join(dag.order)}")
    print()
    print(dag.render_ascii())
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
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
        "worker": {"model": cfg.worker.model, "thinking": cfg.worker.thinking},
        "po": {"model": cfg.po.model, "thinking": cfg.po.thinking},
        "critic": {"model": cfg.critic.model, "thinking": cfg.critic.thinking},
    }
    if getattr(args, "json", False):
        print(json.dumps(out, indent=2))
        return 0
    print(json.dumps(out, indent=2))
    return 0


def _cmd_config_models(args: argparse.Namespace) -> int:
    """`forge-loop config models` — print resolved per-role model + thinking.

    Operators set ``LOOP_WORKER_MODEL`` (etc.) and then want a one-shot
    "did it stick?" view that doesn't require restarting the loop. Per
    issue #34 the table shape is fixed: one row per role, model + thinking
    columns. ``--json`` is provided for machine consumers.
    """
    cfg = load()
    rows = [
        ("worker", cfg.worker.model, cfg.worker.thinking),
        ("po", cfg.po.model, cfg.po.thinking),
        ("critic", cfg.critic.model, cfg.critic.thinking),
    ]
    if getattr(args, "json", False):
        print(
            json.dumps(
                {role: {"model": m, "thinking": t} for role, m, t in rows},
                indent=2,
            )
        )
        return 0
    print(f"{'ROLE':<8} {'MODEL':<22} THINKING")
    for role, model, thinking in rows:
        print(f"{role:<8} {model:<22} {thinking}")
    return 0


def _cmd_roles_list(args: argparse.Namespace) -> int:
    """`forge-loop roles list` — print loaded roles + triggers + next firing.

    Reads ``.forge/roles/*.yaml`` under ``--project-dir`` (default cwd) and
    merges with the built-in defaults. Malformed YAMLs are reported as
    warnings (with line/col) but do not abort the listing.
    """
    from pathlib import Path

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
                    "triggers": [
                        {"on": t.on, "filter": t.filter} for t in r.triggers
                    ],
                    "actions": [
                        {"mcp_tools": list(a.mcp_tools), "shell": a.shell}
                        for a in r.actions
                    ],
                    "output_schema": r.output_schema,
                    "source": r.source_path,
                    "next_firing": _next_firing_label(r),
                }
                for r in result.roles
            ],
            "errors": [
                {"source": e.source, "message": e.message} for e in result.errors
            ],
        }
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
        return 0

    if not result.roles:
        sys.stdout.write("(no roles discovered)\n")
    for r in result.roles:
        triggers = (
            ", ".join(
                t.on + (f"[{','.join(f'{k}={v}' for k, v in t.filter.items())}]" if t.filter else "")
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


def _next_firing_label(role) -> str:  # type: ignore[no-untyped-def]
    """Human-readable next-firing hint for the ``roles list`` view.

    The loop is event-driven (no cron), so "next firing" is really
    "what triggers this role." We render it as the soonest possible
    event description.
    """
    if not role.triggers:
        return "manual only"
    on_set = sorted({t.on for t in role.triggers})
    if "tick" in on_set:
        return "every loop tick"
    return "on " + ", ".join(on_set)


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
    p_run.add_argument(
        "--queue",
        default=None,
        help="Queue backend URL. Default: in-memory (single host). "
        "Pass sqlite:///path/to/queue.db for the durable embedded backend.",
    )
    p_run.set_defaults(func=_cmd_run)
    sub.add_parser("status", help="Print current state file").set_defaults(func=_cmd_status)
    sub.add_parser(
        "doctor",
        help="One-shot health check: tmux session, code freshness, orphan worktrees, halt markers",
    ).set_defaults(func=_cmd_doctor)

    p_cluster = sub.add_parser(
        "cluster",
        help="Cluster-mode commands (multi-host runner coordination)",
    )
    cluster_sub = p_cluster.add_subparsers(dest="cluster_cmd", required=True)
    p_cluster_status = cluster_sub.add_parser(
        "status",
        help="List live runners and their current load",
    )
    p_cluster_status.add_argument(
        "--queue",
        required=True,
        help="Redis URL (must match the one runners booted with)",
    )
    p_cluster_status.add_argument("--json", action="store_true")
    p_cluster_status.set_defaults(func=_cmd_cluster_status)

    p_events = sub.add_parser("events", help="Tail the events log")
    p_events.add_argument("-n", type=int, default=30, help="lines to show (default 30)")
    p_events.set_defaults(func=_cmd_events)

    sub.add_parser("pause", help="Touch pause file").set_defaults(func=_cmd_pause)
    sub.add_parser("resume", help="Remove pause file").set_defaults(func=_cmd_resume)
    sub.add_parser("stop", help="Touch stop file").set_defaults(func=_cmd_stop)
    p_config = sub.add_parser("config", help="Print resolved config")
    p_config.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON (default also emits JSON for back-compat)",
    )
    p_config.set_defaults(func=_cmd_config)
    config_sub = p_config.add_subparsers(dest="config_cmd")
    p_config_models = config_sub.add_parser(
        "models",
        help="Print resolved per-role model + thinking-budget (issue #34)",
    )
    p_config_models.add_argument("--json", action="store_true")
    p_config_models.set_defaults(func=_cmd_config_models)

    p_pipe = sub.add_parser(
        "pipeline",
        help="Inspect the role-chain pipeline defined in .forge/pipeline.yaml",
    )
    pipe_sub = p_pipe.add_subparsers(dest="pipeline_cmd", required=True)
    p_pipe_show = pipe_sub.add_parser("show", help="Print the resolved DAG as ASCII art")
    p_pipe_show.add_argument(
        "--config",
        default=None,
        help="Path to pipeline.yaml (default: ./.forge/pipeline.yaml)",
    )
    p_pipe_show.add_argument("--json", action="store_true", help="Emit JSON instead of ASCII art")
    p_pipe_show.set_defaults(func=_cmd_pipeline_show)

    p_repos = sub.add_parser(
        "repos",
        help="Multirepo management: list/enable/disable repos under .forge/repos/",
    )
    repos_sub = p_repos.add_subparsers(dest="repos_cmd", required=True)
    p_repos_list = repos_sub.add_parser("list", help="List loaded repos + last activity")
    p_repos_list.add_argument(
        "--repos-dir",
        default=None,
        help="Override the .forge/repos directory (default: $LOOP_REPOS_DIR or ./.forge/repos)",
    )
    p_repos_list.add_argument("--json", action="store_true")
    p_repos_list.set_defaults(func=_cmd_repos_list)

    p_repos_dis = repos_sub.add_parser("disable", help="Skip a repo until re-enabled")
    p_repos_dis.add_argument("name", help="Repo name (matches the `name:` field)")
    p_repos_dis.add_argument("--reason", default=None)
    p_repos_dis.add_argument("--repos-dir", default=None)
    p_repos_dis.set_defaults(func=_cmd_repos_disable)

    p_repos_en = repos_sub.add_parser("enable", help="Resume processing a disabled repo")
    p_repos_en.add_argument("name")
    p_repos_en.add_argument("--repos-dir", default=None)
    p_repos_en.set_defaults(func=_cmd_repos_enable)

    p_retry = sub.add_parser(
        "retry",
        help="Re-dispatch a worker for an issue (use --force to bypass guards)",
    )
    p_retry.add_argument("--issue", type=int, required=True, help="GitHub issue number")
    p_retry.add_argument(
        "--force", action="store_true", help="Bypass in-flight and cooldown fingerprint guards"
    )
    p_retry.set_defaults(func=_cmd_retry)

    p_dash = sub.add_parser(
        "dashboard",
        help="Run the operator dashboard (HTMX-driven FastAPI app)",
    )
    p_dash.add_argument(
        "--host",
        default=None,
        help="Bind host. Default 127.0.0.1; refuses 0.0.0.0 without LOOP_DASHBOARD_TOKEN.",
    )
    p_dash.add_argument(
        "--port",
        type=int,
        default=None,
        help="Bind port (default 8765 or $LOOP_DASHBOARD_PORT)",
    )
    p_dash.add_argument(
        "--roles-dir",
        default=None,
        help="Directory holding role yaml files (default: <repo>/roles)",
    )
    p_dash.set_defaults(func=_cmd_dashboard)

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
        "--role",
        default="worker",
        choices=("worker",),
        help="Which role to replay (only 'worker' supported per #24 scope)",
    )
    p_replay.add_argument("--brief", help="Path to the new brief file (.md / .tmpl)")
    p_replay.add_argument(
        "--fixtures-dir",
        help="Directory of recorded SDK sessions for zero-cost replay "
        "(expects tick-{N}-issue-{n}.jsonl or issue-{n}.jsonl)",
    )
    p_replay.add_argument(
        "--suffix",
        default="r",
        help="Suffix appended to the original tick id for replay events (default: r)",
    )
    p_replay.add_argument(
        "--dry-plan",
        action="store_true",
        help="Print the assembled invocations and exit without running anything",
    )
    p_replay.set_defaults(func=_cmd_replay)

    p_replay_diff = replay_sub.add_parser(
        "diff",
        help="Side-by-side per-issue report: original tick vs replay tick",
    )
    p_replay_diff.add_argument("--tick", type=int, required=True, help="Original tick")
    p_replay_diff.add_argument(
        "--replay-tick",
        required=True,
        help="Replay tick id (e.g. '42r')",
    )
    p_replay_diff.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of human-readable",
    )
    p_replay_diff.set_defaults(func=_cmd_replay_diff)

    p_roles = sub.add_parser(
        "roles",
        help="Pluggable roles (.forge/roles/*.yaml) — list, inspect, override built-ins",
    )
    roles_sub = p_roles.add_subparsers(dest="roles_cmd", required=True)
    p_roles_list = roles_sub.add_parser(
        "list",
        help="List loaded roles, their triggers, and next firing",
    )
    p_roles_list.add_argument(
        "--project-dir",
        default=None,
        help="Project root (default: cwd). Loads .forge/roles/*.yaml from here.",
    )
    p_roles_list.add_argument("--json", action="store_true")
    p_roles_list.set_defaults(func=_cmd_roles_list)

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
