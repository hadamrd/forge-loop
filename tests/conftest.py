"""Pytest configuration for the forge-loop test suite.

Issue #39 introduced an extras gate on experimental modules (dashboard,
multirepo, runner_async, integrations, observability, replay, pipeline).
The dev install has every experimental dep available, but the gate
also honours an explicit override env var. We set that override here so
existing experimental-module tests can keep importing without each test
file having to opt in.

`test_install_surface.py` explicitly clears this env var (via
monkeypatch) when it needs to simulate a default-only install.
"""

from __future__ import annotations

import os
from typing import Any

from forge_loop.critic_format import finding_tag


def pytest_configure(config: object) -> None:  # noqa: ARG001 - pytest hook signature
    os.environ.setdefault("FORGE_LOOP_EXPERIMENTAL", "1")


# ---------------------------------------------------------------------------
# Shared review-thread factories (#230).
#
# The critic-vs-human thread classifier (``gh_issues._thread_is_critic``) keys
# off the critic's inline-finding tag. Tests across ``test_gh_review_threads``
# and ``test_orphan_pr_adoption`` need both flavours of thread; centralising the
# factories here (instead of re-spelling divergent ad-hoc dicts per file) keeps
# ONE shape, and — crucially — bases the *critic* body on the REAL
# ``critic_format.finding_tag`` formatter rather than a hard-coded ``**[sevN]**``
# literal. That way, if the producer's tag format ever drifts, these fixtures
# drift with it and the classifier tests catch the desync (the #230
# sev2/architecture concern) instead of silently testing a stale literal.
# ---------------------------------------------------------------------------


def make_critic_thread(
    id_: str = "t-critic",
    *,
    sev: str = "sev3",
    category: str = "correctness",
    resolved: bool = False,
    path: str = "src/app.py",
    line: int = 12,
) -> dict[str, Any]:
    """A leftover *critic* inline-comment review thread.

    Its opening-comment body is built from the real ``finding_tag`` formatter,
    so it carries the exact ``**[<sev>/<category>]**`` signature the critic
    emits. These leftover sev3 notes must NOT, on their own, hold an approved
    PR back (#230 AC3)."""
    body = f"{finding_tag(sev, category)} leftover critic finding"
    return {
        "id": id_,
        "isResolved": resolved,
        "isOutdated": False,
        "path": path,
        "line": line,
        "comments": [{"author": {"login": "critic-bot"}, "body": body, "path": path, "line": line}],
    }


def make_human_thread(
    id_: str = "t-human",
    *,
    resolved: bool = False,
    path: str = "src/app.py",
    line: int = 12,
    body: str = "Please rework this design.",
) -> dict[str, Any]:
    """A human request-changes review thread (free prose, NOT the critic tag).

    AC3: an unresolved human thread MUST hold a PR back even when the PR is
    CLEAN with no block label."""
    return {
        "id": id_,
        "isResolved": resolved,
        "isOutdated": False,
        "path": path,
        "line": line,
        "comments": [{"author": {"login": "alice"}, "body": body, "path": path, "line": line}],
    }
