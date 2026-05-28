"""Thin wrapper around the `gh` CLI for issue ops.

All functions require ``repo="<owner>/<name>"``. The runner passes
``cfg.github_repo`` (from LOOP_GH_REPO or YAML). No hardcoded default.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

DEFAULT_REPO: str | None = None


def _require_repo(repo: str | None) -> str:
    if not repo:
        raise RuntimeError(
            "gh.* called without a repo; pass repo='owner/name' or set LOOP_GH_REPO"
        )
    return repo


def top_issues(label: str, limit: int, repo: str | None = None) -> list[dict[str, Any]]:
    """Return open issues carrying ``label`` (oldest first)."""
    repo = _require_repo(repo)
    cmd = [
        "gh", "issue", "list",
        "--repo", repo,
        "--state", "open",
        "--limit", str(limit),
        "--json", "number,title,body,labels,createdAt,updatedAt",
    ]
    if label:
        cmd.extend(["--label", label])
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    result: list[dict[str, Any]] = json.loads(out.stdout)
    return result


def fetch_issue(issue: int, repo: str | None = None) -> dict[str, Any] | None:
    """Fetch a single issue by number. Returns None if not found / failed."""
    repo = _require_repo(repo)
    r = subprocess.run(
        [
            "gh", "issue", "view", str(issue),
            "--repo", repo,
            "--json", "number,title,body,labels,createdAt,updatedAt,state",
        ],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        return None
    result: dict[str, Any] = json.loads(r.stdout)
    return result


def comment(issue: int, body: str, repo: str | None = None) -> None:
    """Post a comment to an issue. Errors are swallowed (caller logs)."""
    repo = _require_repo(repo)
    subprocess.run(
        ["gh", "issue", "comment", str(issue), "--repo", repo, "--body", body],
        check=False, capture_output=True,
    )


def label(issue: int, labels: list[str], repo: str | None = None) -> None:
    """Add labels to an issue."""
    if not labels:
        return
    repo = _require_repo(repo)
    cmd = ["gh", "issue", "edit", str(issue), "--repo", repo]
    for lab in labels:
        cmd.extend(["--add-label", lab])
    subprocess.run(cmd, check=False, capture_output=True)


def unlabel(issue: int, label: str, repo: str | None = None) -> None:
    """Remove a single label from an issue."""
    repo = _require_repo(repo)
    subprocess.run(
        ["gh", "issue", "edit", str(issue), "--repo", repo, "--remove-label", label],
        check=False, capture_output=True,
    )


def remove_pr_label(pr: int | str, label: str, repo: str | None = None) -> bool:
    """Remove a label from a PR. Best-effort: returns False on failure."""
    repo = _require_repo(repo)
    r = subprocess.run(
        ["gh", "pr", "edit", str(pr), "--repo", repo, "--remove-label", label],
        check=False, capture_output=True, text=True,
    )
    return r.returncode == 0


def create_issue(
    title: str,
    body: str,
    labels: list[str] | None = None,
    repo: str | None = None,
) -> int | None:
    """Open a new issue. Returns the new number or None on failure."""
    repo = _require_repo(repo)
    cmd = [
        "gh", "issue", "create",
        "--repo", repo,
        "--title", title,
        "--body", body,
    ]
    for lab in (labels or []):
        cmd.extend(["--label", lab])
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        return None
    last_line = (r.stdout.strip().splitlines() or [""])[-1]
    if "/issues/" in last_line:
        try:
            return int(last_line.rstrip("/").split("/")[-1])
        except (ValueError, IndexError):
            return None
    return None


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
    for lab in (add_labels or []):
        cmd.extend(["--add-label", lab])
    for lab in (remove_labels or []):
        cmd.extend(["--remove-label", lab])
    if len(cmd) == 5:
        return True
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return r.returncode == 0


def pr_changed_lines(pr: int | str, repo: str | None = None) -> int:
    """Return additions+deletions for a PR. 0 on failure (caller falls back)."""
    repo = _require_repo(repo)
    r = subprocess.run(
        ["gh", "pr", "view", str(pr), "--repo", repo,
         "--json", "additions,deletions"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        return 0
    try:
        obj = json.loads(r.stdout)
        return int(obj.get("additions", 0)) + int(obj.get("deletions", 0))
    except (json.JSONDecodeError, ValueError, TypeError):
        return 0


def prs_by_label(label: str, limit: int, repo: str | None = None) -> list[dict[str, Any]]:
    """Return open PRs carrying ``label`` (oldest updated first)."""
    repo = _require_repo(repo)
    cmd = [
        "gh", "pr", "list",
        "--repo", repo,
        "--state", "open",
        "--limit", str(limit),
        "--json", "number,title,body,headRefName,baseRefName,url,labels,updatedAt",
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


def pr_review_context(pr: int | str, repo: str | None = None) -> str:
    """Fetch review/comment context for a repair worker prompt."""
    repo = _require_repo(repo)
    r = subprocess.run(
        [
            "gh", "pr", "view", str(pr),
            "--repo", repo,
            "--comments",
            "--json", "number,title,body,comments,reviews,url,headRefName",
        ],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        return f"(failed to fetch PR review context: {r.stderr[:300]})"
    try:
        obj = json.loads(r.stdout)
    except json.JSONDecodeError:
        return "(failed to parse PR review context)"
    return _format_pr_context(obj)


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
        check=False, capture_output=True, text=True,
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
                "gh", "api",
                "--method", "POST",
                f"repos/{repo}/pulls/{_pr_number(pr)}/reviews",
                "--input", "-",
            ],
            input=json.dumps(payload),
            text=True, capture_output=True, check=False,
        )
        return r.returncode == 0
    r = subprocess.run(
        ["gh", "pr", "review", str(pr), "--repo", repo, "--comment", "--body", body],
        capture_output=True, text=True, check=False,
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
            "gh", "issue", "view", str(issue),
            "--repo", repo,
            "--json", "state",
        ],
        capture_output=True, text=True, check=False,
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
        capture_output=True, text=True, check=False,
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
