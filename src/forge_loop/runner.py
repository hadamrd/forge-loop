"""Main loop body — orchestrates ticks (pick → dispatch → wait → maybe-redeploy)."""

from __future__ import annotations

import contextlib
import json
import signal
import subprocess
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

from forge_loop import attempts as _attempts
from forge_loop import master_log as _mlog
from forge_loop.config import Config
from forge_loop.critic import review_pr as _critic_review
from forge_loop.deploy import redeploy
from forge_loop.gh import fetch_issue, top_issues
from forge_loop.maintenance import run_maintenance
from forge_loop.po import expand_thin_specs as _po_expand
from forge_loop.state import append_event, consolidate_sprint, write_state
from forge_loop.worker import WorkerOutcome, run_worker

_RUN = True

# Drift detector — keep last 3 tick outcomes' summary tuples
# Each entry: (had_workers: bool, all_failed: bool, error_signature: str)
_RECENT_OUTCOMES: deque[tuple[bool, bool, str]] = deque(maxlen=3)


def _reap_worktree(repo: Path, issue: int) -> None:
    """Force-remove a worker's worktree after success. Best-effort."""
    wt = Path(f"/tmp/wt-loop-{issue}")
    if not wt.exists():
        return
    # The planted .claude/ is locked read-only (chmod 555). Unlock first.
    claude_dir = wt / ".claude"
    if claude_dir.exists():
        subprocess.run(
            ["chmod", "-R", "u+w", str(claude_dir)],
            capture_output=True,
        )
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt)],
        cwd=repo, capture_output=True,
    )


def _error_signature(outcome_error: str | None, stdout_tail: str) -> str:
    """Reduce a failure to a stable signature for drift detection.

    Looks for known terminal patterns; falls back to first 60 chars of error.
    """
    blob = f"{outcome_error or ''} {stdout_tail or ''}"[:2000].lower()
    patterns = [
        ("exit code 137", "oom-exit-137"),
        ("watchdog_worker_killed", "watchdog-kill"),
        ("worker exceeded", "wall-timeout"),
        ("worktree-create-failed", "worktree-create-fail"),
        ("gh_list_failed", "gh-list-fail"),
        ("permission denied", "permission-denied"),
        ("rate limit", "rate-limit"),
    ]
    for needle, tag in patterns:
        if needle in blob:
            return tag
    if outcome_error:
        return f"err:{outcome_error[:60].strip().lower()}"
    return "unknown"


def _check_drift_and_maybe_halt(cfg: Config) -> bool:
    """Returns True if the loop should halt due to drift."""
    if len(_RECENT_OUTCOMES) < 3:
        return False
    # All 3 must be worker-bearing AND all 3 must have failed AND same signature
    sigs = {sig for had_w, all_failed, sig in _RECENT_OUTCOMES if had_w and all_failed}
    if len(sigs) == 1 and all(had_w and all_failed for had_w, all_failed, _ in _RECENT_OUTCOMES):
        sig = next(iter(sigs))
        append_event(cfg.events_file, "loop_drift_halt", signature=sig,
                     last_3=list(_RECENT_OUTCOMES))
        # File a loop:halt issue so the operator wakes up to a clear signal.
        title = f"loop: drift halt — 3 ticks in a row failed ({sig})"
        body = (
            f"The sprint loop self-halted at {time.strftime('%Y-%m-%dT%H:%M:%S%z')} "
            f"after 3 consecutive ticks failed with the same signature: `{sig}`.\n\n"
            f"Last 3 outcomes (had_workers, all_failed, signature):\n"
            + "\n".join(f"- {o}" for o in _RECENT_OUTCOMES)
            + "\n\nSee `docs/ops/loop-runner-events.jsonl` for the full trail. "
            "Resolve the root cause and remove the `docs/ops/loop-runner.stop` "
            "file to resume."
        )
        with contextlib.suppress(subprocess.TimeoutExpired, FileNotFoundError):
            subprocess.run(
                ["gh", "issue", "create",
                 "--repo", cfg.github_repo,
                 "--title", title,
                 "--label", "loop:halt",
                 "--body", body],
                capture_output=True, timeout=30,
            )
        # Best-effort push notification via tput-bell + a marker file the
        # operator can grep for.
        with contextlib.suppress(OSError):
            (cfg.state_dir / "loop-runner.HALT").write_text(
                f"drift: {sig}\nseen at: {time.time()}\n"
            )
        cfg.stop_file.touch()
        return True
    return False


def _consecutive_deploy_fails(cfg: Config) -> int:
    """Best-effort scan of the tail of events for consecutive failed redeploys."""
    if not cfg.events_file.exists():
        return 0
    try:
        with open(cfg.events_file) as f:
            lines = f.readlines()[-200:]
    except OSError:
        return 0
    count = 0
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("kind") != "redeploy":
            continue
        if e.get("ok"):
            break
        count += 1
        if count >= 5:  # cap scan
            break
    return count


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


def _tick(cfg: Config, tick: int) -> None:
    # Maintenance ticks: every Nth tick, run the AI-as-PM subagent instead
    # of dispatching workers. The maintenance agent grooms + triages the
    # backlog so subsequent ticks have a clean queue.
    if cfg.maintenance_every_n_ticks > 0 and tick % cfg.maintenance_every_n_ticks == 0:
        write_state(cfg.state_file, {"state": "maintenance", "tick": tick})
        append_event(cfg.events_file, "maintenance_start", tick=tick)
        brief = cfg.briefs.maintenance  # may be None → maintenance.run_maintenance uses default
        outcome = run_maintenance(
            cfg.repo, cfg.logs_dir,
            brief=brief if brief else None,  # type: ignore[arg-type]
        ) if brief else run_maintenance(cfg.repo, cfg.logs_dir)
        append_event(
            cfg.events_file, "maintenance_done", tick=tick,
            acted_on=outcome.acted_on,
            added_ready=outcome.added_ready,
            closed_dupes=outcome.closed_dupes,
            retitled=outcome.retitled,
            duration_s=round(outcome.duration_s, 1),
        )
        write_state(cfg.state_file, {
            "state": "between-ticks", "tick": tick,
            "last_maintenance": {
                "acted_on": outcome.acted_on,
                "added_ready": outcome.added_ready,
            },
        })
        _short_sleep(cfg.tick_interval_s, cfg)
        return

    try:
        issues = top_issues(cfg.labels.ready, cfg.parallel, repo=cfg.github_repo)
    except subprocess.CalledProcessError as e:
        append_event(cfg.events_file, "gh_list_failed", err=(e.stderr or "")[:200])
        write_state(cfg.state_file, {"state": "gh_error", "tick": tick})
        _short_sleep(60, cfg)
        return

    if not issues:
        append_event(cfg.events_file, "tick_idle", tick=tick)
        write_state(
            cfg.state_file,
            {"state": "idle", "tick": tick, "next_check_s": cfg.tick_interval_s},
        )
        _short_sleep(cfg.tick_interval_s, cfg)
        return

    # PO spec-expansion pass (gap: workers ship janitor PRs when issue bodies
    # are thin; the PO subagent rewrites bodies to feature-grade specs before
    # dispatch). Idempotent — issues already expanded carry the marker.
    if cfg.po.enabled:
        write_state(cfg.state_file, {"state": "po_expanding", "tick": tick})
        append_event(cfg.events_file, "po_start", tick=tick,
                     issues=[i["number"] for i in issues])
        po_outcomes = _po_expand(
            issues, cfg.repo, cfg.logs_dir,
            github_repo=cfg.github_repo,
            timeout_s=cfg.po.timeout_s,
            max_to_expand=cfg.po.max_to_expand_per_tick,
        )
        expanded_nums = [o.issue for o in po_outcomes if not o.skipped]
        append_event(
            cfg.events_file, "po_done", tick=tick,
            expanded=expanded_nums,
            skipped=[o.issue for o in po_outcomes if o.skipped],
            outcomes=[{"issue": o.issue, "skipped": o.skipped,
                       "reason": o.reason,
                       "sections_added": o.sections_added,
                       "duration_s": round(o.duration_s, 1),
                       "error": o.error} for o in po_outcomes],
        )
        # Re-fetch any issues whose bodies were just rewritten so the workers
        # see the new spec, not the stale snapshot we captured at tick start.
        if expanded_nums:
            refreshed = []
            for issue in issues:
                if issue["number"] in expanded_nums:
                    fresh = fetch_issue(issue["number"], repo=cfg.github_repo)
                    refreshed.append(fresh or issue)
                else:
                    refreshed.append(issue)
            issues = refreshed

    write_state(
        cfg.state_file,
        {
            "state": "running",
            "tick": tick,
            "dispatched": [{"issue": i["number"], "title": i["title"]} for i in issues],
        },
    )
    append_event(cfg.events_file, "tick_start", tick=tick, issues=[i["number"] for i in issues])

    # Per-issue: detect risk-gate + fetch past attempt history (if enabled).
    risk_gate_label = cfg.labels.risk_gate
    workers_meta: list[dict[str, Any]] = []
    for i in issues:
        labels = [lab.get("name", "") for lab in (i.get("labels") or [])]
        gated = bool(risk_gate_label) and risk_gate_label in labels
        past: list[dict[str, Any]] = []
        if cfg.attempts.enabled:
            past = _attempts.fetch_history(i["number"], repo=cfg.github_repo)
            past = past[-cfg.attempts.max_history_in_brief:] if past else []
        workers_meta.append({"risk_gated": gated, "past_attempts": past})

    # Bus emitter: any thread (runner, watchdog, etc) calls this to push an
    # event into the shared JSONL. Bound to cfg here so workers can wire it
    # through without importing module state.
    def _bus_emit(kind: str, payload: dict[str, Any]) -> None:
        append_event(cfg.events_file, kind, **payload)

    master_log_path = cfg.logs_dir / "master.log"
    _mlog.info(master_log_path,
               f"tick {tick} dispatching {len(issues)} worker(s): "
               f"{[i['number'] for i in issues]}")

    outcomes: list[WorkerOutcome] = []
    with ThreadPoolExecutor(max_workers=cfg.parallel) as ex:
        futures = [
            ex.submit(
                run_worker, i, cfg.repo, cfg.logs_dir, cfg.worker_timeout_s,
                risk_gated=meta["risk_gated"],
                past_attempts=meta["past_attempts"],
                emit=_bus_emit,
                lumen_top_k=cfg.lumen.top_k,
                lumen_test_pattern=cfg.lumen_test_pattern,
                coauthor=cfg.coauthor,
            )
            for i, meta in zip(issues, workers_meta, strict=True)
        ]
        for fut in futures:
            outcomes.append(fut.result())

    for o in outcomes:
        _mlog.info(master_log_path,
                   f"worker #{o.issue} {o.status} ({o.duration_s:.0f}s) "
                   f"pr={o.pr_url or '-'}")

    # Persist this attempt as a GH issue comment (per-issue history grows).
    if cfg.attempts.enabled:
        for o in outcomes:
            try:
                _attempts.record(
                    o.issue, status=o.status, pr_url=o.pr_url,
                    duration_s=o.duration_s,
                    note=(o.error or "")[:200],
                    event_count=len(o.events or []),
                    repo=cfg.github_repo,
                )
            except Exception as ex_:  # don't fail tick on history-write error
                append_event(cfg.events_file, "attempt_record_failed",
                             issue=o.issue, err=str(ex_)[:200])

    # Critic agent: review PRs the workers opened, before auto-merge fires.
    if cfg.critic.enabled:
        for o in outcomes:
            if o.status in {"open", "merged"} and o.pr_url:
                try:
                    critic_outcome = _critic_review(
                        o.pr_url, o.issue,
                        cfg.repo, cfg.logs_dir,
                        timeout_s=cfg.critic.timeout_s,
                    )
                    append_event(
                        cfg.events_file, "critic_done",
                        issue=o.issue, pr=o.pr_url,
                        verdict=critic_outcome.verdict,
                        reasons=critic_outcome.reasons,
                        duration_s=round(critic_outcome.duration_s, 1),
                    )
                except Exception as ex_:
                    append_event(cfg.events_file, "critic_failed",
                                 issue=o.issue, err=str(ex_)[:200])

    merged_nums = [o.issue for o in outcomes if o.status == "merged"]
    append_event(
        cfg.events_file,
        "tick_done",
        tick=tick,
        merged=merged_nums,
        outcomes=[asdict(o) for o in outcomes],
    )
    write_state(
        cfg.state_file,
        {
            "state": "redeploying" if merged_nums else "finishing-tick",
            "tick": tick,
            "outcomes": [asdict(o) for o in outcomes],
        },
    )

    # Post-merge: reap each merged worker's worktree (gap #2 — they were piling
    # up. The next attempt's _prep_worktree would clean them, but only on
    # collision; successful merges left them dangling.)
    for o in outcomes:
        if o.status == "merged":
            _reap_worktree(cfg.repo, o.issue)
            append_event(cfg.events_file, "worktree_reaped", issue=o.issue)

    if merged_nums:
        ok, log = redeploy(cfg.repo, cfg.deploy_task)
        append_event(cfg.events_file, "redeploy", task=cfg.deploy_task, ok=ok, detail=log)
        # Deploy-fail escalation: 3 in a row → halt (gap #6).
        if not ok and _consecutive_deploy_fails(cfg) >= 3:
            append_event(cfg.events_file, "deploy_drift_halt",
                         consecutive_fails=_consecutive_deploy_fails(cfg))
            with contextlib.suppress(OSError):
                (cfg.state_dir / "loop-runner.HALT").write_text(
                    "deploy: 3 consecutive failures\n"
                )
            cfg.stop_file.touch()

    # Drift detector (gap #3): record outcome signature, halt if 3-in-a-row.
    had_workers = bool(outcomes)
    all_failed = had_workers and all(o.status not in {"merged", "open"} for o in outcomes)
    sig = "ok" if not all_failed else _error_signature(
        outcomes[0].error if outcomes else None,
        outcomes[0].stdout_tail if outcomes else "",
    )
    _RECENT_OUTCOMES.append((had_workers, all_failed, sig))
    if _check_drift_and_maybe_halt(cfg):
        return

    # End-of-tick consolidation — write a 1-line summary, flush noisy events.
    summary = consolidate_sprint(
        cfg.events_file,
        cfg.summaries_file,
        tick,
        [asdict(o) for o in outcomes],
    )
    append_event(cfg.events_file, "sprint_consolidated", **summary)

    write_state(cfg.state_file, {"state": "between-ticks", "tick": tick, "last_summary": summary})
    _short_sleep(cfg.tick_interval_s, cfg)


def run(cfg: Config) -> int:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    cfg.events_file.touch()

    _install_signal_handlers(cfg)

    append_event(
        cfg.events_file,
        "loop_start",
        parallel=cfg.parallel,
        tick_interval=cfg.tick_interval_s,
        max_ticks=cfg.max_ticks,
        label=cfg.labels.ready,
    )
    write_state(cfg.state_file, {"state": "starting", "tick": 0, "parallel": cfg.parallel})

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

        _tick(cfg, tick)

    write_state(cfg.state_file, {"state": "stopped", "tick": tick})
    append_event(cfg.events_file, "loop_stop", tick=tick)
    return 0
