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

DEFAULT_BRIEF = """You are the PO (Product Owner) subagent for the sprint loop.

Your ONE job: rewrite thin issue bodies into feature-grade specs so the
worker that picks the issue up next ships a feature, not a one-line fix.

ISSUE TO EXPAND:
#{issue_number}: {issue_title}
---
{issue_body}
---

CONTEXT TO READ FIRST:
Read your project's contributing/architecture docs (e.g. CONTRIBUTING.md,
ARCHITECTURE.md, docs/), and scan the codebase for similar features or
existing patterns. Re-use existing conventions over inventing new ones.

THE SPEC BAR (issue body must have ALL):
  - **## Problem** — what's broken or missing, with one concrete example.
  - **## Acceptance criteria** — bulleted, falsifiable, testable. ≥3 items.
  - **## Test matrix** — what unit tests, what integration tests, what e2e
    tests, including at least one adversarial / sad-path test.
  - **## Out of scope** — explicit list of things NOT to do (prevents bloat).
  - **## File pointers** — paths to the files the worker should touch.
    If unsure, list candidate areas (`src/.../<module>/`, etc.).

ALGORITHM:
1. Score the current body against the 5 sections above. If 4+ are present
   and substantive, OUTPUT skipped=true and return — no edit needed.
2. Otherwise, write the full spec. Preserve the original body text under
   a `## Original report` section at the bottom so context isn't lost.
3. End the body with the marker line `<!-- po-spec-expanded -->`.
4. Apply via:
   ```
   gh issue edit {issue_number} --repo {github_repo} --body-file <(printf '%s' "$BODY")
   ```
   Then `gh issue edit {issue_number} --repo {github_repo} --add-label "po:expanded"`.

CONSTRAINTS:
- NEVER change the issue title (worker's branch name depends on stable title).
- NEVER close issues or change priority labels.
- NEVER fabricate file paths — if you don't know, write `(investigate)` as
  the file pointer and let the worker discover.
- Cap body at 8000 chars. If a referenced design doc is huge, link don't paste.

FINAL OUTPUT (one JSON line, no prose after):
{{"issue": {issue_number}, "skipped": <bool>, "reason": "<short>", "sections_added": [<list>]}}
"""


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
) -> list[POOutcome]:
    """Run the PO pass over up to ``max_to_expand`` thin issues.

    Skips issues whose body already contains the expansion marker (idempotent)
    and issues whose body already looks substantive (≥1500 chars + has
    "acceptance" header text).
    """
    outcomes: list[POOutcome] = []
    template = brief_template or DEFAULT_BRIEF
    ensure_subagent_trusted(repo)
    n_expanded = 0

    for issue in candidates:
        if n_expanded >= max_to_expand:
            break
        body = (issue.get("body") or "")
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
        outcomes.append(_run_one(issue["number"], brief, repo, logs_dir, timeout_s))
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
) -> POOutcome:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"po-{issue_number}-{int(time.time())}.log"
    started = time.time()

    try:
        with open(log_path, "wb") as logf:
            subprocess.run(
                [
                    "claude", "-p", brief,
                    "--max-turns", "25",
                    "--allow-dangerously-skip-permissions",
                    "--add-dir", str(repo),
                    "--output-format", "stream-json",
                    "--verbose",
                ],
                cwd=repo,
                stdout=logf,
                stderr=subprocess.STDOUT,
                timeout=timeout_s,
                env=_subagent_env(),
            )
    except subprocess.TimeoutExpired:
        return POOutcome(
            issue=issue_number, skipped=False, reason="po-timeout",
            sections_added=[], duration_s=time.time() - started,
            stdout_tail="(timeout)", error=f"po exceeded {timeout_s}s",
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
