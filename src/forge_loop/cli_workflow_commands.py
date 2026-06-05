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


class WorkflowCommandsMixin:
    load: Any

    def _cmd_record_session(self, args: SimpleNamespace) -> int:
        from forge_loop._testing.recorder import SessionRecorder
        from forge_loop.worker import make_brief

        issue: dict[str, Any]
        if args.issue_file:
            issue = json.loads(Path(args.issue_file).read_text())
        else:
            from forge_loop import gh_issues as _gh

            cfg = self.load()
            fetched = _gh.fetch_issue(args.issue, repo=cfg.github_repo)
            if fetched is None:
                sys.stderr.write(f"could not fetch issue #{args.issue}\n")
                return 2
            issue = fetched

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

    def _cmd_retry(self, args: SimpleNamespace) -> int:
        from forge_loop import attempts as _attempts
        from forge_loop import worker as _worker
        from forge_loop.gh_issues import fetch_issue
        from forge_loop.runner import _force_retry_file

        cfg = self.load()
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

    def _cmd_brief(self, args: SimpleNamespace) -> int:
        from forge_loop.briefs import load_template, render_brief

        kind = args.kind

        if args.raw:
            sys.stdout.write(load_template(kind))
            return 0

        issue: dict[str, Any] = {}
        if args.issue_file:
            issue = json.loads(Path(args.issue_file).read_text())
        elif args.issue is not None:
            from forge_loop import gh_issues as _gh

            try:
                cfg = self.load()
                fetched = _gh.fetch_issue(args.issue, repo=cfg.github_repo)
                if fetched is not None:
                    issue = fetched
                else:
                    sys.stderr.write(
                        f"[brief] could not fetch issue #{args.issue}; "
                        "falling back to a placeholder issue.\n"
                    )
            except Exception as e:  # noqa: BLE001 — degrade to placeholder on any failure
                sys.stderr.write(f"[brief] GitHub unavailable ({e}); using placeholder.\n")

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

    def _cmd_replay(self, args: SimpleNamespace) -> int:
        from forge_loop import replay as _replay

        cfg = self.load()
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

    def _cmd_replay_diff(self, args: SimpleNamespace) -> int:
        from forge_loop import replay as _replay

        cfg = self.load()
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
