"""Agent-provider subprocess backends.

The primary worker path remains the Claude Agent SDK. This module contains the
provider-neutral glue for CLI-backed agents, starting with Codex.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentRunResult:
    provider: str
    log_path: Path
    last_message: str
    duration_s: float
    timed_out: bool = False
    error: str | None = None


def subagent_env() -> dict[str, str]:
    """Environment for nested agent CLIs launched from an IDE agent session."""
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_SSE_PORT", None)
    return env


def build_codex_exec_argv(
    *,
    cwd: Path,
    last_message_path: Path,
    model: str | None = None,
    add_dirs: list[Path] | None = None,
    sandbox_args: list[str] | None = None,
) -> list[str]:
    """Build the noninteractive Codex CLI argv.

    The prompt is supplied on stdin via ``-`` to avoid argv-size limits on large
    issue bodies and rendered briefs.

    ``sandbox_args`` are the Codex sandbox flags for the worker's permission
    profile (see ``forge_loop.worker_permissions.codex_sandbox_args``). The
    ``None`` default reproduces the historical full-access flags, so existing
    callers are byte-for-byte unchanged.
    """
    if sandbox_args is None:
        sandbox_args = ["-s", "danger-full-access", "--dangerously-bypass-approvals-and-sandbox"]
    argv = [
        "codex",
        "exec",
        "-",
        "--json",
        "-C",
        str(cwd),
        *sandbox_args,
        "--skip-git-repo-check",
        "--output-last-message",
        str(last_message_path),
    ]
    if model:
        argv.extend(["-m", model])
    for directory in add_dirs or []:
        argv.extend(["--add-dir", str(directory)])
    return argv


def run_codex_exec(
    *,
    prompt: str,
    cwd: Path,
    log_path: Path,
    timeout_s: int,
    model: str | None = None,
    add_dirs: list[Path] | None = None,
    sandbox_args: list[str] | None = None,
) -> AgentRunResult:
    """Run Codex in noninteractive mode and capture JSONL plus final text."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    last_message_path = log_path.with_suffix(log_path.suffix + ".last.txt")
    started = time.time()
    argv = build_codex_exec_argv(
        cwd=cwd,
        last_message_path=last_message_path,
        model=model,
        add_dirs=add_dirs,
        sandbox_args=sandbox_args,
    )
    try:
        with open(log_path, "w", encoding="utf-8") as logf:
            subprocess.run(
                argv,
                cwd=cwd,
                input=prompt,
                stdout=logf,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_s,
                env=subagent_env(),
            )
    except subprocess.TimeoutExpired:
        return AgentRunResult(
            provider="codex",
            log_path=log_path,
            last_message=_read_text(last_message_path),
            duration_s=time.time() - started,
            timed_out=True,
            error=f"codex exceeded {timeout_s}s",
        )
    except OSError as exc:
        return AgentRunResult(
            provider="codex",
            log_path=log_path,
            last_message=_read_text(last_message_path),
            duration_s=time.time() - started,
            error=f"codex launch failed: {exc}",
        )
    return AgentRunResult(
        provider="codex",
        log_path=log_path,
        last_message=_read_text(last_message_path),
        duration_s=time.time() - started,
    )


def extract_last_json_object(text: str) -> dict[str, Any] | None:
    """Return the last valid JSON object embedded in agent final text."""
    for chunk in reversed(text.strip().splitlines()):
        chunk = chunk.strip()
        if chunk.startswith("{") and chunk.endswith("}"):
            try:
                obj = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    for match in reversed(list(re.finditer(r"\{(?:[^{}]|(?:\{[^{}]*\}))*\}", text, re.DOTALL))):
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def extract_github_pr(text: str) -> str | None:
    match = re.search(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+", text)
    return match.group(0) if match else None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
