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

import contextlib
import json
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forge_loop._sdk_events import (
    AssistantTextEvent,
    AssistantThinkingEvent,
    CostTelemetryEvent,
    ErrorEvent,
    FinalResultEvent,
    SdkEvent,
    SystemMessageEvent,
    ToolResultEvent,
    ToolUseEvent,
    TurnStartEvent,
    WorkerMcpFilteredEvent,
    WorkerMcpFilterNoMatchEvent,
    event_to_record,
)
from forge_loop.sandbox.policy import CapabilityPolicy, mcp_allow_patterns

# NOTE: do NOT import `subprocess` here. The new SDK worker path must be
# subprocess-free (issue #2 acceptance criterion); a unit test enforces it.

# The on-the-wire / on-disk contract stays a JSON dict (one line per event in
# ``events.jsonl``): ``on_event`` receives the serialised record so tailers,
# DuckDB (:mod:`forge_loop.eventdb`) and the worker log keep working unchanged.
# The discriminator is now constructed exclusively through the typed
# :data:`forge_loop._sdk_events.SdkEvent` models (issue #148) — a producer typo
# fails at construction instead of silently shipping a bad ``kind`` string.
EventEmitter = Callable[[dict[str, Any]], None]


class _MissingThinkingBlock:
    """Sentinel type for SDKs that predate ``ThinkingBlock``.

    ``ThinkingBlock`` (the extended-thinking content block) is a newer Claude
    Agent SDK addition. When the installed SDK — or a unit-test fake module —
    doesn't declare it, the producer loop falls back to this class so
    ``isinstance(block, <cls>)`` is always ``False`` and the thinking branch is
    simply skipped, never crashing on a missing import.
    """


# Hard cap on tool definitions injected into the SDK init message
# (issue #60). The bundled allow-list (forge-loop + lumen + github) plus
# the standard built-in tools sits comfortably under this. A regression
# test asserts a session never exceeds the cap; if it does, the filter
# stopped working and the worker is paying for a 250-tool firehose
# again. Bump deliberately if a new allowed server pushes us close.
ALLOWED_TOOL_HARD_CAP: int = 60

# Bundled fallback when the operator-configured allow-list matches zero
# servers actually present in the SDK init message — we emit a
# ``worker_mcp_filter_no_match`` event and use this instead so the
# worker doesn't end up with an empty MCP toolbox.
_BUNDLED_DEFAULT_ALLOWED: tuple[str, ...] = ("forge-loop", "lumen", "github")


def build_allowed_tools_patterns(allowed_servers: Iterable[str]) -> list[str]:
    """Build the SDK ``allowed_tools`` list for a given MCP server allow-list.

    The SDK / Claude Code CLI honours glob-style entries — ``mcp__<server>__*``
    keeps every tool from that server while dropping every tool from servers
    that don't appear. The built-in tools (Read, Bash, etc.) are NOT listed
    here because adding them would *narrow* the worker — leaving
    ``allowed_tools`` empty for the built-in side lets the SDK ship its
    normal Sonnet/Opus tool surface intact.

    Returns a list of ``mcp__<server>__*`` patterns. Deduped, order-preserving.
    """
    seen: set[str] = set()
    out: list[str] = []
    for s in allowed_servers:
        name = str(s).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(f"mcp__{name}__*")
    return out


def resolve_mcp_filter(
    *,
    actual_servers: Iterable[str],
    allow_list: Iterable[str],
    emit: EventEmitter,
    default: Iterable[str] = _BUNDLED_DEFAULT_ALLOWED,
) -> tuple[str, ...]:
    """Compute the effective allow-list and emit diagnostic events.

    - Always emits ``worker_mcp_filtered`` with ``kept`` / ``dropped`` so the
      master log shows which servers were dropped from this session.
    - If the operator-configured allow-list matches NONE of the actually
      loaded servers (typical cause: typo like ``forg-loop``), emit
      ``worker_mcp_filter_no_match`` and fall back to ``default`` so the
      worker isn't left with an empty toolbox.

    Returns the allow-list to apply (server names, deduped).
    """
    actual = list(dict.fromkeys(str(s) for s in actual_servers))
    allow = tuple(dict.fromkeys(str(s).strip() for s in allow_list if str(s).strip()))
    matched = [s for s in allow if s in actual]
    if actual and allow and not matched:
        emit(
            event_to_record(
                WorkerMcpFilterNoMatchEvent(
                    configured=list(allow),
                    available=actual,
                    fallback=list(default),
                )
            )
        )
        resolved = tuple(dict.fromkeys(default))
        kept = [s for s in resolved if s in actual]
        dropped = [s for s in actual if s not in resolved]
    else:
        resolved = allow if allow else tuple(default)
        kept = [s for s in resolved if (not actual) or s in actual]
        dropped = [s for s in actual if s not in resolved]
    emit(
        event_to_record(
            WorkerMcpFilteredEvent(
                kept=list(kept),
                dropped=list(dropped),
                configured=list(allow),
            )
        )
    )
    return resolved


@dataclass
class SDKRunResult:
    """Result of a single Claude Agent SDK session.

    ``sdk_session_id`` is the Claude Agent SDK's own session identifier,
    captured from either the ``SystemMessage(subtype="init")`` ``data`` blob
    or the trailing ``ResultMessage`` (whichever surfaces it first — newer
    SDK builds populate both). Threaded through to
    :meth:`forge_loop.worker_sessions.WorkerSessionStore.set_sdk_session_id`
    so a follow-up dispatch can pass ``resume=<id>`` and keep the prompt
    cache warm across critic ping-pong rounds (issue #109 / epic #95).
    """

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
    sdk_session_id: str | None = None
    cache_hit_ratio: float = 0.0


def compute_cache_hit_ratio(usage: dict[str, Any]) -> float:
    """Return cache_read / (cache_read + input_tokens), clamped to [0, 1].

    Anthropic's usage payload reports cached-read tokens alongside the raw
    input-token count. The ratio is the headline operator metric for issue
    #109 — round 2+ of a persistent-worker session should hit >30% because
    the SDK resumes the prior session and the prompt cache is reused.

    Returns ``0.0`` when the denominator is zero (no usage data at all),
    rather than raising — telemetry must never crash the runner.
    """
    try:
        cache_read = float(usage.get("cache_read_input_tokens", 0) or 0)
        plain_input = float(usage.get("input_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    denom = cache_read + plain_input
    if denom <= 0:
        return 0.0
    return max(0.0, min(1.0, cache_read / denom))


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


async def _aclose_stream(stream: Any) -> None:
    """Close the SDK message stream so its subprocess transport is torn down
    *before* the event loop closes.

    The SDK ``query()`` async-generator owns the worker's claude subprocess. If
    it is abandoned — e.g. the worker is cancelled at its deadline — the
    transport's ``__del__`` later calls ``call_soon`` on the already-closed loop
    and prints ``RuntimeError: Event loop is closed`` (and the subprocess can be
    orphaned). Closing on every exit path (normal, error, cancellation) prevents
    both. Shielded so the close survives the ambient timeout cancellation;
    best-effort so a stream without ``aclose`` (or one that errors closing) never
    masks the real outcome.
    """
    aclose = getattr(stream, "aclose", None)
    if aclose is None:
        return
    import anyio

    with anyio.CancelScope(shield=True), contextlib.suppress(Exception):
        await aclose()


async def run_sdk_session(
    prompt: str,
    *,
    cwd: Path,
    max_turns: int = 120,
    env: dict[str, str] | None = None,
    repo: Path | None = None,
    env_path_prepend: Iterable[str] = (),
    env_vars: Mapping[str, str] | Iterable[tuple[str, str]] = (),
    add_dirs: Iterable[Path] = (),
    permission_mode: str = "bypassPermissions",
    sandbox: dict[str, Any] | None = None,
    on_event: EventEmitter | None = None,
    query_fn: Any = None,
    options_cls: Any = None,
    model: str | None = None,
    thinking_budget: str | None = None,
    allowed_mcp_servers: Iterable[str] | None = None,
    load_timeout_ms: int | None = None,
    strict_mcp_config: bool = False,
    mcp_servers: dict[str, Any] | None = None,
    resume: str | None = None,
    secret_names: Iterable[str] | None = None,
    capability_policy: CapabilityPolicy | None = None,
) -> SDKRunResult:
    """Drive one Claude Agent SDK session and stream typed WorkerEvents.

    ``on_event`` receives every event dict (the new typed stream).

    ``query_fn`` / ``options_cls`` are injection points for tests — leaving
    them None imports the real ``claude_agent_sdk`` at call time.
    """
    if query_fn is None or options_cls is None:
        from claude_agent_sdk import (
            ClaudeAgentOptions as _Opts,
        )
        from claude_agent_sdk import (
            query as _query,
        )

        if query_fn is None:
            query_fn = _query
        if options_cls is None:
            options_cls = _Opts
    # ``ThinkingBlock`` is resolved via getattr so an older SDK pin — or a
    # unit-test fake ``claude_agent_sdk`` that doesn't declare it — degrades to
    # the never-matching sentinel above instead of raising ImportError.
    import claude_agent_sdk as _sdk_mod
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    thinking_block_cls: type = getattr(_sdk_mod, "ThinkingBlock", _MissingThinkingBlock)

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
    sdk_session_id: str | None = None

    def emit_record(ev: dict[str, Any]) -> None:
        """Stamp the seq/ts envelope and fan a serialised record out.

        Kept dict-shaped because that is the on-disk / ``on_event`` contract
        (DuckDB + log tailers read JSON lines). Everything inside this session
        constructs a typed :data:`SdkEvent` first and serialises through
        :func:`emit`; ``resolve_mcp_filter`` is the one external caller that
        passes an already-serialised record (it has its own typed models).
        """
        nonlocal seq
        seq += 1
        ev = {"seq": seq, "ts": _utc_now(), **ev}
        events.append(ev)
        if on_event is not None:
            on_event(ev)

    def emit(event: SdkEvent) -> None:
        """Typed emission path — the ONLY way session code produces events.

        The model validates ``kind`` (a ``Literal`` enum member) at
        construction, so a producer typo is a hard error here instead of a
        silent string that no consumer matches (issue #148 / #147).
        """
        emit_record(event_to_record(event))

    # Build ClaudeAgentOptions. ``model`` is well-supported across SDK
    # versions; ``thinking_budget`` is newer — if the installed SDK does
    # not accept it we transparently retry without it (the role still
    # gets the requested model, just without an explicit thinking knob).
    # Provision the worker env from the declared contract (the 2026-06-05
    # silent-toolchain incident). The base is the explicit ``env`` if the
    # caller passed one, else the cleaned os.environ. When a ``repo`` is given
    # we prepend the declared dirs (e.g. ``.venv/bin``) and set the declared
    # vars (e.g. ``VIRTUAL_ENV``) so the worker's required toolchain is on PATH
    # instead of silently inheriting the orchestrator's ambient env.
    effective_env = env if env is not None else _clean_sdk_env()
    if repo is not None and (env_path_prepend or env_vars):
        from forge_loop.worker_env import build_worker_env

        effective_env = build_worker_env(
            effective_env,
            repo=repo,
            path_prepend=env_path_prepend,
            vars=env_vars,
        )
    # Enforce the secret lease at spawn (issue #283). AFTER build_worker_env so
    # toolchain provisioning (PATH/VIRTUAL_ENV) is unaffected: only secret-shaped
    # keys are gated, and only those named in the lease survive. A None/empty
    # lease withholds ALL secret-shaped keys (closed default, fail safe). The
    # withheld NAMES are recorded in the policy attestation, not here — no value
    # is ever logged or emitted.
    from forge_loop.worker_env import scope_secrets

    effective_env, _ = scope_secrets(
        effective_env, CapabilityPolicy(secret_names=tuple(secret_names or ()))
    )
    base_kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "max_turns": max_turns,
        "permission_mode": permission_mode,
        "add_dirs": [str(p) for p in add_dirs],
        "env": effective_env,
    }
    # Host-level confinement for the worker (permission profiles 'standard' /
    # 'readonly' — see forge_loop.worker_permissions). A SandboxSettings dict.
    # Omitted for 'full' so the options are byte-identical to the historical
    # no-sandbox path. Degraded via _OPTIONAL_KNOBS on SDKs too old to accept it.
    if sandbox is not None:
        base_kwargs["sandbox"] = sandbox
    # MCP server allow-list. When a leased CapabilityPolicy is supplied it is
    # the SINGLE SOURCE OF TRUTH for MCP enforcement (#326): the SDK
    # ``allowed_tools`` patterns are derived from ``policy.mcp`` (deny-by-
    # default) — NOT the operator-global ``allowed_mcp_servers`` config. A
    # worker leased without a grant for server X therefore has X's tools
    # excluded from ``allowed_tools`` and is physically unable to invoke them;
    # the brief's printed grant is no longer merely advisory. When NO policy
    # is leased (critic / brainstormer / legacy callers) we fall back to the
    # operator-config path (issue #60): the bundled default keeps a forgetful
    # caller from shipping the 250-tool firehose.
    mcp_filter_default: tuple[str, ...] = _BUNDLED_DEFAULT_ALLOWED
    if capability_policy is not None:
        # Deny-by-default: only servers named in the lease are kept, and an
        # empty grant means an EMPTY allow-list (no bundled fallback), so a
        # no-MCP lease genuinely yields zero MCP tools.
        allow_servers = tuple(g.server for g in capability_policy.mcp if g.server)
        mcp_filter_default = ()
        base_kwargs["allowed_tools"] = mcp_allow_patterns(capability_policy)
    else:
        allow_servers = tuple(s for s in (allowed_mcp_servers or _BUNDLED_DEFAULT_ALLOWED) if s)
        if allow_servers:
            # Prefer the SDK kwarg name (``allowed_tools``). If the installed
            # SDK doesn't accept it we transparently degrade — the spec lists
            # ``disallowed_tools`` as the fallback path.
            base_kwargs["allowed_tools"] = build_allowed_tools_patterns(allow_servers)
    if model:
        base_kwargs["model"] = model
    # SDK init knobs (issue: an early dogfood run hit "Control request timeout:
    # initialize" because the operator's global Claude config had ~10 MCP
    # servers totalling ~250 tools to enumerate at session start).
    #   * ``load_timeout_ms`` extends the SDK's init-handshake window
    #     beyond its ~60s default. 180s (3 min) gives slow MCP enumeration
    #     room without hiding a genuinely wedged session.
    #   * ``strict_mcp_config`` + ``mcp_servers`` together let the worker
    #     bypass the operator's global MCP config entirely and run with
    #     ONLY the explicit set the loop provides. Workers don't need the
    #     operator's Gmail/Drive/Calendar/playwright/persistent-shell etc.
    if load_timeout_ms is not None:
        base_kwargs["load_timeout_ms"] = int(load_timeout_ms)
    # Resume an existing SDK session (issue #109) — the prompt cache survives
    # across critic rounds so iteration 2+ is meaningfully cheaper. Degraded
    # via ``_OPTIONAL_KNOBS`` below if the installed SDK is too old.
    if resume:
        base_kwargs["resume"] = str(resume)
    if strict_mcp_config:
        base_kwargs["strict_mcp_config"] = True
        # When strict, an empty `mcp_servers` means "no MCP servers at all".
        # The default below is the operator's explicit allow-list (may be {}).
        base_kwargs["mcp_servers"] = dict(mcp_servers or {})

    def _instantiate(**extra: Any) -> Any:
        """Build ClaudeAgentOptions, degrading gracefully on TypeErrors.

        Older SDKs may not accept ``allowed_tools`` or ``thinking_budget``;
        rather than crash the worker, we strip them and retry. This keeps
        the per-role model knob working even on a stale SDK pin.
        """
        kwargs = {**base_kwargs, **extra}
        _OPTIONAL_KNOBS = (
            "thinking_budget",
            "allowed_tools",
            "load_timeout_ms",
            "strict_mcp_config",
            "mcp_servers",
            "resume",
            "sandbox",
        )
        try:
            return options_cls(**kwargs)
        except TypeError as exc:
            msg = str(exc)
            for cand in _OPTIONAL_KNOBS:
                if cand in msg and cand in kwargs:
                    kwargs.pop(cand, None)
                    try:
                        return options_cls(**kwargs)
                    except TypeError:
                        continue
            # Last-ditch: drop every optional knob.
            for cand in _OPTIONAL_KNOBS:
                kwargs.pop(cand, None)
            return options_cls(**kwargs)

    if thinking_budget and thinking_budget != "off":
        options = _instantiate(thinking_budget=thinking_budget)
    else:
        options = _instantiate()

    # Hoist the stream to a name so it can be closed on EVERY exit path below.
    stream = query_fn(prompt=prompt, options=options)
    try:
        async for message in stream:
            if isinstance(message, SystemMessage):
                if getattr(message, "subtype", "") == "init":
                    init_data = dict(getattr(message, "data", {}) or {})
                    # Capture the SDK's session id as early as possible —
                    # the init payload carries it on every supported SDK
                    # build, so even a session that errors before a
                    # ResultMessage lands still surfaces an id the runner
                    # can persist for ``resume=`` on the next attempt.
                    _sid = init_data.get("session_id")
                    if isinstance(_sid, str) and _sid:
                        sdk_session_id = _sid
                    emit(TurnStartEvent(data=init_data))
                    # Surface which MCP servers survived the allow-list
                    # filter (issue #60). The init payload's
                    # ``mcp_servers`` is a list of {name, status} dicts in
                    # newer SDKs and a list of names in older ones.
                    raw_servers = init_data.get("mcp_servers") or []
                    actual_names: list[str] = []
                    for entry in raw_servers:
                        if isinstance(entry, dict):
                            nm = entry.get("name") or entry.get("server")
                            if isinstance(nm, str):
                                actual_names.append(nm)
                        elif isinstance(entry, str):
                            actual_names.append(entry)
                    resolve_mcp_filter(
                        actual_servers=actual_names,
                        allow_list=allow_servers,
                        emit=emit_record,
                        default=mcp_filter_default,
                    )
                else:
                    # Non-init system messages (compaction notices, mid-session
                    # status subtypes, etc.) surface as a typed SYSTEM_MESSAGE
                    # event rather than being silently dropped — the master log
                    # keeps a record and consumers branch on the enum, never a
                    # string. ``subtype`` is carried through for downstream
                    # filtering.
                    emit(
                        SystemMessageEvent(
                            subtype=str(getattr(message, "subtype", "") or ""),
                            data=dict(getattr(message, "data", {}) or {}),
                        )
                    )
                continue
            if isinstance(message, AssistantMessage):
                model_seen = getattr(message, "model", "") or model_seen
                for block in getattr(message, "content", []) or []:
                    if isinstance(block, TextBlock):
                        emit(AssistantTextEvent(text=block.text))
                    elif isinstance(block, thinking_block_cls):
                        # Extended-thinking surfaces reasoning as its own block.
                        # Stream it as a typed ASSISTANT_THINKING event so the
                        # master log / eventdb can show the worker's reasoning
                        # trace without consumers re-deriving a string kind.
                        emit(AssistantThinkingEvent(text=getattr(block, "thinking", "") or ""))
                    elif isinstance(block, ToolUseBlock):
                        emit(
                            ToolUseEvent(
                                tool=block.name,
                                input=_safe_input(block.input),
                                tool_use_id=block.id,
                            )
                        )
                continue
            if isinstance(message, UserMessage):
                content = getattr(message, "content", None)
                blocks = content if isinstance(content, list) else []
                for block in blocks:
                    if isinstance(block, ToolResultBlock):
                        emit(
                            ToolResultEvent(
                                tool_use_id=block.tool_use_id,
                                is_error=bool(block.is_error),
                                content=_stringify_tool_result(block.content)[:2000],
                            )
                        )
                continue
            if isinstance(message, ResultMessage):
                final_text = getattr(message, "result", "") or ""
                cost_usd = float(getattr(message, "total_cost_usd", 0.0) or 0.0)
                usage = dict(getattr(message, "usage", {}) or {})
                is_error = bool(getattr(message, "is_error", False))
                num_turns = int(getattr(message, "num_turns", 0) or 0)
                # Newer SDKs put session_id on ResultMessage too; prefer
                # it when present (init may have been an older shape).
                _rsid = getattr(message, "session_id", None)
                if isinstance(_rsid, str) and _rsid:
                    sdk_session_id = _rsid
                cache_ratio = compute_cache_hit_ratio(usage)
                emit(
                    FinalResultEvent(
                        result=final_text,
                        cost_usd=cost_usd,
                        usage=usage,
                        model=model_seen,
                        num_turns=num_turns,
                        is_error=is_error,
                        sdk_session_id=sdk_session_id,
                    )
                )
                # Cost-telemetry event (issue #109 acceptance criterion):
                # operators see input/output token counts and the
                # cache-hit ratio so iteration 2+ being meaningfully
                # cheaper is observable in the event bus, not just the
                # billing dashboard.
                emit(
                    CostTelemetryEvent(
                        input_tokens=int(usage.get("input_tokens", 0) or 0),
                        output_tokens=int(usage.get("output_tokens", 0) or 0),
                        cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
                        cache_creation_input_tokens=int(
                            usage.get("cache_creation_input_tokens", 0) or 0
                        ),
                        cache_hit_ratio=round(cache_ratio, 4),
                        cost_usd=cost_usd,
                        model=model_seen,
                        sdk_session_id=sdk_session_id,
                        resumed=bool(resume),
                    )
                )
                continue
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        error_type, hint = _classify_error(exc)
        error_str = f"{error_type}: {exc}"
        emit(
            ErrorEvent(
                error_type=error_type,
                message=str(exc)[:500],
                retry_hint=hint,
            )
        )
    finally:
        # Always tear the SDK stream (and its subprocess) down before the loop
        # closes — see _aclose_stream. This is the fix for the overnight
        # "Event loop is closed" teardown noise from deadline-cancelled workers.
        await _aclose_stream(stream)

    duration = time.time() - started
    pr_url, status = _extract_pr_status(final_text)
    if error_str is not None and pr_url is None or is_error and pr_url is None:
        status = "failed"
    # Ledger rows expect a model name — fall back to the *requested* model
    # when the response carried none (early-abort, transport error, or an
    # SDK version that does not populate AssistantMessage.model). This
    # preserves the operator's audit trail across roles and was a regression
    # point flagged in issue #34's acceptance criteria.
    resolved_model = model_seen or (model or "")
    return SDKRunResult(
        pr_url=pr_url,
        status=status,
        cost_usd=cost_usd,
        usage=usage,
        model=resolved_model,
        final_result_text=final_text,
        error=error_str,
        events=events,
        duration_s=duration,
        num_turns=num_turns,
        sdk_session_id=sdk_session_id,
        cache_hit_ratio=compute_cache_hit_ratio(usage),
    )
