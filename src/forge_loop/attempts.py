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

from forge_loop import gh as _gh

MARKER = "<!-- forge-loop-attempt -->"
COMMENT_RE = re.compile(re.escape(MARKER) + r".*?```json\s*\n(.*?)\n```", re.DOTALL)

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
    h = hashlib.sha256()
    h.update(str(issue_id).encode("utf-8"))
    h.update(b"\x00")
    h.update((issue_body or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((brief_template_hash or "").encode("utf-8"))
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
        status=status, pr_url=pr_url, duration_s=duration_s,
        note=note, event_count=event_count,
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


def fetch_history(issue: int, repo: str | None = None) -> list[dict[str, Any]]:
    """Fetch all forge-loop attempt records for an issue (oldest first)."""
    records, _ = fetch_history_strict(issue, repo=repo)
    return records


def fetch_history_strict(
    issue: int,
    repo: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Like ``fetch_history`` but also returns the count of corrupt rows."""
    import subprocess

    if not repo:
        raise RuntimeError("fetch_history requires repo='owner/name'")
    r = subprocess.run(
        ["gh", "issue", "view", str(issue), "--repo", repo, "--comments",
         "--json", "comments"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        return [], 0
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError:
        return [], 0
    bodies = [c.get("body", "") for c in payload.get("comments", [])]
    return parse_history_strict(bodies)


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
    "SkipDecision",
    "classify_skip",
    "compute_fingerprint",
    "cooldown_from_env",
    "fetch_history",
    "fetch_history_strict",
    "parse_history",
    "parse_history_strict",
    "record",
    "render_comment",
    # for relative-time tests
    "timedelta",
]
