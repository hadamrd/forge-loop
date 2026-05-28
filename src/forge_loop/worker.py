"""Worker dispatch: spin up a worktree + run `claude -p` on a single issue."""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def ensure_subagent_trusted(target_dir: Path) -> None:
    """Plant `.claude/settings.json` in target_dir if missing.

    Belt-and-suspenders to the static `.claude/settings.json` checked in to
    the main repo: on fresh checkouts / CI / new operator machines where the
    static file might be missing or stale, we still want subagents to start
    in a trusted state. Idempotent — never overwrites an existing file
    (operators may have customised it).

    Without this trust-marker, the harness applies the "untrusted project"
    gate (anthropics/claude-code#58663) and denies Edit/Write/most Bash
    actions even with --allow-dangerously-skip-permissions.
    """
    cdir = target_dir / ".claude"
    settings_path = cdir / "settings.json"
    if settings_path.exists():
        return
    cdir.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(_PERMISSIVE_WORKTREE_SETTINGS)


def _subagent_env() -> dict[str, str]:
    """Env for spawned claude subagents.

    The `claude` CLI checks ``CLAUDECODE=1`` and refuses to run as a fresh
    autonomous session if it's set (because it thinks it's inside an existing
    Claude Code session — see anthropics/claude-code#37442 and
    anthropics/claude-agent-sdk-python#573). When the loop runner ITSELF is
    spawned from inside a Claude Code session (e.g. an operator running it
    via the IDE's Bash tool), CLAUDECODE=1 propagates to the workers and they
    can't acquire Edit/Write permissions.

    Clear the var so the subprocess starts a clean session. Same fix the
    Anthropic SDK recommends.
    """
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_SSE_PORT", None)  # also leaked by some IDE integrations
    return env


@dataclass
class WorkerOutcome:
    issue: int
    title: str
    pr_url: str | None
    status: str  # merged | open | failed | timeout | no_pr
    duration_s: float
    stdout_tail: str
    error: str | None = None
    events: list[dict[str, Any]] | None = None  # appended by subagent via sprint-events.jsonl
    cost_usd: float = 0.0
    usage: dict[str, Any] | None = None
    model: str = ""
    # Issue #132: which manifesto versions were prepended to the worker
    # system prompt for this run. Always a 2-key dict ({"quality": ...,
    # "testing": ...}) when at least one side was present; ``None`` when
    # the repo had no manifestos at all (back-compat baseline).
    manifesto_sha: dict[str, str | None] | None = None


def make_brief(
    issue: dict[str, Any],
    worktree: Path,
    *,
    risk_gated: bool = False,
    past_attempts: list[dict[str, Any]] | None = None,
    lumen_top_k: int = 3,
    lumen_test_pattern: str = "**/*Test.*",
    coauthor: str = "",
    dry_run: bool = False,
    manifesto_bundle: Any | None = None,
) -> str:
    """Render the worker brief for an issue.

    ``risk_gated`` (True): the worker opens the PR but DOES NOT enable
    auto-merge. It posts a comment "ready for human review" and exits.

    ``past_attempts`` (non-empty): includes a "PREVIOUS ATTEMPTS" section
    so the worker can learn from prior tries.
    """
    body = (issue.get("body") or "")[:6000]
    n = issue["number"]

    history_section = ""
    if past_attempts:
        rendered = "\n".join(
            f"- {a.get('ts', '?')} → {a.get('status', '?')}"
            + (f" (note: {a['note']})" if a.get("note") else "")
            + (f"; PR={a['pr_url']}" if a.get("pr_url") else "")
            for a in past_attempts[-10:]
        )
        history_section = (
            "\nPREVIOUS ATTEMPTS ON THIS ISSUE (oldest first):\n"
            f"{rendered}\n"
            "Use these to avoid repeating the same dead-ends.\n"
        )

    merge_step_renumbered = (
        "10. `gh pr create` with a clear title + body, then\n"
        "    STOP. DO NOT enable auto-merge. The `risk:high` label on this issue\n"
        "    means a human must review. Post a comment on the PR: 'Risk-gated;\n"
        "    ready for human review.' Your status is `open` (not `merged`)."
        if risk_gated
        else "10. `gh pr create` with a clear title + body (the body should restate\n"
        "    the acceptance criteria and how they're tested).\n"
        "11. `gh pr merge <N> --squash --auto --delete-branch`."
    )

    lumen_total = lumen_top_k + 1

    final_status = (
        f'{{"issue": {n}, "pr": "<url>", "status": "open", "note": "risk-gated"}}'
        if risk_gated
        else f'{{"issue": {n}, "pr": "<url-or-null>", "status": "merged|open|failed", "note": "<short>"}}'
    )

    coauthor_line = f"Sign as: Co-Authored-By: {coauthor}" if coauthor else ""

    from forge_loop.briefs import render_brief

    rendered = render_brief(
        "worker",
        n=n,
        worktree=worktree,
        issue_title=issue["title"],
        body=body,
        history_section=history_section,
        merge_step_renumbered=merge_step_renumbered,
        lumen_top_k=lumen_top_k,
        lumen_test_pattern=lumen_test_pattern,
        lumen_total=lumen_total,
        coauthor_line=coauthor_line,
        final_status=final_status,
    )
    if dry_run:
        from forge_loop.replay import apply_dry_run_to_brief

        rendered = apply_dry_run_to_brief(rendered)
    # Issue #132 — manifesto injection. The MANIFESTO block goes in FRONT
    # of every other brief line so the worker reads the house rules before
    # it sees the issue body, the contract, or the exit checklist. When
    # the bundle is empty (no manifestos in this repo), inject_into_brief
    # is a no-op and the rendered brief is byte-identical to the
    # pre-feature baseline (back-compat acceptance criterion).
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

LOOP INFRASTRUCTURE — DO NOT TOUCH:
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
    """Stable digest of the worker brief template.

    Used by ``attempts.compute_fingerprint`` so that a meaningful change to
    the worker's instructions (e.g. a new contract clause) invalidates the
    in-flight/cooldown skip — the next dispatch is for materially different
    work even if the issue body hasn't changed.

    Hashes the source of ``make_brief`` AND the externalised template
    file/override so any change to either bumps the digest. Cheap
    (called once per dispatch).
    """
    from forge_loop.briefs import load_template

    src = inspect.getsource(make_brief) + load_template("worker")
    return hashlib.sha256(src.encode("utf-8")).hexdigest()


def _branch_name(n: int, title: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", title.lower())[:40].strip("-")
    return f"loop/{n}-{slug or 'fix'}"


_PERMISSIVE_WORKTREE_SETTINGS = """{
  "permissions": {
    "defaultMode": "bypassPermissions",
    "allow": ["Bash(*)", "Edit(*)", "Write(*)", "Read(*)", "Grep(*)", "Glob(*)", "WebFetch(*)", "WebSearch(*)", "Task(*)", "TodoWrite(*)", "NotebookEdit(*)", "mcp__*"],
    "deny": []
  },
  "hasTrustDialogAccepted": true,
  "hasCompletedProjectOnboarding": true
}
"""


def _drop_permissive_settings(worktree: Path) -> None:
    """Plant a .claude/settings.json in the worktree so the worker subprocess
    doesn't get blocked by the harness 'untrusted project' gate.

    Observed: when workers run `git clean -fd` or similar (common when
    inspecting a worktree to "reset"), they delete the planted file and
    Claude re-evaluates permissions, locking the worker out. Fix:

    1. Chmod 444 the file so a naive `rm` triggers a permission warning
       (workers tend to skip protected files rather than `rm -f`).
    2. The brief calls this out explicitly so the agent doesn't fight it.

    Ref: anthropics/claude-code#58663 + observed deletion behavior in
    parallel sprint runs (PR #993 was the only one of 3 that survived).
    """
    cdir = worktree / ".claude"
    cdir.mkdir(parents=True, exist_ok=True)
    settings_path = cdir / "settings.json"
    settings_path.write_text(_PERMISSIVE_WORKTREE_SETTINGS)
    # Read-only — workers shouldn't be removing this file.
    settings_path.chmod(0o444)
    cdir.chmod(0o555)


def _quarantine_if_blocking(wt: Path) -> Path | None:
    """If `wt` still exists after normal cleanup (e.g. worker-planted files
    owned by a different uid that we can't chmod/rm), rename it out of the
    way so `git worktree add` can proceed. Returns the quarantined path,
    or None if the dir is already gone.

    Quarantined dirs use the suffix ``.stale-<unix-ts>`` so the boot
    reaper + operator can find + sweep them later without risk of colliding
    with the live path.
    """
    if not wt.exists():
        return None
    quarantine = wt.with_name(f"{wt.name}.stale-{int(time.time())}")
    try:
        wt.rename(quarantine)
    except OSError:
        return None
    return quarantine


def _prep_worktree(
    repo: Path,
    n: int,
    branch: str,
    *,
    base_branch: str = "trunk",
) -> tuple[Path, str | None]:
    wt = Path(f"/tmp/wt-loop-{n}")
    # chmod the planted .claude/ back to writable so worktree remove can
    # delete it (we set it read-only at the end of last run to prevent worker
    # tampering).
    claude_dir = wt / ".claude"
    if claude_dir.exists():
        subprocess.run(["chmod", "-R", "u+w", str(claude_dir)], capture_output=True)
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt)],
        cwd=repo,
        capture_output=True,
    )
    if wt.exists():
        with contextlib.suppress(OSError, PermissionError):
            shutil.rmtree(wt)
    # If the worker planted files owned by a different uid (subprocess
    # ran under a different namespace), chmod+rmtree above will silently
    # fail and leave the dir behind. Quarantine it so the new worktree
    # add doesn't collide. Without this, every retry of this issue hits
    # `worktree-create-failed` → infinite loop until operator intervenes.
    _quarantine_if_blocking(wt)
    # If a previous failed attempt left a local branch lying around, delete
    # it so `git worktree add -B` can recreate it cleanly off the freshest
    # origin/<base_branch>. `-B` would overwrite anyway, but we use plain `-b`
    # after an explicit delete to fail loudly if the branch is still in use.
    subprocess.run(
        ["git", "branch", "-D", branch],
        cwd=repo,
        capture_output=True,
    )
    # Force-update the configured upstream branch so the worktree always starts
    # at the freshest commit, even if many PRs landed during the prior tick.
    # `+refs/heads/...` makes the fetch force the ref update (defensive —
    # non-FF should never happen for protected branches, but if it does we want
    # the upstream view).
    remote_ref = f"refs/remotes/origin/{base_branch}"
    subprocess.run(
        [
            "git",
            "fetch",
            "--prune",
            "origin",
            f"+refs/heads/{base_branch}:{remote_ref}",
        ],
        cwd=repo,
        capture_output=True,
    )
    r = subprocess.run(
        ["git", "worktree", "add", str(wt), "-B", branch, f"origin/{base_branch}"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return wt, r.stderr
    _drop_permissive_settings(wt)
    return wt, None


def _prep_repair_worktree(
    repo: Path,
    issue: int,
    branch: str,
) -> tuple[Path, str | None]:
    wt = Path(f"/tmp/wt-loop-{issue}")
    claude_dir = wt / ".claude"
    if claude_dir.exists():
        subprocess.run(["chmod", "-R", "u+w", str(claude_dir)], capture_output=True)
    subprocess.run(["git", "worktree", "remove", "--force", str(wt)], cwd=repo, capture_output=True)
    if wt.exists():
        with contextlib.suppress(OSError, PermissionError):
            shutil.rmtree(wt)
    _quarantine_if_blocking(wt)
    remote_ref = f"refs/remotes/origin/{branch}"
    subprocess.run(
        ["git", "fetch", "--prune", "origin", f"+refs/heads/{branch}:{remote_ref}"],
        cwd=repo,
        capture_output=True,
    )
    r = subprocess.run(
        ["git", "worktree", "add", str(wt), "-B", branch, f"origin/{branch}"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return wt, r.stderr
    _drop_permissive_settings(wt)
    return wt, None


def _extract_outcome(log_path: Path) -> tuple[str | None, str]:
    """Parse the final `result` event from a claude stream-json log."""
    last_result_text = ""
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
                last_result_text = e.get("result", "") or ""

    pr_url: str | None = None
    status = "no_pr"
    for chunk in reversed(last_result_text.strip().splitlines()):
        chunk = chunk.strip()
        if chunk.startswith("{") and chunk.endswith("}"):
            try:
                obj = json.loads(chunk)
                pr_url = obj.get("pr")
                status = obj.get("status", status)
                break
            except json.JSONDecodeError:
                continue

    if pr_url is None and last_result_text:
        m = re.search(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+", last_result_text)
        if m:
            pr_url = m.group(0)
            status = "open"
    return pr_url, status


def _tail(path: Path, n_chars: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n_chars))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def run_worker(
    issue: dict[str, Any],
    repo: Path,
    logs_dir: Path,
    timeout_s: int,
    *,
    risk_gated: bool = False,
    past_attempts: list[dict[str, Any]] | None = None,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
    lumen_top_k: int = 3,
    lumen_test_pattern: str = "**/*Test.*",
    coauthor: str = "",
    tick: int | None = None,
    model: str | None = None,
    thinking: str | None = None,
    provider: str = "claude",
    allowed_mcp_servers: tuple[str, ...] | None = None,
    load_timeout_ms: int | None = None,
    strict_mcp_config: bool = False,
    mcp_servers: dict[str, Any] | None = None,
    base_branch: str = "trunk",
    brief_override: str | None = None,
) -> WorkerOutcome:
    """Run one claude-code worker against an issue.

    ``emit(kind, payload)`` is the bus emitter — used for watchdog events.
    Passed in by the runner; if omitted, watchdog events are silently dropped.

    ``model`` / ``thinking`` (issue #34) are threaded through to the SDK so
    each role can be tuned independently of the Claude Code CLI default.

    ``load_timeout_ms`` / ``strict_mcp_config`` / ``mcp_servers`` defend the
    worker session against operator-global MCP config slowness (~250 tools
    enumerated at init can blow the SDK's default 60s timeout). Operators
    set these via ``worker.load_timeout_ms`` / ``worker.strict_mcp_config`` /
    ``worker.mcp_servers`` in forge-loop.yaml or the matching env vars.
    """
    n = issue["number"]
    title = issue["title"]
    branch = _branch_name(n, title)

    worktree, err = _prep_worktree(repo, n, branch, base_branch=base_branch)
    if err is not None:
        return WorkerOutcome(
            issue=n,
            title=title,
            pr_url=None,
            status="failed",
            duration_s=0.0,
            stdout_tail=err[-500:],
            error="worktree-create-failed",
        )

    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"worker-{n}-{int(time.time())}.log"
    # Issue #132 — discover the active manifestos once at dispatch time.
    # The bundle threads into BOTH the brief renderer (prepends MANIFESTO
    # block) AND the outcome telemetry (``manifesto_sha`` audit field).
    # Discovery sources from the repo checkout, NOT the worktree — the
    # worktree's .forge/ exists post-branch but the manifestos live on
    # the canonical checkout that controls house rules.
    from forge_loop.manifestos import load_manifestos

    manifesto_bundle = load_manifestos(repo)
    manifesto_sha = manifesto_bundle.sha_payload() if manifesto_bundle.any_present else None

    # Iteration loop (issue #78) passes a focused follow-up brief that
    # short-circuits ``make_brief`` — the follow-up session reuses the same
    # worktree + branch and just gets told "your ONLY job is X".
    if brief_override is not None:
        # Even on follow-up runs, prepend the manifesto block so iteration 2
        # is held to the same house rules as iteration 1.
        from forge_loop.manifestos import inject_into_brief

        brief = inject_into_brief(brief_override, manifesto_bundle)
    else:
        brief = make_brief(
            issue,
            worktree,
            risk_gated=risk_gated,
            past_attempts=past_attempts,
            lumen_top_k=lumen_top_k,
            lumen_test_pattern=lumen_test_pattern,
            coauthor=coauthor,
            manifesto_bundle=manifesto_bundle,
        )

    if provider == "codex":
        outcome = _run_worker_codex(
            issue=issue,
            worktree=worktree,
            log_path=log_path,
            brief=brief,
            timeout_s=timeout_s,
            model=model,
        )
        outcome.manifesto_sha = manifesto_sha
        return outcome

    outcome = _run_worker_sdk(
        issue=issue,
        worktree=worktree,
        log_path=log_path,
        brief=brief,
        timeout_s=timeout_s,
        emit=emit,
        tick=tick,
        model=model,
        thinking=thinking,
        allowed_mcp_servers=allowed_mcp_servers,
        load_timeout_ms=load_timeout_ms,
        strict_mcp_config=strict_mcp_config,
        mcp_servers=mcp_servers,
    )
    outcome.manifesto_sha = manifesto_sha
    return outcome


def run_repair_worker(
    issue: dict[str, Any],
    pr: dict[str, Any],
    review_context: str,
    repo: Path,
    logs_dir: Path,
    timeout_s: int,
    *,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
    lumen_top_k: int = 3,
    lumen_test_pattern: str = "**/*Test.*",
    coauthor: str = "",
    tick: int | None = None,
    model: str | None = None,
    thinking: str | None = None,
    provider: str = "claude",
    allowed_mcp_servers: tuple[str, ...] | None = None,
    load_timeout_ms: int | None = None,
    strict_mcp_config: bool = False,
    mcp_servers: dict[str, Any] | None = None,
) -> WorkerOutcome:
    """Repair an existing blocked PR by pushing to its head branch."""
    n = issue["number"]
    title = issue["title"]
    branch = pr.get("headRefName") or ""
    pr_url = pr.get("url")
    if not branch:
        return WorkerOutcome(
            issue=n,
            title=title,
            pr_url=pr_url,
            status="failed",
            duration_s=0.0,
            stdout_tail="missing PR headRefName",
            error="repair-missing-branch",
        )
    worktree, err = _prep_repair_worktree(repo, n, branch)
    if err is not None:
        return WorkerOutcome(
            issue=n,
            title=title,
            pr_url=pr_url,
            status="failed",
            duration_s=0.0,
            stdout_tail=err[-500:],
            error="repair-worktree-create-failed",
        )
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"repair-{n}-{int(time.time())}.log"
    brief = make_repair_brief(
        issue,
        worktree,
        pr=pr,
        review_context=review_context,
        lumen_top_k=lumen_top_k,
        lumen_test_pattern=lumen_test_pattern,
        coauthor=coauthor,
    )
    if provider == "codex":
        return _run_worker_codex(
            issue=issue,
            worktree=worktree,
            log_path=log_path,
            brief=brief,
            timeout_s=timeout_s,
            model=model,
        )
    return _run_worker_sdk(
        issue=issue,
        worktree=worktree,
        log_path=log_path,
        brief=brief,
        timeout_s=timeout_s,
        emit=emit,
        tick=tick,
        model=model,
        thinking=thinking,
        allowed_mcp_servers=allowed_mcp_servers,
        load_timeout_ms=load_timeout_ms,
        strict_mcp_config=strict_mcp_config,
        mcp_servers=mcp_servers,
    )


def _run_worker_codex(
    *,
    issue: dict[str, Any],
    worktree: Path,
    log_path: Path,
    brief: str,
    timeout_s: int,
    model: str | None = None,
) -> WorkerOutcome:
    """Drive a worker through ``codex exec`` and map it to WorkerOutcome."""
    from forge_loop.agent_backend import (
        extract_github_pr,
        extract_last_json_object,
        run_codex_exec,
    )

    n = issue["number"]
    title = issue["title"]
    result = run_codex_exec(
        prompt=brief,
        cwd=worktree,
        log_path=log_path,
        timeout_s=timeout_s,
        model=model,
        add_dirs=[worktree],
    )
    if result.timed_out:
        return WorkerOutcome(
            issue=n,
            title=title,
            pr_url=None,
            status="timeout",
            duration_s=result.duration_s,
            stdout_tail="(timeout)",
            error=result.error,
            usage={},
            model=model or "",
        )
    obj = extract_last_json_object(result.last_message) or {}
    pr_url = obj.get("pr") if isinstance(obj.get("pr"), str) else None
    status = obj.get("status") if isinstance(obj.get("status"), str) else "no_pr"
    if pr_url is None:
        pr_url = extract_github_pr(result.last_message)
        if pr_url:
            status = "open"
    if result.error and pr_url is None:
        status = "failed"
    return WorkerOutcome(
        issue=n,
        title=title,
        pr_url=pr_url,
        status=status,
        duration_s=result.duration_s,
        stdout_tail=_tail(log_path, 500),
        events=_read_subagent_events(worktree),
        cost_usd=0.0,
        usage={},
        model=model or "",
        error=result.error,
    )


def _run_worker_sdk(
    *,
    issue: dict[str, Any],
    worktree: Path,
    log_path: Path,
    brief: str,
    timeout_s: int,
    emit: Callable[[str, dict[str, Any]], None] | None,
    tick: int | None,
    model: str | None = None,
    thinking: str | None = None,
    allowed_mcp_servers: tuple[str, ...] | None = None,
    load_timeout_ms: int | None = None,
    strict_mcp_config: bool = False,
    mcp_servers: dict[str, Any] | None = None,
) -> WorkerOutcome:
    """Drive the SDK session, emit typed WorkerEvents, build a WorkerOutcome.

    The SDK delivers typed messages and a final ResultMessage carries the
    grand-total cost + usage. The events.jsonl format (one JSON dict per
    line, ``kind`` + ``ts`` discriminant) is preserved for backward compat
    with anything that tails the worker log.
    """
    from forge_loop._worker_sdk import run_sdk_session

    n = issue["number"]
    title = issue["title"]
    started = time.time()

    with open(log_path, "w", encoding="utf-8") as log_fh:

        def _on_event(ev: dict[str, Any]) -> None:
            with contextlib.suppress(OSError):
                log_fh.write(json.dumps(ev, default=str) + "\n")
                log_fh.flush()

        async def _drive() -> Any:
            return await run_sdk_session(
                brief,
                cwd=worktree,
                max_turns=120,
                add_dirs=[worktree],
                permission_mode="bypassPermissions",
                on_event=_on_event,
                model=model,
                thinking_budget=thinking,
                allowed_mcp_servers=allowed_mcp_servers,
                load_timeout_ms=load_timeout_ms,
                strict_mcp_config=strict_mcp_config,
                mcp_servers=mcp_servers,
            )

        timed_out = False
        result: Any = None
        try:
            result = _run_with_timeout(_drive, timeout_s)
        except TimeoutError:
            timed_out = True

    duration = time.time() - started

    if timed_out:
        # Record the *requested* model so the audit trail is informative
        # even when no response ever arrived (issue #34).
        return WorkerOutcome(
            issue=n,
            title=title,
            pr_url=None,
            status="timeout",
            duration_s=duration,
            stdout_tail="(timeout)",
            error=f"worker exceeded {timeout_s}s",
            cost_usd=0.0,
            usage={},
            model=model or "",
        )

    assert result is not None
    # ``result.model`` already falls back to the requested model when the
    # response carried none (see _worker_sdk.run_sdk_session).
    model_seen = result.model or (model or "")
    cost_usd = result.cost_usd

    pr_url = result.pr_url
    status = result.status
    if result.error is not None and pr_url is None:
        status = "failed"

    events = _read_subagent_events(worktree)
    return WorkerOutcome(
        issue=n,
        title=title,
        pr_url=pr_url,
        status=status,
        duration_s=duration,
        stdout_tail=_tail(log_path, 500),
        events=events,
        cost_usd=cost_usd,
        usage=dict(result.usage or {}),
        model=model_seen,
        error=result.error,
    )


def _run_with_timeout(coro_factory: Callable[[], Any], timeout_s: int) -> Any:
    """Run an async coroutine factory under a wall-clock timeout.

    Uses ``anyio.run`` + ``anyio.fail_after`` so we share the SDK's event
    loop. Raises :class:`TimeoutError` on deadline expiry — the caller
    converts that to a ``timeout`` WorkerOutcome.
    """
    import anyio

    async def _wrap() -> Any:
        with anyio.fail_after(float(timeout_s)):
            return await coro_factory()

    try:
        return anyio.run(_wrap)
    except TimeoutError:
        raise


def _read_subagent_events(worktree: Path) -> list[dict[str, Any]]:
    """Read sprint-events.jsonl that the subagent (claude -p worker) may have written."""
    path = worktree / "sprint-events.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
