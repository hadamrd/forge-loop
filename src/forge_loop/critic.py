"""Critic agent — reviews a PR before auto-merge.

After a worker opens a PR, we (optionally) dispatch a critic subagent that:
- reads the diff
- reads the linked issue's acceptance criteria
- checks tests + pre-commit gates ran
- returns a typed CriticReport (overall + per-finding severity)

The runner consumes the report to:
- block auto-merge on any sev1 finding (and label PR ``critic:blocking``)
- post sev2/sev3 findings as inline PR review comments (or a summary)
- gate "approve with zero findings" against ``LOOP_CRITIC_MIN_FINDINGS``
  so an empty rubber-stamp on a large diff is surfaced as suspicious.

Trade-off: a critic pass adds ~30-90s per PR but catches the class of
regressions auto-merge alone misses.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge_loop.worker import _subagent_env, ensure_subagent_trusted

VALID_OVERALL = {"approve", "request_changes", "block"}
VALID_SEVERITY = {"sev1", "sev2", "sev3"}
VALID_CATEGORY = {"correctness", "security", "style", "tests", "docs"}


def _default_brief() -> str:
    """Load the critic brief template — bundled or operator-overridden."""
    from forge_loop.briefs import load_template

    return load_template("critic")


@dataclass
class Finding:
    severity: str  # sev1 | sev2 | sev3
    category: str  # correctness | security | style | tests | docs
    file: str | None
    line: int | None
    message: str

    def is_valid(self) -> bool:
        return (
            self.severity in VALID_SEVERITY
            and self.category in VALID_CATEGORY
            and isinstance(self.message, str)
            and bool(self.message.strip())
            and (self.file is None or isinstance(self.file, str))
            and (self.line is None or isinstance(self.line, int))
        )


@dataclass
class CriticReport:
    overall: str  # approve | request_changes | block
    findings: list[Finding] = field(default_factory=list)
    raw: str = ""

    def severities(self) -> set[str]:
        return {f.severity for f in self.findings}

    def has_sev1(self) -> bool:
        return any(f.severity == "sev1" for f in self.findings)

    def has_sev2(self) -> bool:
        return any(f.severity == "sev2" for f in self.findings)


@dataclass
class CriticOutcome:
    verdict: str  # approved | changes_requested | blocked | error
    reasons: list[str]
    duration_s: float
    stdout_tail: str
    report: CriticReport | None = None
    error: str | None = None
    parse_retries: int = 0


def review_pr(
    pr_url: str,
    issue_number: int,
    repo: Path,
    logs_dir: Path,
    timeout_s: int = 600,
    brief_template: str | None = None,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
) -> CriticOutcome:
    """Spawn the critic subagent against an open PR. Synchronous.

    If the subagent's final JSON fails to parse, retry ONCE. If the retry
    also fails, emit ``critic_parse_failed`` via ``emit`` and surface an
    ``error`` verdict (the runner then leaves the PR alone — no auto-block,
    no auto-approve — so a human can intervene).
    """
    template = brief_template or _default_brief()
    brief = template.format(pr_url=pr_url, issue_number=issue_number)

    logs_dir.mkdir(parents=True, exist_ok=True)
    ensure_subagent_trusted(repo)

    started = time.time()
    report: CriticReport | None = None
    last_log_path: Path | None = None
    parse_error: str | None = None
    retries = 0

    for attempt in range(2):  # initial + 1 retry
        log_path = logs_dir / f"critic-{issue_number}-{int(time.time())}-{attempt}.log"
        last_log_path = log_path
        try:
            with open(log_path, "wb") as logf:
                subprocess.run(
                    [
                        "claude",
                        "-p",
                        brief,
                        "--max-turns",
                        "20",
                        "--allow-dangerously-skip-permissions",
                        "--add-dir",
                        str(repo),
                        "--output-format",
                        "stream-json",
                        "--verbose",
                    ],
                    cwd=repo,
                    stdout=logf,
                    stderr=subprocess.STDOUT,
                    timeout=timeout_s,
                    env=_subagent_env(),
                )
        except subprocess.TimeoutExpired:
            return CriticOutcome(
                verdict="error",
                reasons=[],
                duration_s=time.time() - started,
                stdout_tail="(timeout)",
                error=f"critic exceeded {timeout_s}s",
                parse_retries=retries,
            )

        report, parse_error = parse_report_from_log(log_path)
        if report is not None:
            break
        retries = attempt + 1  # we just consumed one parse attempt

    duration = time.time() - started
    tail = _tail(last_log_path, 500) if last_log_path else ""

    if report is None:
        if emit is not None:
            emit(
                "critic_parse_failed",
                {
                    "issue": issue_number,
                    "pr": pr_url,
                    "err": (parse_error or "no_json_found")[:200],
                    "retries": retries,
                },
            )
        return CriticOutcome(
            verdict="error",
            reasons=[],
            duration_s=duration,
            stdout_tail=tail,
            error=parse_error or "critic_parse_failed",
            parse_retries=retries,
        )

    verdict = _verdict_from_overall(report.overall)
    reasons = [f"[{f.severity}/{f.category}] {f.message}" for f in report.findings]
    return CriticOutcome(
        verdict=verdict,
        reasons=reasons,
        duration_s=duration,
        stdout_tail=tail,
        report=report,
        parse_retries=retries,
    )


def _verdict_from_overall(overall: str) -> str:
    if overall == "approve":
        return "approved"
    if overall == "block":
        return "blocked"
    if overall == "request_changes":
        return "changes_requested"
    return "error"


def parse_report_from_log(log_path: Path) -> tuple[CriticReport | None, str | None]:
    """Extract the final JSON CriticReport from a claude stream-json log.

    Returns (report, None) on success or (None, error_message) on failure.
    """
    last_result = ""
    try:
        with open(log_path, "rb") as f:
            for raw in f:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("type") == "result":
                    last_result = e.get("result", "") or ""
    except OSError as ex:
        return None, f"log_read_failed: {ex}"

    if not last_result.strip():
        return None, "empty_result"

    return parse_report_from_text(last_result)


_JSON_OBJ_RE = re.compile(r"\{(?:[^{}]|(?:\{[^{}]*\}))*\}", re.DOTALL)


def parse_report_from_text(text: str) -> tuple[CriticReport | None, str | None]:
    """Find the LAST valid JSON object with an ``overall`` field in ``text``.

    Tolerates surrounding prose / markdown fences. Returns (None, msg) on
    failure so the caller can decide whether to retry.
    """
    # First try: the last non-empty line as a clean JSON object.
    for chunk in reversed(text.strip().splitlines()):
        chunk = chunk.strip()
        if chunk.startswith("```"):
            continue
        if chunk.startswith("{") and chunk.endswith("}"):
            try:
                obj = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            rep = _coerce_report(obj, raw=chunk)
            if rep is not None:
                return rep, None

    # Fallback: scan for any JSON object that has an "overall" key.
    matches = list(_JSON_OBJ_RE.finditer(text))
    for m in reversed(matches):
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or "overall" not in obj:
            continue
        rep = _coerce_report(obj, raw=m.group(0))
        if rep is not None:
            return rep, None

    return None, "no_valid_report"


def _coerce_report(obj: dict[str, Any], raw: str) -> CriticReport | None:
    overall = obj.get("overall")
    if overall not in VALID_OVERALL:
        return None
    raw_findings = obj.get("findings") or []
    if not isinstance(raw_findings, list):
        return None
    findings: list[Finding] = []
    for item in raw_findings:
        if not isinstance(item, dict):
            continue
        line = item.get("line")
        if isinstance(line, str) and line.isdigit():
            line = int(line)
        elif not isinstance(line, int):
            line = None
        f = Finding(
            severity=str(item.get("severity", "")),
            category=str(item.get("category", "")),
            file=item.get("file") if isinstance(item.get("file"), str) else None,
            line=line,
            message=str(item.get("message", "")),
        )
        if f.is_valid():
            findings.append(f)
    return CriticReport(overall=overall, findings=findings, raw=raw)


def _tail(path: Path | None, n: int) -> str:
    if path is None:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Legacy verdict extractor — kept for backwards compatibility with
# tests/callers that pre-date the typed report. Prefer ``parse_report_from_log``.
# ---------------------------------------------------------------------------
def _extract_verdict(log_path: Path) -> tuple[str, list[str]]:
    report, _ = parse_report_from_log(log_path)
    if report is not None:
        verdict = _verdict_from_overall(report.overall)
        reasons = [f.message for f in report.findings]
        return verdict, reasons

    last = ""
    try:
        with open(log_path, "rb") as f:
            for raw in f:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("type") == "result":
                    last = e.get("result", "") or ""
    except OSError:
        return "error", []

    for chunk in reversed(last.strip().splitlines()):
        chunk = chunk.strip()
        if chunk.startswith("{") and chunk.endswith("}"):
            try:
                obj = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            v = obj.get("verdict")
            if v:
                return str(v), list(obj.get("reasons", []) or [])

    if re.search(r"\bchanges[_ ]requested\b", last, re.IGNORECASE):
        return "changes_requested", []
    if re.search(r"\bapproved\b", last, re.IGNORECASE):
        return "approved", []
    return "error", []
