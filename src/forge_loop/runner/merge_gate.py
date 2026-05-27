"""Pre-merge safety gate: refuse to merge a worker's PR when its source
issue was closed mid-flight.

Issue #65 — real correctness gap observed in dogfood: operator closed
issue #47 as dup-of-#55, a worker that started 3min earlier kept running,
opened PR #62, critic approved, runner auto-merged. The closed source
issue did NOT block the merge — operator's intent to STOP was silently
overridden.

Fix: AFTER the critic runs, BEFORE auto-merge can fire, the runner
re-fetches each PR's source issue state via ``gh issue view``. If the
issue is CLOSED (or the gh call fails — conservative), the runner:
  * disables auto-merge on the PR
  * posts an explanatory comment on the PR
  * emits a ``merge_refused_issue_closed`` event
  * leaves the PR open (do not close — operator may still want it)
  * flips the worker outcome status from ``merged`` to ``open`` so
    attempts history reflects the truth.

Adversarial: operator closes AND reopens within the worker's run →
final state OPEN → merge proceeds. We only check ONCE, at gate time,
so the last gh check wins.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any, Protocol

from forge_loop.state import append_event
from forge_loop.worker import WorkerOutcome


class GhMergeGateClient(Protocol):
    """The slice of ``forge_loop.gh`` this gate needs. Lets tests inject a
    spy without monkey-patching the global module."""

    def get_issue_state(self, issue: int,
                        repo: str | None = None) -> str | None: ...

    def disable_pr_auto_merge(self, pr: int | str,
                              repo: str | None = None) -> bool: ...

    def pr_comment(self, pr: int | str, body: str,
                   repo: str | None = None) -> bool: ...


def _refusal_comment(issue_num: int, issue_state: str | None) -> str:
    """The PR comment we leave on a refused merge. Surfaces enough context
    that an operator skimming notifications knows WHY the loop stopped."""
    if issue_state is None:
        state_blurb = "could not be fetched (gh call failed)"
    else:
        state_blurb = f"state: {issue_state.lower()}"
    return (
        f"Source issue #{issue_num} was closed mid-flight ({state_blurb}). "
        "Loop refusing auto-merge. Reopen the issue OR merge manually."
    )


def check_issue_closed_gate(
    outcome: WorkerOutcome,
    *,
    gh: GhMergeGateClient,
    repo: str | None,
    events_file: Any | None = None,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
) -> bool:
    """Re-check the source issue's state for a single outcome.

    Returns True iff the gate REFUSED the merge (issue closed or gh
    unreachable). Mutates ``outcome.status`` from ``merged`` → ``open``
    when refusing so the attempts ledger reflects reality.

    Outcomes without a ``pr_url`` are skipped (nothing to gate).
    """
    if not outcome.pr_url:
        return False

    state = gh.get_issue_state(outcome.issue, repo=repo)

    # OPEN → proceed (the normal happy path).
    if state == "OPEN":
        return False

    # CLOSED or unknown → refuse. Conservative on unknown: we'd rather
    # leave a PR open for human triage than auto-merge while we're not
    # sure the operator still wants it.
    body = _refusal_comment(outcome.issue, state)
    # Best-effort side effects; the event + status flip below MUST still fire.
    with contextlib.suppress(Exception):
        gh.disable_pr_auto_merge(outcome.pr_url, repo=repo)
    with contextlib.suppress(Exception):
        gh.pr_comment(outcome.pr_url, body, repo=repo)

    payload = {
        "issue": outcome.issue,
        "pr": outcome.pr_url,
        "issue_state": state,  # None = gh fetch failed; CLOSED = explicit
    }
    if events_file is not None:
        append_event(events_file, "merge_refused_issue_closed", **payload)
    if emit is not None:
        emit("merge_refused_issue_closed", payload)

    # Reflect truth in attempts history: the worker may have raced ahead
    # of the close and reported "merged", but we just blocked it.
    if outcome.status == "merged":
        outcome.status = "open"

    return True


def apply_issue_closed_gate(
    outcomes: list[WorkerOutcome],
    *,
    gh: GhMergeGateClient,
    repo: str | None,
    events_file: Any | None = None,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
) -> list[int]:
    """Run the gate over every outcome. Returns the list of issue numbers
    whose merge was refused."""
    refused: list[int] = []
    for o in outcomes:
        if check_issue_closed_gate(
            o, gh=gh, repo=repo, events_file=events_file, emit=emit,
        ):
            refused.append(o.issue)
    return refused
