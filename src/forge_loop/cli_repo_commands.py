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


class RepoCommandsMixin:
    load: Any

    def _default_repos_dir(self) -> Path:
        # Settings-driven (issue #84): was env LOOP_REPOS_DIR, now repo.repos_dir.
        try:
            from forge_loop.settings import Settings as _Settings

            path = _Settings.load().repo.repos_dir
            if path is not None:
                return Path(path).expanduser()
        except Exception:  # noqa: BLE001
            pass
        return Path.cwd() / ".forge" / "repos"

    def _cmd_repos_list(self, args: SimpleNamespace) -> int:
        from forge_loop.multirepo import RepoLoadError, is_disabled, load_repos, validate_checkout

        repos_dir = Path(args.repos_dir) if args.repos_dir else self._default_repos_dir()
        try:
            specs = load_repos(repos_dir)
        except RepoLoadError as exc:
            sys.stderr.write(f"[repos list] {exc}\n")
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

        rows: list[dict[str, Any]] = []
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

    def _cmd_repos_disable(self, args: SimpleNamespace) -> int:
        from forge_loop.multirepo import RepoLoadError, disable_repo, load_repos

        repos_dir = Path(args.repos_dir) if args.repos_dir else self._default_repos_dir()
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

    def _cmd_repos_enable(self, args: SimpleNamespace) -> int:
        from forge_loop.multirepo import RepoLoadError, enable_repo, load_repos

        repos_dir = Path(args.repos_dir) if args.repos_dir else self._default_repos_dir()
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

    def _cmd_pipeline_show(self, args: SimpleNamespace) -> int:
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

    def _cmd_config(self, args: SimpleNamespace) -> int:
        """Print the resolved Settings tree (issue #84).

        Output format:
            --yaml (default)  full resolved Settings as YAML — the single
                              source of truth, round-trippable
            --json            JSON shape (legacy summary) for back-compat
                              scripting
        """
        from forge_loop.settings import Settings

        s = Settings.load()
        if getattr(args, "json", False):
            # Back-compat summary surface — pre-#84 callers that scrape JSON.
            cfg = self.load()
            out = {
                "repo": str(cfg.repo),
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
            typer.echo(json.dumps(out, indent=2))
        else:
            typer.echo(s.dump_yaml())
        return 0

    def _cmd_config_models(self, args: SimpleNamespace) -> int:
        cfg = self.load()
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

    def _cmd_roles_list(self, args: SimpleNamespace) -> int:
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
                        "next_firing": self._next_firing_label(r),
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
                f"    next: {self._next_firing_label(r)}\n"
            )
        for err in result.errors:
            sys.stderr.write(f"warning: {err}\n")
        return 0

    def _next_firing_label(self, role: Any) -> str:
        if not role.triggers:
            return "manual only"
        on_set = sorted({t.on for t in role.triggers})
        if "tick" in on_set:
            return "every loop tick"
        return "on " + ", ".join(on_set)
