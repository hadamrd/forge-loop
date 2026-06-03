"""Brief rendering for worker and repair-worker sessions."""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path
from typing import Any

from forge_loop.sandbox import CapabilityPolicy, render_capability_policy


def make_brief(
    issue: dict[str, Any],
    worktree: Path,
    *,
    risk_gated: bool = False,
    past_attempts: list[dict[str, Any]] | None = None,
    blocking_comments: list[str] | None = None,
    lumen_top_k: int = 3,
    lumen_test_pattern: str = "**/*Test.*",
    coauthor: str = "",
    dry_run: bool = False,
    manifesto_bundle: Any | None = None,
    capability_policy: CapabilityPolicy | None = None,
) -> str:
    """Render the worker brief for an issue."""
    body = (issue.get("body") or "")[:6000]
    n = issue["number"]

    history_section = ""
    if past_attempts:
        rendered = "\n".join(
            f"- {a.get('ts', '?')} -> {a.get('status', '?')}"
            + (f" (note: {a['note']})" if a.get("note") else "")
            + (f"; PR={a['pr_url']}" if a.get("pr_url") else "")
            for a in past_attempts[-10:]
        )
        history_section = (
            "\nPREVIOUS ATTEMPTS ON THIS ISSUE (oldest first):\n"
            f"{rendered}\n"
            "Use these to avoid repeating the same dead-ends.\n"
        )

    blocker_section = ""
    if blocking_comments:
        rendered_blockers = "\n\n---\n\n".join(comment[:4000] for comment in blocking_comments[-3:])
        blocker_section = (
            "\nCRITIC / OPERATOR BLOCKERS - HARD ACCEPTANCE CONTRACT:\n"
            "These comments are newer than the original issue body or carry a blocking review.\n"
            "Treat every Required repair, Remaining blocker, sev1 finding, and named proof command below as mandatory.\n"
            "Do not satisfy this issue with adjacent cleanup, nearby tests, or a different proof surface.\n"
            "If a blocker names a file, behavior, proof command, or test shape, implement that exact contract.\n\n"
            f"{rendered_blockers}\n"
        )

    merge_step_renumbered = (
        "10. `gh pr create` with a clear title + body, then\n"
        f"    the body MUST include `Fixes #{n}` so GitHub closes the issue\n"
        "    after the PR is merged.\n"
        "    STOP. DO NOT enable auto-merge. The `risk:high` label on this issue\n"
        "    means a human must review. Post a comment on the PR: 'Risk-gated;\n"
        "    ready for human review.' Your status is `open` (not `merged`)."
        if risk_gated
        else "10. `gh pr create` with a clear title + body (the body should restate\n"
        f"    the acceptance criteria and how they're tested, and MUST include `Fixes #{n}`).\n"
        "11. STOP. DO NOT enable auto-merge and DO NOT merge the PR. The runner\n"
        "    owns merge after critic approval and merge gates. Your status is `open`."
    )

    lumen_total = lumen_top_k + 1
    final_status = (
        f'{{"issue": {n}, "pr": "<url>", "status": "open", "note": "risk-gated"}}'
        if risk_gated
        else f'{{"issue": {n}, "pr": "<url-or-null>", "status": "open|failed", "note": "<short>"}}'
    )
    coauthor_line = f"Sign as: Co-Authored-By: {coauthor}" if coauthor else ""
    capability_policy_section = (
        "\n" + render_capability_policy(capability_policy) if capability_policy is not None else ""
    )

    from forge_loop.briefs import render_brief

    rendered = render_brief(
        "worker",
        n=n,
        worktree=worktree,
        issue_title=issue["title"],
        body=body,
        history_section=history_section + blocker_section,
        merge_step_renumbered=merge_step_renumbered,
        lumen_top_k=lumen_top_k,
        lumen_test_pattern=lumen_test_pattern,
        lumen_total=lumen_total,
        coauthor_line=coauthor_line,
        final_status=final_status,
        capability_policy_section=capability_policy_section,
    )
    if dry_run:
        from forge_loop.replay import apply_dry_run_to_brief

        rendered = apply_dry_run_to_brief(rendered)
    if manifesto_bundle is not None:
        from forge_loop.manifestos import inject_into_brief

        rendered = inject_into_brief(rendered, manifesto_bundle)
    return rendered


def make_repair_brief(
    issue: dict[str, Any],
    worktree: Path,
    *,
    pr: dict[str, Any],
    review_context: str,
    lumen_top_k: int = 3,
    lumen_test_pattern: str = "**/*Test.*",
    coauthor: str = "",
) -> str:
    """Render a worker brief for repairing an existing blocked PR."""
    body = (issue.get("body") or "")[:6000]
    n = issue["number"]
    pr_url = pr.get("url") or f"https://github.com/pull/{pr.get('number', '')}"
    pr_number = pr.get("number", "")
    head = pr.get("headRefName") or ""
    final_status = f'{{"issue": {n}, "pr": "{pr_url}", "status": "open", "note": "repair pushed"}}'
    coauthor_line = f"Sign as: Co-Authored-By: {coauthor}" if coauthor else ""
    return f"""You are an autonomous repair worker in a sprint loop.

WORKTREE (already created): {worktree}
cd there. Stay there. Don't touch the main checkout.

SOURCE ISSUE #{n}: {issue.get("title", "")}
---
{body}
---

EXISTING PR TO REPAIR:
- PR: #{pr_number} {pr_url}
- Branch: {head}

REVIEW / CRITIC CONTEXT TO ADDRESS:
---
{review_context[:12000]}
---

CONTRACT:
1. Repair the EXISTING PR branch. Do not create a new branch and do not open a new PR.
2. Address every unresolved review thread and every sev1/blocking review point with production behavior and tests.
3. If the branch is behind or conflicted, merge/rebase the current base branch and resolve conflicts in scope.
4. Preserve the original issue scope; do not add unrelated refactors.
5. Run focused tests that prove the review comments are fixed.
6. Run formatting/lint gates appropriate for touched files.
7. Commit with a message referencing #{n}.
8. Push the current branch with `git push`.
9. Resolve review threads after fixing them when the GitHub API/CLI allows it; otherwise reply/comment with the fixed evidence.
10. Leave a short PR comment summarizing the repair and remaining state.

LOOP INFRASTRUCTURE - DO NOT TOUCH:
- `{worktree}/.claude/settings.json` is loop-planted. Do NOT `git clean`, `rm`, or chmod it.
- Don't run `git clean -fdx`.

LUMEN TEST DISCOVERY:
If available, query Lumen with the issue title, review findings, and changed files.
Cap at K={lumen_top_k} discovered + 1 authored test. If unavailable, echo a one-line skip and continue.
Test pattern: {lumen_test_pattern}

{coauthor_line}

FINAL LINE OF YOUR OUTPUT MUST BE THIS JSON SHAPE, with no prose after it:
{final_status}
"""


def brief_template_hash() -> str:
    """Stable digest of the worker brief template plus external template."""
    from forge_loop.briefs import load_template

    src = inspect.getsource(make_brief) + load_template("worker")
    return hashlib.sha256(src.encode("utf-8")).hexdigest()
