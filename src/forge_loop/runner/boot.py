"""Signal handlers, version-check / self-restart, orphan worktree reaper,
pipeline validation, and the top-level ``run`` / ``run_async`` entry points.

Extracted from ``runner/__init__.py`` (issue #50). Pure mechanical move:
no behaviour change, no signature change.
"""

from __future__ import annotations

import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from forge_loop.config import Config
from forge_loop.runner._helpers import (
    installed_version as _installed_version,
)
from forge_loop.runner._helpers import (
    reap_orphan_worktrees as _reap_orphan_worktrees_impl,
)
from forge_loop.state import append_event, write_state

_RUN = True


def _reap_orphan_worktrees(repo: Path, events_file: Path) -> int:
    """Backward-compat shim: forwards to ``runner._helpers``."""
    return _reap_orphan_worktrees_impl(repo, events_file)


def _install_signal_handlers(cfg: Config) -> None:
    def _stop(*_: Any) -> None:
        global _RUN
        _RUN = False
        append_event(cfg.events_file, "signal_stop")

    def _pause_toggle(*_: Any) -> None:
        if cfg.pause_file.exists():
            cfg.pause_file.unlink()
            append_event(cfg.events_file, "signal_resume")
        else:
            cfg.pause_file.touch()
            append_event(cfg.events_file, "signal_pause")

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGUSR1, _pause_toggle)


def _short_sleep(seconds: int, cfg: Config) -> None:
    """Sleep but stay responsive to stop/pause signals + touchfiles."""
    for _ in range(seconds):
        if not _RUN or cfg.stop_file.exists() or cfg.pause_file.exists():
            return
        time.sleep(1)


def _validate_pipeline_if_configured(cfg: Config) -> None:
    """Load + validate ``.forge/pipeline.yaml`` at runner startup.

    Soft: if the file is missing we silently skip (legacy hardcoded flow
    remains the default). If it exists but is invalid (cycle, unknown
    role ref, ambiguous after) we emit a ``pipeline_invalid`` event and
    raise — operators should see this at startup, not mid-tick.
    """
    pipeline_yaml = cfg.repo / ".forge" / "pipeline.yaml"
    if not pipeline_yaml.exists():
        return
    try:
        from forge_loop.pipeline import build_dag, load_pipeline
        spec = load_pipeline(pipeline_yaml)
        dag = build_dag(spec)
    except Exception as e:  # noqa: BLE001 — boundary
        append_event(
            cfg.events_file, "pipeline_invalid",
            path=str(pipeline_yaml), error=str(e),
        )
        raise
    append_event(
        cfg.events_file, "pipeline_loaded",
        path=str(pipeline_yaml),
        roles=list(dag.order),
        roots=list(dag.roots),
    )


def run(cfg: Config) -> int:
    from forge_loop.runner.tick import _tick

    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    cfg.events_file.touch()

    _install_signal_handlers(cfg)

    # Boot-time orphan worktree cleanup. /tmp/wt-loop-* should never
    # outlive the loop process; if any are on disk now (operator killed
    # the previous tmux mid-tick, crash, etc.) they would otherwise
    # accumulate forever. The runner is the only owner of these paths.
    _reap_orphan_worktrees(cfg.repo, cfg.events_file)

    # Stamp the installed version so we can detect a self-upgrade
    # (a merged PR bumped our own packaging) and gracefully restart.
    boot_version = _installed_version()

    # Queue bootstrap. Default = in-memory (zero infra, single host).
    # Set LOOP_QUEUE_URL=sqlite:///path/to/queue.db for the durable
    # embedded backend. Multi-host Redis support was removed in #39.
    import os as _os

    from forge_loop.queue import build_queue, default_host_id

    queue_url = _os.environ.get("LOOP_QUEUE_URL")
    queue = build_queue(queue_url)
    host_id = default_host_id()

    append_event(
        cfg.events_file,
        "loop_start",
        parallel=cfg.parallel,
        tick_interval=cfg.tick_interval_s,
        max_ticks=cfg.max_ticks,
        label=cfg.labels.ready,
        runner_id=host_id,
        host_id=host_id,
        distributed=False,
        queue_backend=(queue_url or "memory"),
    )
    write_state(cfg.state_file, {"state": "starting", "tick": 0, "parallel": cfg.parallel})

    # Issue #18 — if `.forge/pipeline.yaml` exists, validate it at startup so
    # the operator sees a clear ValidationError BEFORE we start dispatching.
    # The full chain-driven dispatch is opt-in (see forge_loop.pipeline), so
    # this validation does not change the legacy PO→worker→critic flow.
    _validate_pipeline_if_configured(cfg)

    tick = 0
    while _RUN:
        if cfg.stop_file.exists():
            append_event(cfg.events_file, "stop_file_seen")
            cfg.stop_file.unlink()
            break
        if cfg.pause_file.exists():
            write_state(cfg.state_file, {"state": "paused", "tick": tick})
            time.sleep(15)
            continue

        tick += 1
        if cfg.max_ticks and tick > cfg.max_ticks:
            append_event(cfg.events_file, "max_ticks_reached", tick=tick)
            break

        # Self-upgrade detection: if a merged PR bumped our own package
        # version, the running process is on stale code. Exit cleanly so
        # the all-nighter shim re-execs us against the fresh install.
        # Skipped if boot_version is empty (we never knew our version) or
        # if the current read also returns empty (importlib hiccup).
        if boot_version:
            current_version = _installed_version()
            if current_version and current_version != boot_version:
                append_event(
                    cfg.events_file,
                    "version_changed_restart",
                    boot_version=boot_version,
                    current_version=current_version,
                )
                break

        _tick(cfg, tick)

    write_state(cfg.state_file, {"state": "stopped", "tick": tick})
    append_event(cfg.events_file, "loop_stop", tick=tick)
    # Close SQLite queue if it exposes close(); InMemoryQueue is a no-op.
    close = getattr(queue, "close", None)
    if callable(close):
        close()
    return 0


# ---------------------------------------------------------------------------
# Async orchestrator entry point (issue #7).
# Wires the real PO / worker / critic functions into AsyncOrchestrator and
# loops just like ``run`` above, but ticks dispatch into the pipeline instead
# of running each stage sequentially.
# ---------------------------------------------------------------------------
def run_async(cfg: Config) -> int:
    import asyncio

    from forge_loop import attempts as _attempts
    from forge_loop import gh as _gh
    from forge_loop.critic import review_pr as _critic_review
    from forge_loop.critic_actions import apply_critic_report
    from forge_loop.deploy import redeploy
    from forge_loop.gh import fetch_issue, top_issues
    from forge_loop.po import expand_thin_specs as _po_expand
    from forge_loop.runner._helpers import reap_worktree as _reap_worktree
    from forge_loop.runner.dispatch import _sev_counts
    from forge_loop.runner_async import (
        AsyncPools,
        AsyncQueueCaps,
        run_async_tick,
    )
    from forge_loop.worker import run_worker

    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    cfg.events_file.touch()
    _install_signal_handlers(cfg)

    pools = AsyncPools.from_env(default_worker=cfg.parallel)
    caps = AsyncQueueCaps()

    def _bus_emit(kind: str, payload: dict[str, Any]) -> None:
        append_event(cfg.events_file, kind, **payload)

    append_event(
        cfg.events_file, "loop_start",
        parallel=cfg.parallel, tick_interval=cfg.tick_interval_s,
        max_ticks=cfg.max_ticks, label=cfg.labels.ready,
        orchestrator="async",
        pools={"po": pools.po, "worker": pools.worker, "critic": pools.critic},
    )
    write_state(cfg.state_file, {
        "state": "starting", "tick": 0,
        "orchestrator": "async",
        "pools": {"po": pools.po, "worker": pools.worker, "critic": pools.critic},
    })

    async def _po_fn(issue: dict[str, Any]) -> dict[str, Any]:
        if not cfg.po.enabled:
            return {"issue": issue["number"], "skipped": True, "reason": "po_disabled"}
        outs = await asyncio.to_thread(
            _po_expand, [issue], cfg.repo, cfg.logs_dir,
            github_repo=cfg.github_repo,
            timeout_s=cfg.po.timeout_s,
            max_to_expand=1,
            model=cfg.po.model,
        )
        if not outs:
            return {"issue": issue["number"], "skipped": True, "reason": "po_no_op"}
        o = outs[0]
        return {
            "issue": o.issue, "skipped": o.skipped, "reason": o.reason,
            "sections_added": o.sections_added,
        }

    async def _worker_fn(issue: dict[str, Any], _po: dict[str, Any]) -> dict[str, Any]:
        if not _po.get("skipped"):
            fresh = await asyncio.to_thread(
                fetch_issue, issue["number"], cfg.github_repo,
            )
            if fresh:
                issue = fresh
        labels = [lab.get("name", "") for lab in (issue.get("labels") or [])]
        gated = bool(cfg.labels.risk_gate) and cfg.labels.risk_gate in labels
        past: list[dict[str, Any]] = []
        if cfg.attempts.enabled:
            past = await asyncio.to_thread(
                _attempts.fetch_history, issue["number"], cfg.github_repo,
            )
            past = past[-cfg.attempts.max_history_in_brief:] if past else []
        o = await asyncio.to_thread(
            run_worker, issue, cfg.repo, cfg.logs_dir, cfg.worker_timeout_s,
            risk_gated=gated, past_attempts=past, emit=_bus_emit,
            lumen_top_k=cfg.lumen.top_k,
            lumen_test_pattern=cfg.lumen_test_pattern,
            coauthor=cfg.coauthor,
            model=cfg.worker.model,
            thinking=cfg.worker.thinking,
        )
        return {
            "issue": o.issue, "title": o.title,
            "pr_url": o.pr_url, "status": o.status,
            "duration_s": o.duration_s, "error": o.error,
        }

    async def _critic_fn(wr: dict[str, Any]) -> dict[str, Any]:
        if not cfg.critic.enabled or not wr.get("pr_url"):
            return {"issue": wr.get("issue"), "verdict": "skipped", "reasons": []}
        c = await asyncio.to_thread(
            _critic_review, wr["pr_url"], wr["issue"],
            cfg.repo, cfg.logs_dir, cfg.critic.timeout_s, None, _bus_emit,
            cfg.critic.model,
        )
        if c.report is not None:
            try:
                lines = await asyncio.to_thread(
                    _gh.pr_changed_lines, wr["pr_url"], cfg.github_repo,
                )
                await asyncio.to_thread(
                    apply_critic_report,
                    c.report, wr["pr_url"], lines,
                    cfg.critic.block_on_sev2,
                    cfg.critic.min_findings_for_approve,
                    _gh, cfg.github_repo, _bus_emit,
                )
            except Exception as act_ex:
                append_event(cfg.events_file, "critic_actions_failed",
                             issue=wr.get("issue"), err=str(act_ex)[:200])
        return {
            "issue": wr.get("issue"), "verdict": c.verdict,
            "reasons": c.reasons, "duration_s": c.duration_s,
            "sev_counts": _sev_counts(c),
            "parse_retries": c.parse_retries,
        }

    tick = 0

    async def _one_tick() -> None:
        nonlocal tick
        tick += 1
        try:
            issues = await asyncio.to_thread(
                top_issues, cfg.labels.ready, cfg.parallel, cfg.github_repo,
            )
        except subprocess.CalledProcessError as e:
            append_event(cfg.events_file, "gh_list_failed", err=(e.stderr or "")[:200])
            await asyncio.sleep(min(60, cfg.tick_interval_s))
            return
        if not issues:
            append_event(cfg.events_file, "tick_idle", tick=tick)
            write_state(cfg.state_file, {"state": "idle", "tick": tick})
            await asyncio.sleep(cfg.tick_interval_s)
            return

        append_event(cfg.events_file, "tick_start", tick=tick,
                     issues=[i["number"] for i in issues], orchestrator="async")
        write_state(cfg.state_file, {
            "state": "running", "tick": tick,
            "dispatched": [{"issue": i["number"], "title": i["title"]} for i in issues],
        })
        results, stats = await run_async_tick(
            issues, pools=pools, caps=caps,
            po_fn=_po_fn, worker_fn=_worker_fn, critic_fn=_critic_fn,
            emit=_bus_emit,
            po_timeout_s=float(cfg.po.timeout_s),
            worker_timeout_s=float(cfg.worker_timeout_s),
            critic_timeout_s=float(cfg.critic.timeout_s),
        )
        merged = [r["issue"] for r in results
                  if (r.get("worker") or {}).get("status") == "merged"]
        append_event(cfg.events_file, "tick_done", tick=tick,
                     merged=merged, results=results,
                     stats={
                         "po": stats.po.__dict__,
                         "worker": stats.worker.__dict__,
                         "critic": stats.critic.__dict__,
                     })
        for issue_num in merged:
            _reap_worktree(cfg.repo, issue_num)
        if merged and cfg.deploy_task:
            ok, log = await asyncio.to_thread(redeploy, cfg.repo, cfg.deploy_task)
            append_event(cfg.events_file, "redeploy",
                         task=cfg.deploy_task, ok=ok, detail=log)
        write_state(cfg.state_file, {"state": "between-ticks", "tick": tick})
        await asyncio.sleep(cfg.tick_interval_s)

    async def _main() -> None:
        while _RUN:
            if cfg.stop_file.exists():
                append_event(cfg.events_file, "stop_file_seen")
                cfg.stop_file.unlink()
                break
            if cfg.pause_file.exists():
                write_state(cfg.state_file, {"state": "paused", "tick": tick})
                await asyncio.sleep(15)
                continue
            if cfg.max_ticks and tick >= cfg.max_ticks:
                append_event(cfg.events_file, "max_ticks_reached", tick=tick + 1)
                break
            await _one_tick()

    try:
        asyncio.run(_main())
    finally:
        write_state(cfg.state_file, {"state": "stopped", "tick": tick})
        append_event(cfg.events_file, "loop_stop", tick=tick)
    return 0
