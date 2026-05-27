"""Per-issue attempt history — persisted as GH issue comments.

Workers append a 1-line summary of their attempt as a comment to the issue,
prefixed with a magic marker so future ticks can read them and learn from
past failures.

Why GH comments (vs sidecar JSONL): visible in the UI, persists across the
runner's worktree lifetime, gives humans context too.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from forge_loop import gh as _gh

MARKER = "<!-- forge-loop-attempt -->"
COMMENT_RE = re.compile(re.escape(MARKER) + r".*?```json\s*\n(.*?)\n```", re.DOTALL)


@dataclass
class AttemptRecord:
    ts: str
    status: str  # merged | open | failed | timeout | no_pr
    pr_url: str | None
    duration_s: float
    note: str
    event_count: int


def render_comment(record: AttemptRecord) -> str:
    """Render an attempt record as a GH-flavored markdown comment."""
    payload = {
        "ts": record.ts,
        "status": record.status,
        "pr_url": record.pr_url,
        "duration_s": round(record.duration_s, 1),
        "note": record.note,
        "event_count": record.event_count,
    }
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
    parts.append("\n```json\n" + json.dumps(payload, indent=2) + "\n```")
    return "\n\n".join(parts)


def record(issue: int, *, status: str, pr_url: str | None, duration_s: float,
           note: str = "", event_count: int = 0, repo: str | None = None) -> None:
    """Append an attempt record as a GH comment on the issue."""
    rec = AttemptRecord(
        ts=datetime.now(UTC).isoformat(timespec="seconds"),
        status=status, pr_url=pr_url, duration_s=duration_s,
        note=note, event_count=event_count,
    )
    _gh.comment(issue, render_comment(rec), repo=repo)


def parse_history(comments_body: list[str]) -> list[dict[str, Any]]:
    """Parse a list of comment bodies; return attempt records (oldest first)."""
    out: list[dict[str, Any]] = []
    for body in comments_body:
        if MARKER not in body:
            continue
        m = COMMENT_RE.search(body)
        if not m:
            continue
        try:
            out.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            continue
    return out


def fetch_history(issue: int, repo: str | None = None) -> list[dict[str, Any]]:
    """Fetch all forge-loop attempt records for an issue (oldest first)."""
    import subprocess

    if not repo:
        raise RuntimeError("fetch_history requires repo='owner/name'")
    r = subprocess.run(
        ["gh", "issue", "view", str(issue), "--repo", repo, "--comments",
         "--json", "comments"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        return []
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    bodies = [c.get("body", "") for c in payload.get("comments", [])]
    return parse_history(bodies)
