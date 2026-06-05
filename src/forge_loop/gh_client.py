"""Typed GitHub client (issue #83, completed in #223).

The SINGLE way the codebase talks to GitHub. Every GitHub operation —
issues, PRs, labels, comments, review threads, auto-merge — goes through
:class:`GithubkitClient`, backed by :mod:`githubkit` (the typed,
OpenAPI-generated SDK). The legacy ``gh`` CLI subprocess layer
(``forge_loop.gh``) is GONE: there is no subprocess shell-out to ``gh``
anywhere in ``src/``.

Win shape:
* Per-call latency drops from ~50-100ms subprocess spawn to an HTTP
  round-trip the SDK keeps alive across calls.
* Return values are typed (Pydantic models from githubkit), not
  stringly-typed JSON.
* Pagination + rate-limit handling lives in githubkit, not per-callsite.
* :class:`MockGhClient` records calls in-memory for tests — no
  ``monkeypatch.setattr(subprocess, "run", ...)`` per case.
* :class:`GhError` raises a typed exception with HTTP status + body.

REST is used for issues/PRs/labels/comments/state/create/update/close.
GraphQL (``githubkit.GitHub.graphql``) is used for review threads, the
PR-list enrichment (mergeStateStatus + labels), and the
``enablePullRequestAutoMerge`` / ``disablePullRequestAutoMerge``
mutations — those are GraphQL-only on GitHub.

Auth: ``GITHUB_TOKEN`` or ``GH_TOKEN`` env var ONLY. There is no
``gh auth token`` fallback — the ``gh`` CLI is not installed/used. If
neither env var is set, the client raises a clear :class:`RuntimeError`
naming both env vars.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

logger = logging.getLogger(__name__)

#: The merge strategies GitHub's REST merge endpoint accepts. The loop only
#: ever uses ``squash`` (the convention for loop-authored PRs), but the type
#: is kept open to the full set so the client mirrors githubkit's contract.
MergeMethod = Literal["merge", "squash", "rebase"]

# ---------------------------------------------------------------------------
# Typed return shapes — small, hand-curated dataclasses covering what
# the loop actually reads. Full githubkit models are too wide to expose
# (hundreds of fields per resource); these are the trimmed contract.
# ---------------------------------------------------------------------------


@dataclass
class Issue:
    """Subset of a GitHub issue the loop actually reads."""

    number: int
    title: str
    body: str = ""
    state: str = "open"
    labels: list[str] = field(default_factory=list)


@dataclass
class PullRequest:
    """Subset of a GitHub pull request the loop actually reads."""

    number: int
    title: str
    body: str = ""
    state: str = "open"
    draft: bool = False
    head_ref: str = ""
    base_ref: str = ""
    labels: list[str] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0


@dataclass(frozen=True)
class AutoMergeResult:
    """Outcome of an ``enablePullRequestAutoMerge`` attempt.

    ``enabled`` is the success flag; ``reason`` carries the GraphQL error
    message / HTTP detail when ``enabled`` is False, so callers never log an
    empty-reason ``*_automerge_failed`` event (issue #255 — fail loud, never
    swallow). ``reason`` is the empty string on success.
    """

    enabled: bool
    reason: str = ""


@dataclass(frozen=True)
class MergeResult:
    """Outcome of a direct REST merge (``pulls.merge``).

    ``merged`` is the success flag; ``reason`` carries the HTTP status + body
    tail when the merge fails (e.g. 405 not mergeable, 409 head changed), so
    the fallback path also fails loud (issue #255).
    """

    merged: bool
    reason: str = ""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GhError(RuntimeError):
    """Raised by :class:`GhClient` on a non-2xx response.

    Carries the HTTP status + response body tail so callers + log
    readers can reconstruct what happened without re-issuing the call.
    """

    def __init__(
        self,
        method: str,
        status: int,
        body_tail: str,
        *,
        auth_source: str = "unknown",
    ) -> None:
        super().__init__(
            f"github {method} via {auth_source} returned HTTP {status}: {body_tail[:300]}"
        )
        self.method = method
        self.status = status
        self.body_tail = body_tail
        self.auth_source = auth_source


# ---------------------------------------------------------------------------
# Protocol — call sites depend on this, not the impl.
# ---------------------------------------------------------------------------


class GhClient(Protocol):
    """GitHub operations the loop uses. Subset of githubkit's full API.

    The full surface the ``gh_issues`` facade depends on; both
    :class:`GithubkitClient` (real) and :class:`MockGhClient` (tests)
    implement it.
    """

    # -- issues --------------------------------------------------------------
    def issues_by_label(self, owner: str, repo: str, label: str, limit: int) -> list[Issue]: ...

    def get_issue(self, owner: str, repo: str, number: int) -> Issue | None: ...

    def add_comment(self, owner: str, repo: str, number: int, body: str) -> None: ...

    def add_labels(self, owner: str, repo: str, number: int, labels: list[str]) -> None: ...

    def remove_label(self, owner: str, repo: str, number: int, label: str) -> None: ...

    def create_issue(
        self, owner: str, repo: str, title: str, body: str, labels: list[str]
    ) -> Issue: ...

    def issue_comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]: ...

    def issue_comment_bodies(self, owner: str, repo: str, number: int) -> list[str]: ...

    def update_issue(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        title: str | None = ...,
        body: str | None = ...,
        add_labels: list[str] | None = ...,
        remove_labels: list[str] | None = ...,
    ) -> bool: ...

    def get_issue_state(self, owner: str, repo: str, number: int) -> str | None: ...

    def close_issue(
        self, owner: str, repo: str, number: int, *, reason: str | None = ...
    ) -> bool: ...

    def create_label(
        self, owner: str, repo: str, name: str, color: str, description: str
    ) -> bool: ...

    # -- pull requests -------------------------------------------------------
    def get_pull(self, owner: str, repo: str, number: int) -> PullRequest | None: ...

    def create_pull(
        self,
        owner: str,
        repo: str,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = ...,
    ) -> str: ...

    def add_pr_label(self, owner: str, repo: str, number: int, labels: list[str]) -> bool: ...

    def remove_pr_label(self, owner: str, repo: str, number: int, label: str) -> bool: ...

    def pr_comment(self, owner: str, repo: str, number: int, body: str) -> bool: ...

    def pr_changed_lines(self, owner: str, repo: str, number: int) -> int: ...

    def pr_changed_files(self, owner: str, repo: str, number: int) -> list[str]: ...

    def pr_diff(self, owner: str, repo: str, number: int) -> str: ...

    def pr_precommit_context(self, owner: str, repo: str, number: int) -> tuple[str, str]: ...

    def pr_status_failed(self, owner: str, repo: str, number: int) -> bool: ...

    def list_open_prs(
        self,
        owner: str,
        repo: str,
        *,
        label: str | None = ...,
        head: str | None = ...,
        limit: int = ...,
    ) -> list[dict[str, Any]]: ...

    def find_pr_by_head(self, owner: str, repo: str, head: str) -> dict[str, Any] | None: ...

    def latest_critic_report(self, owner: str, repo: str, number: int) -> str: ...

    # -- review threads ------------------------------------------------------
    def review_threads(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]: ...

    def review_threads_batch(
        self, owner: str, repo: str, numbers: list[int]
    ) -> dict[int, list[dict[str, Any]]]: ...

    def post_review_comment(
        self,
        owner: str,
        repo: str,
        number: int,
        body: str,
        *,
        file: str | None = ...,
        line: int | None = ...,
    ) -> bool: ...

    # -- auto-merge ----------------------------------------------------------
    def enable_pr_auto_merge(
        self, owner: str, repo: str, number: int
    ) -> AutoMergeResult: ...

    def disable_pr_auto_merge(self, owner: str, repo: str, number: int) -> bool: ...

    def merge_pull_request(
        self, owner: str, repo: str, number: int, *, method: MergeMethod = ...
    ) -> MergeResult: ...

    def delete_branch(self, owner: str, repo: str, branch: str) -> bool: ...

    # -- auth ----------------------------------------------------------------
    def check_auth(self) -> None: ...

    @property
    def auth_source(self) -> str: ...


# ---------------------------------------------------------------------------
# Auth resolution — small, explicit, documented.
# ---------------------------------------------------------------------------


class TokenSource(Protocol):
    def env_token(self, name: str) -> str | None: ...


@dataclass(frozen=True)
class ResolvedToken:
    token: str | None
    source: str


class GhTokenSource:
    """Real GitHub token source for SDK clients.

    Reads ``GITHUB_TOKEN`` / ``GH_TOKEN`` from the environment ONLY. There
    is no ``gh auth token`` fallback — the ``gh`` CLI is not part of this
    codebase. Token values are never logged.
    """

    def env_token(self, name: str) -> str | None:
        value = os.environ.get(name)
        return value.strip() if value and value.strip() else None


#: The two env vars (in precedence order) the client reads a token from.
#: ``GITHUB_TOKEN`` is the GitHub Actions convention; ``GH_TOKEN`` is the
#: gh-CLI convention. We accept both so operators' existing ``.env`` works.
_TOKEN_ENV_VARS = ("GITHUB_TOKEN", "GH_TOKEN")


def resolve_token_info(source: TokenSource | None = None) -> ResolvedToken:
    """Resolve a GitHub token from ``GITHUB_TOKEN`` / ``GH_TOKEN`` env.

    Returns ``ResolvedToken(token=None, source="none")`` when neither var
    is set, so callers (e.g. :meth:`GithubkitClient.check_auth`) can raise
    a precise, actionable error instead of failing opaquely mid-request.
    """
    source = source or GhTokenSource()
    for name in _TOKEN_ENV_VARS:
        token = source.env_token(name)
        if token:
            return ResolvedToken(token=token, source=name)
    return ResolvedToken(token=None, source="none")


def resolve_token(source: TokenSource | None = None) -> str | None:
    """Resolve a GitHub token from env (``GITHUB_TOKEN`` then ``GH_TOKEN``).

    Returns ``None`` when neither is set — the caller decides whether
    unauthenticated mode is acceptable. Operators set the token in their
    ``.env`` next to ``LOOP_GH_REPO``.
    """
    return resolve_token_info(source).token


def _require_token(source: TokenSource | None = None) -> ResolvedToken:
    """Resolve a token or raise naming both accepted env vars."""
    resolved = resolve_token_info(source)
    if resolved.token is None:
        raise RuntimeError(
            "no GitHub token available; set GITHUB_TOKEN or GH_TOKEN "
            "(the gh CLI is not used by forge-loop)"
        )
    return resolved


def _label_names(labels: Any) -> list[str]:
    out: list[str] = []
    for raw in labels or []:
        lab = cast(Any, raw)
        name = getattr(lab, "name", None)
        if name is not None:
            out.append(str(name))
    return out


# ---------------------------------------------------------------------------
# Real implementation — wraps githubkit.GitHub.
# ---------------------------------------------------------------------------


def _graphql_error_text(exc: Exception) -> str:
    """Best-effort human-readable cause from a githubkit GraphQL/HTTP failure.

    githubkit raises ``GraphQLFailed`` (carrying ``response.errors``, a list of
    GraphQL error objects with a ``message``) when a mutation returns errors,
    and ``RequestFailed`` (carrying an httpx ``Response``) on transport-level
    4xx/5xx. Either way we extract the most specific message available so the
    ``*_automerge_failed`` event names the real cause (issue #255) — never an
    empty reason. Falls back to ``str(exc)`` for anything else.
    """
    response = getattr(exc, "response", None)
    errors = getattr(response, "errors", None)
    if errors:
        messages = [
            str(getattr(e, "message", None) or e).strip()
            for e in errors
            if (getattr(e, "message", None) or e)
        ]
        if messages:
            return "; ".join(messages)[:300]
    # RequestFailed wraps an httpx.Response; surface status + body tail.
    status = getattr(response, "status_code", None)
    if status is not None:
        body = (getattr(response, "text", "") or "").strip()
        return f"HTTP {status}: {body}"[:300]
    return (str(exc) or exc.__class__.__name__)[:300]


class GithubkitClient:
    """Real GhClient — talks to api.github.com via githubkit.

    One instance per process is enough; githubkit's HTTP client pools
    + reuses connections, so concurrent calls share the pipeline.
    """

    def __init__(
        self, token: str | None = None, *, token_source: TokenSource | None = None
    ) -> None:
        from githubkit import GitHub

        # Env-only auth (#223): require a real token at construction so a
        # misconfigured deployment fails fast with a clear message naming
        # both accepted env vars, instead of issuing unauthenticated calls
        # that 401 deep inside githubkit.
        resolved = (
            _require_token(token_source) if token is None else ResolvedToken(token, "explicit")
        )
        self._auth_source = resolved.source
        self._gh = GitHub(resolved.token)

    @property
    def auth_source(self) -> str:
        return self._auth_source

    def _raise_if_error(self, method: str, response: Any) -> None:
        status = getattr(response, "status_code", None)
        if status and status >= 400:
            body = getattr(response, "text", "") or str(getattr(response, "parsed_data", ""))
            raise GhError(method, status, body, auth_source=self.auth_source)

    def check_auth(self) -> None:
        # A real client cannot be constructed without a token (the ctor
        # raises via ``_require_token``), so reaching here means we have one;
        # verify it actually authenticates against the API.
        resp = self._gh.rest.users.get_authenticated()
        self._raise_if_error("check_auth", resp)

    def issues_by_label(self, owner: str, repo: str, label: str, limit: int) -> list[Issue]:
        resp = self._gh.rest.issues.list_for_repo(
            owner=owner,
            repo=repo,
            labels=label,
            per_page=min(limit, 100),
            state="open",
        )
        self._raise_if_error(f"list_for_repo({label})", resp)
        out: list[Issue] = []
        for item in cast(list[Any], resp.parsed_data or [])[:limit]:
            # Skip PRs — list_for_repo returns issues + PRs by default.
            if getattr(item, "pull_request", None):
                continue
            out.append(
                Issue(
                    number=item.number,
                    title=item.title or "",
                    body=item.body or "",
                    state=str(item.state),
                    labels=_label_names(item.labels),
                )
            )
        return out

    def get_issue(self, owner: str, repo: str, number: int) -> Issue | None:
        try:
            resp = self._gh.rest.issues.get(owner=owner, repo=repo, issue_number=number)
        except Exception:  # noqa: BLE001
            return None
        self._raise_if_error(f"get_issue({number})", resp)
        item = resp.parsed_data
        return Issue(
            number=item.number,
            title=item.title or "",
            body=item.body or "",
            state=str(item.state),
            labels=_label_names(item.labels),
        )

    def add_comment(self, owner: str, repo: str, number: int, body: str) -> None:
        resp = self._gh.rest.issues.create_comment(
            owner=owner,
            repo=repo,
            issue_number=number,
            body=body,
        )
        self._raise_if_error(f"add_comment({number})", resp)

    def add_labels(self, owner: str, repo: str, number: int, labels: list[str]) -> None:
        resp = self._gh.rest.issues.add_labels(
            owner=owner,
            repo=repo,
            issue_number=number,
            labels=labels,
        )
        self._raise_if_error(f"add_labels({number}, {labels})", resp)

    def remove_label(self, owner: str, repo: str, number: int, label: str) -> None:
        try:
            resp = self._gh.rest.issues.remove_label(
                owner=owner,
                repo=repo,
                issue_number=number,
                name=label,
            )
            self._raise_if_error(f"remove_label({number}, {label})", resp)
        except GhError as e:
            # 404 is "label wasn't on the issue" — not an error from the
            # caller's perspective (legacy gh CLI returned ok on this).
            if e.status != 404:
                raise

    def create_issue(
        self, owner: str, repo: str, title: str, body: str, labels: list[str]
    ) -> Issue:
        resp = self._gh.rest.issues.create(
            owner=owner,
            repo=repo,
            title=title,
            body=body,
            labels=list(labels),
        )
        self._raise_if_error(f"create_issue({title!r})", resp)
        item = resp.parsed_data
        return Issue(
            number=item.number,
            title=item.title or "",
            body=item.body or "",
            state=str(item.state),
            labels=_label_names(item.labels),
        )

    def get_pull(self, owner: str, repo: str, number: int) -> PullRequest | None:
        try:
            resp = self._gh.rest.pulls.get(owner=owner, repo=repo, pull_number=number)
        except Exception:  # noqa: BLE001
            return None
        self._raise_if_error(f"get_pull({number})", resp)
        item = resp.parsed_data
        return PullRequest(
            number=item.number,
            title=item.title or "",
            body=item.body or "",
            state=str(item.state),
            draft=bool(getattr(item, "draft", False)),
            head_ref=getattr(item.head, "ref", "") if item.head else "",
            base_ref=getattr(item.base, "ref", "") if item.base else "",
            labels=_label_names(item.labels),
            additions=getattr(item, "additions", 0) or 0,
            deletions=getattr(item, "deletions", 0) or 0,
            changed_files=getattr(item, "changed_files", 0) or 0,
        )

    # -- issue mutations -----------------------------------------------------

    def issue_comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        """Return an issue's comments as ``[{body, createdAt}, ...]`` (oldest first).

        [] on any failure. The operator reply-poller needs ``createdAt`` to
        ignore stale replies, so this preserves the timestamp where
        :meth:`issue_comment_bodies` returns only bodies.
        """
        try:
            resp = self._gh.rest.issues.list_comments(
                owner=owner, repo=repo, issue_number=number, per_page=100
            )
            self._raise_if_error(f"issue_comments({number})", resp)
        except Exception:  # noqa: BLE001 — best-effort: caller treats [] as "no comments"
            return []
        out: list[dict[str, Any]] = []
        for c in cast(list[Any], resp.parsed_data or []):
            created = getattr(c, "created_at", None)
            iso = getattr(created, "isoformat", None)
            out.append(
                {
                    "body": str(getattr(c, "body", "") or ""),
                    "createdAt": iso() if callable(iso) else "",
                }
            )
        return out

    def issue_comment_bodies(self, owner: str, repo: str, number: int) -> list[str]:
        """Return an issue's comment bodies (oldest first). [] on failure."""
        return [c["body"] for c in self.issue_comments(owner, repo, number)]

    def update_issue(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
    ) -> bool:
        """Patch an issue's title/body and add/remove labels. True on success.

        Mirrors the legacy ``gh issue edit`` semantics: a no-op call (no
        fields, no labels) returns True without a round-trip; label adds and
        removes are independent calls so a partial failure is still reported.
        """
        ok = True
        data: dict[str, Any] = {}
        if title is not None:
            data["title"] = title
        if body is not None:
            data["body"] = body
        if data:
            try:
                resp = self._gh.rest.issues.update(
                    owner=owner, repo=repo, issue_number=number, **data
                )
                self._raise_if_error(f"update_issue({number})", resp)
            except Exception:  # noqa: BLE001 — return False, never raise (legacy contract)
                ok = False
        for lab in add_labels or []:
            try:
                self.add_labels(owner, repo, number, [lab])
            except Exception:  # noqa: BLE001
                ok = False
        for lab in remove_labels or []:
            try:
                self.remove_label(owner, repo, number, lab)
            except Exception:  # noqa: BLE001
                ok = False
        return ok

    def get_issue_state(self, owner: str, repo: str, number: int) -> str | None:
        """Return ``"OPEN"`` / ``"CLOSED"`` or ``None`` on any failure.

        Callers (the pre-merge gate, issue #65) treat ``None`` conservatively
        — same as CLOSED — so a transient API outage never silently lands work
        on a ticket the operator may have closed.
        """
        try:
            resp = self._gh.rest.issues.get(owner=owner, repo=repo, issue_number=number)
            self._raise_if_error(f"get_issue_state({number})", resp)
        except Exception:  # noqa: BLE001
            return None
        state = getattr(resp.parsed_data, "state", None)
        return state.upper() if isinstance(state, str) else None

    def close_issue(self, owner: str, repo: str, number: int, *, reason: str | None = None) -> bool:
        """Close an issue. ``reason`` is ``completed`` / ``not planned``.

        Returns True on success. The gh-CLI accepted ``"not planned"`` (with a
        space); the REST API expects ``not_planned`` — we normalise so callers
        keep passing the CLI spelling.
        """
        data: dict[str, Any] = {"state": "closed"}
        if reason:
            normalised = reason.strip().lower().replace(" ", "_")
            if normalised in {"completed", "not_planned", "reopened"}:
                data["state_reason"] = normalised
        try:
            resp = self._gh.rest.issues.update(owner=owner, repo=repo, issue_number=number, **data)
            self._raise_if_error(f"close_issue({number})", resp)
        except Exception:  # noqa: BLE001
            return False
        return True

    def create_label(self, owner: str, repo: str, name: str, color: str, description: str) -> bool:
        """Create a repo label (idempotent at the caller). True iff created.

        A 422 (label already exists) returns False — the legacy
        ``ensure_labels_via_gh`` only counted *newly created* labels.
        """
        try:
            resp = self._gh.rest.issues.create_label(
                owner=owner, repo=repo, name=name, color=color, description=description
            )
            self._raise_if_error(f"create_label({name})", resp)
        except Exception:  # noqa: BLE001 — already-exists / transient: not created
            return False
        return True

    def create_pull(
        self,
        owner: str,
        repo: str,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
    ) -> str:
        """Create a PR and return its HTML URL. Raises :class:`GhError` on fail."""
        resp = self._gh.rest.pulls.create(
            owner=owner,
            repo=repo,
            title=title,
            body=body,
            head=head,
            base=base,
            draft=draft,
        )
        self._raise_if_error(f"create_pull({head}->{base})", resp)
        url = getattr(resp.parsed_data, "html_url", "")
        return url if isinstance(url, str) else ""

    # -- PR labels + reads ---------------------------------------------------

    def add_pr_label(self, owner: str, repo: str, number: int, labels: list[str]) -> bool:
        """Add labels to a PR (PRs are issues for the labels API). True on ok."""
        if not labels:
            return True
        try:
            self.add_labels(owner, repo, number, labels)
        except Exception:  # noqa: BLE001
            return False
        return True

    def remove_pr_label(self, owner: str, repo: str, number: int, label: str) -> bool:
        """Remove a label from a PR. Best-effort: False on failure."""
        try:
            self.remove_label(owner, repo, number, label)
        except Exception:  # noqa: BLE001
            return False
        return True

    def pr_comment(self, owner: str, repo: str, number: int, body: str) -> bool:
        """Post a top-level PR comment (PRs are issues for the comments API)."""
        try:
            self.add_comment(owner, repo, number, body)
        except Exception:  # noqa: BLE001
            return False
        return True

    def pr_changed_lines(self, owner: str, repo: str, number: int) -> int:
        """additions + deletions for a PR. 0 on failure (caller falls back)."""
        pr = self.get_pull(owner, repo, number)
        if pr is None:
            return 0
        return int(pr.additions) + int(pr.deletions)

    def pr_changed_files(self, owner: str, repo: str, number: int) -> list[str]:
        """List the file paths a PR touches. [] on failure."""
        try:
            resp = self._gh.rest.pulls.list_files(
                owner=owner, repo=repo, pull_number=number, per_page=100
            )
            self._raise_if_error(f"pr_changed_files({number})", resp)
        except Exception:  # noqa: BLE001
            return []
        out: list[str] = []
        for entry in cast(list[Any], resp.parsed_data or []):
            path = getattr(entry, "filename", None)
            if isinstance(path, str) and path:
                out.append(path)
        return out

    def pr_diff(self, owner: str, repo: str, number: int) -> str:
        """Return a PR's unified diff. "" on failure (graceful degrade)."""
        try:
            resp = self._gh.rest.pulls.get(
                owner=owner,
                repo=repo,
                pull_number=number,
                headers={"Accept": "application/vnd.github.v3.diff"},
            )
        except Exception:  # noqa: BLE001
            return ""
        status = getattr(resp, "status_code", 200)
        if status and status >= 400:
            return ""
        text = getattr(resp, "text", "")
        return text if isinstance(text, str) else ""

    def pr_precommit_context(self, owner: str, repo: str, number: int) -> tuple[str, str]:
        """Return (PR body, concatenated commit messages). ("","") on failure."""
        body = ""
        try:
            pull = self._gh.rest.pulls.get(owner=owner, repo=repo, pull_number=number)
            self._raise_if_error(f"pr_precommit_context({number})", pull)
            raw_body = getattr(pull.parsed_data, "body", None)
            body = raw_body if isinstance(raw_body, str) else ""
            commits_resp = self._gh.rest.pulls.list_commits(
                owner=owner, repo=repo, pull_number=number, per_page=100
            )
            self._raise_if_error(f"pr_precommit_context.commits({number})", commits_resp)
        except Exception:  # noqa: BLE001
            return "", ""
        chunks: list[str] = []
        for c in cast(list[Any], commits_resp.parsed_data or []):
            message = getattr(getattr(c, "commit", None), "message", None)
            if isinstance(message, str) and message.strip():
                chunks.append(message)
        return body, "\n".join(chunks)

    def pr_status_failed(self, owner: str, repo: str, number: int) -> bool:
        """True iff any required check on the PR's head is in a failing state.

        Mirrors the legacy ``statusCheckRollup`` walk: combined status + check
        runs on the PR head SHA. Read-only; False on any failure so a probe
        error never falsely reports CI failure.
        """
        try:
            pull = self._gh.rest.pulls.get(owner=owner, repo=repo, pull_number=number)
            self._raise_if_error(f"pr_status_failed({number})", pull)
            sha = getattr(getattr(pull.parsed_data, "head", None), "sha", None)
            if not isinstance(sha, str) or not sha:
                return False
            combined = self._gh.rest.repos.get_combined_status_for_ref(
                owner=owner, repo=repo, ref=sha
            )
            self._raise_if_error(f"pr_status_failed.status({number})", combined)
            for st in getattr(combined.parsed_data, "statuses", None) or []:
                if str(getattr(st, "state", "")).upper() in {"FAILURE", "ERROR"}:
                    return True
            runs = self._gh.rest.checks.list_for_ref(owner=owner, repo=repo, ref=sha, per_page=100)
            self._raise_if_error(f"pr_status_failed.checks({number})", runs)
            for run in getattr(runs.parsed_data, "check_runs", None) or []:
                if str(getattr(run, "conclusion", "")).upper() in {
                    "FAILURE",
                    "TIMED_OUT",
                    "CANCELLED",
                    "ACTION_REQUIRED",
                }:
                    return True
        except Exception:  # noqa: BLE001
            return False
        return False

    # -- PR listing (rich, GraphQL) -----------------------------------------

    def list_open_prs(
        self,
        owner: str,
        repo: str,
        *,
        label: str | None = None,
        head: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return open PRs as legacy-shaped dicts via one GraphQL round-trip.

        Each dict carries the exact keys the runner reads:
        ``number, title, body, headRefName, baseRefName, url, labels (list of
        {name}), updatedAt, mergeStateStatus, state, isDraft, mergeable``.
        REST's ``pulls.list`` omits ``mergeStateStatus`` + labels, so the
        legacy code used ``gh pr list --json ...`` (GraphQL under the hood);
        we issue the GraphQL directly. ``label`` / ``head`` filter
        client-side (GraphQL search by label is awkward and the result set is
        small). [] on failure.
        """
        query = (
            "query($owner: String!, $name: String!, $limit: Int!) {\n"
            "  repository(owner: $owner, name: $name) {\n"
            "    pullRequests(states: OPEN, first: $limit, "
            "orderBy: {field: UPDATED_AT, direction: ASC}) {\n"
            "      nodes {\n"
            "        number title body url isDraft updatedAt\n"
            "        headRefName baseRefName state mergeable mergeStateStatus\n"
            "        labels(first: 50) { nodes { name } }\n"
            "      }\n"
            "    }\n"
            "  }\n"
            "}\n"
        )
        try:
            data = self._gh.graphql(
                query, {"owner": owner, "name": repo, "limit": max(min(limit, 100), 1)}
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_open_prs GraphQL failed for %s/%s: %s", owner, repo, exc)
            return []
        nodes = (((data or {}).get("repository") or {}).get("pullRequests") or {}).get(
            "nodes"
        ) or []
        out: list[dict[str, Any]] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            labels = [
                {"name": lab.get("name") or ""}
                for lab in ((node.get("labels") or {}).get("nodes") or [])
                if isinstance(lab, dict)
            ]
            if label and label not in {lab["name"] for lab in labels}:
                continue
            if head is not None and node.get("headRefName") != head:
                continue
            out.append(
                {
                    "number": node.get("number"),
                    "title": node.get("title") or "",
                    "body": node.get("body") or "",
                    "headRefName": node.get("headRefName") or "",
                    "baseRefName": node.get("baseRefName") or "",
                    "url": node.get("url") or "",
                    "labels": labels,
                    "updatedAt": node.get("updatedAt") or "",
                    "mergeStateStatus": node.get("mergeStateStatus") or "",
                    "state": node.get("state") or "",
                    "isDraft": bool(node.get("isDraft")),
                    "mergeable": node.get("mergeable") or "",
                }
            )
        return out

    def find_pr_by_head(self, owner: str, repo: str, head: str) -> dict[str, Any] | None:
        """Return the most recent PR (any state) for head branch ``head``.

        Used by the iteration probe (#78): it must see MERGED / CLOSED PRs too,
        not just open ones. Returns a dict with the keys the probe reads
        (``url, number, state, mergeable, mergeStateStatus, isDraft``) or None.
        GraphQL so we get ``mergeStateStatus`` + ``mergeable`` in one call.
        """
        query = (
            "query($owner: String!, $name: String!, $head: String!) {\n"
            "  repository(owner: $owner, name: $name) {\n"
            "    pullRequests(headRefName: $head, first: 1, "
            "orderBy: {field: UPDATED_AT, direction: DESC}) {\n"
            "      nodes { number url state isDraft mergeable mergeStateStatus }\n"
            "    }\n"
            "  }\n"
            "}\n"
        )
        try:
            data = self._gh.graphql(query, {"owner": owner, "name": repo, "head": head})
        except Exception as exc:  # noqa: BLE001
            logger.warning("find_pr_by_head GraphQL failed for %s: %s", head, exc)
            return None
        nodes = (((data or {}).get("repository") or {}).get("pullRequests") or {}).get(
            "nodes"
        ) or []
        if not nodes or not isinstance(nodes[0], dict):
            return None
        node = nodes[0]
        return {
            "url": node.get("url") or "",
            "number": node.get("number"),
            "state": str(node.get("state") or "").upper(),
            "mergeable": str(node.get("mergeable") or "").upper(),
            "mergeStateStatus": str(node.get("mergeStateStatus") or "").upper(),
            "isDraft": bool(node.get("isDraft")),
        }

    def latest_critic_report(self, owner: str, repo: str, number: int) -> str:
        """Return the most recent critic-report comment body on a PR, or "".

        The critic posts findings as a PR comment carrying ``critic-report`` /
        ``sev1`` / ``sev2``; we scan the last 10 comments newest-first.
        """
        comments = self.issue_comments(owner, repo, number)
        for c in reversed(comments[-10:]):
            body = str(c.get("body") or "")
            low = body.lower()
            if "critic-report" in low or "sev1" in low or "sev2" in low:
                return body[:4000]
        return ""

    # -- review threads (GraphQL) -------------------------------------------

    def review_threads(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        """Fetch a PR's review threads (GraphQL). [] on failure.

        REST + ``gh pr view --comments`` omit inline review threads; those are
        exactly what a repair worker must address, so they are fetched here.
        """
        query = (
            "query($owner: String!, $name: String!, $number: Int!) {\n"
            "  repository(owner: $owner, name: $name) {\n"
            "    pullRequest(number: $number) {\n"
            f"{_REVIEW_THREADS_SELECTION}"
            "    }\n"
            "  }\n"
            "}\n"
        )
        try:
            data = self._gh.graphql(query, {"owner": owner, "name": repo, "number": int(number)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("review_threads GraphQL failed for PR #%s: %s", number, exc)
            return []
        nodes = (
            (((data or {}).get("repository") or {}).get("pullRequest") or {}).get("reviewThreads")
            or {}
        ).get("nodes") or []
        return _parse_thread_nodes(nodes)

    def review_threads_batch(
        self, owner: str, repo: str, numbers: list[int]
    ) -> dict[int, list[dict[str, Any]]]:
        """Fetch review threads for many PRs, batching GraphQL round-trips.

        Replaces the per-PR fan-out (#226): an idle repo with N open PRs cost
        N GraphQL calls per tick. PRs are batched into aliased queries of at
        most ``_REVIEW_THREADS_BATCH_CHUNK`` so nested selections stay under
        GitHub's complexity limit. A chunk failure falls back to per-PR fetches
        so one failed batch never silently degrades *every* PR to "no threads".
        Returns ``{pr_number: [threads]}``; empty input issues no call.
        """
        seen: set[int] = set()
        ordered: list[int] = []
        for raw in numbers:
            n = int(raw)
            if n not in seen:
                seen.add(n)
                ordered.append(n)
        if not ordered:
            return {}
        out: dict[int, list[dict[str, Any]]] = {}
        for start in range(0, len(ordered), _REVIEW_THREADS_BATCH_CHUNK):
            chunk = ordered[start : start + _REVIEW_THREADS_BATCH_CHUNK]
            batched = self._review_threads_chunk(owner, repo, chunk)
            if batched is None:
                logger.warning(
                    "review_threads_batch: batched query failed for PRs %s; "
                    "falling back to per-PR fetch",
                    chunk,
                )
                for n in chunk:
                    out[n] = self.review_threads(owner, repo, n)
            else:
                out.update(batched)
        return out

    def _review_threads_chunk(
        self, owner: str, repo: str, numbers: list[int]
    ) -> dict[int, list[dict[str, Any]]] | None:
        """One chunk of PRs in a single aliased query. None on chunk failure."""
        if not numbers:
            return {}
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
        try:
            data = self._gh.graphql(query, {"owner": owner, "name": repo})
        except Exception as exc:  # noqa: BLE001
            logger.warning("review_threads_batch chunk failed: %s", exc)
            return None
        repo_data = (data or {}).get("repository")
        if not isinstance(repo_data, dict):
            return None
        out: dict[int, list[dict[str, Any]]] = {}
        for idx, num in enumerate(numbers):
            pr_obj = repo_data.get(f"pr{idx}") or {}
            nodes = (pr_obj.get("reviewThreads") or {}).get("nodes", [])
            out[num] = _parse_thread_nodes(nodes)
        return out

    # -- review comments / findings -----------------------------------------

    def post_review_comment(
        self,
        owner: str,
        repo: str,
        number: int,
        body: str,
        *,
        file: str | None = None,
        line: int | None = None,
    ) -> bool:
        r"""Post a critic finding so it ALWAYS lands (the #234 contract).

        With ``file``+``line``, an INLINE review comment is attempted first.
        GitHub 422-rejects an inline comment whose line is not part of the PR's
        diff — routine, since findings often cite lines outside changed hunks.
        On ANY such failure we FALL BACK to a plain (non-inline) review comment
        with the location prepended (``\`{file}:{line}\` — {body}``).

        This fallback is load-bearing: the repair worker rebuilds its brief
        from the posted review context, so a silently-dropped finding blinds
        the repair loop and it never converges. Inline is a nicety; landing the
        finding is the contract. Returns True if posted by either path.
        """
        text = body
        if file is not None and line is not None:
            try:
                resp = self._gh.rest.pulls.create_review(
                    owner=owner,
                    repo=repo,
                    pull_number=number,
                    body=body,
                    event="COMMENT",
                    comments=[{"path": file, "line": int(line), "body": body}],
                )
                self._raise_if_error(f"post_review_comment.inline({number})", resp)
                return True
            except Exception:  # noqa: BLE001 — inline rejected (422 line-not-in-diff): fall back
                text = f"`{file}:{line}` — {body}"
        try:
            resp = self._gh.rest.pulls.create_review(
                owner=owner, repo=repo, pull_number=number, body=text, event="COMMENT"
            )
            self._raise_if_error(f"post_review_comment.plain({number})", resp)
        except Exception:  # noqa: BLE001
            return False
        return True

    # -- auto-merge (GraphQL mutations) -------------------------------------

    def _pull_request_node_id(self, owner: str, repo: str, number: int) -> str | None:
        try:
            resp = self._gh.rest.pulls.get(owner=owner, repo=repo, pull_number=number)
            self._raise_if_error(f"pull_node_id({number})", resp)
        except Exception:  # noqa: BLE001
            return None
        node_id = getattr(resp.parsed_data, "node_id", None)
        return node_id if isinstance(node_id, str) and node_id else None

    def enable_pr_auto_merge(self, owner: str, repo: str, number: int) -> AutoMergeResult:
        """Enable SQUASH auto-merge for a PR (GraphQL).

        Returns :class:`AutoMergeResult`. On failure (e.g. the repo's
        "Allow auto-merge" setting is off, or the PR can't take auto-merge yet)
        the actual GraphQL error message is captured in ``reason`` so callers
        log a NON-EMPTY ``*_automerge_failed`` reason (issue #255 — fail loud,
        never swallow the cause).
        """
        node_id = self._pull_request_node_id(owner, repo, number)
        if node_id is None:
            return AutoMergeResult(False, f"could not resolve PR #{number} node id")
        mutation = (
            "mutation($id: ID!) {\n"
            "  enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: SQUASH}) {\n"
            "    pullRequest { id }\n"
            "  }\n"
            "}\n"
        )
        try:
            self._gh.graphql(mutation, {"id": node_id})
        except Exception as exc:  # noqa: BLE001 — e.g. auto-merge not allowed on the repo
            return AutoMergeResult(False, _graphql_error_text(exc))
        return AutoMergeResult(True)

    def merge_pull_request(
        self, owner: str, repo: str, number: int, *, method: MergeMethod = "squash"
    ) -> MergeResult:
        """Directly merge a PR via the REST merge endpoint.

        The fallback for when GitHub auto-merge can't be enabled (issue #255):
        the loop only reaches here for a critic-approved + CLEAN PR, so a direct
        merge is correct. ``reason`` carries the HTTP status + body tail on
        failure so the failure is never silent.
        """
        try:
            resp = self._gh.rest.pulls.merge(
                owner=owner, repo=repo, pull_number=number, merge_method=method
            )
            self._raise_if_error(f"merge_pull_request({number})", resp)
        except Exception as exc:  # noqa: BLE001
            return MergeResult(False, str(exc)[:300])
        return MergeResult(True)

    def delete_branch(self, owner: str, repo: str, branch: str) -> bool:
        """Delete a branch (``heads/<branch>`` ref). Best-effort: False on fail.

        Used to tidy up after a direct fallback merge. A failure here is
        non-fatal — the PR is already merged — but it is still returned so the
        caller can note it.
        """
        try:
            resp = self._gh.rest.git.delete_ref(owner=owner, repo=repo, ref=f"heads/{branch}")
            self._raise_if_error(f"delete_branch({branch})", resp)
        except Exception:  # noqa: BLE001
            return False
        return True

    def disable_pr_auto_merge(self, owner: str, repo: str, number: int) -> bool:
        """Disable auto-merge on a PR (GraphQL). Best-effort: False on failure.

        Failure is fine (e.g. auto-merge was never enabled).
        """
        node_id = self._pull_request_node_id(owner, repo, number)
        if node_id is None:
            return False
        mutation = (
            "mutation($id: ID!) {\n"
            "  disablePullRequestAutoMerge(input: {pullRequestId: $id}) {\n"
            "    pullRequest { id }\n"
            "  }\n"
            "}\n"
        )
        try:
            self._gh.graphql(mutation, {"id": node_id})
        except Exception:  # noqa: BLE001
            return False
        return True


# ---------------------------------------------------------------------------
# Shared GraphQL selection for a PR's review threads. Used by both the
# single-PR and the batched fetchers so the two cannot drift in fields.
# ---------------------------------------------------------------------------

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

#: Max PR aliases per batched GraphQL query — bounds per-query complexity so a
#: single query over many PRs can't blow GitHub's GraphQL node budget (#226).
_REVIEW_THREADS_BATCH_CHUNK = 10


def _parse_thread_nodes(nodes: Any) -> list[dict[str, Any]]:
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


# ---------------------------------------------------------------------------
# Mock — call recording, canned responses, per-method overrides.
# ---------------------------------------------------------------------------


@dataclass
class MockGhClient:
    """In-memory GhClient for tests — no network, no subprocess.

    Pre-populate ``issues`` / ``pulls`` for read methods. ``calls``
    records every invocation as ``(method_name, kwargs)`` for assertions.
    Tests that want failure modes set ``raise_on``: a dict mapping
    method-name to a GhError to raise.
    """

    issues: dict[tuple[str, str, int], Issue] = field(default_factory=dict)
    pulls: dict[tuple[str, str, int], PullRequest] = field(default_factory=dict)
    issues_by_label_response: list[Issue] = field(default_factory=list)
    raise_on: dict[str, GhError] = field(default_factory=dict)
    raise_on_create_titles: dict[str, Exception] = field(default_factory=dict)
    create_issue_responses: list[int] = field(default_factory=list)
    next_issue_number: int | None = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    auth_source: str = "mock"

    # -- canned data for the extended (PR / review / comment) surface --------
    #: ``{number: [{body, createdAt}, ...]}`` returned by ``issue_comments``.
    issue_comments_by_number: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    #: Result of ``list_open_prs`` (legacy-shaped PR dicts).
    open_prs_response: list[dict[str, Any]] = field(default_factory=list)
    #: ``{number: [thread, ...]}`` for ``review_threads`` / ``review_threads_batch``.
    review_threads_by_pr: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    #: ``{head_ref: pr_dict}`` for ``find_pr_by_head``.
    pr_by_head: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: ``{number: report}`` for ``latest_critic_report``.
    critic_report_by_pr: dict[int, str] = field(default_factory=dict)
    #: ``{number: bool}`` for ``pr_status_failed``.
    pr_status_failed_by_pr: dict[int, bool] = field(default_factory=dict)
    #: ``{number: [path, ...]}`` for ``pr_changed_files``.
    pr_files_by_pr: dict[int, list[str]] = field(default_factory=dict)
    #: ``{number: (body, commit_text)}`` for ``pr_precommit_context``.
    precommit_by_pr: dict[int, tuple[str, str]] = field(default_factory=dict)
    #: ``{number: diff}`` for ``pr_diff``.
    diff_by_pr: dict[int, str] = field(default_factory=dict)
    #: Recorded post_review_comment outcomes; controls inline 422 simulation.
    inline_review_fails: bool = False
    create_pull_url: str = "https://github.com/o/r/pull/999"
    #: When set, ``enable_pr_auto_merge`` returns ``AutoMergeResult(False, ...)``
    #: with this reason — simulates the repo's auto-merge feature being off (#255).
    auto_merge_fail_reason: str | None = None
    #: When set, ``merge_pull_request`` returns ``MergeResult(False, ...)`` with
    #: this reason — simulates a direct-merge failure (e.g. PR not mergeable).
    merge_fail_reason: str | None = None
    #: When True, ``delete_branch`` reports failure (still non-fatal).
    delete_branch_fails: bool = False

    def _record(self, method: str, **kwargs: Any) -> None:
        self.calls.append((method, kwargs))
        if method in self.raise_on:
            raise self.raise_on[method]

    def issues_by_label(self, owner: str, repo: str, label: str, limit: int) -> list[Issue]:
        self._record("issues_by_label", owner=owner, repo=repo, label=label, limit=limit)
        return list(self.issues_by_label_response[:limit])

    def get_issue(self, owner: str, repo: str, number: int) -> Issue | None:
        self._record("get_issue", owner=owner, repo=repo, number=number)
        return self.issues.get((owner, repo, number))

    def add_comment(self, owner: str, repo: str, number: int, body: str) -> None:
        self._record("add_comment", owner=owner, repo=repo, number=number, body=body)

    def add_labels(self, owner: str, repo: str, number: int, labels: list[str]) -> None:
        self._record("add_labels", owner=owner, repo=repo, number=number, labels=labels)

    def remove_label(self, owner: str, repo: str, number: int, label: str) -> None:
        self._record("remove_label", owner=owner, repo=repo, number=number, label=label)

    def get_pull(self, owner: str, repo: str, number: int) -> PullRequest | None:
        self._record("get_pull", owner=owner, repo=repo, number=number)
        return self.pulls.get((owner, repo, number))

    # ``create_issue`` counters: tests inspect ``next_issue_number`` to
    # pre-stage numbers (epic-first ordering), and ``create_issue_responses``
    # can override per-call returns. By default we auto-assign monotonically.
    def create_issue(
        self, owner: str, repo: str, title: str, body: str, labels: list[str]
    ) -> Issue:
        self._record(
            "create_issue",
            owner=owner,
            repo=repo,
            title=title,
            body=body,
            labels=list(labels),
        )
        if title in self.raise_on_create_titles:
            raise self.raise_on_create_titles[title]
        responses = self.create_issue_responses
        n = responses.pop(0) if responses else self._next_number()
        issue = Issue(number=n, title=title, body=body, state="open", labels=list(labels))
        self.issues[(owner, repo, n)] = issue
        return issue

    def check_auth(self) -> None:
        self._record("check_auth")

    # -- extended surface (PR / review / comment) ---------------------------

    def issue_comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        self._record("issue_comments", owner=owner, repo=repo, number=number)
        return list(self.issue_comments_by_number.get(number, []))

    def issue_comment_bodies(self, owner: str, repo: str, number: int) -> list[str]:
        self._record("issue_comment_bodies", owner=owner, repo=repo, number=number)
        return [str(c.get("body") or "") for c in self.issue_comments_by_number.get(number, [])]

    def update_issue(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
    ) -> bool:
        self._record(
            "update_issue",
            owner=owner,
            repo=repo,
            number=number,
            title=title,
            body=body,
            add_labels=add_labels,
            remove_labels=remove_labels,
        )
        return True

    def get_issue_state(self, owner: str, repo: str, number: int) -> str | None:
        self._record("get_issue_state", owner=owner, repo=repo, number=number)
        issue = self.issues.get((owner, repo, number))
        return issue.state.upper() if issue else None

    def close_issue(self, owner: str, repo: str, number: int, *, reason: str | None = None) -> bool:
        self._record("close_issue", owner=owner, repo=repo, number=number, reason=reason)
        return True

    def create_label(self, owner: str, repo: str, name: str, color: str, description: str) -> bool:
        self._record(
            "create_label", owner=owner, repo=repo, name=name, color=color, description=description
        )
        return True

    def create_pull(
        self,
        owner: str,
        repo: str,
        *,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
    ) -> str:
        self._record(
            "create_pull",
            owner=owner,
            repo=repo,
            title=title,
            body=body,
            head=head,
            base=base,
            draft=draft,
        )
        return self.create_pull_url

    def add_pr_label(self, owner: str, repo: str, number: int, labels: list[str]) -> bool:
        self._record("add_pr_label", owner=owner, repo=repo, number=number, labels=list(labels))
        return True

    def remove_pr_label(self, owner: str, repo: str, number: int, label: str) -> bool:
        self._record("remove_pr_label", owner=owner, repo=repo, number=number, label=label)
        return True

    def pr_comment(self, owner: str, repo: str, number: int, body: str) -> bool:
        self._record("pr_comment", owner=owner, repo=repo, number=number, body=body)
        return True

    def pr_changed_lines(self, owner: str, repo: str, number: int) -> int:
        self._record("pr_changed_lines", owner=owner, repo=repo, number=number)
        pr = self.pulls.get((owner, repo, number))
        return (pr.additions + pr.deletions) if pr else 0

    def pr_changed_files(self, owner: str, repo: str, number: int) -> list[str]:
        self._record("pr_changed_files", owner=owner, repo=repo, number=number)
        return list(self.pr_files_by_pr.get(number, []))

    def pr_diff(self, owner: str, repo: str, number: int) -> str:
        self._record("pr_diff", owner=owner, repo=repo, number=number)
        return self.diff_by_pr.get(number, "")

    def pr_precommit_context(self, owner: str, repo: str, number: int) -> tuple[str, str]:
        self._record("pr_precommit_context", owner=owner, repo=repo, number=number)
        return self.precommit_by_pr.get(number, ("", ""))

    def pr_status_failed(self, owner: str, repo: str, number: int) -> bool:
        self._record("pr_status_failed", owner=owner, repo=repo, number=number)
        return self.pr_status_failed_by_pr.get(number, False)

    def list_open_prs(
        self,
        owner: str,
        repo: str,
        *,
        label: str | None = None,
        head: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self._record("list_open_prs", owner=owner, repo=repo, label=label, head=head, limit=limit)
        out = self.open_prs_response
        if label:
            out = [p for p in out if label in {lab.get("name") for lab in p.get("labels") or []}]
        if head is not None:
            out = [p for p in out if p.get("headRefName") == head]
        return [dict(p) for p in out[:limit]]

    def find_pr_by_head(self, owner: str, repo: str, head: str) -> dict[str, Any] | None:
        self._record("find_pr_by_head", owner=owner, repo=repo, head=head)
        found = self.pr_by_head.get(head)
        return dict(found) if found else None

    def latest_critic_report(self, owner: str, repo: str, number: int) -> str:
        self._record("latest_critic_report", owner=owner, repo=repo, number=number)
        return self.critic_report_by_pr.get(number, "")

    def review_threads(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        self._record("review_threads", owner=owner, repo=repo, number=number)
        return list(self.review_threads_by_pr.get(number, []))

    def review_threads_batch(
        self, owner: str, repo: str, numbers: list[int]
    ) -> dict[int, list[dict[str, Any]]]:
        self._record("review_threads_batch", owner=owner, repo=repo, numbers=list(numbers))
        return {n: list(self.review_threads_by_pr.get(n, [])) for n in numbers}

    def post_review_comment(
        self,
        owner: str,
        repo: str,
        number: int,
        body: str,
        *,
        file: str | None = None,
        line: int | None = None,
    ) -> bool:
        # Mirror the real fallback: inline attempt → on failure, plain comment
        # with the location prepended. Records BOTH attempts so tests can assert
        # the #234 fallback fired.
        if file is not None and line is not None:
            self._record(
                "post_review_comment_inline",
                owner=owner,
                repo=repo,
                number=number,
                body=body,
                file=file,
                line=line,
            )
            if not self.inline_review_fails:
                return True
            body = f"`{file}:{line}` — {body}"
        self._record("post_review_comment_plain", owner=owner, repo=repo, number=number, body=body)
        return True

    def enable_pr_auto_merge(self, owner: str, repo: str, number: int) -> AutoMergeResult:
        self._record("enable_pr_auto_merge", owner=owner, repo=repo, number=number)
        if self.auto_merge_fail_reason is not None:
            return AutoMergeResult(False, self.auto_merge_fail_reason)
        return AutoMergeResult(True)

    def disable_pr_auto_merge(self, owner: str, repo: str, number: int) -> bool:
        self._record("disable_pr_auto_merge", owner=owner, repo=repo, number=number)
        return True

    def merge_pull_request(
        self, owner: str, repo: str, number: int, *, method: MergeMethod = "squash"
    ) -> MergeResult:
        self._record(
            "merge_pull_request",
            owner=owner,
            repo=repo,
            number=number,
            merge_method=method,
        )
        if self.merge_fail_reason is not None:
            return MergeResult(False, self.merge_fail_reason)
        return MergeResult(True)

    def delete_branch(self, owner: str, repo: str, branch: str) -> bool:
        self._record("delete_branch", owner=owner, repo=repo, branch=branch)
        return not self.delete_branch_fails

    def _next_number(self) -> int:
        existing = [n for (_, _, n) in self.issues]
        seed = self.next_issue_number
        if seed is not None and not existing:
            self.next_issue_number = seed + 1
            return seed
        return (max(existing) if existing else (seed or 1000)) + 1


# ---------------------------------------------------------------------------
# Backlog helper — used by the brainstormer (issue #123). Lives here so
# call sites stay out of subprocess / gh-CLI territory.
# ---------------------------------------------------------------------------


@dataclass
class OpenBacklog:
    """Open backlog snapshot: epics (label=``epic``) vs. everything else.

    The brainstormer feeds this into its prompt so the SDK session can
    see what already exists and avoid re-proposing duplicates.
    """

    epics: list[Issue] = field(default_factory=list)
    tickets: list[Issue] = field(default_factory=list)


def list_open_backlog(
    client: GhClient,
    owner: str,
    repo: str,
    *,
    limit: int = 50,
    epic_label: str = "epic",
) -> OpenBacklog:
    """Return open epics + tickets for ``owner/repo``.

    Implementation: one labelled list for epics, one un-labelled list for
    "everything open" minus those epics. Both are bounded by ``limit`` so
    a stale repo doesn't blow up the prompt budget.

    We avoid shelling out to ``gh`` directly — the brainstormer should
    only ever touch GitHub through :class:`GhClient`.
    """
    epics = client.issues_by_label(owner, repo, epic_label, limit)
    epic_numbers = {e.number for e in epics}

    # "Tickets" = open issues that aren't epics. The protocol only exposes
    # ``issues_by_label`` — we approximate "all open" by listing the empty
    # label-set; implementations that don't support that gracefully return
    # [] and the brainstormer simply gets no ticket context.
    try:
        all_open = client.issues_by_label(owner, repo, "", limit * 2)
    except Exception:  # noqa: BLE001 — best-effort context only
        all_open = []
    tickets = [i for i in all_open if i.number not in epic_numbers][:limit]
    return OpenBacklog(epics=epics, tickets=tickets)


__all__ = [
    "GhClient",
    "GhError",
    "GhTokenSource",
    "GithubkitClient",
    "Issue",
    "MockGhClient",
    "OpenBacklog",
    "PullRequest",
    "ResolvedToken",
    "TokenSource",
    "list_open_backlog",
    "resolve_token_info",
    "resolve_token",
]
