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

Model knob
----------
``review_pr`` accepts an optional ``model`` arg (issue #34) which is
threaded through to the underlying ``claude -p`` subprocess as
``--model <name>``. Thinking-budget configurability is deferred until
the critic migrates from `claude -p` to the Claude Agent SDK (separate
follow-up issue) — the CLI has no thinking-budget flag today.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forge_loop.worker import ensure_subagent_trusted

VALID_OVERALL = {"approve", "request_changes", "block"}
VALID_SEVERITY = {"sev1", "sev2", "sev3"}
VALID_CATEGORY = {"correctness", "security", "style", "tests", "docs", "product"}
PRECOMMIT_BYPASS_TAG = "precommit_bypass"
_NO_VERIFY_RE = re.compile(r"\bgit(?:\s+-[cC]\s+\S+)*\s+commit\b[^\n]*\s(?:--no-verify|-n)\b")
_BODY_NO_VERIFY_ACTION_RE = re.compile(
    r"\b(?:i\s+)?(?:ran|run|used|use|called|call|executed|execute)\s+"
    r"git(?:\s+-[cC]\s+\S+)*\s+commit\b[^\n]*\s(?:--no-verify|-n)\b",
    re.IGNORECASE,
)
_NO_VERIFY_STATIC_CONTEXT_RE = re.compile(
    r"\b(?:flag|detect|test|tests|rule|brief|manifesto|requires|mention|mentions|"
    r"statement|statements)\b",
    re.IGNORECASE,
)
_BYPASS_HEADING_RE = re.compile(
    r"^##\s+Pre-commit bypass justification\s*$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class ManifestoViolation:
    """A specific rule in a project manifesto the PR diff violates.

    Emitted by the critic LLM and parsed by ``_coerce_report``. Any
    violation with ``severity == "sev1"`` blocks auto-merge (see
    ``_coerce_report`` and ``critic_actions.plan_actions``).
    """

    rule_id: str
    manifesto: str
    quote: str
    suggested_fix: str
    severity: str  # sev1 | sev2 | sev3

    def is_valid(self) -> bool:
        return (
            isinstance(self.rule_id, str)
            and bool(self.rule_id.strip())
            and isinstance(self.manifesto, str)
            and bool(self.manifesto.strip())
            and isinstance(self.quote, str)
            and isinstance(self.suggested_fix, str)
            and self.severity in VALID_SEVERITY
        )


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
    manifesto_violations: list[ManifestoViolation] = field(default_factory=list)
    raw: str = ""

    def severities(self) -> set[str]:
        return {f.severity for f in self.findings}

    def has_sev1(self) -> bool:
        return any(f.severity == "sev1" for f in self.findings) or any(
            v.severity == "sev1" for v in self.manifesto_violations
        )

    def has_sev2(self) -> bool:
        return any(f.severity == "sev2" for f in self.findings) or any(
            v.severity == "sev2" for v in self.manifesto_violations
        )

    def has_sev1_manifesto_violation(self) -> bool:
        return any(v.severity == "sev1" for v in self.manifesto_violations)


@dataclass
class CriticOutcome:
    verdict: str  # approved | changes_requested | blocked | error
    reasons: list[str]
    duration_s: float
    stdout_tail: str
    report: CriticReport | None = None
    error: str | None = None
    parse_retries: int = 0


def detect_precommit_bypass(commit_text: str, *, pr_body: str) -> CriticReport:
    """Flag `git commit --no-verify` unless the PR body justifies it."""

    if not _has_no_verify_command(commit_text) and not _has_no_verify_body_action(pr_body):
        return CriticReport(overall="approve", findings=[])
    if _has_precommit_bypass_justification(pr_body):
        return CriticReport(overall="approve", findings=[])
    return CriticReport(
        overall="request_changes",
        findings=[
            Finding(
                severity="sev1",
                category="correctness",
                file=None,
                line=None,
                message=(
                    f"{PRECOMMIT_BYPASS_TAG}: `git commit --no-verify` requires a "
                    "`## Pre-commit bypass justification` section in the PR body."
                ),
            )
        ],
    )


def _has_no_verify_command(text: str) -> bool:
    for line in _command_segments(text):
        match = _NO_VERIFY_RE.search(line)
        if match is not None and not _is_static_no_verify_context(line, match):
            return True
    return False


def _worker_command_context(issue_number: int, logs_dir: Path) -> str:
    chunks: list[str] = []
    for pattern in (f"worker-{issue_number}-*.log", f"repair-{issue_number}-*.log"):
        for path in sorted(logs_dir.glob(pattern)):
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                item = event.get("item")
                if not isinstance(item, dict):
                    continue
                if item.get("type") != "command_execution":
                    continue
                command = item.get("command")
                if isinstance(command, str):
                    chunks.append(command)
    return "\n".join(chunks)


def _command_segments(text: str) -> list[str]:
    lines = text.splitlines()
    segments: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        segment = line.rstrip()
        while segment.endswith("\\") and i + 1 < len(lines):
            segment = f"{segment[:-1]} {lines[i + 1].strip()}"
            i += 1
        segments.append(segment)
        if i + 1 < len(lines) and "git" in line and "commit" in line:
            segments.append(f"{line.rstrip()} {lines[i + 1].strip()}")
        i += 1
    return segments


def _has_no_verify_body_action(text: str) -> bool:
    for line in text.splitlines():
        match = _BODY_NO_VERIFY_ACTION_RE.search(line)
        if match is not None and not _is_static_no_verify_context(line, match):
            return True
    return False


def _is_static_no_verify_context(line: str, match: re.Match[str]) -> bool:
    return _NO_VERIFY_STATIC_CONTEXT_RE.search(line[: match.start()]) is not None


def _has_precommit_bypass_justification(pr_body: str) -> bool:
    match = _BYPASS_HEADING_RE.search(pr_body)
    if match is None:
        return False
    tail = pr_body[match.end() :]
    body = tail.split("\n## ", 1)[0].strip()
    return bool(body)


def _fetch_pr_precommit_context(pr_url: str, repo: Path) -> tuple[str, str]:
    """Return PR body plus commit metadata for deterministic local checks."""

    from forge_loop import gh

    return gh.pr_precommit_context(pr_url, repo)


def _with_deterministic_precommit_findings(
    report: CriticReport,
    *,
    pr_url: str,
    repo: Path,
    issue_number: int,
    logs_dir: Path,
) -> CriticReport:
    pr_body, commit_text = _fetch_pr_precommit_context(pr_url, repo)
    worker_text = _worker_command_context(issue_number, logs_dir)
    if worker_text:
        commit_text = f"{commit_text}\n{worker_text}"
    deterministic = detect_precommit_bypass(commit_text, pr_body=pr_body)
    if not deterministic.findings:
        return report
    overall = report.overall
    if overall == "approve":
        overall = deterministic.overall
    return CriticReport(
        overall=overall,
        findings=[*report.findings, *deterministic.findings],
        manifesto_violations=report.manifesto_violations,
        raw=report.raw,
    )


def review_pr(
    pr_url: str,
    issue_number: int,
    repo: Path,
    logs_dir: Path,
    timeout_s: int = 600,
    brief_template: str | None = None,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
    model: str | None = None,
    provider: str = "claude",
) -> CriticOutcome:
    """Spawn the critic subagent against an open PR. Synchronous.

    If the subagent's final JSON fails to parse, retry ONCE. If the retry
    also fails, emit ``critic_parse_failed`` via ``emit`` and surface an
    ``error`` verdict (the runner then leaves the PR alone — no auto-block,
    no auto-approve — so a human can intervene).
    """
    template = brief_template or _default_brief()
    from forge_loop._critic_sdk import load_manifestos_text

    manifestos = load_manifestos_text(repo)
    brief = template.format(
        pr_url=pr_url,
        issue_number=issue_number,
        manifestos=manifestos,
    )

    logs_dir.mkdir(parents=True, exist_ok=True)
    ensure_subagent_trusted(repo)

    started = time.time()
    report: CriticReport | None = None
    last_log_path: Path | None = None
    parse_error: str | None = None
    retries = 0

    if provider == "codex":
        from forge_loop.agent_backend import run_codex_exec

        log_path = logs_dir / f"critic-{issue_number}-{int(time.time())}-codex.log"
        result = run_codex_exec(
            prompt=brief,
            cwd=repo,
            log_path=log_path,
            timeout_s=timeout_s,
            model=model,
            add_dirs=[repo],
        )
        if result.timed_out:
            return CriticOutcome(
                verdict="error",
                reasons=[],
                duration_s=result.duration_s,
                stdout_tail="(timeout)",
                error=result.error,
            )
        report, parse_error = parse_report_from_text(result.last_message)
        tail = _tail(log_path, 500)
        if report is None:
            if emit is not None:
                emit(
                    "critic_parse_failed",
                    {
                        "issue": issue_number,
                        "pr": pr_url,
                        "err": (parse_error or result.error or "no_json_found")[:200],
                        "retries": 0,
                    },
                )
            return CriticOutcome(
                verdict="error",
                reasons=[],
                duration_s=result.duration_s,
                stdout_tail=tail,
                error=parse_error or result.error or "critic_parse_failed",
            )
        report = _with_deterministic_precommit_findings(
            report,
            pr_url=pr_url,
            repo=repo,
            issue_number=issue_number,
            logs_dir=logs_dir,
        )
        verdict = _verdict_from_overall(report.overall)
        reasons = [f"[{f.severity}/{f.category}] {f.message}" for f in report.findings]
        return CriticOutcome(
            verdict=verdict,
            reasons=reasons,
            duration_s=result.duration_s,
            stdout_tail=tail,
            report=report,
        )

    # SDK path (issue #85): replaces the legacy ``claude -p`` subprocess
    # call. We still write the assistant's final text to a per-attempt
    # log file for operator postmortems, but the parse path now uses
    # ``parse_report_from_text`` directly (no stream-json detour). The
    # ``_subagent_env`` env-wiring is no longer needed — the SDK handles
    # auth itself.
    from forge_loop._critic_sdk import run_critic_sdk

    for attempt in range(2):  # initial + 1 retry
        log_path = logs_dir / f"critic-{issue_number}-{int(time.time())}-{attempt}.log"
        last_log_path = log_path
        sdk_result = run_critic_sdk(
            prompt=brief,
            cwd=repo,
            timeout_s=timeout_s,
            model=model,
            add_dirs=(repo,),
        )
        # Mirror the final assistant text to disk so the existing
        # _tail(log_path, 500) read for stdout_tail keeps working and
        # operators can grep critic-*.log as before.
        with suppress(OSError):
            log_path.write_text(sdk_result.last_message or "")
        if sdk_result.timed_out:
            return CriticOutcome(
                verdict="error",
                reasons=[],
                duration_s=time.time() - started,
                stdout_tail="(timeout)",
                error=f"critic exceeded {timeout_s}s",
                parse_retries=retries,
            )
        if sdk_result.error:
            # An SDK-side failure (auth, transport) — surface as error
            # verdict so the runner doesn't auto-approve.
            return CriticOutcome(
                verdict="error",
                reasons=[],
                duration_s=time.time() - started,
                stdout_tail=sdk_result.error[:500],
                error=sdk_result.error,
                parse_retries=retries,
            )

        report, parse_error = parse_report_from_text(sdk_result.last_message)
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

    report = _with_deterministic_precommit_findings(
        report,
        pr_url=pr_url,
        repo=repo,
        issue_number=issue_number,
        logs_dir=logs_dir,
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

    # Back-compat: missing field is fine, defaults to empty list.
    raw_violations = obj.get("manifesto_violations") or []
    if not isinstance(raw_violations, list):
        raw_violations = []
    violations: list[ManifestoViolation] = []
    for item in raw_violations:
        if not isinstance(item, dict):
            continue
        rule_id = item.get("rule_id")
        manifesto = item.get("manifesto")
        # Drop entries missing the identifying fields entirely; we won't
        # fabricate a rule_id for the model.
        if not isinstance(rule_id, str) or not rule_id.strip():
            continue
        if not isinstance(manifesto, str) or not manifesto.strip():
            continue
        sev_raw = item.get("severity")
        # Defensive default: unknown/missing severity → sev3 (non-blocking).
        severity = sev_raw if sev_raw in VALID_SEVERITY else "sev3"
        v = ManifestoViolation(
            rule_id=rule_id,
            manifesto=manifesto,
            quote=str(item.get("quote", "")),
            suggested_fix=str(item.get("suggested_fix", "")),
            severity=severity,
        )
        if v.is_valid():
            violations.append(v)

    # sev1 manifesto violation forces request_changes even if the model
    # said "approve" — manifesto compliance is a hard gate.
    if overall == "approve" and any(v.severity == "sev1" for v in violations):
        overall = "request_changes"

    return CriticReport(
        overall=overall,
        findings=findings,
        manifesto_violations=violations,
        raw=raw,
    )


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
