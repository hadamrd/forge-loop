"""Controlled subprocess execution with mandatory timeout + bus emit.

Every shell call workers (and the master loop) make should go through this
so we never hang a tick on a runaway `curl` or stuck process. The wrapper
- enforces a hard timeout (no overriding to None)
- emits `controlled_exec_start` + `controlled_exec_done` events to the bus
- captures stdout/stderr (tail-only by default) for the event payload

This is the canonical primitive for "run a shell command from inside the loop".
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EmitFn = Callable[[str, dict[str, Any]], None]


@dataclass
class ExecResult:
    cmd: list[str]
    label: str
    exit_code: int
    duration_s: float
    stdout_tail: str
    stderr_tail: str
    timed_out: bool


MAX_TIMEOUT_S = 1800  # 30min hard ceiling; anyone needing more should restructure
MIN_TIMEOUT_S = 1


def run(
    cmd: list[str],
    *,
    timeout_s: int,
    label: str = "exec",
    cwd: Path | None = None,
    on_start: EmitFn | None = None,
    on_done: EmitFn | None = None,
    capture_tail_chars: int = 1500,
) -> ExecResult:
    """Run a shell command with a mandatory bounded timeout.

    ``timeout_s`` is clamped to [MIN_TIMEOUT_S, MAX_TIMEOUT_S]. Even if the
    caller passes 0 or a negative, the command still runs with the MIN floor.

    ``on_start`` / ``on_done`` are optional callbacks (kind, payload) that the
    runner can wire up to emit events. They never raise — exceptions are
    swallowed so a logging hiccup doesn't fail the exec.
    """
    safe_timeout = max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, int(timeout_s or MIN_TIMEOUT_S)))

    _safely_call(on_start, "controlled_exec_start", {
        "label": label, "cmd": cmd, "timeout_s": safe_timeout, "cwd": str(cwd) if cwd else None,
    })

    started = time.time()
    timed_out = False
    stdout = b""
    stderr = b""
    exit_code = -1

    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, timeout=safe_timeout, check=False
        )
        exit_code = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired as e:
        timed_out = True
        exit_code = 124  # GNU coreutils `timeout` convention
        if e.stdout:
            stdout = e.stdout
        if e.stderr:
            stderr = e.stderr

    duration_s = time.time() - started
    stdout_tail = _tail(stdout, capture_tail_chars)
    stderr_tail = _tail(stderr, capture_tail_chars)

    result = ExecResult(
        cmd=cmd, label=label, exit_code=exit_code, duration_s=duration_s,
        stdout_tail=stdout_tail, stderr_tail=stderr_tail, timed_out=timed_out,
    )

    _safely_call(on_done, "controlled_exec_done", {
        "label": label, "exit_code": exit_code, "duration_s": round(duration_s, 2),
        "timed_out": timed_out,
        "stdout_tail": stdout_tail[-500:],  # smaller for event log
        "stderr_tail": stderr_tail[-500:],
    })
    return result


def _tail(b: bytes, n_chars: int) -> str:
    if not b:
        return ""
    s = b.decode("utf-8", errors="replace")
    return s[-n_chars:] if len(s) > n_chars else s


def _safely_call(cb: EmitFn | None, kind: str, payload: dict[str, Any]) -> None:
    if cb is None:
        return
    import contextlib
    with contextlib.suppress(Exception):
        cb(kind, payload)
