"""Single shared parser for GitHub PR references (#242 review fix).

Three modules independently grew a near-identical GitHub-PR-URL regex — the
recovery boot-probe (:func:`forge_loop.runner.recovery._parse_pr_url`), the
critic's repo-slug extractor (:func:`forge_loop.critic._repo_slug_from_pr_url`)
and the durable-findings canonical key (:func:`forge_loop.critic.canonical_pr_key`).
Divergent spellings risk format drift between the critic *write* path and the
repair *read* path of the findings store, which would silently make
``open_findings(pr)`` return ``[]`` and starve the worker — the very Q10 failure
mode #242 exists to kill. This module is the ONE parser they all build on.

The single :data:`_PR_REF_RE` recognises a GitHub PR/issue identity in:

- the HTML url ``github.com/{owner}/{repo}/pull/{n}``,
- the REST api url ``api.github.com/repos/{owner}/{repo}/pulls/{n}``,
- the scp-like ``github.com:{owner}/{repo}`` spelling, with an optional
  ``.git`` suffix,

ignoring any trailing slash, query string or anchor.
"""

from __future__ import annotations

import re

#: ONE regex for every GitHub PR/issue reference in the codebase. ``pull`` and
#: ``pulls`` (HTML vs REST), ``issues`` (so the slug extractor handles issue
#: urls too), an optional ``repos/`` (REST) and ``.git`` suffix, and a
#: ``[:/]`` host separator (scp-like remotes) all collapse here. Capturing
#: ``owner``/``repo``/``num`` lets callers build whatever projection they need.
_PR_REF_RE = re.compile(
    r"github\.com[:/](?:repos/)?"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/"
    r"(?:pull|pulls|issues)/(?P<num>\d+)",
    re.IGNORECASE,
)


def parse_pr_url(pr_url: str | None) -> tuple[str, str, int] | None:
    """Return ``(owner, repo, number)`` for a GitHub PR/issue url, else ``None``.

    Accepts the HTML ``github.com/{o}/{r}/pull/{n}`` and the REST
    ``api.github.com/repos/{o}/{r}/pulls/{n}`` shapes (plus ``.git`` and
    scp-like ``github.com:o/r`` spellings); trailing slash, query string and
    anchor are ignored.
    """

    if not pr_url:
        return None
    match = _PR_REF_RE.search(pr_url.strip())
    if match is None:
        return None
    return match.group("owner"), match.group("repo"), int(match.group("num"))


def repo_slug(pr_url: str) -> str:
    """Return ``owner/repo`` parsed out of a PR/issue url, or ``""``."""

    parsed = parse_pr_url(pr_url)
    if parsed is None:
        return ""
    owner, repo, _ = parsed
    return f"{owner}/{repo}"
