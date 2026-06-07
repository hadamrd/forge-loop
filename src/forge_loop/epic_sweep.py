"""Epic auto-close sweep — close an epic once every tracked sub-issue is
resolved (issue #367).

Background
==========

Epics never auto-close. When an ``epic``-labelled issue is broken into
sub-issues and every sub-issue lands (closed, or its PR merged — a merged
PR closes its sub-issue, so "merged" is covered by "closed"), the parent
epic stays **open** forever. Nothing in the loop reaps it: the LLM backlog
groomer is explicitly told to SKIP ``epic``-labelled issues
(``runner/tick.py`` DEFAULT_BRIEF STEP 2), so it will never close them
either. The result is permanent backlog bloat — operators can't tell a
live epic from a finished one.

This is the deterministic, Python-native counterpart to the stuck-issue
sweep (#129, ``stuck_sweep.py``) and the branch sweep (#146): pure logic
separated from :class:`forge_loop.gh_client.GhClient` I/O, every GhClient
error caught and recorded (never crash the tick), and a single typed
summary event emitted by the tick wiring (``run_epic_sweep`` in
``runner/tick_checks.py``).

Close condition (exact)
=======================

An epic is closed **iff**:

* it carries the configured ``epic_label`` (default ``"epic"``), and
* it is still ``state=open`` (the gate that makes re-runs a no-op —
  idempotency), and
* it has **≥ 1** tracked sub-issue, and
* **every** tracked sub-issue is CLOSED.

Conservative by construction:

* An epic with ≥ 1 OPEN sub-issue is NEVER closed (the core regression
  guard — listed under ``skipped_open_subs``).
* An epic with ZERO tracked sub-issues is NEVER closed (it may just not
  have been broken down yet — listed under ``skipped_no_subs``).
* If :meth:`GhClient.sub_issues` raises, the epic is recorded under
  ``errors`` and skipped — never closed — and the sweep does not raise.

No cascade
==========

Sub-issues for **all** epics are read up front (a snapshot) before any
``close_issue`` call. So closing a child epic in this pass can never flip
a parent epic's "all sub-issues closed" condition mid-pass: the parent was
already evaluated against the child's pre-close (open) state. Only epics
that *independently* meet the condition close in a single pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from forge_loop.gh_client import Issue, SubIssue
from forge_loop.log import get_logger

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class GhClientLike(Protocol):
    """Minimal subset of :class:`forge_loop.gh_client.GhClient` the sweep needs.

    Defined locally so tests can pass a hand-rolled fake without satisfying
    the full protocol. Both production implementations (``GithubkitClient``
    and the in-memory ``MockGhClient``) structurally match this.
    """

    def issues_by_label(self, owner: str, repo: str, label: str, limit: int) -> list[Issue]: ...
    def sub_issues(self, owner: str, repo: str, number: int) -> list[SubIssue]: ...
    def add_comment(self, owner: str, repo: str, number: int, body: str) -> None: ...
    def close_issue(
        self, owner: str, repo: str, number: int, *, reason: str | None = ...
    ) -> bool: ...


@dataclass
class EpicSweepReport:
    """Result of one :func:`sweep` call.

    Every list holds epic issue NUMBERS. ``errors`` maps an epic number to a
    short reason so the operator can see why a close didn't land without
    grepping structlog. Mutually exclusive: each evaluated epic lands in
    exactly one of ``closed`` / ``skipped_open_subs`` / ``skipped_no_subs`` /
    ``errors``.
    """

    closed: list[int] = field(default_factory=list)
    skipped_open_subs: list[int] = field(default_factory=list)
    skipped_no_subs: list[int] = field(default_factory=list)
    errors: dict[int, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _is_closed(sub: SubIssue) -> bool:
    """True iff a sub-issue is closed. A merged PR closes its issue, so
    "merged" is covered. Case-insensitive — GraphQL returns ``CLOSED``,
    REST returns ``closed``."""
    return str(sub.state).strip().upper() == "CLOSED"


def build_close_comment(epic_number: int, subs: list[SubIssue]) -> str:
    """Build the audit comment posted before an epic is auto-closed.

    Lists every sub-issue by number, linking the resolving PR when GitHub
    surfaced one (``#146 (PR #149), #150 (PR #152), #151``). Pure function so
    it can be unit-tested directly with a populated closing-PR.
    """
    parts: list[str] = []
    for s in subs:
        if s.closing_pr:
            parts.append(f"#{s.number} (PR #{s.closing_pr})")
        else:
            parts.append(f"#{s.number}")
    listing = ", ".join(parts)
    return (
        f"Auto-closing epic #{epic_number}: all {len(subs)} sub-issues resolved "
        f"— {listing}.\n\n"
        "Closed automatically by the forge-loop epic sweep (issue #367) because "
        "every tracked sub-issue is closed/merged. Re-open if this was premature."
    )


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def sweep(
    gh_client: GhClientLike,
    *,
    owner: str,
    repo: str,
    epic_label: str = "epic",
    limit: int = 100,
) -> EpicSweepReport:
    """Close every open epic whose tracked sub-issues are all resolved.

    Parameters
    ----------
    gh_client:
        Anything matching :class:`GhClientLike` — production passes the real
        ``GithubkitClient``; tests pass an in-memory mock.
    owner, repo:
        GitHub owner/repo for the API calls.
    epic_label:
        Label that marks an issue as an epic. Default ``"epic"``.
    limit:
        Max epics to consider in one pass (bounds the GraphQL fan-out).

    Returns
    -------
    EpicSweepReport summarising what we touched. Never raises — every
    GhClient error is caught and recorded.
    """
    log = get_logger()
    report = EpicSweepReport()

    try:
        epics = gh_client.issues_by_label(owner, repo, epic_label, limit)
    except Exception as ex:  # noqa: BLE001 — a flaky list must never crash the tick
        log.warning("epic_sweep_list_failed", err=str(ex)[:200])
        return report

    # Phase 1 — read all sub-issues up front (snapshot). Doing every read
    # before any close guarantees closing one epic this pass can't cascade
    # into closing another (the parent is evaluated against the child's
    # pre-close state).
    snapshot: list[tuple[Issue, list[SubIssue]]] = []
    for epic in epics:
        if str(epic.state).strip().lower() != "open":
            # ``issues_by_label`` already filters to open in production; this
            # is the belt-and-braces gate that also makes re-runs a no-op.
            continue
        try:
            subs = list(gh_client.sub_issues(owner, repo, epic.number))
        except Exception as ex:  # noqa: BLE001 — degrade to "skip", never close
            log.warning("epic_sweep_sub_issues_failed", epic=epic.number, err=str(ex)[:200])
            report.errors[epic.number] = f"sub_issues: {ex}"[:200]
            continue
        snapshot.append((epic, subs))

    # Phase 2 — decide + act on the snapshot.
    for epic, subs in snapshot:
        if not subs:
            report.skipped_no_subs.append(epic.number)
            continue
        if any(not _is_closed(s) for s in subs):
            report.skipped_open_subs.append(epic.number)
            continue

        # All sub-issues closed → post the audit comment (best-effort) then
        # close the epic. The close is the load-bearing op; a comment-only
        # failure still lets the close proceed (mirrors stuck_sweep.py).
        body = build_close_comment(epic.number, subs)
        try:
            gh_client.add_comment(owner, repo, epic.number, body)
        except Exception as ex:  # noqa: BLE001 — comment is best-effort
            log.info("epic_sweep_comment_failed", epic=epic.number, err=str(ex)[:200])

        try:
            ok = gh_client.close_issue(owner, repo, epic.number, reason="completed")
        except Exception as ex:  # noqa: BLE001
            log.warning("epic_sweep_close_failed", epic=epic.number, err=str(ex)[:200])
            report.errors[epic.number] = f"close: {ex}"[:200]
            continue
        if ok:
            report.closed.append(epic.number)
        else:
            report.errors[epic.number] = "close_returned_false"

    return report


__all__ = [
    "EpicSweepReport",
    "GhClientLike",
    "build_close_comment",
    "sweep",
]
