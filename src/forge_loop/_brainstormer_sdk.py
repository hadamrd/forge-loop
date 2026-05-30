"""SDK-driven brainstormer shim (issue #123).

Mirrors :mod:`forge_loop._critic_sdk`: a single synchronous entry point
that drives one Claude Agent SDK session and returns ``last_message`` +
``duration_s`` + ``error``. The brainstormer reads the final assistant
message as a JSON ``BrainstormReport`` payload.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from forge_loop._critic_sdk import CriticSdkResult, run_critic_sdk

# Type alias for callers that want a self-documenting return name.
BrainstormerSdkResult = CriticSdkResult


def run_brainstormer_sdk(
    prompt: str,
    *,
    cwd: Path,
    timeout_s: int,
    model: str | None = None,
    thinking_budget: str | None = None,
    allowed_mcp_servers: tuple[str, ...] | None = None,
    load_timeout_ms: int | None = None,
    strict_mcp_config: bool = False,
    mcp_servers: dict[str, Any] | None = None,
    add_dirs: tuple[Path, ...] = (),
    query_fn: Any = None,
    options_cls: Any = None,
) -> BrainstormerSdkResult:
    """One-shot SDK session for the brainstormer.

    Shape-identical to :func:`forge_loop._critic_sdk.run_critic_sdk` so
    tests can swap one for the other. Implemented as a thin pass-through
    rather than a copy of the asyncio plumbing — there's exactly one
    correct way to drive a single SDK session, and we already have it.
    """
    return run_critic_sdk(
        prompt,
        cwd=cwd,
        timeout_s=timeout_s,
        model=model,
        thinking_budget=thinking_budget,
        allowed_mcp_servers=allowed_mcp_servers,
        load_timeout_ms=load_timeout_ms,
        strict_mcp_config=strict_mcp_config,
        mcp_servers=mcp_servers,
        add_dirs=add_dirs,
        query_fn=query_fn,
        options_cls=options_cls,
    )


__all__ = ["BrainstormerSdkResult", "run_brainstormer_sdk"]
