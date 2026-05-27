"""MCP server — exposes forge-loop capabilities as tools (#979).

Every primary function from the package is registered as an MCP tool so any
MCP client (Claude Desktop, Cursor, codex-cli, etc) can drive sprints + ops.

Top-level tool: ``run_sprint_workflow`` — runs the full loop with the
same env knobs as the CLI's ``run`` subcommand.

Run via:
    forge-loop mcp serve            # stdio transport (default for MCP clients)
"""

from __future__ import annotations

import functools
import os
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from forge_loop import attempts as _attempts
from forge_loop import controlled_exec as _cx
from forge_loop import eventdb as _eventdb
from forge_loop import gh as _gh
from forge_loop import manual as _manual
from forge_loop import operator as _operator
from forge_loop import state as _state
from forge_loop.config import load as load_config
from forge_loop.critic import review_pr as _critic_review
from forge_loop.deploy import redeploy as _redeploy
from forge_loop.maintenance import run_maintenance as _run_maintenance
from forge_loop.runner import run as _run_loop
from forge_loop.worker import run_worker as _run_worker
from forge_loop.worker_logs import read_worker_logs as _read_worker_logs

mcp = FastMCP("forge-loop")


# ── Rate limiting ──────────────────────────────────────────────────────────
# Each MCP server process keeps a per-tool call counter. When a tool's count
# exceeds its cap, subsequent invocations return a structured error and emit
# a `mcp_tool_rate_limited` event on the configured events bus. Resets every
# time the MCP server is started (= per-worker-invocation for the loop's own
# dispatch path).
#
# Defaults:
#   * LOOP_MCP_CAP_DEFAULT (env): default cap for every tool, default 20.
#   * LOOP_MCP_CAP_<TOOL_NAME_UPPER> (env): per-tool override.
#
# Mutating tools (issue/comment/label/dispatch) should be tightened
# explicitly; pure-read tools can stay at the default.

_TOOL_CALLS: Counter[str] = Counter()
_DEFAULT_CAP = int(os.environ.get("LOOP_MCP_CAP_DEFAULT", "20"))


def _cap_for(tool_name: str) -> int:
    env_key = f"LOOP_MCP_CAP_{tool_name.upper()}"
    return int(os.environ.get(env_key, _DEFAULT_CAP))


def _emit_rate_limited(tool_name: str, cap: int, count: int) -> None:
    """Best-effort: write a `mcp_tool_rate_limited` event to the configured
    events bus. Swallows OSError so a broken bus does not crash a tool call.
    """
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001
        return
    try:
        _state.append_event(
            cfg.events_file,
            "mcp_tool_rate_limited",
            tool=tool_name, cap=cap, count=count,
        )
    except OSError:
        pass


def rate_limited(tool_name: str | None = None) -> Callable[..., Any]:
    """Decorator: cap a tool's per-process call count.

    Usage:
        @mcp.tool()
        @rate_limited("gh_create_issue")
        def gh_create_issue(...): ...

    The cap is read at decoration time from env (so tests can override).
    Returns a structured error dict on exceed; never raises.
    """

    def _decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        name = tool_name or fn.__name__
        cap = _cap_for(name)

        @functools.wraps(fn)
        def _wrapper(*args: Any, **kwargs: Any) -> Any:
            _TOOL_CALLS[name] += 1
            count = _TOOL_CALLS[name]
            if count > cap:
                _emit_rate_limited(name, cap, count)
                return {
                    "ok": False,
                    "error": "rate_limited",
                    "tool": name,
                    "cap": cap,
                    "count": count,
                    "hint": f"set LOOP_MCP_CAP_{name.upper()}=N to raise the cap",
                }
            return fn(*args, **kwargs)

        return _wrapper

    return _decorate


# ── GH tools ────────────────────────────────────────────────────────────────


@mcp.tool()
def gh_top_issues(label: str = "loop:ready", limit: int = 3) -> list[dict[str, Any]]:
    """Return open issues carrying ``label`` (oldest first, up to ``limit``)."""
    cfg = load_config()
    return _gh.top_issues(label, limit, repo=cfg.github_repo)


@mcp.tool()
@rate_limited("gh_comment")
def gh_comment(issue: int, body: str) -> str:
    """Post a comment on an issue. Returns ``ok`` or an error description."""
    cfg = load_config()
    _gh.comment(issue, body, repo=cfg.github_repo)
    return "ok"


@mcp.tool()
@rate_limited("gh_unlabel")
def gh_unlabel(issue: int, label: str) -> str:
    """Remove a label from an issue (used to drop ``loop:ready`` after dispatch)."""
    cfg = load_config()
    _gh.unlabel(issue, label, repo=cfg.github_repo)
    return "ok"


@mcp.tool()
@rate_limited("gh_create_issue")
def gh_create_issue(title: str, body: str, labels: list[str] | None = None) -> dict[str, Any]:
    """Open a new issue in the configured repo. Returns ``{number}`` or ``{error}``."""
    cfg = load_config()
    n = _gh.create_issue(title, body, labels, repo=cfg.github_repo)
    return {"number": n} if n is not None else {"error": "gh issue create failed"}


@mcp.tool()
@rate_limited("gh_update_issue")
def gh_update_issue(
    issue: int,
    title: str | None = None,
    body: str | None = None,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
) -> dict[str, Any]:
    """Patch an issue (any combination of title/body/labels). Returns ``{ok}``."""
    cfg = load_config()
    ok = _gh.update_issue(issue, title, body, add_labels, remove_labels, repo=cfg.github_repo)
    return {"ok": ok}


@mcp.tool()
@rate_limited("gh_close_issue")
def gh_close_issue(
    issue: int, reason: str = "completed", comment_body: str | None = None
) -> dict[str, Any]:
    """Close an issue with an optional comment. ``reason`` ∈ {completed, not planned}."""
    cfg = load_config()
    ok = _gh.close_issue(issue, reason, comment_body, repo=cfg.github_repo)
    return {"ok": ok}


# ── Backlog maintenance ─────────────────────────────────────────────────────


@mcp.tool()
@rate_limited("groom_backlog")
def groom_backlog(timeout_s: int = 1800) -> dict[str, Any]:
    """Run one pass of the AI-as-PM backlog-maintenance subagent.

    Triages, retitles, dedupes, closes stale, marks `loop:ready` on well-formed
    P0/P1 issues. Returns the action counts.
    """
    cfg = load_config()
    o = _run_maintenance(cfg.repo, cfg.logs_dir, timeout_s=timeout_s)
    return {
        "acted_on": o.acted_on,
        "added_ready": o.added_ready,
        "closed_dupes": o.closed_dupes,
        "retitled": o.retitled,
        "duration_s": round(o.duration_s, 1),
    }


# ── Deploy tool ─────────────────────────────────────────────────────────────


@mcp.tool()
@rate_limited("redeploy_project")
def redeploy_project(task_name: str | None = None) -> dict[str, Any]:
    """Trigger a redeploy via the configured deploy task.

    Uses ``cfg.deploy_task`` (from LOOP_DEPLOY_TASK or YAML) unless ``task_name``
    is explicitly passed. Returns ``{ok: bool, tail: <last 800 chars of output>}``.
    """
    cfg = load_config()
    ok, tail = _redeploy(cfg.repo, task_name or cfg.deploy_task)
    return {"ok": ok, "tail": tail}


# ── Worker tool ─────────────────────────────────────────────────────────────


@mcp.tool()
@rate_limited("dispatch_worker")
def dispatch_worker(issue_number: int, timeout_s: int = 3600) -> dict[str, Any]:
    """Run a single ``claude -p`` worker against one issue (synchronous).

    Fetches the issue via ``gh``, prepares a worktree, dispatches the worker,
    waits for it to complete, and returns the outcome dict.
    """
    cfg = load_config()
    issues = _gh.top_issues("", limit=1, repo=cfg.github_repo)
    matches = [i for i in issues if i.get("number") == issue_number]
    if not matches:
        # Fall back to a direct GH fetch since `gh issue list` doesn't filter
        # by number — use `gh issue view` instead.
        import json
        import subprocess

        r = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                str(issue_number),
                "--repo",
                cfg.github_repo,
                "--json",
                "number,title,body,labels",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            return {"status": "failed", "error": f"gh view failed: {r.stderr[:200]}"}
        issue = json.loads(r.stdout)
    else:
        issue = matches[0]
    outcome = _run_worker(issue, cfg.repo, cfg.logs_dir, timeout_s)
    return asdict(outcome)


# ── Top-level workflow tool ─────────────────────────────────────────────────


@mcp.tool()
@rate_limited("run_sprint_workflow")
def run_sprint_workflow(
    parallel: int = 3,
    max_ticks: int = 1,
    query_label: str = "loop:ready",
    tick_interval_s: int = 60,
    worker_timeout_s: int = 3600,
) -> dict[str, Any]:
    """Run the sprint loop for ``max_ticks`` ticks, returning a summary.

    THIS IS THE TOP-LEVEL ENTRY for an MCP client. It bundles the GH-issue
    pickup, parallel worker dispatch, and rig redeploy into one tool call.

    Defaults to ``max_ticks=1`` so an MCP call doesn't accidentally run
    forever — pass ``max_ticks=0`` to loop indefinitely (use with caution
    in interactive clients).

    Returns the final state dict (state, tick count, outcomes if any).
    """
    import os

    # Inject env knobs so config.load() picks them up; restore after.
    saved = {
        k: os.environ.get(k)
        for k in [
            "LOOP_PARALLEL",
            "LOOP_MAX_TICKS",
            "LOOP_QUERY_LABEL",
            "LOOP_TICK_INTERVAL_S",
            "LOOP_WORKER_TIMEOUT_S",
        ]
    }
    os.environ["LOOP_PARALLEL"] = str(parallel)
    os.environ["LOOP_MAX_TICKS"] = str(max_ticks)
    os.environ["LOOP_QUERY_LABEL"] = query_label
    os.environ["LOOP_TICK_INTERVAL_S"] = str(tick_interval_s)
    os.environ["LOOP_WORKER_TIMEOUT_S"] = str(worker_timeout_s)
    try:
        cfg = load_config()
        _run_loop(cfg)
        import json

        return json.loads(Path(cfg.state_file).read_text()) if cfg.state_file.exists() else {}
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── Read-only introspection tools ───────────────────────────────────────────


@mcp.tool()
def loop_status() -> dict[str, Any]:
    """Return the current state.json (state, tick, dispatched, outcomes)."""
    import json

    cfg = load_config()
    if not cfg.state_file.exists():
        return {"state": "uninitialised"}
    result: dict[str, Any] = json.loads(cfg.state_file.read_text())
    return result


@mcp.tool()
def worker_logs(
    issue: int,
    kind_filter: str | None = None,
    tail: int = 50,
    attempt: int | None = None,
) -> list[dict[str, Any]]:
    """Tail the per-worker stream-json log for ``issue`` (#63).

    Returns the ``tail`` most-recent events from the worker log under
    ``cfg.logs_dir/worker-<issue>-*.log``. Optionally filtered by event
    ``kind`` (e.g. ``"tool_use"``, ``"assistant_text"``,
    ``"final_result"``, ``"error"``, ``"tool_result"``).

    ``attempt`` selects which run when an issue was retried:
    ``None`` / ``1`` = latest, ``2`` = previous, etc.

    Fat payloads (``tool_use.input``, ``tool_result.content``) are
    truncated to 500 chars per row so the response fits in a typical
    Claude context window. Returns ``[]`` (NOT an error) when no log
    exists yet — the worker may still be starting up.
    """
    cfg = load_config()
    return _read_worker_logs(
        cfg.logs_dir,
        issue,
        kind_filter=kind_filter,
        tail=tail,
        attempt=attempt,
    )


@mcp.tool()
def loop_events(n: int = 30) -> list[dict[str, Any]]:
    """Tail the structured events log (last ``n`` entries)."""
    import json

    cfg = load_config()
    if not cfg.events_file.exists():
        return []
    lines = cfg.events_file.read_text().splitlines()[-n:]
    return [json.loads(line) for line in lines if line.strip()]


# ── Attempt history + critic tools ──────────────────────────────────────────


@mcp.tool()
def attempts_history(issue: int) -> list[dict[str, Any]]:
    """Return the per-issue attempt history (oldest first).

    Each entry is the structured record stashed as a GH comment by past
    worker dispatches: ``{ts, status, pr_url, duration_s, note, event_count}``.
    Use this to learn what's already been tried on an issue before dispatching
    a fresh worker.
    """
    cfg = load_config()
    return _attempts.fetch_history(issue, repo=cfg.github_repo)


@mcp.tool()
def critic_review_pr(pr_url: str, issue_number: int, timeout_s: int = 600) -> dict[str, Any]:
    """Run the critic agent against an open PR. Returns verdict + reasons.

    The critic reads the diff + linked issue, posts a GH review (approve or
    request-changes), and returns its verdict so the caller can decide
    whether to proceed with auto-merge.
    """
    cfg = load_config()
    o = _critic_review(pr_url, issue_number, cfg.repo, cfg.logs_dir, timeout_s=timeout_s)
    return {
        "verdict": o.verdict,
        "reasons": o.reasons,
        "duration_s": round(o.duration_s, 1),
        "error": o.error,
    }


# ── Manual (operator runbook) tools ─────────────────────────────────────────


@mcp.tool()
def manual_topics() -> list[dict[str, str]]:
    """List all manual / runbook topics the AI can consult.

    Each entry is ``{topic, title, path}``. Use ``manual_lookup(topic)`` to
    read a topic's full markdown body, or ``manual_search(query)`` for keyword
    search.
    """
    cfg = load_config()
    return [
        {"topic": e.topic, "title": e.title, "path": str(e.path)}
        for e in _manual.list_topics(cfg.repo)
    ]


@mcp.tool()
def manual_lookup(topic: str) -> dict[str, Any]:
    """Return the full markdown body of one manual topic by key.

    Topic keys are filename stems (e.g. ``secrets``, ``k3s-rig``, ``deploy``).
    Case-insensitive. Returns ``{error}`` if no such topic.
    """
    cfg = load_config()
    e = _manual.lookup(cfg.repo, topic)
    if e is None:
        return {"error": f"no manual entry for topic '{topic}'"}
    return {"topic": e.topic, "title": e.title, "body": e.body, "path": str(e.path)}


@mcp.tool()
def manual_search(query: str, limit: int = 5) -> list[dict[str, str]]:
    """Search manual topics + bodies for ``query`` (case-insensitive substring).

    Returns up to ``limit`` matches, topic-matches ranked above title-matches
    ranked above body-matches.
    """
    cfg = load_config()
    matches = _manual.search(cfg.repo, query, limit=limit)
    return [{"topic": e.topic, "title": e.title, "snippet": e.body[:300]} for e in matches]


# ── Event bus + controlled exec ─────────────────────────────────────────────


@mcp.tool()
def emit_event(kind: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Append a structured event to the loop's bus (loop-runner-events.jsonl).

    Use this from a worker subagent (or any orchestrator agent) to publish
    progress / discovery / blocker signals back to the master. Visible via
    ``loop_events()`` and to anyone tailing the JSONL.

    ``kind`` is a short string label (e.g. ``"investigation_started"``,
    ``"bug_found"``, ``"test_failed"``, ``"pr_opened"``, ``"blocked"``).
    ``payload`` is arbitrary metadata.
    """
    cfg = load_config()
    _state.append_event(cfg.events_file, kind, **(payload or {}))
    return {"ok": True, "kind": kind}


@mcp.tool()
def controlled_exec(
    cmd: list[str],
    timeout_s: int = 60,
    label: str = "exec",
    cwd: str | None = None,
) -> dict[str, Any]:
    """Run a shell command with a MANDATORY timeout + bus emit.

    Use this instead of raw Bash when you want guaranteed-bounded execution.
    The wrapper emits ``controlled_exec_start`` before and
    ``controlled_exec_done`` after to the loop's event bus.

    ``timeout_s`` is clamped to [1, 1800]. A timeout-killed process returns
    ``exit_code=124`` (GNU coreutils convention) and ``timed_out=True``.
    """
    cfg = load_config()

    def _emit(kind: str, p: dict[str, Any]) -> None:
        _state.append_event(cfg.events_file, kind, **p)

    cwd_path = Path(cwd) if cwd else None
    result = _cx.run(
        cmd,
        timeout_s=timeout_s,
        label=label,
        cwd=cwd_path,
        on_start=_emit,
        on_done=_emit,
    )
    return {
        "cmd": result.cmd,
        "label": result.label,
        "exit_code": result.exit_code,
        "duration_s": round(result.duration_s, 2),
        "timed_out": result.timed_out,
        "stdout_tail": result.stdout_tail,
        "stderr_tail": result.stderr_tail,
    }


# ── Operator checkpoint ─────────────────────────────────────────────────────


@mcp.tool()
def ask_operator(
    question: str,
    options: list[str],
    context: str = "",
    timeout_s: int | None = None,
) -> dict[str, Any]:
    """Pause the worker and ask the human operator a question.

    Use this when you hit a HIGH-RISK decision that can't be auto-resolved:
    deleting data, destructive renames, schema-breaking choices, ambiguous
    intent. The loop emits an ``operator_question`` event, posts the
    question via the configured channel(s), and blocks until the operator
    replies with ``/forge-answer <option>`` on the worker's GitHub issue.

    Channels:
    - GitHub issue comment (default; needs ``LOOP_OPERATOR_ISSUE``).
    - Webhook (``LOOP_OPERATOR_WEBHOOK`` set).
    - Slack incoming webhook (``LOOP_OPERATOR_SLACK_WEBHOOK`` set).

    Timeout (default 30min) is overridable via ``LOOP_OPERATOR_TIMEOUT_S``
    or the ``timeout_s`` arg. On timeout, returns
    ``{"status": "timeout", "answer": null}`` — the worker should exit
    ``operator_no_response`` (no PR).
    """
    cfg = load_config()
    issue = _operator.env_issue()
    eff_timeout = timeout_s if timeout_s is not None else _operator.env_timeout_s()
    req = _operator.AskRequest(
        question=question,
        options=options,
        context=context,
        issue=issue,
        repo=cfg.github_repo,
        timeout_s=eff_timeout,
    )

    def _emit(kind: str, payload: dict[str, Any]) -> None:
        _state.append_event(cfg.events_file, kind, **payload)

    try:
        result = _operator.ask(
            req,
            emit=_emit,
            webhook_url=_operator.env_webhook(),
            slack_url=_operator.env_slack(),
        )
    except _operator.OperatorTimeout:
        return {
            "status": "timeout",
            "answer": None,
            "note": (
                f"no operator reply within {eff_timeout}s — worker should "
                "exit operator_no_response (no PR)"
            ),
        }
    except _operator.OperatorError as e:
        return {"status": "error", "answer": None, "error": str(e)}
    return {
        "status": result.status,
        "answer": result.answer,
        "raw_reply": result.raw_reply,
        "elapsed_s": round(result.elapsed_s, 1),
        "posted_channels": result.posted_channels,
    }


# ── Event DB (DuckDB) tools ─────────────────────────────────────────────────


@mcp.tool()
def events_query(sql: str, max_rows: int = 100) -> list[dict[str, Any]]:
    """Run a SELECT statement against the event bus via DuckDB.

    Views available:
    - ``events``    columns vary by event kind; common cols: ``ts``, ``kind``
    - ``summaries`` per-tick consolidated rows: ``ts``, ``tick``, ``merged``, ``failed``, ``pr_urls``, ...

    READ-ONLY: only SELECT/WITH/SHOW/DESCRIBE/PRAGMA accepted.
    Example: ``SELECT kind, COUNT(*) AS n FROM events GROUP BY kind ORDER BY n DESC``.
    """
    cfg = load_config()
    return _eventdb.query(sql, cfg.events_file, cfg.summaries_file, max_rows=max_rows)


@mcp.tool()
def events_recent(
    kind: str | None = None,
    since_minutes: int | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Most-recent events, optionally filtered by ``kind`` + time window."""
    cfg = load_config()
    return _eventdb.recent(
        cfg.events_file,
        kind=kind,
        since_minutes=since_minutes,
        limit=limit,
    )


@mcp.tool()
def events_count_by_kind(since_minutes: int | None = None) -> list[dict[str, Any]]:
    """Group event counts by kind (descending). Optional time window."""
    cfg = load_config()
    return _eventdb.count_by_kind(cfg.events_file, since_minutes=since_minutes)


# ── One-call situational awareness (issue #64) ──────────────────────────────


@mcp.tool()
def loop_snapshot(since_minutes: int = 15) -> dict[str, Any]:
    """Single-call ``what is the loop doing`` snapshot.

    Returns a flat dict with everything an operator (or LLM agent)
    typically needs to debug the loop in one round-trip, replacing the
    usual fan-out of ``loop_status`` + ``events_recent`` + ``gh pr list``
    + ``ls /tmp/wt-loop-*`` + ``attempts_history``.

    Keys (see ``forge_loop.snapshot.build_snapshot`` for the contract):
        - ``tick``, ``state``, ``runner_id`` — from the state file +
          ``loop_start`` event.
        - ``queue_depth`` — count of issues carrying the configured
          ready label.
        - ``in_flight`` — list of ``{issue, branch, worktree,
          last_event_age_s, sdk_log_size}`` per active worker.
        - ``recent_kinds`` — ``{kind: count}`` over the window.
        - ``open_prs`` — ``[{number, title, branch}, ...]`` for the
          configured repo.
        - ``halt_marker`` — ``{present, path, age_s, reason}``.
        - ``last_drift_event`` — most-recent ``*drift*`` event in the
          window (or ``None``).

    SDK log content is intentionally NOT included — use ``worker_logs``
    (separate tool) for that. The payload is sized to fit in a single
    LLM system message.
    """
    from forge_loop.snapshot import build_snapshot

    cfg = load_config()
    return build_snapshot(cfg, since_minutes=since_minutes)


def serve_stdio() -> int:
    """Entry point for ``forge-loop mcp serve``. Runs the MCP server on stdio."""
    mcp.run()
    return 0
