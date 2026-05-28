"""Stuck-issue sweep — demote ``loop:ready`` issues that the iteration loop
has repeatedly given up on (issue #129).

Background
==========

The existing ``maintenance.py`` runs an LLM-driven backlog GROOMER that
closes dupes, retitles, and adds ``loop:ready``. It does **not** scan
events.jsonl for issues the runner has already exhausted iteration on.

That gap leaves stuck issues looking healthy: the iteration loop bails
with ``worker_iterations_exhausted`` and the escalation path is supposed
to drop ``loop:ready`` and add ``loop:needs-human``. But any transient
gap in that escalation (label-API hiccup, partial gh-cli failure, the
pre-#128 bug that dropped the label removal entirely) leaves the issue
``loop:ready`` and the dispatcher keeps re-picking it on every tick.

This sweep is the health-check that closes the gap. It runs once per
tick after the iteration loop, before the next dispatch batch. It reads
the tail of events.jsonl, counts exhausted attempts per issue (resetting
on any success-shaped event after an exhausted one), and demotes
anything that crosses the configured threshold while still wearing the
``loop:ready`` label.

The sweep is conservative on purpose:

* GhClient errors are caught and logged as ``stuck_sweep_demote_failed``
  so a flaky GitHub never crashes the tick.
* The success-after-exhausted heuristic means a recovered issue won't
  get demoted just because an earlier attempt failed.
* The label check is the final gate: we only demote issues that *still*
  carry ``loop:ready``, so the sweep is idempotent across ticks.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from forge_loop.events import EventBase, StuckSweepDemotedEvent, emit
from forge_loop.log import get_logger


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


# Event kinds we treat as "this issue made forward progress" — seeing one
# of these AFTER an exhausted event for the same issue means the issue
# recovered and the prior exhausted count is wiped. ``redeploy`` with
# ok=True is excluded because it's repo-level, not issue-level.
SUCCESS_KINDS: frozenset[str] = frozenset({
    "worker_merged",
    "pr_merged",
    "worker_iteration_merged",
    "iteration_merged",
})


@dataclass(frozen=True)
class Demotion:
    """One issue the sweep decided to demote.

    ``attempts`` is the count of ``worker_iterations_exhausted`` events
    we observed since the most recent success-shaped event. ``last_state``
    is the ``final_state`` field copied from the most recent exhausted
    event, so the demotion comment can tell the operator where the loop
    stopped without re-reading the log.
    """

    issue: int
    attempts: int
    last_state: str
    pr_url: str | None
    ok: bool  # False means the gh API call(s) failed; sweep still records it


@dataclass
class SweepReport:
    """Result of one ``sweep()`` call.

    ``demotions`` lists every issue we attempted to demote this tick.
    ``scanned`` is the number of events read from the file (≤ ``tail``).
    ``errors`` captures any gh API failures, keyed by issue number, so
    the operator can see why a demotion didn't land without grepping the
    structured log.
    """

    demotions: list[Demotion] = field(default_factory=list)
    scanned: int = 0
    errors: dict[int, str] = field(default_factory=dict)

    def issue_numbers(self) -> list[int]:
        return [d.issue for d in self.demotions]


class GhClientLike(Protocol):
    """Minimal subset of ``forge_loop.gh_client.GhClient`` the sweep needs.

    Defined locally so tests can pass a hand-rolled fake without having
    to satisfy the full protocol. The two production implementations
    (``GithubkitClient`` and the in-memory ``FakeGhClient``) both
    structurally match this.
    """

    def get_issue(self, owner: str, repo: str, number: int) -> Any: ...
    def add_labels(self, owner: str, repo: str, number: int, labels: list[str]) -> None: ...
    def remove_label(self, owner: str, repo: str, number: int, label: str) -> None: ...
    def add_comment(self, owner: str, repo: str, number: int, body: str) -> None: ...


EmitFn = Callable[[EventBase], None]


# ---------------------------------------------------------------------------
# Event tail reader
# ---------------------------------------------------------------------------


def _read_tail(events_file: Path, tail: int) -> list[dict[str, Any]]:
    """Return the last ``tail`` JSON-lines from ``events_file``.

    Missing file → empty list (a fresh runner has no events yet). Lines
    that fail to parse are silently skipped — events.jsonl is append-only
    and the JSON encoder is well-behaved, so corruption here usually
    means a partial write at process kill, which we should tolerate.
    """
    if not events_file.exists():
        return []
    try:
        # We read the whole file because ``tail`` is small (default 100)
        # and events.jsonl is bounded by the rotate setting. Seeking from
        # the end would save IO on huge logs but adds complexity that
        # isn't earning its keep here.
        lines = events_file.read_text().splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-tail:]:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Core counting logic
# ---------------------------------------------------------------------------


@dataclass
class _IssueTally:
    attempts: int = 0
    last_state: str = ""
    pr_url: str | None = None


def _tally_exhausted(events: list[dict[str, Any]]) -> dict[int, _IssueTally]:
    """Walk events in order, counting exhausted attempts per issue.

    A success-shaped event for the same issue zeroes the running count —
    that's the "recovered after a bad run" case from the test matrix.
    The last-seen ``final_state`` / ``pr_url`` win, so the demotion
    comment reflects the most recent failure shape.
    """
    tallies: dict[int, _IssueTally] = defaultdict(_IssueTally)
    for rec in events:
        kind = rec.get("kind")
        issue = rec.get("issue")
        if not isinstance(issue, int):
            continue
        if kind == "worker_iterations_exhausted":
            t = tallies[issue]
            t.attempts += 1
            fs = rec.get("final_state")
            if isinstance(fs, str):
                t.last_state = fs
            pr = rec.get("pr_url")
            if isinstance(pr, str) or pr is None:
                t.pr_url = pr
        elif kind in SUCCESS_KINDS:
            # Recovery — wipe the running count for this issue.
            tallies[issue] = _IssueTally()
    return tallies


# ---------------------------------------------------------------------------
# Demotion
# ---------------------------------------------------------------------------


def _demote_one(
    gh: GhClientLike,
    owner: str,
    repo: str,
    issue: int,
    tally: _IssueTally,
    *,
    ready_label: str,
    needs_human_label: str,
) -> tuple[bool, str | None]:
    """Drop ``ready_label`` + add ``needs_human_label`` + post a comment.

    Returns ``(ok, error)``. ``ok=True`` means at least the label flip
    landed — the comment is best-effort and a comment-only failure still
    counts as a successful demotion (the dispatcher won't re-pick the
    issue, which is what matters).

    Issues that no longer carry ``ready_label`` are skipped (returns
    ``(False, "not_ready")``) so the sweep is idempotent across ticks
    and won't fight with a concurrent escalate_to_human run.
    """
    log = get_logger()
    # Idempotency guard. If the issue lost loop:ready since the last tick
    # — either escalate_to_human caught up or a human edited the issue —
    # we have nothing to do.
    try:
        cur = gh.get_issue(owner, repo, issue)
    except Exception as ex:  # noqa: BLE001 — gh layer raises domain-specific types we don't import here
        log.warning("stuck_sweep_get_issue_failed", issue=issue, err=str(ex)[:200])
        return False, f"get_issue: {ex}"
    if cur is None:
        return False, "issue_not_found"
    labels = list(getattr(cur, "labels", []) or [])
    if ready_label not in labels:
        return False, "not_ready"

    body = (
        f"forge-loop stuck-sweep: this issue has hit "
        f"`worker_iterations_exhausted` {tally.attempts}x without recovering.\n"
        f"\n"
        f"- last state: `{tally.last_state or 'unknown'}`\n"
        f"- last PR: {tally.pr_url or '(none)'}\n"
        f"\n"
        f"Dropping `{ready_label}` and adding `{needs_human_label}` so the "
        f"dispatcher stops re-picking it. A human should look at the "
        f"worktree / PR and either fix the underlying bug or close the "
        f"issue.\n"
    )

    # Label flip first — that's the load-bearing operation. Comment is
    # cosmetic; we still report ok=True if only the comment fails.
    try:
        gh.add_labels(owner, repo, issue, [needs_human_label])
        gh.remove_label(owner, repo, issue, ready_label)
    except Exception as ex:  # noqa: BLE001
        log.warning("stuck_sweep_label_failed", issue=issue, err=str(ex)[:200])
        return False, f"label: {ex}"

    try:
        gh.add_comment(owner, repo, issue, body)
    except Exception as ex:  # noqa: BLE001
        log.info("stuck_sweep_comment_failed", issue=issue, err=str(ex)[:200])
        # Don't fail the demotion — the labels are what matter.

    return True, None


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def sweep(
    events_file: Path,
    gh_client: GhClientLike,
    *,
    owner: str,
    repo: str,
    threshold: int = 2,
    ready_label: str = "loop:ready",
    needs_human_label: str = "loop:needs-human",
    tail: int = 100,
    emit_fn: EmitFn | None = None,
) -> SweepReport:
    """Sweep the event tail for stuck issues and demote them.

    Parameters
    ----------
    events_file:
        Path to ``loop-runner-events.jsonl``. Missing file → empty sweep.
    gh_client:
        Anything matching :class:`GhClientLike` — production code passes
        the real ``GithubkitClient``; tests pass an in-memory fake.
    owner, repo:
        GitHub owner/repo for the gh API calls. Sourced from
        ``settings.repo.github`` at the tick caller.
    threshold:
        Minimum exhausted-attempt count to trigger demotion. Defaults to
        2 — one bad run is forgivable, two is a pattern. Configurable via
        ``settings.maintenance.stuck_threshold_attempts``.
    ready_label / needs_human_label:
        Label names. Defaults match production; settings override at
        the caller.
    tail:
        How many trailing event records to scan. 100 is plenty for a
        single tick — the dispatcher would have re-fired the issue many
        times in that window if it were still ``loop:ready``.
    emit_fn:
        Injection point for the typed-event emitter. Defaults to a
        closure over :func:`forge_loop.events.emit` bound to
        ``events_file``. Tests override to capture without disk IO.

    Returns
    -------
    SweepReport summarising what we touched. Never raises.
    """
    log = get_logger()
    if threshold < 1:
        threshold = 1  # nonsense thresholds get clamped, not crashed

    if emit_fn is None:
        def emit_fn(ev: EventBase) -> None:  # noqa: E306 — local closure
            emit(events_file, ev)

    events = _read_tail(events_file, tail)
    report = SweepReport(scanned=len(events))
    tallies = _tally_exhausted(events)

    # Stable ordering so logs / tests are deterministic.
    for issue in sorted(tallies):
        tally = tallies[issue]
        if tally.attempts < threshold:
            continue
        ok, err = _demote_one(
            gh_client,
            owner,
            repo,
            issue,
            tally,
            ready_label=ready_label,
            needs_human_label=needs_human_label,
        )
        # ``not_ready`` is a silent skip: the issue already shed the
        # ready label (escalate_to_human caught up, or a human edited
        # it). Nothing to record, nothing to emit — the sweep is
        # idempotent across ticks precisely because this branch fires
        # cheaply and exits.
        if err == "not_ready":
            d = Demotion(
                issue=issue,
                attempts=tally.attempts,
                last_state=tally.last_state,
                pr_url=tally.pr_url,
                ok=False,
            )
            report.demotions.append(d)
            continue
        # We record EVERY remaining attempted demotion, even ones that
        # failed (gh blew up). The Demotion.ok flag distinguishes them.
        # This matters because the operator surface (dashboard, dump)
        # wants to show "what did the sweep try to touch" not "what
        # landed."
        d = Demotion(
            issue=issue,
            attempts=tally.attempts,
            last_state=tally.last_state,
            pr_url=tally.pr_url,
            ok=ok,
        )
        report.demotions.append(d)
        if not ok:
            report.errors[issue] = err or "unknown"
        try:
            emit_fn(StuckSweepDemotedEvent(
                issue=issue,
                attempts=tally.attempts,
                last_state=tally.last_state,
                pr_url=tally.pr_url,
                ok=ok,
                reason=err or "",
            ))
        except Exception as ex:  # noqa: BLE001 — never crash the sweep on emit
            log.warning("stuck_sweep_emit_failed", issue=issue, err=str(ex)[:200])

    return report


__all__ = [
    "Demotion",
    "GhClientLike",
    "SUCCESS_KINDS",
    "SweepReport",
    "sweep",
]
