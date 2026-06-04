"""Worker iteration loop — keep dispatching sub-sessions until merge (issue #78).

A one-shot worker session can fail in many "almost-shipped" ways:
edits-without-commit, commits-without-push, push-without-PR, PR-blocked-by-critic,
PR-CI-red, PR-conflicted. The runner used to give up after attempt 1.

This module gives the runner a state machine:

  1. After the worker session exits with ``status != "merged"``, call
     ``probe_worker_state(worktree, branch, repo, issue_n)``.
  2. If the state is non-terminal AND attempt count < ``LOOP_WORKER_MAX_ITERATIONS``,
     dispatch a follow-up worker session with a focused brief
     (``next_brief(state, ...)``) that re-uses the SAME worktree + branch.
  3. After N attempts without merge → label the GH issue ``loop:needs-human``,
     emit ``worker_iterations_exhausted``, comment with diagnostic.

The probe is read-only (subprocess calls to ``git`` / ``gh``).
The brief router returns ``None`` for terminal states (merged, or healthy PR
where ``gh pr merge --auto`` is the action — no LLM needed).
"""

from __future__ import annotations

import contextlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

# Default ``run`` shim — call ``subprocess.run`` with safe defaults. Injected
# in tests so the probe state machine is fully unit-testable without forking.
RunFn = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]


def _default_run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


class WorkerState(StrEnum):
    """All states the iteration loop can detect after a worker session.

    Ordered roughly from "most successful" to "most stuck". The state name is
    serialised into the ``worker_iteration`` event so dashboards / logs can
    bucket attempts by failure mode without parsing free-form notes.
    """

    DONE_MERGED = "done_merged"  # terminal: PR merged, nothing to do
    PR_OPEN_HEALTHY = (
        "pr_open_healthy"  # PR open, CI green, no critic block → enable auto-merge, no LLM
    )
    PR_OPEN_BLOCKED = "pr_open_blocked"  # critic posted sev1/sev2 → fix_critic brief
    PR_OPEN_CI_FAILED = "pr_open_ci_failed"  # CI red → fix_ci brief
    PR_OPEN_DIRTY = (
        "pr_open_dirty"  # PR open AND worktree dirty → commit brief (rare; reuses commit template)
    )
    PR_OPEN_CONFLICT = "pr_open_conflict"  # mergeable=CONFLICTING → resolve_conflict
    COMMITTED_NOT_PUSHED = "committed_not_pushed"  # local commits ahead of origin → push
    PUSHED_NO_PR = "pushed_no_pr"  # branch pushed but no PR → open_pr
    DIRTY_NO_COMMIT = "dirty_no_commit"  # files changed but no commits → commit
    CLEAN_NOTHING = (
        "clean_nothing"  # worker did nothing (no diff, no commits, no PR) → complete_work
    )
    CLOSED_PR_ABANDONED = (
        "closed_pr_abandoned"
        # A prior attempt for this branch already opened a PR that's been
        # CLOSED (not merged). Without this terminal short-circuit the
        # iteration loop spins forever trying to push to a branch whose
        # output has already been thrown away.
    )


# Terminal: do not dispatch a follow-up worker session.
TERMINAL_STATES = frozenset(
    {
        WorkerState.DONE_MERGED,
        WorkerState.CLOSED_PR_ABANDONED,
    }
)

# Non-LLM action: probe sets up ``gh pr merge --auto`` and exits without dispatch.
NON_LLM_STATES = frozenset({WorkerState.PR_OPEN_HEALTHY})

# Map each non-terminal state to the template file in
# ``src/forge_loop/briefs/iter/<kind>.md.tmpl``. Used by ``next_brief``.
STATE_TO_BRIEF_KIND: dict[WorkerState, str] = {
    WorkerState.DIRTY_NO_COMMIT: "commit",
    WorkerState.PR_OPEN_DIRTY: "commit",
    WorkerState.COMMITTED_NOT_PUSHED: "push",
    WorkerState.PUSHED_NO_PR: "open_pr",
    WorkerState.PR_OPEN_BLOCKED: "fix_critic",
    WorkerState.PR_OPEN_CI_FAILED: "fix_ci",
    WorkerState.PR_OPEN_CONFLICT: "resolve_conflict",
    WorkerState.CLEAN_NOTHING: "complete_work",
}


@dataclass
class ProbeContext:
    """Side data the brief renderer needs that's already collected by the probe."""

    pr_url: str | None = None
    critic_report: str = ""


def probe_worker_state(
    worktree: Path,
    branch: str,
    repo: str,
    issue_n: int,
    *,
    run: RunFn = _default_run,
) -> tuple[WorkerState, ProbeContext]:
    """Inspect the worktree + GH state, return the most specific WorkerState.

    Read-only. Never raises. Subprocess failures degrade to the most
    conservative state (``CLEAN_NOTHING``), which forces a re-attempt rather
    than silently dropping the issue.

    Returns ``(state, ctx)`` — ``ctx`` carries data the brief renderer may
    need (the PR URL, the latest critic report) so callers don't have to
    re-probe.
    """
    ctx = ProbeContext()

    # 1. Worktree gone? Treat as nothing-to-do.
    if not worktree.exists():
        return WorkerState.CLEAN_NOTHING, ctx

    # 2. PR exists for this branch?
    pr_view: dict[str, Any] | None = None
    try:
        r = run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                "url,number,state,mergeable,mergeStateStatus,isDraft",
                "--limit",
                "1",
            ],
            worktree,
        )
        if r.returncode == 0 and r.stdout.strip():
            arr = json.loads(r.stdout)
            if arr:
                pr_view = arr[0]
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        pr_view = None

    if pr_view:
        ctx.pr_url = pr_view.get("url")
        if pr_view.get("state") == "MERGED":
            return WorkerState.DONE_MERGED, ctx
        # A CLOSED-but-not-merged PR is a previously-abandoned attempt:
        # the operator (or auto-rescue) decided this branch's output
        # shouldn't ship. Short-circuit so we don't spin forever trying
        # to push to a branch whose work is already thrown away.
        if pr_view.get("state") == "CLOSED":
            return WorkerState.CLOSED_PR_ABANDONED, ctx

    # Refresh origin/<branch> so the ahead-count below isn't measured
    # against a stale tracking ref. Without this, a prior push that
    # succeeded server-side still shows as "local commits ahead" on
    # disk — driving the iteration loop into a push-forever cycle.
    with contextlib.suppress(subprocess.SubprocessError, OSError):
        run(["git", "fetch", "--quiet", "origin", branch], worktree)

    # 3. Worktree dirty?
    dirty = False
    try:
        r = run(["git", "status", "--porcelain"], worktree)
        dirty = bool(r.stdout.strip()) if r.returncode == 0 else False
    except (subprocess.SubprocessError, OSError):
        pass

    # 4. Local commits ahead of origin?
    # First: does origin/<branch> actually exist? If not, the branch
    # was never pushed and we must NOT fall through to a "0 ahead"
    # verdict. Before this check the probe misclassified locally-
    # committed-but-not-pushed branches as PUSHED_NO_PR, driving the
    # iteration loop into an open_pr brief on a branch GitHub can't
    # see. Dogfood-caught on forge-loop #125 and #126.
    origin_exists = False
    try:
        r_remote = run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
            worktree,
        )
        origin_exists = r_remote.returncode == 0 and bool(r_remote.stdout.strip())
    except (subprocess.SubprocessError, OSError):
        origin_exists = False

    ahead = 0
    if origin_exists:
        try:
            r = run(
                ["git", "rev-list", "--count", f"origin/{branch}..HEAD"],
                worktree,
            )
            ahead = int(r.stdout.strip() or "0") if r.returncode == 0 else 0
        except (subprocess.SubprocessError, ValueError, OSError):
            ahead = -1  # transient git failure — treat as unknown
    else:
        # Branch isn't on origin yet — sentinel = -1 routes to the
        # local-commits-no-upstream fallback further down (which returns
        # COMMITTED_NOT_PUSHED). PUSHED_NO_PR cannot be reached here.
        ahead = -1

    # 5. With a PR — classify the PR-side state.
    if pr_view and pr_view.get("state") == "OPEN":
        if dirty:
            return WorkerState.PR_OPEN_DIRTY, ctx
        # Merge state — DIRTY = conflicts, BLOCKED = critic/required-review,
        # BEHIND = stale, CLEAN/UNSTABLE/HAS_HOOKS = healthy-ish.
        mss = (pr_view.get("mergeStateStatus") or "").upper()
        mergeable = (pr_view.get("mergeable") or "").upper()
        if mss == "DIRTY" or mergeable == "CONFLICTING":
            return WorkerState.PR_OPEN_CONFLICT, ctx
        # CI / checks state via pr view.
        ci_failed = _ci_failed(run, worktree, pr_view.get("number"))
        if ci_failed:
            return WorkerState.PR_OPEN_CI_FAILED, ctx
        critic_report = _fetch_critic_report(run, worktree, repo, issue_n, pr_view.get("number"))
        if mss == "BLOCKED" or critic_report:
            ctx.critic_report = critic_report or "(critic report not found; rerun the critic check)"
            return WorkerState.PR_OPEN_BLOCKED, ctx
        return WorkerState.PR_OPEN_HEALTHY, ctx

    # 6. No (open) PR — bucket by local state.
    if dirty:
        return WorkerState.DIRTY_NO_COMMIT, ctx
    if ahead > 0:
        return WorkerState.COMMITTED_NOT_PUSHED, ctx
    if ahead == 0:
        # Local matches origin/branch — branch is pushed, just no PR.
        return WorkerState.PUSHED_NO_PR, ctx
    # ahead == -1: origin/<branch> didn't exist; need to see if any commits exist
    # ahead of base_branch (heuristic: any commits at all on this branch?).
    with contextlib.suppress(subprocess.SubprocessError, OSError):
        r = run(["git", "log", "-1", "--format=%H"], worktree)
        if r.returncode == 0 and r.stdout.strip():
            # Has commits, but no origin tracking → needs push.
            base_r = run(
                ["git", "rev-parse", "--abbrev-ref", "@{upstream}"],
                worktree,
            )
            if base_r.returncode != 0:
                return WorkerState.COMMITTED_NOT_PUSHED, ctx
    return WorkerState.CLEAN_NOTHING, ctx


def _ci_failed(run: RunFn, worktree: Path, pr_number: int | None) -> bool:
    """True if any required PR check is in a failing terminal state."""
    if pr_number is None:
        return False
    try:
        r = run(
            ["gh", "pr", "view", str(pr_number), "--json", "statusCheckRollup"],
            worktree,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return False
        data = json.loads(r.stdout)
        checks = data.get("statusCheckRollup") or []
        for c in checks:
            # GitHub returns either "conclusion" (check runs) or "state" (statuses).
            conclusion = (c.get("conclusion") or "").upper()
            state = (c.get("state") or "").upper()
            if conclusion in {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}:
                return True
            if state in {"FAILURE", "ERROR"}:
                return True
        return False
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return False


def _fetch_critic_report(
    run: RunFn,
    worktree: Path,
    repo: str,
    issue_n: int,
    pr_number: int | None,
) -> str:
    """Best-effort fetch of the latest critic comment body on the PR.

    The critic posts findings as a PR comment prefixed with ``critic-report``
    (see ``critic_actions.py``). We grep the last 5 comments for that marker.
    Returns ``""`` if no critic report exists.
    """
    if pr_number is None:
        return ""
    try:
        r = run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "comments",
            ],
            worktree,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return ""
        data = json.loads(r.stdout)
        comments = data.get("comments") or []
        # Walk in reverse — most recent first.
        for c in reversed(comments[-10:]):
            body = c.get("body") or ""
            if "critic-report" in body.lower() or "sev1" in body.lower() or "sev2" in body.lower():
                return body[:4000]
        return ""
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return ""


def _load_template(kind: str) -> str:
    """Read a brief template from ``src/forge_loop/briefs/iter/<kind>.md.tmpl``."""
    here = Path(__file__).resolve().parent.parent / "briefs" / "iter" / f"{kind}.md.tmpl"
    return here.read_text(encoding="utf-8")


def next_brief(
    state: WorkerState,
    prior_outcome: Any,
    critic_report: str,
    issue: dict[str, Any],
    *,
    attempt: int = 2,
    max_attempts: int = 3,
    base_branch: str = "trunk",
    coauthor: str = "",
    pr_url: str | None = None,
    worktree: str | Path | None = None,
) -> str | None:
    """Return the focused follow-up brief for ``state``, or None if terminal.

    Terminal cases:
      - ``DONE_MERGED`` → ``None`` (nothing to do).
      - ``PR_OPEN_HEALTHY`` → ``None`` (caller enables auto-merge; no LLM).

    All other states map through ``STATE_TO_BRIEF_KIND`` to a tiny imperative
    template. Templates are intentionally short (<15 lines) and IMPERATIVE.
    """
    if state in TERMINAL_STATES or state in NON_LLM_STATES:
        return None
    kind = STATE_TO_BRIEF_KIND.get(state)
    if kind is None:
        return None
    tpl = _load_template(kind)
    branch = ""
    # Best-effort branch resolution from the prior_outcome (WorkerOutcome) or
    # fallback to the loop's naming convention.
    if prior_outcome is not None:
        branch = getattr(prior_outcome, "branch", "") or ""
    if not branch:
        branch = f"loop/{issue['number']}-iter"
    # Worktree path for the worker to `cd` into. Defaults to the legacy
    # flat path only when the caller didn't supply the real (per-repo
    # namespaced) worktree — kept for back-compat with direct callers.
    worktree_str = str(worktree) if worktree is not None else f"/tmp/wt-loop-{issue['number']}"
    return tpl.format(
        issue_n=issue["number"],
        worktree=worktree_str,
        title=issue.get("title", ""),
        body=(issue.get("body") or "")[:4000],
        branch=branch,
        base_branch=base_branch,
        coauthor=coauthor or "Claude Opus 4.8 <noreply@anthropic.com>",
        pr_url=pr_url or "",
        critic_report=critic_report or "(no critic report captured)",
        attempt=attempt,
        max_attempts=max_attempts,
    )


def is_terminal(state: WorkerState) -> bool:
    return state in TERMINAL_STATES


def brief_kind_for(state: WorkerState) -> str | None:
    """Public helper — what ``brief_kind`` label to put on the
    ``worker_iteration`` event for this state."""
    if state in TERMINAL_STATES:
        return None
    if state in NON_LLM_STATES:
        return "enable_automerge"
    return STATE_TO_BRIEF_KIND.get(state)


def enable_auto_merge(
    pr_url: str,
    *,
    worktree: Path,
    run: RunFn = _default_run,
) -> bool:
    """Healthy PR shortcut — turn on ``gh pr merge --auto`` and return success.

    Used when ``probe_worker_state`` returns ``PR_OPEN_HEALTHY``: no LLM is
    needed, we just tell GitHub to merge as soon as required checks pass.
    """
    try:
        r = run(
            [
                "gh",
                "pr",
                "merge",
                pr_url,
                "--squash",
                "--auto",
                "--delete-branch",
            ],
            worktree,
        )
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def run_iteration_loop(
    outcome: Any,
    issue: dict[str, Any],
    *,
    repo: str,
    base_branch: str,
    worktree: Path,
    max_iterations: int,
    dispatch_worker: Callable[[dict[str, Any], str], Any],
    emit: Callable[[str, dict[str, Any]], None],
    run: RunFn = _default_run,
    coauthor: str = "",
) -> Any:
    """Drive the iteration state machine until merge or N attempts exhausted.

    Args:
        outcome: the original WorkerOutcome from the first attempt (attempt=1).
        issue: GH issue dict (number, title, body).
        repo: ``owner/repo`` for gh calls.
        base_branch: trunk / main.
        worktree: ``/tmp/wt-loop-<N>``.
        max_iterations: cap (default 3 — operator sets via LOOP_WORKER_MAX_ITERATIONS).
        dispatch_worker: callable ``(issue, follow_up_brief) -> WorkerOutcome``.
        emit: event bus ``(kind, payload)`` — emits ``worker_iteration`` per attempt
            and ``worker_iterations_exhausted`` on failure.

    Returns the final outcome (may be the original, or a successful follow-up).
    """
    issue_n = issue["number"]
    branch = getattr(outcome, "branch", "") or ""
    if not branch:
        try:
            r = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], worktree)
            if r.returncode == 0:
                branch = r.stdout.strip()
        except (subprocess.SubprocessError, OSError):
            branch = ""
    if not branch or branch in {"HEAD", base_branch}:
        branch = f"loop/{issue_n}"
    current = outcome
    # attempt=1 was the original; iteration loop starts at attempt=2.
    for attempt in range(2, max_iterations + 1):
        state, ctx = probe_worker_state(worktree, branch, repo, issue_n, run=run)
        brief_kind = brief_kind_for(state)
        emit(
            "worker_iteration",
            {
                "issue": issue_n,
                "attempt": attempt,
                "state": state.value,
                "brief_kind": brief_kind,
                "pr_url": ctx.pr_url,
            },
        )
        if state == WorkerState.CLOSED_PR_ABANDONED:
            # The branch has a CLOSED-but-not-merged PR — a prior attempt
            # was thrown away. Label the issue so it doesn't keep popping
            # back to the dispatcher every tick, and post a diagnostic
            # comment pointing at the closed PR for operator review.
            escalate_to_human(issue_n, repo, state, worktree, ctx.pr_url, run=run)
            return current
        if is_terminal(state):
            return current
        if state in NON_LLM_STATES:
            ok = enable_auto_merge(ctx.pr_url or "", worktree=worktree, run=run)
            emit("worker_iteration_automerge", {"issue": issue_n, "ok": ok, "pr": ctx.pr_url})
            if ok and ctx.pr_url:
                # Mutate outcome to reflect we have a merging PR.
                try:
                    current.pr_url = ctx.pr_url
                    current.status = "open"
                except AttributeError:
                    pass
            return current
        brief = next_brief(
            state,
            current,
            ctx.critic_report,
            issue,
            attempt=attempt,
            max_attempts=max_iterations,
            base_branch=base_branch,
            coauthor=coauthor,
            pr_url=ctx.pr_url,
            worktree=worktree,
        )
        if brief is None:
            return current
        # Dispatch a follow-up worker session reusing the same worktree+branch.
        current = dispatch_worker(issue, brief)
        if getattr(current, "status", "") == "merged":
            return current

    # Exhausted all attempts — escalate.
    final_state, final_ctx = probe_worker_state(worktree, branch, repo, issue_n, run=run)
    emit(
        "worker_iterations_exhausted",
        {
            "issue": issue_n,
            "attempts": max_iterations,
            "final_state": final_state.value,
            "pr_url": final_ctx.pr_url,
        },
    )
    escalate_to_human(issue_n, repo, final_state, worktree, final_ctx.pr_url, run=run)
    return current


def escalate_to_human(
    issue_n: int,
    repo: str,
    state: WorkerState,
    worktree: Path,
    pr_url: str | None,
    *,
    run: RunFn = _default_run,
) -> bool:
    """After N attempts without merge: label issue + post diagnostic comment.

    Best-effort. Returns True if at least the label landed; the comment is
    cosmetic. Never raises.
    """
    # Add loop:needs-human AND remove loop:ready in one call so the
    # dispatcher stops re-picking the issue on the next tick. Before
    # this, escalated issues kept reappearing in `top_issues` because
    # only the needs-human label was added — loop:ready survived.
    # Dogfood-caught on forge-loop #125/#126 (CTO observed loop "not
    # reliable" — same stuck issue served on every tick).
    label_ok = False
    try:
        r = run(
            [
                "gh",
                "issue",
                "edit",
                str(issue_n),
                "--repo",
                repo,
                "--add-label",
                "loop:needs-human",
                "--remove-label",
                "loop:ready",
            ],
            worktree,
        )
        label_ok = r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        label_ok = False

    diag = (
        f"forge-loop: worker iteration loop exhausted after max attempts.\n"
        f"\n"
        f"- last state: `{state.value}`\n"
        f"- worktree: `{worktree}`\n"
        f"- PR: {pr_url or '(none)'}\n"
        f"\n"
        f"The branch is left in place for human inspection. Run "
        f"`cd {worktree} && git status` to pick up where the loop stopped.\n"
    )
    with contextlib.suppress(subprocess.SubprocessError, OSError):
        run(
            [
                "gh",
                "issue",
                "comment",
                str(issue_n),
                "--repo",
                repo,
                "--body",
                diag,
            ],
            worktree,
        )
    return label_ok
