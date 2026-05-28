"""The main ``_tick`` body and its immediate per-tick helpers.

Extracted from ``runner/__init__.py`` (issue #50). Pure mechanical move:
no behaviour change, no signature change.
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

from forge_loop import attempts as _attempts
from forge_loop import master_log as _mlog
from forge_loop import worker as _worker
from forge_loop.config import Config
from forge_loop.deploy import redeploy
from forge_loop.gh import fetch_issue, pr_review_context, prs_by_label, top_issues, unlabel
from forge_loop.maintenance import run_maintenance
from forge_loop.po import expand_thin_specs as _po_expand
from forge_loop.runner._helpers import (
    consume_force_set as _consume_force_set_impl,
)
from forge_loop.runner._helpers import (
    error_signature as _error_signature,
)
from forge_loop.runner._helpers import (
    force_retry_file as _force_retry_file_impl,
)
from forge_loop.runner._helpers import (
    reap_worktree as _reap_worktree,
)
from forge_loop.runner.dispatch import (
    _run_critic_for_outcomes,
    _run_repair_workers,
    _run_workers,
)
from forge_loop.runner.drift import (
    _RECENT_OUTCOMES,
    _check_drift_and_maybe_halt,
    _maybe_deploy_drift_halt,
)
from forge_loop.state import append_event, consolidate_sprint, write_state
from forge_loop.worker import WorkerOutcome


def _force_retry_file(cfg: Config) -> Path:
    return _force_retry_file_impl(cfg.state_dir)


def _consume_force_set(cfg: Config) -> set[int]:
    return _consume_force_set_impl(cfg.state_dir)


def _issue_number_from_pr(pr: dict[str, Any]) -> int | None:
    import re

    for value in (pr.get("headRefName"), pr.get("body"), pr.get("title")):
        if not isinstance(value, str):
            continue
        match = re.search(r"(?:^|/)loop/(\d+)-", value)
        if match:
            return int(match.group(1))
        match = re.search(r"(?:refs?|closes|fixes|resolves)\s+#(\d+)", value, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _blocking_pr_repairs(cfg: Config) -> list[tuple[dict[str, Any], dict[str, Any], str]]:
    repairs: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for pr in prs_by_label("critic:blocking", cfg.parallel, repo=cfg.github_repo):
        issue_num = _issue_number_from_pr(pr)
        if issue_num is None:
            append_event(
                cfg.events_file,
                "repair_pr_skipped",
                pr=pr.get("url"),
                reason="source_issue_not_found",
            )
            continue
        issue = fetch_issue(issue_num, repo=cfg.github_repo)
        if not issue:
            append_event(
                cfg.events_file,
                "repair_pr_skipped",
                pr=pr.get("url"),
                issue=issue_num,
                reason="issue_fetch_failed",
            )
            continue
        repairs.append((issue, pr, pr_review_context(pr["number"], repo=cfg.github_repo)))
    return repairs


_TEST_FILE_GLOBS = (
    "**/test/**",
    "**/tests/**",
    "**/*Test.java",
    "**/*Test.kt",
    "**/*.test.ts",
    "**/*.test.tsx",
    "**/*.test.js",
    "**/*.spec.ts",
    "**/*.spec.tsx",
    "**/*.spec.js",
    "**/*_test.go",
    "**/test_*.py",
)


def _rescue_uncommitted_work(o: WorkerOutcome, cfg: Config) -> str | None:
    """Auto-commit + format + push + open + auto-merge a PR for a dirty worker.

    Zero-touch policy: the operator (CTO) writes the vision + tickets;
    the loop ships the code. Rescue therefore goes ALL THE WAY:

      1. Run the project's format command (``cfg.worker.rescue_format_cmd``,
         e.g. ``./gradlew spotlessApply --no-daemon`` or ``pnpm format``)
         on the worktree so pre-commit gates would pass downstream.
      2. ``git add -A && git commit --no-verify`` (bypasses operator's
         pre-commit hooks — they get checked again at CI).
      3. ``git push -u origin <branch>``.
      4. ``gh pr create`` — DRAFT only if the diff contains NO test files
         (heuristic: worker stopped before writing tests, so likely
         incomplete and needs review). Otherwise READY.
      5. ``gh pr merge <pr> --squash --auto --delete-branch`` so the
         moment CI passes, the loop merges it. The critic gates on
         sev1 via its existing label path.
      6. Labels: ``loop:auto-rescued`` always; ``loop:needs-review``
         only when the diff had no tests (the draft case).

    Returns the PR URL on success, or None when there's nothing to rescue
    or any subprocess failure. Never raises.
    """
    import contextlib
    import fnmatch
    import os as _os
    import subprocess as _sp
    from pathlib import Path as _Path

    wt = _Path(f"/tmp/wt-loop-{o.issue}")
    if not wt.exists():
        return None

    # Uncommitted changes?
    porcelain = _sp.run(
        ["git", "status", "--porcelain"],
        cwd=wt,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if porcelain.returncode != 0 or not porcelain.stdout.strip():
        return None

    # Determine branch.
    branch_r = _sp.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=wt,
        capture_output=True,
        text=True,
        timeout=10,
    )
    branch = branch_r.stdout.strip() if branch_r.returncode == 0 else ""
    if not branch or branch in ("trunk", "main", "HEAD"):
        return None

    # 1. Format the worktree before committing so CI gates don't reject.
    # Configurable per-project via worker.rescue_format_cmd (default
    # empty = skip). Best-effort: any failure here is NON-fatal.
    fmt_cmd_str = getattr(cfg.worker, "rescue_format_cmd", "") or ""
    if fmt_cmd_str.strip():
        with contextlib.suppress(_sp.SubprocessError):
            _sp.run(
                fmt_cmd_str,
                cwd=wt,
                shell=True,
                capture_output=True,
                text=True,
                timeout=300,
                env={**_os.environ, "JAVA_TOOL_OPTIONS": "-Xmx1500m"},
            )

    # 2. Stage + commit (bypass operator pre-commit; CI will re-check).
    commit_msg = (
        f"feat(loop): auto-shipped from worker session — closes #{o.issue}\n"
        "\n"
        "Worker exited its SDK session without running git commit. The\n"
        "loop captured the uncommitted output, applied the configured\n"
        "format command, committed, and pushed. Auto-merge is enabled;\n"
        "CI gates + critic-block-on-sev1 are the merge contract.\n"
        "\n"
        f"Worker status: {o.status}\n"
        f"Worker log: docs/ops/loop-runner-logs/worker-{o.issue}-*.log\n"
    )
    if cfg.coauthor:
        commit_msg += f"\nCo-Authored-By: {cfg.coauthor}\n"

    if _sp.run(["git", "add", "-A"], cwd=wt, capture_output=True, timeout=60).returncode != 0:
        return None
    if (
        _sp.run(
            ["git", "commit", "--no-verify", "-m", commit_msg, "--allow-empty-message"],
            cwd=wt,
            capture_output=True,
            timeout=60,
        ).returncode
        != 0
    ):
        return None
    if (
        _sp.run(
            ["git", "push", "-u", "origin", branch],
            cwd=wt,
            capture_output=True,
            timeout=120,
        ).returncode
        != 0
    ):
        return None

    # 3. Detect test files in the diff to decide DRAFT vs READY.
    diff_r = _sp.run(
        ["git", "diff", "--name-only", f"origin/{cfg.base_branch}"],
        cwd=wt,
        capture_output=True,
        text=True,
        timeout=30,
    )
    changed_files = diff_r.stdout.strip().splitlines() if diff_r.returncode == 0 else []
    has_tests = any(fnmatch.fnmatch(f, g) for f in changed_files for g in _TEST_FILE_GLOBS)

    # 4. Open PR. With tests → READY (loop trusts the work). Without → DRAFT.
    pr_args = [
        "gh",
        "pr",
        "create",
        "--repo",
        cfg.github_repo,
        "--base",
        cfg.base_branch,
        "--head",
        branch,
        "--title",
        f"feat(loop): auto-shipped #{o.issue} — worker session captured",
        "--body",
        (
            f"**Auto-shipped by forge-loop** — worker for #{o.issue} exited its\n"
            "SDK session without committing. The loop captured + formatted +\n"
            "pushed the output. Auto-merge is enabled.\n"
            "\n"
            f"- has tests in diff: **{has_tests}**\n"
            f"- worker status: `{o.status}`\n"
            f"- worker log: `docs/ops/loop-runner-logs/worker-{o.issue}-*.log`\n"
            "\n"
            "Merge gate: CI must pass + critic must not block on sev1.\n"
        ),
        "--label",
        "loop:auto-rescued",
    ]
    if not has_tests:
        # No tests → mark as needing human review and leave draft.
        pr_args.insert(3, "--draft")
        pr_args.extend(["--label", "loop:needs-review"])
    pr_r = _sp.run(pr_args, cwd=wt, capture_output=True, text=True, timeout=60)
    if pr_r.returncode != 0:
        return None
    url = pr_r.stdout.strip().splitlines()[-1] if pr_r.stdout.strip() else ""
    if not url.startswith("https://github.com/"):
        return None

    # 5. If READY, enable auto-merge so the moment CI greens + critic
    # doesn't sev1-block, the merge fires without operator action.
    if has_tests:
        _sp.run(
            [
                "gh",
                "pr",
                "merge",
                url,
                "--squash",
                "--auto",
                "--delete-branch",
            ],
            cwd=wt,
            capture_output=True,
            timeout=60,
        )
        # If --auto isn't available on this repo (branch protection
        # required), fall back to an immediate admin merge so the work
        # lands without manual help.
        # NB: --admin requires the operator's gh token to have admin.
        # Best-effort: failure here just leaves the PR open + auto-merge
        # request live for whenever CI completes.

    return url


def _remove_ready_label(
    cfg: Config,
    issue: int,
    *,
    status: str,
    pr_url: str | None = None,
) -> None:
    try:
        unlabel(issue, cfg.labels.ready, repo=cfg.github_repo)
        append_event(
            cfg.events_file,
            "issue_ready_label_removed",
            issue=issue,
            status=status,
            pr_url=pr_url,
            label=cfg.labels.ready,
        )
    except Exception as ex_:  # do not fail the tick on label hygiene
        append_event(
            cfg.events_file,
            "issue_ready_label_remove_failed",
            issue=issue,
            err=str(ex_)[:200],
        )


def _tick(cfg: Config, tick: int) -> None:
    # Imported lazily to avoid an import cycle (boot.py imports tick.py).
    from forge_loop.runner.boot import _short_sleep

    # Maintenance ticks: every Nth tick, run the AI-as-PM subagent instead
    # of dispatching workers. The maintenance agent grooms + triages the
    # backlog so subsequent ticks have a clean queue.
    if cfg.maintenance_every_n_ticks > 0 and tick % cfg.maintenance_every_n_ticks == 0:
        write_state(cfg.state_file, {"state": "maintenance", "tick": tick})
        append_event(cfg.events_file, "maintenance_start", tick=tick)
        brief = cfg.briefs.maintenance  # may be None → maintenance.run_maintenance uses default
        outcome = (
            run_maintenance(
                cfg.repo,
                cfg.logs_dir,
                brief=brief if brief else None,  # type: ignore[arg-type]
            )
            if brief
            else run_maintenance(cfg.repo, cfg.logs_dir)
        )
        append_event(
            cfg.events_file,
            "maintenance_done",
            tick=tick,
            acted_on=outcome.acted_on,
            added_ready=outcome.added_ready,
            closed_dupes=outcome.closed_dupes,
            retitled=outcome.retitled,
            duration_s=round(outcome.duration_s, 1),
        )
        write_state(
            cfg.state_file,
            {
                "state": "between-ticks",
                "tick": tick,
                "last_maintenance": {
                    "acted_on": outcome.acted_on,
                    "added_ready": outcome.added_ready,
                },
            },
        )
        _short_sleep(cfg.tick_interval_s, cfg)
        return

    def _bus_emit(kind: str, payload: dict[str, Any]) -> None:
        append_event(cfg.events_file, kind, **payload)

    repairs = _blocking_pr_repairs(cfg)
    if repairs:
        master_log_path = cfg.logs_dir / "master.log"
        issue_nums = [issue["number"] for issue, _, _ in repairs]
        write_state(
            cfg.state_file,
            {
                "state": "repairing",
                "tick": tick,
                "dispatched": [
                    {"issue": issue["number"], "title": issue["title"]}
                    for issue, _, _ in repairs
                ],
            },
        )
        append_event(
            cfg.events_file,
            "repair_tick_start",
            tick=tick,
            issues=issue_nums,
            prs=[pr.get("url") for _, pr, _ in repairs],
        )
        _mlog.info(master_log_path, f"tick {tick} repairing blocked PR(s): {issue_nums}")
        outcomes = _run_repair_workers(
            cfg,
            repairs,
            tick,
            master_log_path=master_log_path,
            bus_emit=_bus_emit,
        )
        if cfg.critic.enabled:
            _run_critic_for_outcomes(cfg, outcomes, _bus_emit)
        append_event(
            cfg.events_file,
            "repair_tick_done",
            tick=tick,
            outcomes=[asdict(o) for o in outcomes],
        )
        for o in outcomes:
            if o.status in {"open", "merged"}:
                _reap_worktree(cfg.repo, o.issue)
                append_event(cfg.events_file, "worktree_reaped", issue=o.issue, status=o.status)
        summary = consolidate_sprint(
            cfg.events_file,
            cfg.summaries_file,
            tick,
            [asdict(o) for o in outcomes],
        )
        write_state(
            cfg.state_file,
            {"state": "between-ticks", "tick": tick, "last_summary": summary},
        )
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
        append_event(cfg.events_file, "po_start", tick=tick, issues=[i["number"] for i in issues])
        po_outcomes = _po_expand(
            issues,
            cfg.repo,
            cfg.logs_dir,
            github_repo=cfg.github_repo,
            timeout_s=cfg.po.timeout_s,
            max_to_expand=cfg.po.max_to_expand_per_tick,
            model=cfg.po.model,
            provider=getattr(cfg.po, "provider", "claude"),
        )
        expanded_nums = [o.issue for o in po_outcomes if not o.skipped]
        append_event(
            cfg.events_file,
            "po_done",
            tick=tick,
            expanded=expanded_nums,
            skipped=[o.issue for o in po_outcomes if o.skipped],
            outcomes=[
                {
                    "issue": o.issue,
                    "skipped": o.skipped,
                    "reason": o.reason,
                    "sections_added": o.sections_added,
                    "duration_s": round(o.duration_s, 1),
                    "error": o.error,
                }
                for o in po_outcomes
            ],
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
    # Also apply the fingerprint-based skip guards (in-flight / cooldown) so
    # a half-finished prior dispatch doesn't get re-done and dupe a PR.
    risk_gate_label = cfg.labels.risk_gate
    workers_meta: list[dict[str, Any]] = []
    force_set = _consume_force_set(cfg)
    cooldown_s = _attempts.cooldown_from_env()
    brief_hash = _worker.brief_template_hash()
    issues_to_dispatch: list[dict[str, Any]] = []
    for i in issues:
        labels = [lab.get("name", "") for lab in (i.get("labels") or [])]
        gated = bool(risk_gate_label) and risk_gate_label in labels
        past: list[dict[str, Any]] = []
        corrupt = 0
        if cfg.attempts.enabled:
            past, corrupt = _attempts.fetch_history_strict(
                i["number"],
                repo=cfg.github_repo,
            )
            if corrupt:
                append_event(
                    cfg.events_file,
                    "attempts_corrupt",
                    issue=i["number"],
                    rows=corrupt,
                )
        fp = _attempts.compute_fingerprint(
            i["number"],
            i.get("body") or "",
            brief_hash,
        )
        forced = i["number"] in force_set
        if cfg.attempts.enabled and not forced:
            decision = _attempts.classify_skip(
                past,
                fp,
                cooldown_s=cooldown_s,
            )
            if decision.kind == "in_flight":
                append_event(
                    cfg.events_file,
                    "worker_skip_in_flight",
                    issue=i["number"],
                    pr_url=decision.pr_url,
                    fingerprint=fp[:12],
                    matched_ts=decision.matched_ts,
                )
                _remove_ready_label(
                    cfg,
                    i["number"],
                    status="in_flight",
                    pr_url=decision.pr_url,
                )
                continue
            if decision.kind == "cooldown":
                append_event(
                    cfg.events_file,
                    "worker_skip_cooldown",
                    issue=i["number"],
                    fingerprint=fp[:12],
                    cooldown_remaining_s=decision.cooldown_remaining_s,
                    matched_ts=decision.matched_ts,
                )
                continue
        trimmed = past[-cfg.attempts.max_history_in_brief :] if past else []
        workers_meta.append(
            {
                "risk_gated": gated,
                "past_attempts": trimmed,
                "brief_fingerprint": fp,
                "forced": forced,
            }
        )
        issues_to_dispatch.append(i)
    issues = issues_to_dispatch

    if not issues:
        # All candidates were skipped (in-flight or cooldown). Idle the tick.
        append_event(cfg.events_file, "tick_all_skipped", tick=tick)
        write_state(
            cfg.state_file,
            {"state": "idle", "tick": tick, "next_check_s": cfg.tick_interval_s},
        )
        _short_sleep(cfg.tick_interval_s, cfg)
        return

    master_log_path = cfg.logs_dir / "master.log"
    _mlog.info(
        master_log_path,
        f"tick {tick} dispatching {len(issues)} worker(s): {[i['number'] for i in issues]}",
    )

    # forge-loop assumes Claude Code subscription-mode billing (flat). The
    # per-tick token-cost gate was removed in issue #38: it only made sense
    # under per-token billing, and the implementation was buggy under the
    # subscription operator persona we actually support.
    outcomes: list[WorkerOutcome]
    outcomes, _used_pipeline = _run_workers(
        cfg,
        issues,
        workers_meta,
        tick,
        master_log_path=master_log_path,
        bus_emit=_bus_emit,
    )

    # Issue #78 — worker iteration loop. For each outcome that didn't reach
    # ``merged`` on attempt 1, probe the worker state (DIRTY_NO_COMMIT,
    # COMMITTED_NOT_PUSHED, PUSHED_NO_PR, PR_OPEN_BLOCKED, PR_OPEN_CI_FAILED,
    # PR_OPEN_CONFLICT, PR_OPEN_HEALTHY, CLEAN_NOTHING) and dispatch a
    # focused follow-up worker session — up to ``cfg.worker_max_iterations``
    # attempts. After N attempts without merge, the issue gets labeled
    # ``loop:needs-human``.
    if cfg.worker_max_iterations > 1 and outcomes:
        from forge_loop.runner.iteration import run_iteration_loop

        issue_by_n = {i["number"]: i for i in issues}
        for idx, o in enumerate(list(outcomes)):
            if o.status == "merged":
                continue
            issue = issue_by_n.get(o.issue)
            if issue is None:
                continue
            wt = Path(f"/tmp/wt-loop-{o.issue}")

            def _dispatch_follow_up(_issue: dict[str, Any], brief: str) -> WorkerOutcome:
                """Dispatch one follow-up worker session reusing the worktree."""
                from forge_loop.worker import run_worker

                return run_worker(
                    _issue,
                    cfg.repo,
                    cfg.logs_dir,
                    cfg.worker_timeout_s,
                    risk_gated=False,
                    past_attempts=[],
                    emit=_bus_emit,
                    lumen_top_k=cfg.lumen.top_k,
                    lumen_test_pattern=cfg.lumen_test_pattern,
                    coauthor=cfg.coauthor,
                    tick=tick,
                    model=cfg.worker.model,
                    thinking=cfg.worker.thinking,
                    provider=getattr(cfg.worker, "provider", "claude"),
                    allowed_mcp_servers=cfg.worker.allowed_mcp_tools,
                    load_timeout_ms=cfg.worker.load_timeout_ms,
                    strict_mcp_config=cfg.worker.strict_mcp_config,
                    mcp_servers=cfg.worker.mcp_servers,
                    base_branch=cfg.base_branch,
                    brief_override=brief,
                )

            try:
                new_outcome = run_iteration_loop(
                    o,
                    issue,
                    repo=cfg.github_repo or "",
                    base_branch=cfg.base_branch,
                    worktree=wt,
                    max_iterations=cfg.worker_max_iterations,
                    dispatch_worker=_dispatch_follow_up,
                    emit=_bus_emit,
                    coauthor=cfg.coauthor,
                )
                outcomes[idx] = new_outcome
            except Exception as ex_:  # never fail the tick on iteration loop bugs
                append_event(
                    cfg.events_file,
                    "worker_iteration_failed",
                    issue=o.issue,
                    err=str(ex_)[:200],
                )

    # Persist this attempt as a GH issue comment (per-issue history grows).
    fingerprint_by_issue = {
        i["number"]: meta.get("brief_fingerprint", "")
        for i, meta in zip(issues, workers_meta, strict=True)
    }
    if cfg.attempts.enabled:
        for o in outcomes:
            try:
                _attempts.record(
                    o.issue,
                    status=o.status,
                    pr_url=o.pr_url,
                    duration_s=o.duration_s,
                    note=(o.error or "")[:200],
                    event_count=len(o.events or []),
                    repo=cfg.github_repo,
                    brief_fingerprint=fingerprint_by_issue.get(o.issue, ""),
                )
            except Exception as ex_:  # don't fail tick on history-write error
                append_event(
                    cfg.events_file, "attempt_record_failed", issue=o.issue, err=str(ex_)[:200]
                )

    # Critic agent: review PRs the workers opened, before auto-merge fires.
    # In pipeline-driven mode the critic ran as a chain step already.
    if cfg.critic.enabled and not _used_pipeline:
        _run_critic_for_outcomes(cfg, outcomes, _bus_emit)

    for o in outcomes:
        if o.status in {"open", "merged"} and o.pr_url:
            _remove_ready_label(cfg, o.issue, status=o.status, pr_url=o.pr_url)

    # Issue #65 — pre-merge gate. AFTER the critic has had its say but
    # BEFORE we declare any outcome "merged", re-check that the source
    # issue is still OPEN. An operator who closed it mid-flight
    # (close-as-dup / not-planned / scope-change) wants the loop to STOP,
    # even if the worker raced to the finish. Conservative on gh failure:
    # refuse rather than risk landing a 1300-LOC refactor on a closed
    # ticket.
    from forge_loop import gh as _gh
    from forge_loop.runner.merge_gate import apply_issue_closed_gate

    refused = apply_issue_closed_gate(
        outcomes,
        gh=_gh,
        repo=cfg.github_repo,
        events_file=cfg.events_file,
        emit=_bus_emit,
    )
    if refused:
        _mlog.info(
            master_log_path,
            f"merge gate refused {len(refused)} PR(s) — closed issues: {refused}",
        )

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

    # Post-tick: auto-rescue uncommitted work, then reap.
    #
    # Real failure mode observed in the Titan dogfood:
    # workers consume 50-90 turns writing + editing real implementation +
    # tests, then exit cleanly without ever running ``git commit``.
    # ``final_result.result == ""`` and there's no PR. With the old reap
    # policy the worktree was nuked and the work was lost ($16+ wasted in
    # one night across 3 issues).
    #
    # Fix: BEFORE reaping any non-merged worktree, check if it has
    # uncommitted changes. If yes, the loop AUTO-COMMITS + pushes + opens
    # a draft PR labelled ``loop:needs-review`` so the operator can pick
    # up the work. The outcome's pr_url + status get updated to reflect
    # the rescue. After rescue, the worktree gets reaped normally (the
    # work is on origin).
    _REAPABLE_STATUSES = frozenset({"merged", "open"})
    for o in outcomes:
        if o.status not in _REAPABLE_STATUSES:
            rescued = _rescue_uncommitted_work(o, cfg)
            if rescued is not None:
                # Rescue succeeded — outcome was mutated in place.
                # Treat as "open" so the reap proceeds normally.
                o.status = "open"
                o.pr_url = rescued
                append_event(
                    cfg.events_file,
                    "worker_work_rescued",
                    issue=o.issue,
                    pr=rescued,
                    hint="Worker exited dirty; loop auto-committed + opened draft PR. Review for completeness.",
                )

        if o.status in _REAPABLE_STATUSES:
            _reap_worktree(cfg.repo, o.issue)
            append_event(
                cfg.events_file,
                "worktree_reaped",
                issue=o.issue,
                status=o.status,
            )
        else:
            # Preserve for operator inspection (rescue declined the work —
            # e.g. no uncommitted changes, or push failed).
            wt_path = f"/tmp/wt-loop-{o.issue}"
            append_event(
                cfg.events_file,
                "worktree_preserved",
                issue=o.issue,
                status=o.status,
                path=wt_path,
                hint=(
                    f"Worker exited with status={o.status!r} and auto-rescue "
                    "either found no dirty changes or couldn't push. Inspect "
                    f"{wt_path} manually. Reaped at next loop boot unless "
                    "you `git worktree remove --force` it sooner."
                ),
            )

    if merged_nums and cfg.deploy_task:
        ok, log = redeploy(cfg.repo, cfg.deploy_task)
        append_event(cfg.events_file, "redeploy", task=cfg.deploy_task, ok=ok, detail=log)
        _maybe_deploy_drift_halt(cfg, ok)

    # Drift detector (gap #3): record outcome signature, halt if 3-in-a-row.
    had_workers = bool(outcomes)
    all_failed = had_workers and all(o.status not in {"merged", "open"} for o in outcomes)
    sig = (
        "ok"
        if not all_failed
        else _error_signature(
            outcomes[0].error if outcomes else None,
            outcomes[0].stdout_tail if outcomes else "",
        )
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
