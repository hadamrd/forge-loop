"""Per-issue attempt history — persisted as GH issue comments.

Workers append a 1-line summary of their attempt as a comment to the issue,
prefixed with a magic marker so future ticks can read them and learn from
past failures.

Why GH comments (vs sidecar JSONL): visible in the UI, persists across the
runner's worktree lifetime, gives humans context too.

Each record also carries a ``brief_fingerprint`` so the runner can skip an
issue when the latest attempt was for the same (issue body + brief
template) combination — preventing dupe PRs on partial failures and tight
retry loops. See ``compute_fingerprint`` / ``classify_skip``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from forge_loop import gh_issues as _gh

MARKER = "<!-- forge-loop-attempt -->"
COMMENT_RE = re.compile(re.escape(MARKER) + r".*?```json\s*\n(.*?)\n```", re.DOTALL)
BLOCKING_COMMENT_MARKERS = (
    "critic found",
    "critic still blocks",
    "post-merge critic",
    "blocking repair",
    "remaining blocker",
    "required repair",
)

# Default cooldown after a failed attempt before the same fingerprint is
# eligible for a fresh dispatch. Override via LOOP_RETRY_COOLDOWN_S.
DEFAULT_RETRY_COOLDOWN_S = 3600


@dataclass
class AttemptRecord:
    ts: str
    status: str  # merged | open | failed | timeout | no_pr
    pr_url: str | None
    duration_s: float
    note: str
    event_count: int
    brief_fingerprint: str = ""


def compute_fingerprint(
    issue_id: int | str,
    issue_body: str,
    brief_template_hash: str,
) -> str:
    """Stable sha256 over (issue_id, issue_body, brief_template_hash).

    Changing the issue body OR the brief template invalidates the fingerprint
    (the worker is being asked to do meaningfully different work). Identical
    inputs → identical fingerprint, regardless of when called.
    """
    # ``errors="surrogatepass"`` keeps the hasher robust against lone-surrogate
    # codepoints that can sneak into issue bodies via copy-paste of broken
    # unicode (hypothesis found this — see tests/property/test_fingerprint_property.py).
    # Without it, ``encode("utf-8")`` raises UnicodeEncodeError mid-tick.
    h = hashlib.sha256()
    h.update(str(issue_id).encode("utf-8", errors="surrogatepass"))
    h.update(b"\x00")
    h.update((issue_body or "").encode("utf-8", errors="surrogatepass"))
    h.update(b"\x00")
    h.update((brief_template_hash or "").encode("utf-8", errors="surrogatepass"))
    return h.hexdigest()


def render_comment(record: AttemptRecord) -> str:
    """Render an attempt record as a GH-flavored markdown comment."""
    payload: dict[str, Any] = {
        "ts": record.ts,
        "status": record.status,
        "pr_url": record.pr_url,
        "duration_s": round(record.duration_s, 1),
        "note": record.note,
        "event_count": record.event_count,
    }
    if record.brief_fingerprint:
        payload["brief_fingerprint"] = record.brief_fingerprint
    pretty_status = {
        "merged": ":white_check_mark: MERGED",
        "open": ":hourglass_flowing_sand: PR OPEN",
        "failed": ":x: FAILED",
        "timeout": ":alarm_clock: TIMEOUT",
        "no_pr": ":warning: NO PR",
    }.get(record.status, record.status.upper())

    parts = [
        MARKER,
        f"**forge-loop attempt** — {pretty_status} (~{round(record.duration_s)}s)",
    ]
    if record.pr_url:
        parts.append(f"PR: {record.pr_url}")
    if record.note:
        parts.append(f"Note: {record.note}")
    if record.brief_fingerprint:
        parts.append(f"fingerprint: `{record.brief_fingerprint[:12]}`")
    parts.append("\n```json\n" + json.dumps(payload, indent=2) + "\n```")
    return "\n\n".join(parts)


def record(
    issue: int,
    *,
    status: str,
    pr_url: str | None,
    duration_s: float,
    note: str = "",
    event_count: int = 0,
    repo: str | None = None,
    brief_fingerprint: str = "",
) -> None:
    """Append an attempt record as a GH comment on the issue."""
    rec = AttemptRecord(
        ts=datetime.now(UTC).isoformat(timespec="seconds"),
        status=status,
        pr_url=pr_url,
        duration_s=duration_s,
        note=note,
        event_count=event_count,
        brief_fingerprint=brief_fingerprint,
    )
    _gh.comment(issue, render_comment(rec), repo=repo)


def parse_history(comments_body: list[str]) -> list[dict[str, Any]]:
    """Parse a list of comment bodies; return attempt records (oldest first).

    Malformed JSON blocks are silently skipped — see ``parse_history_strict``
    for a variant that surfaces corruption to the caller.
    """
    records, _ = parse_history_strict(comments_body)
    return records


def parse_history_strict(
    comments_body: list[str],
) -> tuple[list[dict[str, Any]], int]:
    """Parse history, returning (records, corrupt_count).

    A "corrupt" row is a comment that carries the forge-loop marker but
    whose embedded JSON block fails to decode. Callers can emit an
    ``attempts_corrupt`` event when ``corrupt_count > 0`` and still treat
    the parsed records as authoritative for the rest.
    """
    out: list[dict[str, Any]] = []
    corrupt = 0
    for body in comments_body:
        if MARKER not in body:
            continue
        m = COMMENT_RE.search(body)
        if not m:
            # marker present but no json block → treat as corrupt.
            corrupt += 1
            continue
        try:
            out.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            corrupt += 1
            continue
    return out, corrupt


def parse_blocking_comments(
    comments_body: list[str],
    *,
    limit: int = 3,
    max_chars: int = 4000,
) -> list[str]:
    """Extract recent critic/operator blocker comments for the next worker.

    Attempt records only say "failed/no_pr/merged"; they do not carry the
    human or critic repair contract. Keep those comments visible to the worker
    so it cannot satisfy the issue with adjacent cleanup.
    """
    blockers: list[str] = []
    for body in comments_body:
        normalized = body.lower()
        if MARKER in body:
            continue
        if any(marker in normalized for marker in BLOCKING_COMMENT_MARKERS):
            blockers.append(body.strip()[:max_chars])
    return blockers[-limit:]


def fetch_history(issue: int, repo: str | None = None) -> list[dict[str, Any]]:
    """Fetch all forge-loop attempt records for an issue (oldest first)."""
    records, _ = fetch_history_strict(issue, repo=repo)
    return records


def fetch_history_strict(
    issue: int,
    repo: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Like ``fetch_history`` but also returns the count of corrupt rows.

    Reuses :func:`forge_loop.gh.issue_comment_bodies` for the single
    ``gh issue view --comments`` round-trip instead of shelling out to an
    identical subprocess of its own (issue #226 — the inline duplicate was a
    second copy of the same fetch).
    """
    if not repo:
        raise RuntimeError("fetch_history requires repo='owner/name'")
    return parse_history_strict(_gh.issue_comment_bodies(issue, repo=repo))


def fetch_blocking_comments(issue: int, repo: str | None = None) -> list[str]:
    """Fetch recent critic/operator blocker comments for an issue."""
    if not repo:
        raise RuntimeError("fetch_blocking_comments requires repo='owner/name'")
    return parse_blocking_comments(_gh.issue_comment_bodies(issue, repo=repo))


@dataclass
class IssueAttempts:
    """The full per-issue comment-derived view, computed from ONE fetch.

    ``fetch_history_strict`` and ``fetch_blocking_comments`` each shell the
    identical ``gh issue view <n> --comments`` subprocess; calling both per
    issue per tick fetched the same payload twice (issue #226). This bundles
    the single fetch + both parses so the tick loop pays one round-trip.
    """

    history: list[dict[str, Any]]
    corrupt: int
    blocking_comments: list[str]


def fetch_issue_attempts(
    issue: int,
    repo: str | None = None,
    *,
    blocking_limit: int = 3,
    blocking_max_chars: int = 4000,
) -> IssueAttempts:
    """Fetch an issue's comment payload ONCE and derive both views from it.

    Collapses the two identical ``gh issue view --comments`` round-trips that
    ``fetch_history_strict`` + ``fetch_blocking_comments`` made into a single
    fetch per issue per tick. The parse functions (``parse_history_strict`` /
    ``parse_blocking_comments``) are pure and reused unchanged, so the derived
    history / corrupt-count / blocking-comment results are byte-identical to
    calling the two fetchers separately.
    """
    if not repo:
        raise RuntimeError("fetch_issue_attempts requires repo='owner/name'")
    bodies = _gh.issue_comment_bodies(issue, repo=repo)
    history, corrupt = parse_history_strict(bodies)
    blocking = parse_blocking_comments(bodies, limit=blocking_limit, max_chars=blocking_max_chars)
    return IssueAttempts(history=history, corrupt=corrupt, blocking_comments=blocking)


# ---------------------------------------------------------------------------
# Skip classification
# ---------------------------------------------------------------------------


@dataclass
class SkipDecision:
    """Result of evaluating an issue's history against the current fingerprint.

    ``kind`` is one of:
        - ``""`` (empty)   — dispatch as normal
        - ``"in_flight"``  — latest matching attempt is still ``open``
        - ``"cooldown"``   — latest matching attempt ``failed`` within window
    """

    kind: str = ""
    pr_url: str | None = None
    cooldown_remaining_s: int = 0
    matched_ts: str | None = None


_FAIL_STATUSES = {"failed", "timeout", "no_pr"}


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def classify_skip(
    history: list[dict[str, Any]],
    fingerprint: str,
    *,
    cooldown_s: int = DEFAULT_RETRY_COOLDOWN_S,
    now: datetime | None = None,
) -> SkipDecision:
    """Decide whether to skip dispatch based on the latest matching attempt.

    Only the *latest* attempt with the same fingerprint matters: a stale
    failed attempt followed by a fresh open one means "in flight". The
    fingerprint must match exactly — a change to the issue body bumps it
    and clears the skip.
    """
    if not fingerprint or not history:
        return SkipDecision()
    now = now or datetime.now(UTC)
    latest: dict[str, Any] | None = None
    for rec in history:
        if rec.get("brief_fingerprint") == fingerprint:
            latest = rec
    if latest is None:
        return SkipDecision()

    status = latest.get("status", "")
    if status == "open":
        return SkipDecision(
            kind="in_flight",
            pr_url=latest.get("pr_url"),
            matched_ts=latest.get("ts"),
        )
    if status in _FAIL_STATUSES:
        when = _parse_iso(latest.get("ts"))
        if when is None:
            return SkipDecision()
        elapsed = (now - when).total_seconds()
        if elapsed < cooldown_s:
            return SkipDecision(
                kind="cooldown",
                cooldown_remaining_s=int(cooldown_s - elapsed),
                matched_ts=latest.get("ts"),
            )
    return SkipDecision()


# ---------------------------------------------------------------------------
# Repair backoff classification (issue #248)
#
# The new-dispatch path already backs an issue off after a failed attempt
# (``classify_skip`` above). The repair path had no equivalent, so a PR stuck
# on ``critic:blocking`` was re-selected every tick forever, pinning a worker
# slot and starving the ready backlog. This mirrors the ``classify_skip``
# cooldown semantics for *repair* selection — same shape, same vocabulary,
# keyed on consecutive re-blocks of a single PR rather than issue fingerprint.
# ---------------------------------------------------------------------------

# Default backoff window after a PR hits the consecutive-block cap before it
# is offered for repair again. Override via LOOP_REPAIR_COOLDOWN_S / the
# ``repair.cooldown_s`` setting.
DEFAULT_REPAIR_COOLDOWN_S = 3600


@dataclass
class RepairBackoffDecision:
    """Result of evaluating a PR's consecutive-block streak.

    ``skip`` is True iff the PR has re-blocked at least ``max_consecutive``
    ticks in a row AND its most recent block is still inside the cooldown
    window — i.e. it should be excluded from repair selection this tick to
    free its slot. Once the window elapses the PR becomes selectable again
    (one retry per window), mirroring ``classify_skip``'s cooldown arm.
    """

    skip: bool = False
    consecutive_blocks: int = 0
    cooldown_remaining_s: int = 0


def classify_repair_backoff(
    block_timestamps: list[str],
    *,
    max_consecutive: int,
    cooldown_s: int = DEFAULT_REPAIR_COOLDOWN_S,
    now: datetime | None = None,
) -> RepairBackoffDecision:
    """Decide whether a PR should back off from repair selection.

    ``block_timestamps`` is the run of CONSECUTIVE recent re-block timestamps
    for one PR (oldest→newest); the caller is responsible for resetting the
    run on a clearing event. ``max_consecutive <= 0`` disables the backoff
    (feature-off / legacy behaviour) — the PR is never skipped.
    """
    consecutive = len(block_timestamps)
    if max_consecutive <= 0 or consecutive < max_consecutive:
        return RepairBackoffDecision(skip=False, consecutive_blocks=consecutive)
    when = _parse_iso(block_timestamps[-1])
    if when is None:
        return RepairBackoffDecision(skip=False, consecutive_blocks=consecutive)
    now = now or datetime.now(UTC)
    elapsed = (now - when).total_seconds()
    if elapsed < cooldown_s:
        return RepairBackoffDecision(
            skip=True,
            consecutive_blocks=consecutive,
            cooldown_remaining_s=int(cooldown_s - elapsed),
        )
    return RepairBackoffDecision(skip=False, consecutive_blocks=consecutive)


def repair_cooldown_from_env(default_s: int = DEFAULT_REPAIR_COOLDOWN_S) -> int:
    """Resolve the repair backoff window via the unified Settings layer.

    Mirrors :func:`cooldown_from_env` but reads ``repair.cooldown_s``
    (env ``LOOP_REPAIR_COOLDOWN_S`` > yaml > default).
    """
    try:
        from forge_loop.settings import Settings

        return max(0, Settings.load().repair.cooldown_s)
    except Exception:  # noqa: BLE001
        return default_s


def cooldown_from_env(default_s: int = DEFAULT_RETRY_COOLDOWN_S) -> int:
    """Resolve the cooldown window via the unified Settings layer (issue #84).

    Was ``LOOP_RETRY_COOLDOWN_S`` env-only; now resolves through
    ``attempts.cooldown_s`` with the usual env > yaml > default precedence.
    """
    try:
        from forge_loop.settings import Settings

        return max(0, Settings.load().attempts.cooldown_s)
    except Exception:  # noqa: BLE001
        return default_s


__all__ = [
    "AttemptRecord",
    "MARKER",
    "DEFAULT_RETRY_COOLDOWN_S",
    "DEFAULT_REPAIR_COOLDOWN_S",
    "IssueAttempts",
    "RepairBackoffDecision",
    "SkipDecision",
    "classify_repair_backoff",
    "classify_skip",
    "compute_fingerprint",
    "cooldown_from_env",
    "repair_cooldown_from_env",
    "fetch_history",
    "fetch_history_strict",
    "fetch_blocking_comments",
    "fetch_issue_attempts",
    "parse_blocking_comments",
    "parse_history",
    "parse_history_strict",
    "record",
    "render_comment",
    # for relative-time tests
    "timedelta",
]
