"""Thin wrapper around the `gh` CLI for issue ops.

All functions require ``repo="<owner>/<name>"``. The runner passes
``cfg.github_repo`` (from LOOP_GH_REPO or YAML). No hardcoded default.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from forge_loop import gh_issues

logger = logging.getLogger(__name__)

auth_source = "gh cli"

DEFAULT_REPO: str | None = None


def _require_repo(repo: str | None) -> str:
    return gh_issues.require_repo(repo)


def _split_repo(repo: str) -> tuple[str, str]:
    return gh_issues.split_repo(repo)


def top_issues(label: str, limit: int, repo: str | None = None) -> list[dict[str, Any]]:
    """Return open issues carrying ``label`` (oldest first)."""
    return gh_issues.top_issues(label, limit, repo=repo)


def fetch_issue(issue: int, repo: str | None = None) -> dict[str, Any] | None:
    """Fetch a single issue by number. Returns None if not found / failed."""
    return gh_issues.fetch_issue(issue, repo=repo)


def issue_comment_bodies(issue: int, repo: str | None = None) -> list[str]:
    """Fetch issue comment bodies. Returns an empty list on GitHub/CLI failure."""
    repo = _require_repo(repo)
    r = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(issue),
            "--repo",
            repo,
            "--comments",
            "--json",
            "comments",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return []
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    return [str(c.get("body") or "") for c in payload.get("comments", [])]


def comment(issue: int, body: str, repo: str | None = None) -> None:
    """Post a comment to an issue. Errors are swallowed (caller logs)."""
    gh_issues.comment(issue, body, repo=repo)


def label(issue: int, labels: list[str], repo: str | None = None) -> None:
    """Add labels to an issue."""
    gh_issues.label(issue, labels, repo=repo)


def unlabel(issue: int, label: str, repo: str | None = None) -> None:
    """Remove a single label from an issue."""
    gh_issues.unlabel(issue, label, repo=repo)


def remove_pr_label(pr: int | str, label: str, repo: str | None = None) -> bool:
    """Remove a label from a PR. Best-effort: returns False on failure."""
    repo = _require_repo(repo)
    r = subprocess.run(
        ["gh", "pr", "edit", str(pr), "--repo", repo, "--remove-label", label],
        check=False,
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def create_issue(
    title: str,
    body: str,
    labels: list[str] | None = None,
    repo: str | None = None,
) -> int | None:
    """Open a new issue. Returns the new number or None on failure."""
    return gh_issues.create_issue(title, body, labels, repo=repo)


def update_issue(
    issue: int,
    title: str | None = None,
    body: str | None = None,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    repo: str | None = None,
) -> bool:
    """Patch an issue. Returns True on success."""
    repo = _require_repo(repo)
    cmd = ["gh", "issue", "edit", str(issue), "--repo", repo]
    if title is not None:
        cmd.extend(["--title", title])
    if body is not None:
        cmd.extend(["--body", body])
    for lab in add_labels or []:
        cmd.extend(["--add-label", lab])
    for lab in remove_labels or []:
        cmd.extend(["--remove-label", lab])
    if len(cmd) == 5:
        return True
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return r.returncode == 0


def pr_changed_lines(pr: int | str, repo: str | None = None) -> int:
    """Return additions+deletions for a PR. 0 on failure (caller falls back)."""
    repo = _require_repo(repo)
    r = subprocess.run(
        ["gh", "pr", "view", str(pr), "--repo", repo, "--json", "additions,deletions"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return 0
    try:
        obj = json.loads(r.stdout)
        return int(obj.get("additions", 0)) + int(obj.get("deletions", 0))
    except (json.JSONDecodeError, ValueError, TypeError):
        return 0


def pr_precommit_context(pr_url: str, cwd: Path) -> tuple[str, str]:
    """Return PR body and commit metadata text for local deterministic checks."""

    r = subprocess.run(
        ["gh", "pr", "view", pr_url, "--json", "body,commits"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return "", ""
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError:
        return "", ""

    body = payload.get("body") if isinstance(payload.get("body"), str) else ""
    commit_chunks: list[str] = []
    commits = payload.get("commits")
    if isinstance(commits, list):
        for commit in commits:
            if not isinstance(commit, dict):
                continue
            for key in ("messageHeadline", "messageBody", "message"):
                value = commit.get(key)
                if isinstance(value, str) and value.strip():
                    commit_chunks.append(value)
    return body, "\n".join(commit_chunks)


def pr_diff(pr_url: str, cwd: Path) -> str:
    """Return the unified diff for a PR. Empty string on failure.

    Mirrors the swallow-and-return-empty convention used throughout this
    module (``pr_precommit_context``, ``pr_changed_files``): a closed /
    non-existent / network-failing PR yields ``""`` rather than raising,
    so the manifesto-suggest context assembly (#134) degrades gracefully
    instead of crashing the command.
    """
    r = subprocess.run(
        ["gh", "pr", "diff", pr_url],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return ""
    return r.stdout or ""


def pr_changed_files(pr_url: str, cwd: Path) -> list[str]:
    """Return the list of file paths a PR touches. Empty list on failure.

    Used by the #144 critic rule to decide whether a PR's diff touches
    packaging files (``pyproject.toml`` / ``setup.py`` / ``setup.cfg``).
    """
    r = subprocess.run(
        ["gh", "pr", "view", pr_url, "--json", "files"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return []
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    files = payload.get("files")
    if not isinstance(files, list):
        return []
    paths: list[str] = []
    for entry in files:
        if isinstance(entry, dict):
            path = entry.get("path")
            if isinstance(path, str) and path:
                paths.append(path)
    return paths


def prs_by_label(label: str, limit: int, repo: str | None = None) -> list[dict[str, Any]]:
    """Return open PRs carrying ``label`` (oldest updated first)."""
    repo = _require_repo(repo)
    cmd = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        str(limit),
        "--json",
        "number,title,body,headRefName,baseRefName,url,labels,updatedAt",
    ]
    if label:
        cmd.extend(["--label", label])
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return []
    try:
        result: list[dict[str, Any]] = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    return sorted(result, key=lambda p: str(p.get("updatedAt") or ""))


#: Critic verdict labels that mean a PR is NOT approved — it is still the
#: repair loop's job and is NOT eligible for terminal auto-merge. Declared here
#: (the lowest-level ``gh`` module) so the repair selector AND the adoption /
#: automerge steps share ONE source of truth instead of re-spelling the string
#: literals on each side (manifesto: no stringly-typed cross-module
#: discriminator — a typo on one side silently breaks the gate).
CRITIC_BLOCK_LABELS = frozenset({"critic:blocking", "critic:suspicious"})


def _pr_label_names(pr: dict[str, Any]) -> set[str]:
    return {str(label.get("name") or "") for label in pr.get("labels") or []}


#: The critic posts inline findings as ``**[sevN/category]** <message>`` (see
#: ``critic_actions.post_critic_actions``). That machine signature on the
#: *opening* comment of a thread — NOT the author login — is how we tell a
#: leftover *critic* thread from a genuine *human* review thread. We cannot key
#: off ``author{login}``: when the loop dogfoods itself the critic and human
#: reviewers can share one GitHub identity, so the login does not discriminate.
#: The critic's body format is stable code, so it does.
_CRITIC_INLINE_RE = re.compile(r"^\s*\*\*\[sev[123]/")


def _thread_is_critic(thread: dict[str, Any]) -> bool:
    """Return ``True`` iff a review thread was opened by the critic.

    Classification keys off the *opening* comment (the one that created the
    thread): a human reply on a critic thread does not make it human, and a
    critic reply on a human thread does not make it critic. A thread with no
    comments — or whose opening comment does not match the critic's stable
    ``**[sevN/...]**`` finding format — is treated as NOT-critic (i.e. human),
    the conservative direction: we never auto-merge over a thread we cannot
    prove is the critic's own leftover sev3 note (AC3).
    """
    comments = thread.get("comments") or []
    if not comments:
        return False
    body = str((comments[0] or {}).get("body") or "")
    return bool(_CRITIC_INLINE_RE.match(body))


def human_unresolved_threads(
    threads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return the unresolved review threads NOT authored by the critic.

    Issue #230 / AC3: leftover *critic* sev3 inline-comment threads on an
    approved PR are informational and must NOT, on their own, keep the PR off
    the merge conveyor (gating on them is exactly what caused the #229
    multi-hour stall). But unresolved *human* review threads requesting changes
    MUST still hold the PR back. A human inline-comment thread does not flip
    ``mergeStateStatus`` off CLEAN, so merge state alone cannot encode this — we
    filter the threads explicitly by their (non-critic) authorship signature.
    """
    return [t for t in threads if not bool(t.get("isResolved")) and not _thread_is_critic(t)]


def is_approved_mergeable(
    pr: dict[str, Any],
    *,
    unresolved_threads: list[dict[str, Any]] | None = None,
) -> bool:
    """Return ``True`` iff a loop PR is critic-approved AND cleanly mergeable.

    Issue #230. "Approved" is represented operationally by the *absence* of a
    critic block label: the runner removes ``critic:blocking`` /
    ``critic:suspicious`` the moment the critic's latest verdict is *approve*
    (see ``dispatch.py``), so a PR carrying neither label has a latest verdict
    of approved. "Mergeable" is ``mergeStateStatus == CLEAN``.

    Such a PR is *terminal* for the repair loop. The critic posts sev3 findings
    as inline comments, which remain unresolved review threads even after an
    APPROVE verdict — those leftover threads must NOT, on their own, re-enter
    the PR into the repair set (the #229 multi-hour stall).

    AC3: a genuinely blocked PR keeps a block label, and a human "request
    changes" review usually drops ``mergeStateStatus`` off CLEAN under branch
    protection — both fail this predicate. But a human inline-comment thread
    leaves merge state CLEAN, so when the caller has the PR's unresolved review
    threads it passes them as ``unresolved_threads`` and we additionally exclude
    any PR carrying an unresolved *human* thread (see
    :func:`human_unresolved_threads`). When ``unresolved_threads`` is omitted,
    the predicate falls back to label + CLEAN only.
    """
    if _pr_label_names(pr) & CRITIC_BLOCK_LABELS:
        return False
    if str(pr.get("mergeStateStatus") or "").upper() != "CLEAN":
        return False
    # AC3: an unresolved *human* review thread keeps the PR off the merge
    # conveyor even though merge state is CLEAN; leftover critic sev3 threads do
    # not. ``human_unresolved_threads`` filters the latter out.
    return not (unresolved_threads and human_unresolved_threads(unresolved_threads))


def prs_requiring_repair(
    limit: int,
    repo: str | None = None,
    *,
    on_skip: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Return open PRs the repair loop should revisit.

    A PR needs repair when it is explicitly critic-blocked, has unresolved
    review threads, or is merge-conflicted/dirty. Review threads are not
    exposed by ``gh pr list`` or ``gh pr view --comments``, so this function
    enriches the open PR list with a GraphQL pass before the dispatcher decides
    whether to spawn a repair worker.

    Issue #230: a critic-approved, CLEAN PR (see :func:`is_approved_mergeable`)
    is NOT returned even if it still carries unresolved sev3 review threads —
    it is terminal and belongs on the merge conveyor, not the repair loop.
    Re-dispatching a repair worker against such a PR caused a multi-hour stall
    (#229). Each excluded PR is passed to ``on_skip`` (when provided) so the
    caller can emit a structured skip event — no silent drop.
    """
    repo = _require_repo(repo)
    prs = _open_prs(limit=max(limit, 50), repo=repo)
    # Idle short-circuit: with no open PRs there is nothing to enrich, so skip
    # the GraphQL pass entirely (issue #226).
    if not prs:
        return []

    # One GraphQL round-trip for ALL open PRs instead of one per PR. An idle
    # repo with N open PRs used to cost N ``gh api graphql`` subprocesses per
    # tick just to discover nothing needed repair (issue #226 — N+1 fan-out).
    numbers = [int(pr["number"]) for pr in prs if pr.get("number") is not None]
    threads_by_pr = review_threads_batch(numbers, repo=repo)

    repairs: list[dict[str, Any]] = []
    for pr in prs:
        reasons: list[str] = []
        labels = _pr_label_names(pr)
        if "critic:blocking" in labels:
            reasons.append("critic:blocking")

        merge_state = str(pr.get("mergeStateStatus") or "").upper()
        if merge_state in {"DIRTY", "CONFLICTING"}:
            reasons.append(f"merge_state:{merge_state.lower()}")

        all_threads = threads_by_pr.get(int(pr["number"]), [])
        threads = [t for t in all_threads if not bool(t.get("isResolved"))]

        # #230: leftover sev3 *critic* threads on an APPROVED + CLEAN PR are
        # not, on their own, a repair trigger. They only count when the PR is
        # otherwise blocked (block label / DIRTY / CONFLICTING — i.e. NOT
        # approved-mergeable), where a human review or merge conflict is the
        # real driver. An unresolved *human* thread, however, keeps the PR out
        # of the approved-mergeable set (AC3) — ``is_approved_mergeable`` is
        # passed the threads so it can make that distinction.
        approved_mergeable = is_approved_mergeable(pr, unresolved_threads=threads)
        if threads and not approved_mergeable:
            reasons.append("unresolved_review_threads")

        if reasons:
            enriched = dict(pr)
            enriched["repairReasons"] = reasons
            enriched["unresolvedReviewThreads"] = threads
            repairs.append(enriched)
        elif threads and approved_mergeable and on_skip is not None:
            # Approved + CLEAN with only leftover sev3 critic threads → terminal.
            # Surface the exclusion so the caller emits a skip event (#230 AC1/AC5).
            enriched = dict(pr)
            enriched["unresolvedReviewThreads"] = threads
            enriched["approvedMergeableSkip"] = True
            on_skip(enriched)

    return sorted(repairs, key=lambda p: str(p.get("updatedAt") or ""))[:limit]


def open_prs(limit: int, repo: str | None = None) -> list[dict[str, Any]]:
    """Return all open PRs with the field set the #213 adoption scan needs.

    Unlike :func:`prs_by_label`, this includes ``mergeStateStatus`` so the
    orphaned-PR adoption scan can gate auto-merge on ``CLEAN``. Sorted oldest
    updated first so the oldest orphan is adopted first.
    """
    repo = _require_repo(repo)
    prs = _open_prs(limit=limit, repo=repo)
    return sorted(prs, key=lambda p: str(p.get("updatedAt") or ""))


def _open_prs(limit: int, repo: str) -> list[dict[str, Any]]:
    cmd = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "open",
        "--limit",
        str(limit),
        "--json",
        "number,title,body,headRefName,baseRefName,url,labels,updatedAt,mergeStateStatus",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return []
    try:
        result: list[dict[str, Any]] = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    return result


def pr_review_context(pr: int | str, repo: str | None = None) -> str:
    """Fetch review/comment context for a repair worker prompt."""
    repo = _require_repo(repo)
    r = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--comments",
            "--json",
            "number,title,body,comments,reviews,url,headRefName",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return f"(failed to fetch PR review context: {r.stderr[:300]})"
    try:
        obj = json.loads(r.stdout)
    except json.JSONDecodeError:
        return "(failed to parse PR review context)"
    obj["reviewThreads"] = review_threads(pr, repo=repo)
    return _format_pr_context(obj)


def unresolved_review_threads(pr: int | str, repo: str | None = None) -> list[dict[str, Any]]:
    """Return unresolved PR review threads. Empty on API failure."""
    return [t for t in review_threads(pr, repo=repo) if not bool(t.get("isResolved"))]


# Shared GraphQL selection for a PR's review threads. Used by both the
# single-PR (``review_threads``) and the batched (``review_threads_batch``)
# fetchers so the two cannot drift in the fields they request.
_REVIEW_THREADS_SELECTION = """
          reviewThreads(first: 100) {
            nodes {
              id
              isResolved
              isOutdated
              path
              line
              comments(first: 20) {
                nodes {
                  author { login }
                  body
                  url
                  path
                  line
                  createdAt
                }
              }
            }
          }
"""


def _parse_thread_nodes(nodes: Any) -> list[dict[str, Any]]:
    if not isinstance(nodes, list):
        return []
    return [_normalise_review_thread(t) for t in nodes if isinstance(t, dict)]


def review_threads(pr: int | str, repo: str | None = None) -> list[dict[str, Any]]:
    """Fetch PR review threads via GraphQL.

    GitHub's REST and ``gh pr view --comments`` output omit inline review
    threads. Those are the comments operators expect a repair worker to fix,
    so silently losing them makes the loop appear idle even though PRs are
    still blocked.
    """
    repo = _require_repo(repo)
    try:
        owner, name = repo.split("/", 1)
    except ValueError:
        return []
    query = (
        "query($owner: String!, $name: String!, $number: Int!) {\n"
        "  repository(owner: $owner, name: $name) {\n"
        "    pullRequest(number: $number) {\n"
        f"{_REVIEW_THREADS_SELECTION}"
        "    }\n"
        "  }\n"
        "}\n"
    )
    r = subprocess.run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={int(_pr_number(pr))}",
            "-f",
            f"query={query}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return []
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    nodes = (
        data.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
        .get("nodes", [])
    )
    return _parse_thread_nodes(nodes)


# Max PR aliases per batched GraphQL query. Each alias requests
# ``reviewThreads(first: 100)`` with nested comments, so a single query over
# all 50 open PRs can blow GitHub's GraphQL node/complexity budget and fail
# wholesale. Chunking bounds per-query complexity and limits the blast radius
# of any one chunk failing (issue #226 review — sev2/correctness).
_REVIEW_THREADS_BATCH_CHUNK = 10


def review_threads_batch(
    pr_numbers: list[int],
    repo: str | None = None,
) -> dict[int, list[dict[str, Any]]]:
    """Fetch review threads for many PRs, batching GraphQL round-trips.

    Replaces the per-PR ``review_threads`` fan-out (issue #226): an idle repo
    with N open PRs cost N ``gh api graphql`` subprocesses per tick just to
    discover nothing needed repair. PRs are batched into aliased queries of at
    most ``_REVIEW_THREADS_BATCH_CHUNK`` so a real workload's nested
    ``reviewThreads`` selections stay under GitHub's GraphQL complexity limits.

    Failure handling preserves the per-PR fetcher's blast radius: if a chunk
    query fails (non-zero return, JSON error, or malformed payload), it is
    logged and each PR in that chunk falls back to its own ``review_threads``
    call, so a single failed batch never silently degrades *every* PR to "no
    threads" and drops the ``unresolved_review_threads`` repair reason.

    Returns a ``{pr_number: [normalised threads]}`` map; PRs missing from the
    response map to ``[]``. An empty ``pr_numbers`` issues no subprocess at all.
    """
    repo = _require_repo(repo)
    # De-dup while preserving order so each PR gets exactly one alias.
    seen: set[int] = set()
    numbers: list[int] = []
    for raw in pr_numbers:
        num = int(raw)
        if num not in seen:
            seen.add(num)
            numbers.append(num)
    if not numbers:
        return {}

    out: dict[int, list[dict[str, Any]]] = {}
    for start in range(0, len(numbers), _REVIEW_THREADS_BATCH_CHUNK):
        chunk = numbers[start : start + _REVIEW_THREADS_BATCH_CHUNK]
        batched = _review_threads_chunk(chunk, repo=repo)
        if batched is None:
            # Chunk-level failure: fall back to per-PR fetch so only the PRs
            # whose individual queries *also* fail degrade to [] — instead of
            # the whole chunk silently looking idle.
            logger.warning(
                "review_threads_batch: batched query failed for PRs %s; "
                "falling back to per-PR fetch",
                chunk,
            )
            for num in chunk:
                out[num] = review_threads(num, repo=repo)
        else:
            out.update(batched)
    return out


def _review_threads_chunk(
    numbers: list[int],
    repo: str,
) -> dict[int, list[dict[str, Any]]] | None:
    """Fetch one chunk of PRs in a single aliased GraphQL query.

    Returns the ``{pr_number: [threads]}`` map on success, or ``None`` to
    signal a chunk-level failure (non-zero return, JSON error, or malformed
    payload) so the caller can fall back to per-PR fetches.
    """
    if not numbers:
        return {}
    try:
        owner, name = repo.split("/", 1)
    except ValueError:
        return None

    aliases = "\n".join(
        f"    pr{idx}: pullRequest(number: {num}) {{\n{_REVIEW_THREADS_SELECTION}    }}"
        for idx, num in enumerate(numbers)
    )
    query = (
        "query($owner: String!, $name: String!) {\n"
        "  repository(owner: $owner, name: $name) {\n"
        f"{aliases}\n"
        "  }\n"
        "}\n"
    )
    r = subprocess.run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-f",
            f"query={query}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    # A partial GraphQL failure still returns 0 but carries top-level errors and
    # a null repository; treat that as a chunk failure so callers fall back.
    if data.get("errors"):
        return None
    repo_data = (data.get("data") or {}).get("repository") or {}
    if not isinstance(repo_data, dict):
        return None
    out: dict[int, list[dict[str, Any]]] = {}
    for idx, num in enumerate(numbers):
        pr_obj = repo_data.get(f"pr{idx}") or {}
        nodes = (pr_obj.get("reviewThreads") or {}).get("nodes", [])
        out[num] = _parse_thread_nodes(nodes)
    return out


def _normalise_review_thread(thread: dict[str, Any]) -> dict[str, Any]:
    comments = []
    for comment in (thread.get("comments") or {}).get("nodes") or []:
        if not isinstance(comment, dict):
            continue
        comments.append(
            {
                "author": comment.get("author") or {},
                "body": comment.get("body") or "",
                "url": comment.get("url") or "",
                "path": comment.get("path") or thread.get("path") or "",
                "line": comment.get("line") or thread.get("line") or "",
                "createdAt": comment.get("createdAt") or "",
            }
        )
    return {
        "id": thread.get("id") or "",
        "isResolved": bool(thread.get("isResolved")),
        "isOutdated": bool(thread.get("isOutdated")),
        "path": thread.get("path") or "",
        "line": thread.get("line") or "",
        "comments": comments,
    }


def _format_pr_context(obj: dict[str, Any]) -> str:
    lines = [
        f"PR #{obj.get('number')}: {obj.get('title') or ''}",
        f"URL: {obj.get('url') or ''}",
        f"Head: {obj.get('headRefName') or ''}",
        "",
        "PR BODY:",
        str(obj.get("body") or "").strip() or "(empty)",
    ]
    comments = obj.get("comments") or []
    if comments:
        lines.extend(["", "TOP-LEVEL COMMENTS:"])
        for c in comments[-10:]:
            author = (c.get("author") or {}).get("login") or "unknown"
            body = str(c.get("body") or "").strip()
            if body:
                lines.append(f"- {author}: {body[:1500]}")
    reviews = obj.get("reviews") or []
    if reviews:
        lines.extend(["", "REVIEWS:"])
        for r in reviews[-10:]:
            author = (r.get("author") or {}).get("login") or "unknown"
            body = str(r.get("body") or "").strip()
            state = r.get("state") or ""
            if body:
                lines.append(f"- {author} [{state}]: {body[:1500]}")
    threads = obj.get("reviewThreads") or []
    if threads:
        lines.extend(["", "REVIEW THREADS:"])
        for t in threads[-20:]:
            resolved = t.get("isResolved")
            for c in t.get("comments") or []:
                author = (c.get("author") or {}).get("login") or "unknown"
                path = c.get("path") or ""
                line = c.get("line") or ""
                body = str(c.get("body") or "").strip()
                if body:
                    lines.append(f"- {path}:{line} resolved={resolved} {author}: {body[:1500]}")
    return "\n".join(lines)[:12000]


def add_pr_label(pr: int | str, labels: list[str], repo: str | None = None) -> bool:
    """Add labels to a PR. ``pr`` may be a PR number or URL.

    `gh pr edit` shares the issue-edit code path under the hood, but using
    the PR-specific subcommand avoids ambiguity when issue/PR numbers
    overlap and makes the intent grep-able.
    """
    if not labels:
        return True
    repo = _require_repo(repo)
    cmd = ["gh", "pr", "edit", str(pr), "--repo", repo]
    for lab in labels:
        cmd.extend(["--add-label", lab])
    r = subprocess.run(cmd, check=False, capture_output=True, text=True)
    return r.returncode == 0


def disable_pr_auto_merge(pr: int | str, repo: str | None = None) -> bool:
    """Disable auto-merge on a PR. Best-effort: returns False if the call
    fails (e.g. auto-merge was never enabled — which is fine)."""
    repo = _require_repo(repo)
    r = subprocess.run(
        ["gh", "pr", "merge", str(pr), "--repo", repo, "--disable-auto"],
        check=False,
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def enable_pr_auto_merge(pr: int | str, repo: str | None = None) -> bool:
    """Enable squash auto-merge for a PR. Best-effort: returns False on failure."""
    repo = _require_repo(repo)
    r = subprocess.run(
        [
            "gh",
            "pr",
            "merge",
            str(pr),
            "--repo",
            repo,
            "--squash",
            "--auto",
            "--delete-branch",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def post_review_comment(
    pr: int | str,
    body: str,
    file: str | None = None,
    line: int | None = None,
    repo: str | None = None,
) -> bool:
    """Post a critic finding to a PR so it ALWAYS lands.

    When ``file``+``line`` are provided, an inline review comment is attempted
    first via the GitHub API. GitHub **422-rejects an inline comment whose line
    is not part of the PR's diff** — a routine case, since critic findings often
    reference lines outside the changed hunks. On any such failure we **fall
    back** to a plain ``--comment`` review with the location prepended to the
    text, rather than dropping the finding.

    This fallback is load-bearing, not cosmetic: the repair worker rebuilds its
    brief from the posted review context (see ``pr_review_context``), so a
    silently-dropped finding blinds the repair loop and it never converges. The
    finding must reach the PR by *some* path; inline is a nicety, landing it is
    the contract. Returns True if the finding was posted by either path.
    """
    repo = _require_repo(repo)
    if file is not None and line is not None:
        payload = {
            "body": body,
            "event": "COMMENT",
            "comments": [{"path": file, "line": int(line), "body": body}],
        }
        r = subprocess.run(
            [
                "gh",
                "api",
                "--method",
                "POST",
                f"repos/{repo}/pulls/{_pr_number(pr)}/reviews",
                "--input",
                "-",
            ],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            check=False,
        )
        if r.returncode == 0:
            return True
        # Inline rejected (commonly a 422: line not in the diff). Fall back to a
        # plain review comment so the finding still reaches the repair worker,
        # with the location preserved in text.
        body = f"`{file}:{line}` — {body}"
    r = subprocess.run(
        ["gh", "pr", "review", str(pr), "--repo", repo, "--comment", "--body", body],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.returncode == 0


def get_issue_state(issue: int, repo: str | None = None) -> str | None:
    """Return the issue's current state (``"OPEN"``/``"CLOSED"``) or ``None``
    on failure (network, auth, bad number).

    Used by the runner's pre-merge gate (issue #65): an operator who closes
    an issue mid-flight (close-as-dup, not-planned, scope change) expects
    the loop to STOP, even if a worker has already opened a PR. Callers
    treat ``None`` conservatively — same as CLOSED — because the operator's
    intent to stop must not be silently overridden by a transient gh
    outage.
    """
    repo = _require_repo(repo)
    r = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(issue),
            "--repo",
            repo,
            "--json",
            "state",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return None
    try:
        obj = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    state = obj.get("state")
    if not isinstance(state, str):
        return None
    return state.upper()


def pr_comment(pr: int | str, body: str, repo: str | None = None) -> bool:
    """Post a top-level comment on a PR. Returns True on success."""
    repo = _require_repo(repo)
    r = subprocess.run(
        ["gh", "pr", "comment", str(pr), "--repo", repo, "--body", body],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.returncode == 0


def _pr_number(pr: int | str) -> str:
    """Extract a PR number from an int / URL / numeric string."""
    s = str(pr)
    if "/" in s:
        return s.rstrip("/").rsplit("/", 1)[-1]
    return s


def close_issue(
    issue: int,
    reason: str | None = None,
    comment_body: str | None = None,
    repo: str | None = None,
) -> bool:
    """Close an issue (optionally with a comment + reason).

    ``reason`` is one of ``completed`` / ``not planned`` (gh CLI convention).
    """
    repo = _require_repo(repo)
    if comment_body:
        comment(issue, comment_body, repo)
    cmd = ["gh", "issue", "close", str(issue), "--repo", repo]
    if reason:
        cmd.extend(["--reason", reason])
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return r.returncode == 0
