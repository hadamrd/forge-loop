"""Typed GitHub client (issue #83).

Replaces the 18 ``subprocess.run(["gh", ...])`` callsites in
:mod:`forge_loop.gh` with a single :class:`GhClient` backed by
:mod:`githubkit` — the typed, async-capable, OpenAPI-generated SDK.

Win shape:
* Per-call latency drops from ~50-100ms subprocess spawn to an HTTP
  round-trip the SDK can keep-alive across calls.
* Return values are typed (Pydantic models from githubkit), not
  stringly-typed JSON.
* Pagination + rate-limit handling lives in githubkit, not per-callsite.
* :class:`MockGhClient` records calls in-memory for tests — no
  ``monkeypatch.setattr(subprocess, "run", ...)`` per case.
* :class:`GhError` raises a typed exception with HTTP status + body.

Auth: ``GH_TOKEN`` env var (universal pattern across dev tools).

Migration pattern: this PR ships the framework (Protocol, githubkit-
backed impl, mock, auth, error type) + tests. Per-method follow-ups
swap each ``forge_loop.gh.<fn>`` body to call through the client.
The legacy ``forge_loop.gh`` surface keeps working with zero changes
until each method migrates.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol


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


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GhError(RuntimeError):
    """Raised by :class:`GhClient` on a non-2xx response.

    Carries the HTTP status + response body tail so callers + log
    readers can reconstruct what happened without re-issuing the call.
    """

    def __init__(self, method: str, status: int, body_tail: str) -> None:
        super().__init__(f"github {method} returned HTTP {status}: {body_tail[:300]}")
        self.method = method
        self.status = status
        self.body_tail = body_tail


# ---------------------------------------------------------------------------
# Protocol — call sites depend on this, not the impl.
# ---------------------------------------------------------------------------


class GhClient(Protocol):
    """GitHub operations the loop uses. Subset of githubkit's full API."""

    def issues_by_label(self, owner: str, repo: str, label: str, limit: int) -> list[Issue]:
        ...

    def get_issue(self, owner: str, repo: str, number: int) -> Issue | None:
        ...

    def add_comment(self, owner: str, repo: str, number: int, body: str) -> None:
        ...

    def add_labels(self, owner: str, repo: str, number: int, labels: list[str]) -> None:
        ...

    def remove_label(self, owner: str, repo: str, number: int, label: str) -> None:
        ...

    def get_pull(self, owner: str, repo: str, number: int) -> PullRequest | None:
        ...


# ---------------------------------------------------------------------------
# Auth resolution — small, explicit, documented.
# ---------------------------------------------------------------------------


def resolve_token() -> str | None:
    """Resolve a GitHub token from env (canonical for dev tools).

    Precedence:
    1. ``GH_TOKEN`` env (gh CLI convention)
    2. ``GITHUB_TOKEN`` env (GitHub Actions convention)
    3. None — caller decides whether unauthenticated mode is acceptable.

    Operators set the token in their ``.env`` next to ``LOOP_GH_REPO``.
    A future Settings field could surface this if needed.
    """
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or None


# ---------------------------------------------------------------------------
# Real implementation — wraps githubkit.GitHub.
# ---------------------------------------------------------------------------


class GithubkitClient:
    """Real GhClient — talks to api.github.com via githubkit.

    One instance per process is enough; githubkit's HTTP client pools
    + reuses connections, so concurrent calls share the pipeline.
    """

    def __init__(self, token: str | None = None) -> None:
        from githubkit import GitHub
        self._gh = GitHub(token or resolve_token())

    def _raise_if_error(self, method: str, response: Any) -> None:
        status = getattr(response, "status_code", None)
        if status and status >= 400:
            body = getattr(response, "text", "") or str(getattr(response, "parsed_data", ""))
            raise GhError(method, status, body)

    def issues_by_label(self, owner: str, repo: str, label: str, limit: int) -> list[Issue]:
        resp = self._gh.rest.issues.list_for_repo(
            owner=owner, repo=repo, labels=label, per_page=min(limit, 100), state="open",
        )
        self._raise_if_error(f"list_for_repo({label})", resp)
        out: list[Issue] = []
        for item in resp.parsed_data[:limit]:
            # Skip PRs — list_for_repo returns issues + PRs by default.
            if getattr(item, "pull_request", None):
                continue
            out.append(Issue(
                number=item.number,
                title=item.title or "",
                body=item.body or "",
                state=str(item.state),
                labels=[lab.name for lab in (item.labels or []) if hasattr(lab, "name")],
            ))
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
            labels=[lab.name for lab in (item.labels or []) if hasattr(lab, "name")],
        )

    def add_comment(self, owner: str, repo: str, number: int, body: str) -> None:
        resp = self._gh.rest.issues.create_comment(
            owner=owner, repo=repo, issue_number=number, body=body,
        )
        self._raise_if_error(f"add_comment({number})", resp)

    def add_labels(self, owner: str, repo: str, number: int, labels: list[str]) -> None:
        resp = self._gh.rest.issues.add_labels(
            owner=owner, repo=repo, issue_number=number, labels=labels,
        )
        self._raise_if_error(f"add_labels({number}, {labels})", resp)

    def remove_label(self, owner: str, repo: str, number: int, label: str) -> None:
        try:
            resp = self._gh.rest.issues.remove_label(
                owner=owner, repo=repo, issue_number=number, name=label,
            )
            self._raise_if_error(f"remove_label({number}, {label})", resp)
        except GhError as e:
            # 404 is "label wasn't on the issue" — not an error from the
            # caller's perspective (legacy gh CLI returned ok on this).
            if e.status != 404:
                raise

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
            labels=[lab.name for lab in (item.labels or []) if hasattr(lab, "name")],
            additions=getattr(item, "additions", 0) or 0,
            deletions=getattr(item, "deletions", 0) or 0,
            changed_files=getattr(item, "changed_files", 0) or 0,
        )


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
    calls: list[tuple[str, dict]] = field(default_factory=list)

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
    "GithubkitClient",
    "Issue",
    "MockGhClient",
    "OpenBacklog",
    "PullRequest",
    "list_open_backlog",
    "resolve_token",
]
