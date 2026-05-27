"""Operator-checkpoint channels — let a worker ask the human a question mid-flight.

A worker calls the MCP tool ``ask_operator(question, options, context)`` when
it hits a high-risk decision (e.g. "delete this migration?", "is this rename
intentional?"). This module:

1. Posts the question via one or more channels (GitHub issue comment is the
   default; Slack webhook and a generic webhook are optional).
2. Polls the worker's GitHub issue for a reply matching
   ``/forge-answer <option>``.
3. Returns the operator's choice or raises :class:`OperatorTimeout` if no
   reply arrives within the timeout.

The polling lives in this module rather than ``worker.py`` because the
worker is itself an autonomous subprocess — the "polling loop" referenced
in the spec is the MCP tool's polling loop, which blocks the tool call
until the operator responds. The worker is effectively paused for the
duration of that tool call.

Side-effects (HTTP, ``gh``, sleep) go through injectable hooks so the
unit tests don't shell out.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Default timeout: 30 minutes (per spec).
DEFAULT_TIMEOUT_S = 30 * 60
# Polling cadence — small enough that a fast human reply lands in <30s,
# large enough that we don't hammer the GH API.
DEFAULT_POLL_INTERVAL_S = 20

# Marker the operator types in a GH comment to answer. Case-insensitive,
# anchored to start-of-line so it doesn't catch quoted prose.
ANSWER_MARKER_RE = re.compile(r"(?im)^\s*/forge-answer\s+(?P<option>\S.*?)\s*$")


class OperatorTimeout(Exception):
    """Raised when no operator reply arrives within the timeout."""


class OperatorError(Exception):
    """Raised on misuse — missing config, no channels, etc."""


@dataclass
class AskRequest:
    question: str
    options: list[str]
    context: str = ""
    issue: int | None = None  # GH issue # where reply will be posted
    repo: str | None = None
    timeout_s: int = DEFAULT_TIMEOUT_S
    poll_interval_s: int = DEFAULT_POLL_INTERVAL_S
    channels: list[str] = field(default_factory=list)


@dataclass
class AskResult:
    status: str  # "answered" | "timeout"
    answer: str | None
    raw_reply: str | None
    elapsed_s: float
    posted_channels: list[str]


# ── Channel adapters ────────────────────────────────────────────────────────


def _format_question_body(req: AskRequest) -> str:
    """Markdown body posted to the channel."""
    opts = "\n".join(f"- `{o}`" for o in req.options)
    ctx = f"\n\n**Context:**\n{req.context}" if req.context else ""
    return (
        f"**[forge-loop] Operator question**\n\n"
        f"{req.question}\n\n"
        f"**Options:**\n{opts}"
        f"{ctx}\n\n"
        f"Reply with a comment containing a line like "
        f"`/forge-answer <option>` (e.g. `/forge-answer {req.options[0]}`).\n"
        f"Timeout: {req.timeout_s}s."
    )


def post_github_issue(
    req: AskRequest,
    *,
    runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
) -> bool:
    """Post the question as a comment on ``req.issue``. Returns True on success."""
    if not req.issue or not req.repo:
        raise OperatorError("post_github_issue requires AskRequest.issue and .repo to be set")
    body = _format_question_body(req)
    cmd = [
        "gh",
        "issue",
        "comment",
        str(req.issue),
        "--repo",
        req.repo,
        "--body",
        body,
    ]
    runner = runner or _default_runner
    code, _out, _err = runner(cmd)
    return code == 0


def post_webhook(
    req: AskRequest,
    url: str,
    *,
    opener: Callable[[urllib.request.Request], Any] | None = None,
) -> bool:
    """POST a JSON payload describing the question to ``url``."""
    payload = {
        "kind": "operator_question",
        "question": req.question,
        "options": req.options,
        "context": req.context,
        "issue": req.issue,
        "repo": req.repo,
        "timeout_s": req.timeout_s,
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = opener or _default_opener
    try:
        resp = opener(request)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False
    try:
        code = getattr(resp, "status", None) or resp.getcode()
    except Exception:
        code = 0
    return 200 <= int(code) < 300


def post_slack(
    req: AskRequest,
    url: str,
    *,
    opener: Callable[[urllib.request.Request], Any] | None = None,
) -> bool:
    """POST a Slack-formatted message to a Slack incoming-webhook URL."""
    text = (
        f":warning: *forge-loop operator question* "
        f"(issue #{req.issue})\n\n"
        f">{req.question}\n\n"
        f"*Options:* {', '.join(f'`{o}`' for o in req.options)}\n"
        f"Reply on the GitHub issue with `/forge-answer <option>`."
    )
    body = json.dumps({"text": text}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = opener or _default_opener
    try:
        resp = opener(request)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False
    try:
        code = getattr(resp, "status", None) or resp.getcode()
    except Exception:
        code = 0
    return 200 <= int(code) < 300


def _default_runner(cmd: list[str]) -> tuple[int, str, str]:
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return r.returncode, r.stdout, r.stderr


def _default_opener(request: urllib.request.Request) -> Any:
    return urllib.request.urlopen(request, timeout=10)  # noqa: S310


# ── Reply polling ───────────────────────────────────────────────────────────


def fetch_issue_comments(
    issue: int,
    repo: str,
    *,
    runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Return the comments on ``issue`` (oldest first).

    Each entry has at least ``body`` and ``createdAt`` keys.
    """
    runner = runner or _default_runner
    cmd = [
        "gh",
        "issue",
        "view",
        str(issue),
        "--repo",
        repo,
        "--json",
        "comments",
    ]
    code, out, _err = runner(cmd)
    if code != 0:
        return []
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return []
    raw = payload.get("comments") or []
    return [c for c in raw if isinstance(c, dict)]


def match_answer(
    comments: list[dict[str, Any]],
    options: list[str],
    *,
    posted_after_iso: str | None = None,
) -> tuple[str | None, str | None]:
    """Find the first comment containing a valid ``/forge-answer <option>``.

    Returns ``(option, raw_marker_line)`` or ``(None, None)``.

    - ``posted_after_iso`` filters out comments at-or-before the timestamp
      (so we don't match a stale reply from a previous question on the
      same issue). Comparison is lexicographic on ISO 8601 strings, which
      is correct as long as both sides are UTC ISO-8601.
    - The matched option is case-insensitive but normalised back to the
      exact casing of the option in ``options`` (operators are forgiving).
    """
    option_index = {o.lower(): o for o in options}
    for c in comments:
        created = c.get("createdAt") or ""
        if posted_after_iso and created and created <= posted_after_iso:
            continue
        body = c.get("body") or ""
        for m in ANSWER_MARKER_RE.finditer(body):
            raw = m.group("option").strip()
            # Allow trailing punctuation/explanation: "/forge-answer yes — let's"
            head = raw.split(None, 1)[0].rstrip(".,;:!?")
            canonical = option_index.get(head.lower())
            if canonical is not None:
                return canonical, m.group(0).strip()
    return None, None


# ── Top-level orchestrator ──────────────────────────────────────────────────


def ask(
    req: AskRequest,
    *,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
    webhook_url: str | None = None,
    slack_url: str | None = None,
    runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
    opener: Callable[[urllib.request.Request], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    now_iso: Callable[[], str] | None = None,
) -> AskResult:
    """Post the question on all enabled channels, then poll for a reply.

    Channels:
    - ``github`` (default; requires ``req.issue`` + ``req.repo``)
    - ``webhook`` (when ``webhook_url`` is set)
    - ``slack`` (when ``slack_url`` is set)

    ``emit`` is the loop's event-bus emitter (used to emit
    ``operator_question`` / ``operator_answer`` / ``operator_timeout``).

    Raises :class:`OperatorError` if no channel is configured or
    ``options`` is empty.
    """
    if not req.options:
        raise OperatorError("ask_operator requires a non-empty options list")
    # Validate option strings — must be a single non-empty token so the
    # /forge-answer marker is unambiguous.
    for o in req.options:
        if not o or any(c.isspace() for c in o):
            raise OperatorError(f"option {o!r} must be a non-empty whitespace-free token")

    channels = (
        list(req.channels)
        if req.channels
        else _default_channels(
            req,
            webhook_url=webhook_url,
            slack_url=slack_url,
        )
    )
    if not channels:
        raise OperatorError(
            "ask_operator has no channels configured — set LOOP_OPERATOR_ISSUE+repo "
            "for GitHub, or LOOP_OPERATOR_WEBHOOK / LOOP_OPERATOR_SLACK_WEBHOOK"
        )

    started_iso = (now_iso or _utc_iso)()
    posted_channels: list[str] = []
    for ch in channels:
        ok = False
        if ch == "github":
            ok = post_github_issue(req, runner=runner)
        elif ch == "webhook" and webhook_url:
            ok = post_webhook(req, webhook_url, opener=opener)
        elif ch == "slack" and slack_url:
            ok = post_slack(req, slack_url, opener=opener)
        if ok:
            posted_channels.append(ch)

    if emit is not None:
        emit(
            "operator_question",
            {
                "issue": req.issue,
                "question": req.question,
                "options": req.options,
                "context": req.context,
                "timeout_s": req.timeout_s,
                "channels": posted_channels,
            },
        )

    # Only the GH channel supports a reply, by design. If we couldn't post
    # there but the operator is still expected to reply on the issue,
    # we still poll — but if there's no issue at all, we can only
    # time out.
    deadline = monotonic() + req.timeout_s
    start_mono = monotonic()
    answer: str | None = None
    raw_reply: str | None = None
    while monotonic() < deadline:
        if req.issue and req.repo:
            comments = fetch_issue_comments(req.issue, req.repo, runner=runner)
            answer, raw_reply = match_answer(
                comments,
                req.options,
                posted_after_iso=started_iso,
            )
            if answer is not None:
                break
        # Cap the sleep so the last iteration doesn't overshoot the deadline.
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(req.poll_interval_s, max(remaining, 0.0)))

    elapsed = monotonic() - start_mono

    if answer is None:
        if emit is not None:
            emit(
                "operator_timeout",
                {
                    "issue": req.issue,
                    "question": req.question,
                    "elapsed_s": round(elapsed, 1),
                },
            )
        raise OperatorTimeout(f"no operator reply within {req.timeout_s}s for issue #{req.issue}")

    if emit is not None:
        emit(
            "operator_answer",
            {
                "issue": req.issue,
                "answer": answer,
                "elapsed_s": round(elapsed, 1),
            },
        )
    return AskResult(
        status="answered",
        answer=answer,
        raw_reply=raw_reply,
        elapsed_s=elapsed,
        posted_channels=posted_channels,
    )


def _default_channels(
    req: AskRequest,
    *,
    webhook_url: str | None,
    slack_url: str | None,
) -> list[str]:
    out: list[str] = []
    if req.issue and req.repo:
        out.append("github")
    if webhook_url:
        out.append("webhook")
    if slack_url:
        out.append("slack")
    return out


def _utc_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


# ── Env helpers — used by the MCP tool ──────────────────────────────────────


def env_timeout_s(default: int = DEFAULT_TIMEOUT_S) -> int:
    raw = os.environ.get("LOOP_OPERATOR_TIMEOUT_S")
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def env_webhook() -> str | None:
    return os.environ.get("LOOP_OPERATOR_WEBHOOK") or None


def env_slack() -> str | None:
    return os.environ.get("LOOP_OPERATOR_SLACK_WEBHOOK") or None


def env_issue() -> int | None:
    raw = os.environ.get("LOOP_OPERATOR_ISSUE")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
