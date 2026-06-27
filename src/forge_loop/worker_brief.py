"""Brief rendering for worker and repair-worker sessions."""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path
from typing import Any

from forge_loop.log import get_logger
from forge_loop.memory.models import MemoryKind
from forge_loop.memory.store import MemoryStore
from forge_loop.sandbox import CapabilityPolicy, render_capability_policy

_log = get_logger("forge_loop.worker_brief")

#: Max episodes injected into a repair brief's PRIOR ATTEMPTS section so the
#: brief cannot grow unbounded from accumulated episodic memory (failed first,
#: then shipped).
_PRIOR_EPISODE_CAP = 2
#: Per-episode body truncation cap (chars): a single large episode body cannot
#: blow up the brief.
_PRIOR_EPISODE_BODY_CAP = 600
#: Marker appended to a truncated episode body.
_PRIOR_EPISODE_TRUNCATION_MARKER = "…[truncated]"
#: Max learned skill cards injected into a brief (token budget).
_SKILL_TREE_CAP = 3


def _render_skill_tree(memory_store: MemoryStore | None, issue: dict[str, Any]) -> str:
    """Render the retrieved learned-skills section for an issue, or ``""``.

    Retrieves up to :data:`_SKILL_TREE_CAP` active procedural skill cards most
    relevant to the issue (title + body) and renders them. Degrades to ``""``
    when no store is wired, nothing matches, or the store raises — so the brief
    is byte-identical to the historical output in every empty case, mirroring
    :func:`_render_prior_episodes`.
    """
    if memory_store is None:
        return ""
    try:
        from forge_loop.memory.skills import render_skill_section, retrieve_skills_for

        query = f"{issue.get('title', '')} {issue.get('body', '') or ''}"
        hits = retrieve_skills_for(memory_store, query, k=_SKILL_TREE_CAP)
    except Exception:  # noqa: BLE001 — boundary; degrade gracefully
        _log.warning("worker_brief_skill_tree_unavailable")
        return ""
    section = render_skill_section(hits)
    return f"\n{section}" if section else ""


def _render_prior_episodes(memory_store: MemoryStore | None, n: int) -> str:
    """Render the bounded ``PRIOR ATTEMPTS / LESSONS`` section for issue ``#n``.

    Loads the *active* episodic memory items for the source issue by their
    deterministic ids (``episodic-failed-{n}`` first, then
    ``episodic-shipped-{n}``) and renders each episode's title + body, with the
    body truncated to :data:`_PRIOR_EPISODE_BODY_CAP` chars and the whole
    section capped at :data:`_PRIOR_EPISODE_CAP` episodes.

    Returns ``""`` when no store is wired, when the store has no active episodes
    for the issue, or when the store raises — so the brief is byte-identical to
    the historical output in every empty case. The degrade-gracefully shape
    mirrors :meth:`brainstormer.Brainstormer._load_rejected_paths`. Superseded
    rows never appear because lookup goes through the active-only
    :meth:`MemoryStore.list_active` query path, not a raw ``get()``.
    """
    if memory_store is None:
        return ""
    try:
        active = {
            item.memory_id: item for item in memory_store.list_active(kind=MemoryKind.EPISODIC)
        }
    except Exception:  # noqa: BLE001 — boundary; degrade gracefully
        _log.warning("repair_brief_prior_episodes_unavailable")
        return ""

    ordered_ids = (f"episodic-failed-{n}", f"episodic-shipped-{n}")
    episodes = [active[mid] for mid in ordered_ids if mid in active][:_PRIOR_EPISODE_CAP]
    if not episodes:
        return ""

    blocks: list[str] = []
    for item in episodes:
        body = item.body.strip()
        if len(body) > _PRIOR_EPISODE_BODY_CAP:
            body = body[:_PRIOR_EPISODE_BODY_CAP] + _PRIOR_EPISODE_TRUNCATION_MARKER
        blocks.append(f"- {item.title}\n{body}")
    rendered = "\n\n".join(blocks)
    return (
        "\nPRIOR ATTEMPTS / LESSONS (from durable episodic memory):\n"
        "These are real outcomes from earlier attempts on this exact ticket. "
        "Do not repeat the dead-ends they describe.\n"
        f"{rendered}\n"
    )


def _render_verify_section(verify_commands: tuple[str, ...]) -> str:
    """Render the canonical "Definition of done" verify block.

    Injects the project's CANONICAL check commands so the worker never has to
    guess ``mypy`` vs ``python -m mypy`` (the 2026-06-05 silent-toolchain
    incident: the worker tried command variants for ~20 min). Empty when no
    commands are declared — the brief reads identically to the historical one.
    """
    if not verify_commands:
        return ""
    rendered = "\n".join(f"   - `{cmd}`" for cmd in verify_commands)
    return (
        "\nDEFINITION OF DONE — run THESE EXACT commands to verify (do not "
        "guess variants):\n"
        f"{rendered}\n"
        "   These are the project's canonical gates. Run them verbatim from the "
        "worktree root; every one MUST pass (exit 0) before you open the PR.\n"
    )


def _render_scope_discipline(cap: int, *, repair: bool) -> str:
    """Render the upfront SCOPE DISCIPLINE block (the #261 convergence fix).

    A convergence experiment proved workers GROW a too-big PR
    (+1013 -> +1033 -> +1128 over 3 rounds) instead of cutting scope. The
    critic diagnosing the monolith after-the-fact wasn't enough — this block
    sets the minimal-diff, single-mechanism expectation UPFRONT so the worker
    ships small by default and proposes follow-up sub-tickets past the cap.

    ``cap > 0`` cites a concrete "~N net LOC" soft ceiling; ``cap == 0`` keeps
    the single-mechanism prose without a number. The ``repair`` variant ADDS a
    CUT-do-not-grow directive — the exact #261 repair failure mode where each
    repair round enlarged the diff.
    """
    cap_line = (
        f"- SOFT NET-DIFF CAP ~{cap} LOC: if your change exceeds it, or needs "
        "more than ONE mechanism, STOP. Implement only the core mechanism and "
        "propose the rest as follow-up sub-tickets in the PR body."
        if cap > 0
        else "- If your change needs more than ONE mechanism, STOP. Implement only "
        "the core mechanism and propose the rest as follow-up sub-tickets in the "
        "PR body."
    )
    repair_line = (
        "\n- THIS IS A REPAIR. CUT, do not grow. SHRINK the diff to the single "
        "core mechanism; never ship a larger diff than you started with — "
        "a repair that enlarges the PR is the failure mode this rule exists to "
        "prevent."
        if repair
        else ""
    )
    return (
        "\nSCOPE DISCIPLINE — ship the SMALLEST viable change:\n"
        "- ONE mechanism: implement the acceptance criteria's PRIMARY ask, "
        'nothing more. No alternative implementations "to be safe".\n'
        "- Prefer EDITING or DELETING over ADDING. A large pure-addition diff "
        "is a red flag, not progress.\n"
        f"{cap_line}\n"
        "- Dead code, unused exports, and speculative generality are DEFECTS, "
        "not foresight.\n"
        f"- A converging PR shrinks under review, it never grows.{repair_line}\n"
    )


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
    verify_commands: tuple[str, ...] = (),
    scope_soft_loc_cap: int = 150,
    memory_store: MemoryStore | None = None,
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
    verify_section = _render_verify_section(verify_commands)
    scope_discipline_section = _render_scope_discipline(scope_soft_loc_cap, repair=False)

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
        verify_section=verify_section,
        scope_discipline_section=scope_discipline_section,
    )
    if dry_run:
        from forge_loop.replay import apply_dry_run_to_brief

        rendered = apply_dry_run_to_brief(rendered)
    if manifesto_bundle is not None:
        from forge_loop.manifestos import inject_into_brief

        rendered = inject_into_brief(rendered, manifesto_bundle)
    skill_tree_section = _render_skill_tree(memory_store, issue)
    if skill_tree_section:
        rendered = skill_tree_section + rendered
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
    verify_commands: tuple[str, ...] = (),
    scope_soft_loc_cap: int = 150,
    memory_store: MemoryStore | None = None,
) -> str:
    """Render a worker brief for repairing an existing blocked PR."""
    body = (issue.get("body") or "")[:6000]
    n = issue["number"]
    prior_episodes_section = _render_prior_episodes(memory_store, n)
    skill_tree_section = _render_skill_tree(memory_store, issue)
    pr_url = pr.get("url") or f"https://github.com/pull/{pr.get('number', '')}"
    pr_number = pr.get("number", "")
    head = pr.get("headRefName") or ""
    final_status = f'{{"issue": {n}, "pr": "{pr_url}", "status": "open", "note": "repair pushed"}}'
    coauthor_line = f"Sign as: Co-Authored-By: {coauthor}" if coauthor else ""
    verify_section = _render_verify_section(verify_commands)
    scope_discipline_section = _render_scope_discipline(scope_soft_loc_cap, repair=True)
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
{skill_tree_section}{prior_episodes_section}{scope_discipline_section}
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
{verify_section}
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
