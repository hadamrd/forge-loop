"""Per-process rate limiting for MCP tools."""

from __future__ import annotations

import functools
import os
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from forge_loop import state as _state
from forge_loop.config import load as load_config

_TOOL_CALLS: Counter[str] = Counter()


def rate_limited(tool_name: str | None = None) -> Callable[..., Any]:
    """Decorator: cap a tool's per-process call count."""

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


def _cap_for(tool_name: str) -> int:
    env_key = f"LOOP_MCP_CAP_{tool_name.upper()}"
    return int(os.environ.get(env_key, _default_cap()))


def _default_cap() -> int:
    try:
        from forge_loop.settings import Settings

        return Settings.load().misc.mcp_cap_default
    except Exception:  # noqa: BLE001
        return 20


def _emit_rate_limited(tool_name: str, cap: int, count: int) -> None:
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001
        return
    with suppress(OSError):
        _state.append_event(
            cfg.events_file,
            "mcp_tool_rate_limited",
            tool=tool_name,
            cap=cap,
            count=count,
        )
