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
from enum import StrEnum
from pathlib import Path
from typing import Any

from forge_loop._sdk_events import SdkEventKind, parse_sdk_event

# Cap for the rich error excerpt persisted to the per-critic error log.
# The acceptance criteria for #270 require >= 2000 chars (vs the legacy
# 200/300/500 truncations that threw the real cause away).
ERROR_DETAIL_MAX = 4000


class CriticErrorClass(StrEnum):
    """Machine-readable cause category for a ``verdict="error"`` critic run.

    Shared discriminator imported by both ``_critic_sdk`` (producer) and
    ``critic`` (consumer / event emitter) so error verdicts can be tallied
    by cause instead of grepped by hand (#270). Per the manifesto's
    "no stringly-typed cross-module event boundaries" rule this is a
    ``str`` Enum compared with ``is``, never a bare string literal.
    """

    EVENT_LOOP_CLOSED = "event_loop_closed"
    PARSE_FAILURE = "parse_failure"
    TIMEOUT = "timeout"
    SDK_TRANSPORT = "sdk_transport"
    UNKNOWN = "unknown"


# Substrings (lower-cased) that mark a transport/connection-layer failure.
_TRANSPORT_MARKERS = (
    "connection",
    "transport",
    "broken pipe",
    "clienterror",
    "remoteprotocol",
    "econnreset",
    "httpx",
    "read timed out",
)

# Causes that a transient blip — a second call may succeed, so the critic
# retries these within its attempt budget before giving up (#270).
TRANSIENT_ERROR_CLASSES = frozenset(
    {CriticErrorClass.EVENT_LOOP_CLOSED, CriticErrorClass.SDK_TRANSPORT}
)


def classify_critic_error_text(text: str | None) -> CriticErrorClass:
    """Classify a critic failure from its message text.

    Used for the SDK's own ``error`` string (no exception object in hand).
    """
    t = (text or "").lower()
    if "event loop is closed" in t:
        return CriticErrorClass.EVENT_LOOP_CLOSED
    # Transport markers (incl. "read timed out") FIRST: a read-timeout is a
    # transient transport blip and must classify as SDK_TRANSPORT (retryable),
    # not the terminal TIMEOUT. Only a bare timeout with no transport marker is
    # TIMEOUT. (Fixes the sev2: the "read timed out" marker was unreachable.)
    if any(marker in t for marker in _TRANSPORT_MARKERS):
        return CriticErrorClass.SDK_TRANSPORT
    if "timeout" in t or "timed out" in t:
        return CriticErrorClass.TIMEOUT
    return CriticErrorClass.UNKNOWN


def classify_critic_error(exc: BaseException) -> CriticErrorClass:
    """Classify a critic failure from the captured exception type/message."""
    if isinstance(exc, TimeoutError):
        return CriticErrorClass.TIMEOUT
    return classify_critic_error_text(f"{type(exc).__name__}: {exc}")


def is_transient_critic_error(error_class: CriticErrorClass | None) -> bool:
    """True if this cause is worth a retry-with-backoff before giving up."""
    return error_class in TRANSIENT_ERROR_CLASSES

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
    # Machine-readable cause category, set on every error path (#270).
    error_class: CriticErrorClass | None = None
    # Full class name + a >= 2000-char excerpt (incl. traceback when the
    # failure was an exception) for the per-critic error log. The 200-char
    # event field stays for back-compat; this is the rich detail.
    error_detail: str | None = None


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
    import traceback

    from forge_loop._worker_sdk import run_sdk_session

    start = time.time()

    async def _drive() -> tuple[str, str | None]:
        last_text = ""

        def _capture(event: dict[str, Any]) -> None:
            nonlocal last_text
            # Typed boundary (issue #148). ``_worker_sdk`` emits records whose
            # ``kind`` is a :class:`SdkEventKind` member; we parse the raw dict
            # into the discriminated :data:`SdkEvent` union and compare with
            # ``is`` — no string literals, no field-name drift. This is the
            # exact boundary the #147 hot-fix patched by hand: the producer
            # said ``kind="final_result"`` while this consumer looked for
            # ``type="result"``, silently eating every assistant text.
            parsed = parse_sdk_event(event)
            if parsed is None:
                return
            if parsed.kind is SdkEventKind.FINAL_RESULT:
                msg = getattr(parsed, "result", "")
                if msg:
                    last_text = msg
            elif parsed.kind is SdkEventKind.ASSISTANT_TEXT:
                msg = getattr(parsed, "text", "")
                if msg:
                    last_text = msg

        # The critic is the TRUSTED reviewer, not a sandboxed worker (#283 /
        # PR #289 review). ``run_sdk_session``'s secret lease defaults CLOSED
        # (fail-safe for least-privilege workers), which would strip every
        # secret-shaped key — including the SDK auth secret and GITHUB_TOKEN —
        # from the reviewer's env. Thread an explicit "keep all my secrets"
        # lease enumerating the secret-shaped keys the operator launched us
        # with so the reviewer retains the credentials it needs.
        from forge_loop.worker_env import secret_shaped_keys

        critic_secret_lease = secret_shaped_keys(os.environ)
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
                    secret_names=critic_secret_lease,
                ),
                timeout=float(timeout_s),
            )
        except TimeoutError:
            return last_text, "timeout"
        # Prefer the SDK's own canonical final-text field if our capture
        # missed it. ``SDKRunResult.final_result_text`` is the single source
        # of truth (issue #148 removed the legacy ``last_message`` alias).
        final_text = getattr(result, "final_result_text", "") or last_text
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
        # Preserve the REAL cause instead of collapsing it into a 300-char
        # string (#270): the exception class drives classification and the
        # full traceback is persisted to the per-critic error log on disk.
        detail = f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
        return CriticSdkResult(
            last_message="",
            duration_s=time.time() - start,
            timed_out=False,
            error=f"sdk_session_failed: {type(exc).__name__}: {exc}"[:300],
            error_class=classify_critic_error(exc),
            error_detail=detail[:ERROR_DETAIL_MAX],
        )

    if err == "timeout":
        return CriticSdkResult(
            last_message=last_text,
            duration_s=time.time() - start,
            timed_out=True,
            error="timeout",
            error_class=CriticErrorClass.TIMEOUT,
            error_detail=f"critic SDK session timed out after {timeout_s}s",
        )
    if err:
        return CriticSdkResult(
            last_message=last_text,
            duration_s=time.time() - start,
            timed_out=False,
            error=err,
            error_class=classify_critic_error_text(err),
            error_detail=err[:ERROR_DETAIL_MAX],
        )
    return CriticSdkResult(
        last_message=last_text,
        duration_s=time.time() - start,
        timed_out=False,
        error=None,
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
    "ERROR_DETAIL_MAX",
    "TRANSIENT_ERROR_CLASSES",
    "CriticErrorClass",
    "CriticSdkResult",
    "classify_critic_error",
    "classify_critic_error_text",
    "is_transient_critic_error",
    "load_manifestos_text",
    "run_critic_sdk",
    "run_po_sdk",
]
