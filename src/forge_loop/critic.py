"""Critic agent — reviews a PR before auto-merge.

After a worker opens a PR, we (optionally) dispatch a critic subagent that:
- reads the diff
- reads the linked issue's acceptance criteria
- checks tests + pre-commit gates ran
- posts ``gh pr review --approve`` OR ``--request-changes`` on the PR

If the critic requests changes, the worker's auto-merge is preempted by the
review block. If the critic approves, auto-merge proceeds as normal.

Trade-off: a critic pass adds ~30-90s per PR but catches the class of
regressions auto-merge alone misses (issue's acceptance criteria not met,
fix is too narrow, tests added but don't actually exercise the change).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from forge_loop.worker import _subagent_env, ensure_subagent_trusted

DEFAULT_BRIEF = """You are the CRITIC agent in a Titan sprint loop. A worker just opened a PR.
Your job: review it before auto-merge.

PR URL: {pr_url}
Linked issue: #{issue_number}

DO:
1. Read the issue via `gh issue view {issue_number} --comments` to learn the
   acceptance criteria. Note any "Acceptance" or "Out of scope" sections.
2. Read the PR diff: `gh pr diff {pr_url}` (or the number form).
3. Read the PR description: `gh pr view {pr_url} --json title,body`.
4. Decide: does the diff satisfy the issue's acceptance criteria?
   - Was a relevant test added or modified?
   - Is the fix the smallest correct one? (Out-of-scope bloat is a red flag.)
   - Are pre-commit gates 0-5 honored?
   - Are there obvious correctness gaps (untested error paths, hardcoded
     values, swallowed exceptions)?

POST EXACTLY ONE REVIEW:
- Approve: `gh pr review {pr_url} --approve --body "<one-line reason>"`
- Block:   `gh pr review {pr_url} --request-changes --body "<numbered list of issues>"`

FINAL OUTPUT (one JSON line, no prose after):
{{"verdict": "approved|changes_requested", "reasons": ["..."], "issue": {issue_number}}}

Hard rules:
- Do NOT push code. Do NOT edit files.
- Do NOT comment on style (the formatter does that).
- Focus on correctness + scope + tests.
- If genuinely uncertain, lean APPROVE (CI + tests still gate; this is a
  qualitative review layer, not a blocker)."""


@dataclass
class CriticOutcome:
    verdict: str  # approved | changes_requested | error
    reasons: list[str]
    duration_s: float
    stdout_tail: str
    error: str | None = None


def review_pr(
    pr_url: str,
    issue_number: int,
    repo: Path,
    logs_dir: Path,
    timeout_s: int = 600,
    brief_template: str | None = None,
) -> CriticOutcome:
    """Spawn the critic subagent against an open PR. Synchronous."""
    template = brief_template or DEFAULT_BRIEF
    brief = template.format(pr_url=pr_url, issue_number=issue_number)

    logs_dir.mkdir(parents=True, exist_ok=True)
    ensure_subagent_trusted(repo)
    log_path = logs_dir / f"critic-{issue_number}-{int(time.time())}.log"

    started = time.time()
    try:
        with open(log_path, "wb") as logf:
            subprocess.run(
                [
                    "claude", "-p", brief,
                    "--max-turns", "20",
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
        return CriticOutcome(
            verdict="error", reasons=[], duration_s=time.time() - started,
            stdout_tail="(timeout)", error=f"critic exceeded {timeout_s}s",
        )

    duration = time.time() - started
    verdict, reasons = _extract_verdict(log_path)
    return CriticOutcome(
        verdict=verdict, reasons=reasons,
        duration_s=duration, stdout_tail=_tail(log_path, 500),
    )


def _extract_verdict(log_path: Path) -> tuple[str, list[str]]:
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
                obj = json.loads(chunk)
                return (
                    str(obj.get("verdict", "error")),
                    list(obj.get("reasons", []) or []),
                )
            except json.JSONDecodeError:
                continue

    # Fallback: text scan for "approved" / "changes_requested"
    if re.search(r"\bchanges[_ ]requested\b", last, re.IGNORECASE):
        return "changes_requested", []
    if re.search(r"\bapproved\b", last, re.IGNORECASE):
        return "approved", []
    return "error", []


def _tail(path: Path, n: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
