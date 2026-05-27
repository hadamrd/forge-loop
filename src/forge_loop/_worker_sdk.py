"""SDK-based worker session driver — typed-message stream, no subprocess.

This module is intentionally subprocess-free. It drives the official
Claude Agent SDK (Python) in-process, normalises every typed message into
a ``WorkerEvent`` dict (see :mod:`forge_loop.eventdb`), and returns a
:class:`SDKRunResult` summarising the session.

Splitting this out of :mod:`forge_loop.worker` keeps the SDK execution path
clean of any ``subprocess`` import — the worker's only remaining shell-outs
are the git-worktree management helpers, which now live next to the
backward-compat parsers and never touch the SDK call path.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# NOTE: do NOT import `subprocess` here. The new SDK worker path must be
# subprocess-free (issue #2 acceptance criterion); a unit test enforces it.

EventEmitter = Callable[[dict[str, Any]], None]
BudgetHook = Callable[[float, dict[str, Any], str | None], bool]


@dataclass
class SDKRunResult:
    """Result of a single Claude Agent SDK session."""

    pr_url: str | None
    status: str
    cost_usd: float
    usage: dict[str, Any]
    model: str
    final_result_text: str
    error: str | None
    events: list[dict[str, Any]] = field(default_factory=list)
    duration_s: float = 0.0
    num_turns: int = 0


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _classify_error(exc: BaseException) -> tuple[str, str | None]:
    """Map an exception to ``(error_type, retry_hint)``.

    The SDK surfaces transport-level failures as plain Python exceptions
    whose ``str`` carries the HTTP status. We pattern-match common cases so
    the loop's master log shows actionable categories rather than a bare
    ``RuntimeError``.
    """
    msg = str(exc)
    et = type(exc).__name__
    low = msg.lower()
    if "429" in msg or ("rate" in low and "limit" in low) or "overloaded" in low:
        return ("rate_limit", "retry after exponential backoff (server returned 429)")
    if "401" in msg or "403" in msg or "authentication" in low:
        return ("auth", "check ANTHROPIC_API_KEY / re-login the SDK")
    if "timeout" in low or "timed out" in low:
        return ("timeout", "retry with lower max_turns or a smaller brief")
    if "connection" in low or "network" in low:
        return ("network", "transient transport failure — safe to retry")
    return (et, None)


def _extract_pr_status(final_text: str) -> tuple[str | None, str]:
    """Pull ``(pr_url, status)`` from the worker's final result text.

    The worker brief instructs the agent to emit a trailing JSON object
    like ``{"issue":..., "pr": "...", "status": "merged"}``. Falls back to
    a GitHub PR URL regex if the structured object is missing.
    """
    text = final_text or ""
    for chunk in reversed(text.strip().splitlines()):
        c = chunk.strip()
        if c.startswith("{") and c.endswith("}"):
            try:
                obj = json.loads(c)
            except json.JSONDecodeError:
                continue
            pr = obj.get("pr")
            status = str(obj.get("status", "no_pr"))
            return (pr if isinstance(pr, str) else None, status)
    m = re.search(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+", text)
    if m:
        return (m.group(0), "open")
    return (None, "no_pr")


def _safe_input(v: Any) -> dict[str, Any]:
    if isinstance(v, dict):
        out: dict[str, Any] = {}
        for k, val in v.items():
            if isinstance(val, (int, float, bool)) or val is None:
                out[str(k)] = val
            else:
                out[str(k)] = str(val)[:400]
        return out
    return {"_raw": str(v)[:400]}


def _stringify_tool_result(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if isinstance(blk, dict):
                t = blk.get("text") or blk.get("content") or ""
                parts.append(str(t))
            else:
                parts.append(str(blk))
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _clean_sdk_env() -> dict[str, str]:
    """Env for the SDK-spawned `claude` helper process.

    Even though we're using the typed SDK, under the hood it still launches
    the `claude` CLI as a child process. The CLI refuses to start a fresh
    autonomous session when ``CLAUDECODE=1`` is inherited from a parent
    Claude Code IDE session (see anthropics/claude-code#37442). Strip the
    leaked vars so workers always start in a clean session — same fix the
    legacy subprocess path applied via ``_subagent_env``.
    """
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_SSE_PORT", None)
    return env


async def run_sdk_session(
    prompt: str,
    *,
    cwd: Path,
    max_turns: int = 120,
    env: dict[str, str] | None = None,
    add_dirs: Iterable[Path] = (),
    permission_mode: str = "bypassPermissions",
    on_event: EventEmitter | None = None,
    budget_should_stop: BudgetHook | None = None,
    query_fn: Any = None,
    options_cls: Any = None,
) -> SDKRunResult:
    """Drive one Claude Agent SDK session and stream typed WorkerEvents.

    ``on_event`` receives every event dict (the new typed stream).
    ``budget_should_stop(cost, usage, model)`` is consulted after every
    assistant turn; returning True aborts the iteration with an ``error``
    event of type ``budget_exceeded``.

    ``query_fn`` / ``options_cls`` are injection points for tests — leaving
    them None imports the real ``claude_agent_sdk`` at call time.
    """
    if query_fn is None or options_cls is None:
        from claude_agent_sdk import (  # type: ignore[import-not-found]
            ClaudeAgentOptions as _Opts,
        )
        from claude_agent_sdk import (
            query as _query,
        )
        if query_fn is None:
            query_fn = _query
        if options_cls is None:
            options_cls = _Opts
    from claude_agent_sdk import (  # type: ignore[import-not-found]
        AssistantMessage,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    started = time.time()
    seq = 0
    events: list[dict[str, Any]] = []
    final_text = ""
    cost_usd = 0.0
    usage: dict[str, Any] = {}
    model_seen = ""
    is_error = False
    num_turns = 0
    error_str: str | None = None

    def emit(ev: dict[str, Any]) -> None:
        nonlocal seq
        seq += 1
        ev = {"seq": seq, "ts": _utc_now(), **ev}
        events.append(ev)
        if on_event is not None:
            on_event(ev)

    options = options_cls(
        cwd=str(cwd),
        max_turns=max_turns,
        permission_mode=permission_mode,
        add_dirs=[str(p) for p in add_dirs],
        env=env if env is not None else _clean_sdk_env(),
    )

    try:
        async for message in query_fn(prompt=prompt, options=options):
            if isinstance(message, SystemMessage):
                if getattr(message, "subtype", "") == "init":
                    emit({
                        "kind": "turn_start",
                        "data": dict(getattr(message, "data", {}) or {}),
                    })
                continue
            if isinstance(message, AssistantMessage):
                model_seen = getattr(message, "model", "") or model_seen
                for block in getattr(message, "content", []) or []:
                    if isinstance(block, TextBlock):
                        emit({"kind": "assistant_text", "text": block.text})
                    elif isinstance(block, ToolUseBlock):
                        emit({
                            "kind": "tool_use",
                            "tool": block.name,
                            "input": _safe_input(block.input),
                            "tool_use_id": block.id,
                        })
                if budget_should_stop is not None and budget_should_stop(
                    cost_usd, usage, model_seen,
                ):
                    emit({
                        "kind": "error",
                        "error_type": "budget_exceeded",
                        "message": "ticket budget exceeded",
                        "retry_hint": None,
                    })
                    error_str = "budget_exceeded: ticket budget exceeded"
                    break
                continue
            if isinstance(message, UserMessage):
                content = getattr(message, "content", None)
                blocks = content if isinstance(content, list) else []
                for block in blocks:
                    if isinstance(block, ToolResultBlock):
                        emit({
                            "kind": "tool_result",
                            "tool_use_id": block.tool_use_id,
                            "is_error": bool(block.is_error),
                            "content": _stringify_tool_result(block.content)[:2000],
                        })
                continue
            if isinstance(message, ResultMessage):
                final_text = getattr(message, "result", "") or ""
                cost_usd = float(getattr(message, "total_cost_usd", 0.0) or 0.0)
                usage = dict(getattr(message, "usage", {}) or {})
                is_error = bool(getattr(message, "is_error", False))
                num_turns = int(getattr(message, "num_turns", 0) or 0)
                emit({
                    "kind": "final_result",
                    "result": final_text,
                    "cost_usd": cost_usd,
                    "usage": usage,
                    "model": model_seen,
                    "num_turns": num_turns,
                    "is_error": is_error,
                })
                continue
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        error_type, hint = _classify_error(exc)
        error_str = f"{error_type}: {exc}"
        emit({
            "kind": "error",
            "error_type": error_type,
            "message": str(exc)[:500],
            "retry_hint": hint,
        })

    duration = time.time() - started
    pr_url, status = _extract_pr_status(final_text)
    if error_str is not None and pr_url is None or is_error and pr_url is None:
        status = "failed"
    return SDKRunResult(
        pr_url=pr_url,
        status=status,
        cost_usd=cost_usd,
        usage=usage,
        model=model_seen,
        final_result_text=final_text,
        error=error_str,
        events=events,
        duration_s=duration,
        num_turns=num_turns,
    )
