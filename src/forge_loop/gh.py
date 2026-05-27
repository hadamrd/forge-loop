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
