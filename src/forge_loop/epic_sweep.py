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
from datetime import UTC, datetime
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
    exactly one of ``closed`` / ``expired`` / ``skipped_open_subs`` /
    ``skipped_no_subs`` / ``errors``.

    ``closed`` were closed via the all-subs-resolved (completed) path;
    ``expired`` were closed via the TTL path (issue #435) — an open epic with
    zero open sub-issues that aged past ``epic_ttl_days``. They are reported
    distinctly because they mean different things to an operator: completed
    work vs. an undecomposed epic reaped for staleness.
    """

    closed: list[int] = field(default_factory=list)
    expired: list[int] = field(default_factory=list)
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


def build_expiry_comment(epic_number: int, age_days: int, ttl_days: int) -> str:
    """Build the audit comment posted before a TTL-expired epic is closed (#435).

    Deliberately and textually DISTINCT from :func:`build_close_comment`: the
    completed comment lists resolved sub-issues, this one names the age + TTL
    (``open 158 days, TTL 90 days``) so an operator can tell at a glance the
    epic was reaped for *staleness*, not because its work finished.
    """
    return (
        f"Auto-expiring epic #{epic_number}: open {age_days} days, TTL {ttl_days} "
        "days, with zero open sub-issues.\n\n"
        "Closed automatically by the forge-loop epic TTL sweep (issue #435) "
        "because it exceeded the configured epic time-to-live without being "
        "broken into tracked sub-issues. Re-open if this was premature."
    )


def _age_days(created_at: str | None, now: datetime) -> int | None:
    """Whole-day age (``now − created_at``) of an epic, or ``None`` when the
    timestamp is missing/unparseable (cannot prove age → caller must fail safe
    and never expire). Mirrors ``control/status._oldest_age_days`` for a single
    issue: aware-datetime math, floored at 0."""
    if not created_at:
        return None
    try:
        created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    reference = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return max(0, (reference - created).days)


def _post_and_close(
    gh_client: GhClientLike,
    *,
    owner: str,
    repo: str,
    epic_number: int,
    comment: str,
    reason: str,
) -> tuple[bool, str | None]:
    """Post the audit comment (best-effort) then close the epic.

    Shared by the completed and TTL close paths so neither reinvents the
    comment-then-close-then-record sequence (manifesto Q7). Returns
    ``(closed_ok, error_reason)``: a comment failure is swallowed (best-effort,
    mirrors stuck_sweep.py); only the close is load-bearing. ``error_reason`` is
    ``None`` on success, else a short string for ``report.errors``.
    """
    log = get_logger()
    try:
        gh_client.add_comment(owner, repo, epic_number, comment)
    except Exception as ex:  # noqa: BLE001 — comment is best-effort
        log.info("epic_sweep_comment_failed", epic=epic_number, err=str(ex)[:200])
    try:
        ok = gh_client.close_issue(owner, repo, epic_number, reason=reason)
    except Exception as ex:  # noqa: BLE001
        log.warning("epic_sweep_close_failed", epic=epic_number, err=str(ex)[:200])
        return False, f"close: {ex}"[:200]
    if ok:
        return True, None
    return False, "close_returned_false"


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
    epic_ttl_days: int = 0,
    now: datetime | None = None,
) -> EpicSweepReport:
    """Close every open epic whose tracked sub-issues are all resolved, plus
    expire stale undecomposed epics past a TTL (issue #435).

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
    epic_ttl_days:
        TTL in whole days for the expiry pass (issue #435). An open epic with
        **zero open** sub-issues whose age (``now − created_at``) exceeds this
        is closed via :func:`build_expiry_comment` and reported under
        ``expired``. A value ``<= 0`` **disables** the TTL pass entirely (the
        pre-#435 no-op). The all-subs-resolved (completed) close and the
        ≥1-open-sub fail-safe both dominate the TTL check.
    now:
        Reference instant for the age computation, **injected** so the function
        stays deterministic/clock-free in tests. Defaults to the current UTC
        time (mirrors ``tick_checks._agent_live_paths``); only consulted when
        the TTL pass is enabled.

    Returns
    -------
    EpicSweepReport summarising what we touched. Never raises — every
    GhClient error is caught and recorded.
    """
    report = EpicSweepReport()
    clock = now if now is not None else datetime.now(UTC)
    log = get_logger()

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

    # Phase 2 — decide + act on the snapshot. Bucket order encodes the
    # precedence the spec mandates: the ≥1-open-sub fail-safe DOMINATES (an
    # ancient epic with live work is never expired); the all-subs-resolved
    # (completed) close is checked before the TTL pass (an old, fully-resolved
    # epic still closes via the completed comment, not the expiry comment); the
    # TTL pass only ever reaps the leak population — epics with zero tracked
    # sub-issues — and only when enabled + provably aged past the TTL.
    for epic, subs in snapshot:
        if subs:
            if any(not _is_closed(s) for s in subs):
                report.skipped_open_subs.append(epic.number)  # fail-safe: live work
                continue
            # All sub-issues closed → completed close path (unchanged, #367).
            ok, err = _post_and_close(
                gh_client,
                owner=owner,
                repo=repo,
                epic_number=epic.number,
                comment=build_close_comment(epic.number, subs),
                reason="completed",
            )
            if ok:
                report.closed.append(epic.number)
            elif err is not None:
                report.errors[epic.number] = err
            continue

        # Zero tracked sub-issues — the immortal leak population (#435). Expire
        # iff the TTL pass is enabled AND the age is provable AND past the TTL;
        # a None age (unknown created_at) fails safe → never expired.
        age = _age_days(epic.created_at, clock) if epic_ttl_days > 0 else None
        if age is not None and age > epic_ttl_days:
            ok, err = _post_and_close(
                gh_client,
                owner=owner,
                repo=repo,
                epic_number=epic.number,
                comment=build_expiry_comment(epic.number, age, epic_ttl_days),
                reason="not_planned",
            )
            if ok:
                report.expired.append(epic.number)
            elif err is not None:
                report.errors[epic.number] = err
            continue
        report.skipped_no_subs.append(epic.number)

    return report


__all__ = [
    "EpicSweepReport",
    "GhClientLike",
    "build_close_comment",
    "build_expiry_comment",
    "sweep",
]
