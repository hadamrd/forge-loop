"""GitHub issue operations backed by :mod:`forge_loop.gh_client`."""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from forge_loop.gh_client import GhClient, GithubkitClient, Issue

_GH_CLIENT: GhClient | None = None


def require_repo(repo: str | None) -> str:
    if not repo:
        raise RuntimeError("gh.* called without a repo; pass repo='owner/name' or set LOOP_GH_REPO")
    return repo


def split_repo(repo: str) -> tuple[str, str]:
    try:
        owner, name = repo.split("/", 1)
    except ValueError as exc:
        raise RuntimeError(f"invalid GitHub repo {repo!r}; expected owner/name") from exc
    if not owner or not name:
        raise RuntimeError(f"invalid GitHub repo {repo!r}; expected owner/name")
    return owner, name


def set_client(client: GhClient | None) -> None:
    global _GH_CLIENT
    _GH_CLIENT = client


def client() -> GhClient:
    global _GH_CLIENT
    if _GH_CLIENT is None:
        _GH_CLIENT = GithubkitClient()
    return _GH_CLIENT


def top_issues(label: str, limit: int, repo: str | None = None) -> list[dict[str, Any]]:
    repo = require_repo(repo)
    owner, name = split_repo(repo)
    return [_issue_payload(issue) for issue in client().issues_by_label(owner, name, label, limit)]


def fetch_issue(issue: int, repo: str | None = None) -> dict[str, Any] | None:
    repo = require_repo(repo)
    owner, name = split_repo(repo)
    found = client().get_issue(owner, name, issue)
    if found is None:
        return None
    return _issue_payload(found)


def comment(issue: int, body: str, repo: str | None = None) -> None:
    repo = require_repo(repo)
    owner, name = split_repo(repo)
    with suppress(Exception):
        client().add_comment(owner, name, issue, body)


def label(issue: int, labels: list[str], repo: str | None = None) -> None:
    if not labels:
        return
    repo = require_repo(repo)
    owner, name = split_repo(repo)
    with suppress(Exception):
        client().add_labels(owner, name, issue, labels)


def unlabel(issue: int, label: str, repo: str | None = None) -> None:
    repo = require_repo(repo)
    owner, name = split_repo(repo)
    with suppress(Exception):
        client().remove_label(owner, name, issue, label)


def create_issue(
    title: str,
    body: str,
    labels: list[str] | None = None,
    repo: str | None = None,
) -> int | None:
    repo = require_repo(repo)
    owner, name = split_repo(repo)
    try:
        issue = client().create_issue(owner, name, title, body, labels or [])
    except Exception:
        return None
    return issue.number


def _issue_payload(issue: Issue) -> dict[str, Any]:
    return {
        "number": issue.number,
        "title": issue.title,
        "body": issue.body,
        "state": issue.state,
        "labels": [{"name": label} for label in issue.labels],
        "createdAt": "",
        "updatedAt": "",
    }

