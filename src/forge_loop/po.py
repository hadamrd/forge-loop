"""PO (Product Owner) spec-expander agent — fattens thin issues before workers pick them up.

Why this exists:
    A worker's PR depth = the issue body's spec depth. Thin issues
    ("fix the Random thing") get thin one-line PRs. Issues with a real
    spec (problem statement, acceptance criteria, test matrix, scope,
    file pointers) get real multi-file feature PRs.

    The maintenance subagent labels things ``loop:ready`` but does not
    expand bodies. So the loop ends up shipping janitor work even when
    the underlying intent is feature-grade.

When this runs:
    Before each non-maintenance tick, the runner calls
    :func:`expand_thin_specs` with the top-N candidate issues. The PO
    pass walks them, rewrites bodies that are below the "feature-grade
    spec" bar, and pushes the rewritten bodies via ``gh issue edit
    --body-file``. Re-runs are idempotent: a marker comment
    ``<!-- po-spec-expanded -->`` is dropped so subsequent passes skip
    the same issue.

Boundaries:
    The PO MUST NOT:
    - change the issue title beyond minor scope tightening
    - close issues (that's maintenance's job)
    - add labels other than ``po:expanded``
    - touch code or open PRs

Falls back to a no-op if the issue body already meets the spec bar
(has a clear acceptance section + at least one test requirement).

Model knob
----------
``expand_thin_specs`` accepts an optional ``model`` arg (issue #34) which
threads through as ``--model <name>`` on the underlying ``claude -p``
subprocess invocation. Thinking-budget configurability for the PO role
is INTENTIONALLY deferred — the `claude -p` CLI does not expose a
thinking-budget flag, so we wait until the PO migrates to the Claude
Agent SDK (separate follow-up issue) before wiring that knob.
"""

from __future__ import annotations

import json
import re
# subprocess imports removed in #85 — PO now drives the Claude Agent SDK
# via _critic_sdk.run_po_sdk(). Codex provider still uses agent_backend.
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge_loop.worker import ensure_subagent_trusted


def _default_brief() -> str:
    """Load the PO brief template — bundled or operator-overridden."""
    from forge_loop.briefs import load_template

    return load_template("po")


@dataclass
class POOutcome:
    issue: int
    skipped: bool
    reason: str
    sections_added: list[str]
    duration_s: float
    stdout_tail: str
    error: str | None = None


def _has_expansion_marker(body: str) -> bool:
    return "<!-- po-spec-expanded -->" in (body or "")


def expand_thin_specs(
    candidates: list[dict[str, Any]],
    repo: Path,
    logs_dir: Path,
    *,
    github_repo: str,
    timeout_s: int = 480,
    brief_template: str | None = None,
    max_to_expand: int = 3,
    model: str | None = None,
    provider: str = "claude",
) -> list[POOutcome]:
    """Run the PO pass over up to ``max_to_expand`` thin issues.

    Skips issues whose body already contains the expansion marker (idempotent)
    and issues whose body already looks substantive (≥1500 chars + has
    "acceptance" header text).
    """
    outcomes: list[POOutcome] = []
    template = brief_template or _default_brief()
    ensure_subagent_trusted(repo)
    n_expanded = 0

    for issue in candidates:
        if n_expanded >= max_to_expand:
            break
        body = issue.get("body") or ""
        if _has_expansion_marker(body):
            continue
        if _looks_substantive(body):
            continue

        brief = template.format(
            issue_number=issue["number"],
            issue_title=issue.get("title", ""),
            issue_body=body[:4000],
            github_repo=github_repo,
        )
        outcomes.append(
            _run_one(
                issue["number"],
                brief,
                repo,
                logs_dir,
                timeout_s,
                model=model,
                provider=provider,
            )
        )
        n_expanded += 1

    return outcomes


def _looks_substantive(body: str) -> bool:
    if not body or len(body) < 800:
        return False
    lowered = body.lower()
    has_acceptance = bool(re.search(r"##\s*acceptance|acceptance criteria", lowered))
    has_tests = bool(re.search(r"##\s*test|test matrix|test plan", lowered))
    has_scope = bool(re.search(r"##\s*out of scope|out-of-scope", lowered))
    return sum([has_acceptance, has_tests, has_scope]) >= 2


def _run_one(
    issue_number: int,
    brief: str,
    repo: Path,
    logs_dir: Path,
    timeout_s: int,
    *,
    model: str | None = None,
    provider: str = "claude",
) -> POOutcome:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"po-{issue_number}-{int(time.time())}.log"

    if provider == "codex":
        from forge_loop.agent_backend import extract_last_json_object, run_codex_exec

        result = run_codex_exec(
            prompt=brief,
            cwd=repo,
            log_path=log_path,
            timeout_s=timeout_s,
            model=model,
            add_dirs=[repo],
        )
        if result.timed_out:
            return POOutcome(
                issue=issue_number,
                skipped=False,
                reason="po-timeout",
                sections_added=[],
                duration_s=result.duration_s,
                stdout_tail="(timeout)",
                error=result.error,
            )
        parsed = extract_last_json_object(result.last_message) or {
            "skipped": False,
            "reason": "no-final-json",
        }
        return POOutcome(
            issue=issue_number,
            skipped=bool(parsed.get("skipped", False)),
            reason=str(parsed.get("reason", "")),
            sections_added=list(parsed.get("sections_added", []) or []),
            duration_s=result.duration_s,
            stdout_tail=_tail(log_path, 400),
            error=result.error,
        )

    # SDK path (issue #85): replaces ``claude -p`` subprocess. The PO
    # final message text gets written to log_path so _extract_outcome
    # (which scans the log) keeps working without rewrite.
    from forge_loop._critic_sdk import run_po_sdk

    sdk_result = run_po_sdk(
        prompt=brief,
        cwd=repo,
        timeout_s=timeout_s,
        model=model,
        add_dirs=(repo,),
    )
    try:
        log_path.write_text(sdk_result.last_message or "")
    except OSError:
        pass
    if sdk_result.timed_out:
        return POOutcome(
            issue=issue_number,
            skipped=False,
            reason="po-timeout",
            sections_added=[],
            duration_s=sdk_result.duration_s,
            stdout_tail="(timeout)",
            error=f"po exceeded {timeout_s}s",
        )
    if sdk_result.error:
        return POOutcome(
            issue=issue_number,
            skipped=False,
            reason="po-sdk-error",
            sections_added=[],
            duration_s=sdk_result.duration_s,
            stdout_tail=sdk_result.error[:500],
            error=sdk_result.error,
        )

    duration = sdk_result.duration_s
    # Parse the assistant's final text directly — SDK path doesn't write
    # stream-json so the legacy log-walker (_extract_outcome) finds
    # nothing. The agent's contract is "last line of last message is
    # a JSON object with skipped/reason/sections_added".
    parsed: dict[str, Any] = {}
    for chunk in reversed((sdk_result.last_message or "").strip().splitlines()):
        chunk = chunk.strip()
        if chunk.startswith("{") and chunk.endswith("}"):
            try:
                parsed = json.loads(chunk)
                break
            except json.JSONDecodeError:
                continue
    if not parsed:
        parsed = {"skipped": False, "reason": "no-final-json"}
    return POOutcome(
        issue=issue_number,
        skipped=bool(parsed.get("skipped", False)),
        reason=str(parsed.get("reason", "")),
        sections_added=list(parsed.get("sections_added", []) or []),
        duration_s=duration,
        stdout_tail=_tail(log_path, 400),
    )


def _extract_outcome(log_path: Path) -> dict[str, Any]:
    last = ""
    with open(log_path, "rb") as f:
        for raw in f:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") == "result":
                last = e.get("result", "") or ""

    for chunk in reversed(last.strip().splitlines()):
        chunk = chunk.strip()
        if chunk.startswith("{") and chunk.endswith("}"):
            try:
                obj: dict[str, Any] = json.loads(chunk)
                return obj
            except json.JSONDecodeError:
                continue
    return {"skipped": False, "reason": "no-final-json"}


def _tail(path: Path, n: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
