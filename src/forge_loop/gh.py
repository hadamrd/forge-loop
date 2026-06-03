"""Thin wrapper around the `gh` CLI for issue ops.

All functions require ``repo="<owner>/<name>"``. The runner passes
``cfg.github_repo`` (from LOOP_GH_REPO or YAML). No hardcoded default.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from forge_loop import gh_issues

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


def prs_requiring_repair(limit: int, repo: str | None = None) -> list[dict[str, Any]]:
    """Return open PRs the repair loop should revisit.

    A PR needs repair when it is explicitly critic-blocked, has unresolved
    review threads, or is merge-conflicted/dirty. Review threads are not
    exposed by ``gh pr list`` or ``gh pr view --comments``, so this function
    enriches the open PR list with a GraphQL pass before the dispatcher decides
    whether to spawn a repair worker.
    """
    repo = _require_repo(repo)
    prs = _open_prs(limit=max(limit, 50), repo=repo)
    repairs: list[dict[str, Any]] = []
    for pr in prs:
        reasons: list[str] = []
        labels = {str(label.get("name") or "") for label in pr.get("labels") or []}
        if "critic:blocking" in labels:
            reasons.append("critic:blocking")

        merge_state = str(pr.get("mergeStateStatus") or "").upper()
        if merge_state in {"DIRTY", "CONFLICTING"}:
            reasons.append(f"merge_state:{merge_state.lower()}")

        threads = unresolved_review_threads(pr["number"], repo=repo)
        if threads:
            reasons.append("unresolved_review_threads")

        if reasons:
            enriched = dict(pr)
            enriched["repairReasons"] = reasons
            enriched["unresolvedReviewThreads"] = threads
            repairs.append(enriched)

    return sorted(repairs, key=lambda p: str(p.get("updatedAt") or ""))[:limit]


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
    query = """
    query($owner: String!, $name: String!, $number: Int!) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $number) {
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
        }
      }
    }
    """
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
    if not isinstance(nodes, list):
        return []
    return [_normalise_review_thread(t) for t in nodes if isinstance(t, dict)]


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
    """Post a review on a PR. When ``file``+``line`` are provided, post as a
    single inline comment via the GitHub API (``gh api``). Otherwise post a
    plain ``--comment`` review with ``body`` as the summary.

    Returns True on success, False on API failure (caller decides whether to
    fall back to a summary comment).
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
        return r.returncode == 0
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
