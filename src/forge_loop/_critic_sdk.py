"""SDK-driven critic / PO shim (issue #85).

Both ``critic.py`` and ``po.py`` historically spawn ``claude -p`` via
``subprocess.run`` and parse stream-json log files. The worker has been
on the Claude Agent SDK since PR #27 — that gives typed events, cost
hooks, model/thinking/MCP-filter knobs, and graceful cancellation.

This module ships a small synchronous wrapper around
:func:`forge_loop._worker_sdk.run_sdk_session` shaped for the
critic/PO use case: one prompt in, the final assistant text + duration
+ error out. Same shape, two callers.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Canonical manifesto location. Can be overridden by LOOP_MANIFESTOS_DIR
# (operator escape hatch — primarily for tests). The default tracks the
# project layout: ``docs/manifestos/*.md`` at the repo root.
DEFAULT_MANIFESTOS_SUBDIR = ("docs", "manifestos")


def _manifestos_dir(repo: Path) -> Path:
    override = os.environ.get("LOOP_MANIFESTOS_DIR")
    if override:
        return Path(override).expanduser()
    return Path(repo, *DEFAULT_MANIFESTOS_SUBDIR)


def load_manifestos_text(repo: Path) -> str:
    """Load every manifesto file under the canonical manifestos dir and
    return a single block of text ready to interpolate into the critic
    prompt.

    Each manifesto is rendered as::

        ## Manifesto: <filename>

        <full body>

    If the directory is missing or empty, returns a short placeholder so
    the prompt template doesn't end up with a dangling ``{manifestos}``
    section — the critic will simply have no rules to enforce.
    """
    d = _manifestos_dir(repo)
    if not d.is_dir():
        return "(no manifestos configured — manifesto compliance check skipped)"

    chunks: list[str] = []
    for path in sorted(d.iterdir()):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".md", ".markdown", ".txt"}:
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            continue
        chunks.append(f"## Manifesto: {path.name}\n\n{body.rstrip()}\n")

    if not chunks:
        return "(no manifestos configured — manifesto compliance check skipped)"
    return "\n".join(chunks)


@dataclass
class CriticSdkResult:
    """Sync-shaped return value for the critic / PO SDK driver.

    Mirrors the shape the legacy subprocess path produced (``last_message``
    text, ``duration_s``, ``error``, ``timed_out``) so the calling
    parser doesn't have to change.
    """

    last_message: str = ""
    duration_s: float = 0.0
    timed_out: bool = False
    error: str | None = None


def run_critic_sdk(
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
) -> CriticSdkResult:
    """Synchronous wrapper — runs one SDK session under an event loop.

    Catches asyncio.TimeoutError and the SDK's own load-timeout errors;
    returns them as typed fields on :class:`CriticSdkResult` so the caller
    can branch without try/except gymnastics.

    ``query_fn`` / ``options_cls`` are injection points for tests so the
    SDK module doesn't have to be importable at unit-test time.
    """
    import time

    from forge_loop._worker_sdk import run_sdk_session

    start = time.time()

    async def _drive() -> tuple[str, str | None]:
        last_text = ""

        def _capture(event: dict[str, Any]) -> None:
            nonlocal last_text
            # The worker SDK emits "result"-shaped events at session end with
            # the assistant's final message text. We accumulate so the
            # latest wins on disk.
            if event.get("type") == "result":
                msg = event.get("result") or event.get("last_message") or ""
                if msg:
                    last_text = msg
            elif event.get("type") == "assistant_text":
                # Some SDK shapes stream the assistant tokens directly; pick
                # up the final concatenated text via the same mechanism.
                msg = event.get("text", "")
                if msg:
                    last_text = msg

        try:
            result = await asyncio.wait_for(
                run_sdk_session(
                    prompt=prompt,
                    cwd=cwd,
                    add_dirs=add_dirs,
                    on_event=_capture,
                    query_fn=query_fn,
                    options_cls=options_cls,
                    model=model,
                    thinking_budget=thinking_budget,
                    allowed_mcp_servers=allowed_mcp_servers,
                    load_timeout_ms=load_timeout_ms,
                    strict_mcp_config=strict_mcp_config,
                    mcp_servers=mcp_servers,
                ),
                timeout=float(timeout_s),
            )
        except asyncio.TimeoutError:
            return last_text, "timeout"
        # Prefer the SDK's last_message if our capture missed it.
        final_text = getattr(result, "last_message", "") or last_text
        return final_text, getattr(result, "error", None)

    try:
        try:
            asyncio.get_running_loop()
            # Already in an event loop (rare for the sync critic path,
            # but defensive). Schedule on a fresh thread-local loop.
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                last_text, err = ex.submit(asyncio.run, _drive()).result()
        except RuntimeError:
            last_text, err = asyncio.run(_drive())
    except Exception as exc:  # noqa: BLE001 — boundary
        return CriticSdkResult(
            last_message="",
            duration_s=time.time() - start,
            timed_out=False,
            error=f"sdk_session_failed: {type(exc).__name__}: {exc}"[:300],
        )

    return CriticSdkResult(
        last_message=last_text,
        duration_s=time.time() - start,
        timed_out=(err == "timeout"),
        error=err if err and err != "timeout" else None,
    )


# Alias for the PO use case — same shape, different docs string for
# discoverability.
def run_po_sdk(*args: Any, **kwargs: Any) -> CriticSdkResult:
    """Synchronous SDK driver for the PO subagent.

    Same contract as :func:`run_critic_sdk`. Aliased so call sites in
    ``po.py`` document themselves clearly without having to import a
    differently-named module.
    """
    return run_critic_sdk(*args, **kwargs)


__all__ = [
    "CriticSdkResult",
    "load_manifestos_text",
    "run_critic_sdk",
    "run_po_sdk",
]
