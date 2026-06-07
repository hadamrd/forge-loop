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
from dataclasses import dataclass, field
from typing import Any, Protocol

from forge_loop.config import MutationGateConfig
from forge_loop.state import append_event
from forge_loop.worker import WorkerOutcome


class GhMergeGateClient(Protocol):
    """The slice of ``forge_loop.gh`` this gate needs. Lets tests inject a
    spy without monkey-patching the global module."""

    def get_issue_state(self, issue: int, repo: str | None = None) -> str | None: ...

    def disable_pr_auto_merge(self, pr: int | str, repo: str | None = None) -> bool: ...

    def pr_comment(self, pr: int | str, body: str, repo: str | None = None) -> bool: ...


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
            o,
            gh=gh,
            repo=repo,
            events_file=events_file,
            emit=emit,
        ):
            refused.append(o.issue)
    return refused


# ---------------------------------------------------------------------------
# Oracle-strength gate (issue #381): refuse merge when the scoped mutation
# check (#379) reports surviving mutants above the configured threshold for the
# single high-risk module. A wrong-but-green patch must NOT auto-merge and then
# promote itself into durable cognition (episodic memory / frontier advance)
# merely because the suite is vacuous.
# ---------------------------------------------------------------------------

# Distinct refusal reasons carried on the emitted event so an operator (and the
# scorecard projection) can tell "the oracle is weak" from "we never got an
# oracle reading" — the latter is the conservative-on-uncertainty branch.
_REASON_SURVIVORS = "survivors_exceed_threshold"
_REASON_UNAVAILABLE = "mutation_result_unavailable"


@dataclass(frozen=True)
class MutationCheckResult:
    """The slice of the scoped mutation-check (#379) this gate consumes.

    Minimal result type injected by the caller so this ticket does NOT depend
    on #379's runner being merged (see issue #381 "Out of scope"). When #379
    lands it can either satisfy this shape structurally or be adapted at the
    one wiring site in :mod:`forge_loop.runner.tick`.
    """

    module: str
    survivor_count: int
    survivors: list[str] = field(default_factory=list)


def _mutation_refusal_reason(
    result: MutationCheckResult | None, config: MutationGateConfig
) -> str | None:
    """Return the refusal reason, or ``None`` when the gate should pass.

    Conservative on uncertainty: a ``None`` result (mutation check
    unavailable/errored) is REFUSED rather than treated as a pass, mirroring
    the issue-closed gate's "refuse rather than land on uncertainty" stance.
    """
    if result is None:
        return _REASON_UNAVAILABLE
    if result.survivor_count > config.survivor_threshold:
        return _REASON_SURVIVORS
    return None


def _mutation_refusal_comment(
    result: MutationCheckResult | None,
    config: MutationGateConfig,
    reason: str,
) -> str:
    """PR comment naming the surviving mutant(s) so the refusal is actionable."""
    if reason == _REASON_UNAVAILABLE:
        return (
            f"Mutation check for `{config.module}` was unavailable/errored. "
            "Loop refusing auto-merge (conservative on uncertainty) — cannot "
            "confirm the oracle would catch a planted bug. Re-run the scoped "
            "mutation check, then re-trigger."
        )
    survivors = (result.survivors if result else None) or ["(unnamed survivor)"]
    bullets = "\n".join(f"- {s}" for s in survivors)
    count = result.survivor_count if result else 0
    return (
        f"Mutation check for `{config.module}` found {count} surviving mutant(s) "
        f"(threshold {config.survivor_threshold}). Loop refusing auto-merge: the "
        "tests would not catch these planted bugs, so the patch must not promote "
        "itself into durable cognition. Surviving mutants:\n"
        f"{bullets}"
    )


def apply_mutation_survivor_gate(
    outcomes: list[WorkerOutcome],
    *,
    result: MutationCheckResult | None,
    config: MutationGateConfig,
    gh: GhMergeGateClient,
    repo: str | None,
    events_file: Any | None = None,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
) -> list[int]:
    """Refuse merge for outcomes when the scoped mutation check is too weak.

    Returns the list of issue numbers whose merge was refused (empty when the
    gate passes or is disabled). On refusal each PR has auto-merge disabled, a
    survivor-naming comment posted, and a ``merge_refused_mutation_survivors``
    event emitted; a ``merged`` status is flipped to ``open`` so the attempts
    ledger and memory promotion reflect the refusal.

    ``config.enabled=False`` makes this a no-op regardless of the result,
    preserving the pre-#381 behaviour.
    """
    if not config.enabled:
        return []

    reason = _mutation_refusal_reason(result, config)
    if reason is None:
        return []

    body = _mutation_refusal_comment(result, config, reason)
    survivor_count = result.survivor_count if result is not None else None
    survivors = list(result.survivors) if result is not None else []

    refused: list[int] = []
    for o in outcomes:
        if not o.pr_url:
            continue
        # Best-effort side effects; the event + status flip below MUST still fire.
        with contextlib.suppress(Exception):
            gh.disable_pr_auto_merge(o.pr_url, repo=repo)
        with contextlib.suppress(Exception):
            gh.pr_comment(o.pr_url, body, repo=repo)

        payload = {
            "issue": o.issue,
            "pr": o.pr_url,
            "module": config.module,
            "survivor_count": survivor_count,
            "survivors": survivors,
            "reason": reason,
        }
        if events_file is not None:
            append_event(events_file, "merge_refused_mutation_survivors", **payload)
        if emit is not None:
            emit("merge_refused_mutation_survivors", payload)

        if o.status == "merged":
            o.status = "open"
        refused.append(o.issue)
    return refused
