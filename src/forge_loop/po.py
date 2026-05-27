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
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge_loop.worker import _subagent_env, ensure_subagent_trusted


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
            _run_one(issue["number"], brief, repo, logs_dir, timeout_s, model=model)
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


def _build_po_argv(brief: str, repo: Path, model: str | None) -> list[str]:
    """Assemble the ``claude -p`` argv for the PO subagent.

    Split out so unit tests can assert on the argv directly without
    monkey-patching subprocess.run gymnastics. ``--model`` is threaded
    through when ``model`` is set (issue #34).
    """
    argv = [
        "claude",
        "-p",
        brief,
        "--max-turns",
        "25",
        "--allow-dangerously-skip-permissions",
        "--add-dir",
        str(repo),
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if model:
        argv.extend(["--model", model])
    return argv


def _run_one(
    issue_number: int,
    brief: str,
    repo: Path,
    logs_dir: Path,
    timeout_s: int,
    *,
    model: str | None = None,
) -> POOutcome:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"po-{issue_number}-{int(time.time())}.log"
    started = time.time()

    try:
        with open(log_path, "wb") as logf:
            subprocess.run(
                _build_po_argv(brief, repo, model),
                cwd=repo,
                stdout=logf,
                stderr=subprocess.STDOUT,
                timeout=timeout_s,
                env=_subagent_env(),
            )
    except subprocess.TimeoutExpired:
        return POOutcome(
            issue=issue_number,
            skipped=False,
            reason="po-timeout",
            sections_added=[],
            duration_s=time.time() - started,
            stdout_tail="(timeout)",
            error=f"po exceeded {timeout_s}s",
        )

    duration = time.time() - started
    parsed = _extract_outcome(log_path)
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
