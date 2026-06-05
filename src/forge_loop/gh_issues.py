"""GitHub operations facade — the module-level surface the loop calls.

Every function here delegates to a single process-wide
:class:`forge_loop.gh_client.GithubkitClient` (the only thing that talks to
GitHub). This module is the stable, stringly-shaped call surface the runner +
CLI depend on (e.g. ``top_issues`` returns ``list[dict]``,
``pr_review_context`` returns a formatted string). It exists so call sites
stay out of subprocess / gh-CLI territory and never construct the SDK client
themselves.

The legacy ``forge_loop.gh`` (a ``gh`` CLI subprocess wrapper) was DELETED in
issue #223. Importers were repointed here (``from forge_loop import gh_issues
as gh``), so the ``gh.<fn>(...)`` call shape is preserved byte-for-byte.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from forge_loop.gh_client import GhClient, GithubkitClient, Issue

_GH_CLIENT: GhClient | None = None

#: Best-effort label for telemetry. Reflects the configured client's auth
#: source once one is constructed; ``"github-client"`` until then. Callers read
#: it via ``getattr(gh, "auth_source", "github-client")``.
auth_source = "github-client"


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
    global _GH_CLIENT, auth_source
    _GH_CLIENT = client
    if client is not None:
        auth_source = getattr(client, "auth_source", "github-client")


def client() -> GhClient:
    global _GH_CLIENT, auth_source
    if _GH_CLIENT is None:
        _GH_CLIENT = GithubkitClient()
        auth_source = getattr(_GH_CLIENT, "auth_source", "github-client")
    return _GH_CLIENT


def _owner_name(repo: str | None) -> tuple[str, str]:
    return split_repo(require_repo(repo))


def _pr_number(pr: int | str) -> int:
    """Extract a PR number from an int / URL / numeric string."""
    s = str(pr)
    if "/" in s:
        s = s.rstrip("/").rsplit("/", 1)[-1]
    return int(s)


# --------------------------------------------------------------------------- #
# Issues
# --------------------------------------------------------------------------- #


def top_issues(label: str, limit: int, repo: str | None = None) -> list[dict[str, Any]]:
    """Return open issues carrying ``label`` (oldest first)."""
    owner, name = _owner_name(repo)
    return [_issue_payload(issue) for issue in client().issues_by_label(owner, name, label, limit)]


def fetch_issue(issue: int, repo: str | None = None) -> dict[str, Any] | None:
    """Fetch a single issue by number. None if not found / failed."""
    owner, name = _owner_name(repo)
    found = client().get_issue(owner, name, issue)
    return _issue_payload(found) if found is not None else None


def issue_comment_bodies(issue: int, repo: str | None = None) -> list[str]:
    """Fetch issue comment bodies. [] on GitHub/API failure."""
    owner, name = _owner_name(repo)
    return client().issue_comment_bodies(owner, name, issue)


def issue_comments(issue: int, repo: str | None = None) -> list[dict[str, Any]]:
    """Fetch issue comments as ``[{body, createdAt}, ...]``. [] on failure."""
    owner, name = _owner_name(repo)
    return client().issue_comments(owner, name, issue)


def comment(issue: int, body: str, repo: str | None = None) -> None:
    """Post a comment to an issue. Errors are swallowed (caller logs)."""
    owner, name = _owner_name(repo)
    with suppress(Exception):
        client().add_comment(owner, name, issue, body)


def label(issue: int, labels: list[str], repo: str | None = None) -> None:
    """Add labels to an issue."""
    if not labels:
        return
    owner, name = _owner_name(repo)
    with suppress(Exception):
        client().add_labels(owner, name, issue, labels)


def unlabel(issue: int, label: str, repo: str | None = None) -> None:
    """Remove a single label from an issue."""
    owner, name = _owner_name(repo)
    with suppress(Exception):
        client().remove_label(owner, name, issue, label)


def create_issue(
    title: str,
    body: str,
    labels: list[str] | None = None,
    repo: str | None = None,
) -> int | None:
    """Open a new issue. Returns the new number or None on failure."""
    owner, name = _owner_name(repo)
    try:
        issue = client().create_issue(owner, name, title, body, labels or [])
    except Exception:
        return None
    return issue.number


def update_issue(
    issue: int,
    title: str | None = None,
    body: str | None = None,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    repo: str | None = None,
) -> bool:
    """Patch an issue. Returns True on success."""
    owner, name = _owner_name(repo)
    return client().update_issue(
        owner,
        name,
        issue,
        title=title,
        body=body,
        add_labels=add_labels,
        remove_labels=remove_labels,
    )


def get_issue_state(issue: int, repo: str | None = None) -> str | None:
    """Return ``"OPEN"`` / ``"CLOSED"`` or None on failure.

    Callers treat None conservatively (same as CLOSED) so a transient API
    outage never silently lands work on a closed ticket (issue #65).
    """
    owner, name = _owner_name(repo)
    return client().get_issue_state(owner, name, issue)


def close_issue(
    issue: int,
    reason: str | None = None,
    comment_body: str | None = None,
    repo: str | None = None,
) -> bool:
    """Close an issue (optionally with a comment + reason).

    ``reason`` is ``completed`` / ``not planned`` (gh CLI convention; the
    client normalises it to the REST ``state_reason``).
    """
    owner, name = _owner_name(repo)
    if comment_body:
        comment(issue, comment_body, repo)
    return client().close_issue(owner, name, issue, reason=reason)


def create_labels(repo: str, labels: list[tuple[str, str, str]]) -> list[str]:
    """Create the loop's vocabulary labels (idempotent). Returns names CREATED.

    Each label is ``(name, color_hex, description)``. Already-existing labels
    are silently skipped (the create returns False for them).
    """
    owner, name = split_repo(repo)
    created: list[str] = []
    cl = client()
    for lab_name, color, desc in labels:
        if cl.create_label(owner, name, lab_name, color, desc):
            created.append(lab_name)
    return created


# --------------------------------------------------------------------------- #
# Pull requests — listing
# --------------------------------------------------------------------------- #


def prs_by_label(label: str, limit: int, repo: str | None = None) -> list[dict[str, Any]]:
    """Return open PRs carrying ``label`` (oldest updated first)."""
    owner, name = _owner_name(repo)
    prs = client().list_open_prs(owner, name, label=label or None, limit=limit)
    return sorted(prs, key=lambda p: str(p.get("updatedAt") or ""))


def open_prs(limit: int, repo: str | None = None) -> list[dict[str, Any]]:
    """Return all open PRs (incl. ``mergeStateStatus``), oldest updated first."""
    owner, name = _owner_name(repo)
    prs = client().list_open_prs(owner, name, limit=limit)
    return sorted(prs, key=lambda p: str(p.get("updatedAt") or ""))


def prs_requiring_repair(limit: int, repo: str | None = None) -> list[dict[str, Any]]:
    """Return open PRs the repair loop should revisit.

    A PR needs repair when it is critic-blocked, has unresolved review threads,
    or is merge-conflicted/dirty. Enriches the open-PR list with ONE batched
    GraphQL review-threads pass (issue #226 — no per-PR N+1 fan-out). Idle
    short-circuit: with no open PRs there is nothing to enrich.
    """
    owner, name = _owner_name(repo)
    cl = client()
    prs = sorted(
        cl.list_open_prs(owner, name, limit=max(limit, 50)),
        key=lambda p: str(p.get("updatedAt") or ""),
    )
    if not prs:
        return []
    numbers = [int(pr["number"]) for pr in prs if pr.get("number") is not None]
    threads_by_pr = cl.review_threads_batch(owner, name, numbers)

    repairs: list[dict[str, Any]] = []
    for pr in prs:
        reasons: list[str] = []
        labels = {str(lab.get("name") or "") for lab in pr.get("labels") or []}
        if "critic:blocking" in labels:
            reasons.append("critic:blocking")

        merge_state = str(pr.get("mergeStateStatus") or "").upper()
        if merge_state in {"DIRTY", "CONFLICTING"}:
            reasons.append(f"merge_state:{merge_state.lower()}")

        all_threads = threads_by_pr.get(int(pr["number"]), [])
        threads = [t for t in all_threads if not bool(t.get("isResolved"))]
        if threads:
            reasons.append("unresolved_review_threads")

        if reasons:
            enriched = dict(pr)
            enriched["repairReasons"] = reasons
            enriched["unresolvedReviewThreads"] = threads
            repairs.append(enriched)

    return sorted(repairs, key=lambda p: str(p.get("updatedAt") or ""))[:limit]


# --------------------------------------------------------------------------- #
# Pull requests — reads
# --------------------------------------------------------------------------- #


def pr_changed_lines(pr: int | str, repo: str | None = None) -> int:
    """additions + deletions for a PR. 0 on failure (caller falls back)."""
    owner, name = _owner_name(repo)
    return client().pr_changed_lines(owner, name, _pr_number(pr))


def pr_changed_files(pr_url: str, repo: str) -> list[str]:
    """Return the file paths a PR touches. [] on failure.

    ``repo`` is the ``owner/name`` string (the legacy critic call passed the
    local checkout ``Path``; that is no longer needed — files come from the
    REST API). Accepts a ``Path``-like too via ``str()`` for back-compat.
    """
    owner, name = split_repo(str(repo).strip() or "")
    return client().pr_changed_files(owner, name, _pr_number(pr_url))


def pr_diff(pr_url: str, repo: str) -> str:
    """Return the unified diff for a PR. "" on failure (graceful degrade)."""
    owner, name = split_repo(str(repo).strip() or "")
    return client().pr_diff(owner, name, _pr_number(pr_url))


def pr_precommit_context(pr_url: str, repo: str) -> tuple[str, str]:
    """Return (PR body, commit metadata text). ("","") on failure."""
    owner, name = split_repo(str(repo).strip() or "")
    return client().pr_precommit_context(owner, name, _pr_number(pr_url))


def pr_status_failed(pr: int | str, repo: str | None = None) -> bool:
    """True iff any required check on the PR is in a failing terminal state."""
    owner, name = _owner_name(repo)
    return client().pr_status_failed(owner, name, _pr_number(pr))


def find_pr_by_head(head: str, repo: str | None = None) -> dict[str, Any] | None:
    """Return the most recent PR (any state) for head branch ``head``, or None."""
    owner, name = _owner_name(repo)
    return client().find_pr_by_head(owner, name, head)


def latest_critic_report(pr: int | str, repo: str | None = None) -> str:
    """Return the most recent critic-report comment body on a PR, or ""."""
    owner, name = _owner_name(repo)
    return client().latest_critic_report(owner, name, _pr_number(pr))


# --------------------------------------------------------------------------- #
# Review threads
# --------------------------------------------------------------------------- #


def review_threads(pr: int | str, repo: str | None = None) -> list[dict[str, Any]]:
    """Fetch a PR's review threads via GraphQL. [] on failure."""
    owner, name = _owner_name(repo)
    return client().review_threads(owner, name, _pr_number(pr))


def unresolved_review_threads(pr: int | str, repo: str | None = None) -> list[dict[str, Any]]:
    """Return unresolved PR review threads. [] on API failure."""
    return [t for t in review_threads(pr, repo=repo) if not bool(t.get("isResolved"))]


def review_threads_batch(
    pr_numbers: list[int],
    repo: str | None = None,
) -> dict[int, list[dict[str, Any]]]:
    """Fetch review threads for many PRs, batching GraphQL round-trips (#226)."""
    owner, name = _owner_name(repo)
    return client().review_threads_batch(owner, name, [int(n) for n in pr_numbers])


def pr_review_context(pr: int | str, repo: str | None = None) -> str:
    """Fetch review/comment context for a repair worker prompt (formatted str)."""
    owner, name = _owner_name(repo)
    number = _pr_number(pr)
    cl = client()
    pull = cl.get_pull(owner, name, number)
    if pull is None:
        return "(failed to fetch PR review context)"
    obj: dict[str, Any] = {
        "number": pull.number,
        "title": pull.title,
        "body": pull.body,
        "url": f"https://github.com/{owner}/{name}/pull/{pull.number}",
        "headRefName": pull.head_ref,
        "comments": [
            {"author": {"login": ""}, "body": b}
            for b in cl.issue_comment_bodies(owner, name, number)
        ],
        "reviews": [],
        "reviewThreads": cl.review_threads(owner, name, number),
    }
    return _format_pr_context(obj)


# --------------------------------------------------------------------------- #
# Pull requests — mutations
# --------------------------------------------------------------------------- #


def create_pull(
    title: str,
    body: str,
    head: str,
    base: str,
    repo: str,
    *,
    draft: bool = False,
) -> str:
    """Open a PR (head -> base) and return its URL. Raises on failure."""
    owner, name = split_repo(repo)
    return client().create_pull(
        owner, name, title=title, body=body, head=head, base=base, draft=draft
    )


def add_pr_label(pr: int | str, labels: list[str], repo: str | None = None) -> bool:
    """Add labels to a PR. True on success."""
    owner, name = _owner_name(repo)
    return client().add_pr_label(owner, name, _pr_number(pr), labels)


def remove_pr_label(pr: int | str, label: str, repo: str | None = None) -> bool:
    """Remove a label from a PR. Best-effort: False on failure."""
    owner, name = _owner_name(repo)
    return client().remove_pr_label(owner, name, _pr_number(pr), label)


def pr_comment(pr: int | str, body: str, repo: str | None = None) -> bool:
    """Post a top-level comment on a PR. True on success."""
    owner, name = _owner_name(repo)
    return client().pr_comment(owner, name, _pr_number(pr), body)


def post_review_comment(
    pr: int | str,
    body: str,
    file: str | None = None,
    line: int | None = None,
    repo: str | None = None,
) -> bool:
    """Post a critic finding so it ALWAYS lands (inline → plain fallback)."""
    owner, name = _owner_name(repo)
    return client().post_review_comment(owner, name, _pr_number(pr), body, file=file, line=line)


def enable_pr_auto_merge(pr: int | str, repo: str | None = None) -> bool:
    """Enable squash auto-merge for a PR. Best-effort: False on failure."""
    owner, name = _owner_name(repo)
    return client().enable_pr_auto_merge(owner, name, _pr_number(pr))


def disable_pr_auto_merge(pr: int | str, repo: str | None = None) -> bool:
    """Disable auto-merge on a PR. Best-effort: False on failure."""
    owner, name = _owner_name(repo)
    return client().disable_pr_auto_merge(owner, name, _pr_number(pr))


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #


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
