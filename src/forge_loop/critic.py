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

from forge_loop import critic_format
from forge_loop.events import read_events
from forge_loop.worker import ensure_subagent_trusted

VALID_OVERALL = {"approve", "request_changes", "block"}
# The severity / category vocabulary lives in ``critic_format`` (the single
# source of truth shared with the gh_issues thread classifier — #230). We alias
# it here so existing call sites keep using ``VALID_SEVERITY`` / ``VALID_CATEGORY``
# unchanged. The "anti-slop" lenses (architecture / performance) and ``product``
# are part of that one vocabulary.
VALID_SEVERITY = set(critic_format.SEVERITIES)
VALID_CATEGORY = set(critic_format.CATEGORIES)
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
    # Teaching-critic (Ch9 convergence). ``minimal_path_to_green`` is the
    # explicit, ordered, MINIMAL must-fix set the worker has to clear to merge —
    # the acceptance predicate, stated rather than discovered by violation.
    # ``follow_ups`` are advisory items that do NOT block (optional polish, plus
    # sev3 nits demoted out of the blocking set once a PR has stalled for
    # ``sev3_demotion_round_threshold`` rounds). ``round_number`` is how many
    # critic reviews this PR has had BEFORE this one (0 == first review).
    minimal_path_to_green: list[str] = field(default_factory=list)
    follow_ups: list[Finding] = field(default_factory=list)
    round_number: int = 0

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


PIP_EDITABLE_POISON_TAG = "pip-editable-poison"
_PACKAGING_FILENAMES = {"pyproject.toml", "setup.py", "setup.cfg"}
# Matches `pip install -e ...`, `pip install .`/`./`, and the `python -m pip`
# form (the `pip install` substring is present in all of them). Deliberately
# narrow: a plain `pip install requests` must NOT match (that is the negative
# AC case). We only flag an editable (`-e`) install or a bare root install
# whose target is the current directory.
_PIP_EDITABLE_RE = re.compile(
    r"\bpip\s+install\b[^\n]*?(?:\s-e\b|\s\.(?=\s|/|$))",
)


def detect_pip_editable_poison(worker_text: str, *, changed_files: list[str]) -> CriticReport:
    """Flag a PR that touches packaging files AND ran `pip install -e` (#144).

    Both conditions are required (logical AND): the worker session log must
    contain an editable/root pip install AND the PR diff must touch
    ``pyproject.toml`` / ``setup.py`` / ``setup.cfg``. A `pip install
    requests` on its own — or a packaging change with no editable install —
    does not trip the rule.
    """
    touches_packaging = any(
        Path(p).name in _PACKAGING_FILENAMES for p in changed_files if isinstance(p, str)
    )
    if not touches_packaging:
        return CriticReport(overall="approve", findings=[])
    if not _has_pip_editable_install(worker_text):
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
                    f"{PIP_EDITABLE_POISON_TAG}: worker ran `pip install -e` (or a root "
                    "`pip install .`) while the PR touches packaging files "
                    "(pyproject.toml/setup.py/setup.cfg). An editable install leaks the "
                    "worktree into the operator's system Python (see #144). Use a "
                    "worktree-local `uv venv` + `uv pip install -e .` instead."
                ),
            )
        ],
    )


def _has_pip_editable_install(text: str) -> bool:
    return any(_PIP_EDITABLE_RE.search(line) is not None for line in _command_segments(text))


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
                events = list(read_events(path))
            except OSError:
                continue
            for event in events:
                item = event.get("item")
                if not isinstance(item, dict):
                    continue
                if item.get("type") != "command_execution":
                    continue
                command = item.get("command")
                if isinstance(command, str):
                    chunks.append(command)
    return "\n".join(chunks)


def count_prior_critic_rounds(issue_number: int, logs_dir: Path) -> int:
    """How many critic reviews this PR/issue has already had.

    The round source for the teaching critic (Ch9). Each ``review_pr`` call
    writes one or more ``critic-{issue}-{ts}-{attempt}.log`` files (SDK path)
    or a single ``critic-{issue}-{ts}-codex.log`` (codex path). Counting the
    DISTINCT ``{ts}`` stamps already on disk yields the number of completed
    prior reviews — the retry-attempt suffix (``-0``/``-1``) of a single review
    must NOT inflate the count, so we key on the timestamp, not the file.

    Returns 0 when the logs dir is missing or holds no prior critic logs (the
    first review of this PR is round 0).
    """
    if not logs_dir.is_dir():
        return 0
    stamps: set[str] = set()
    for path in logs_dir.glob(f"critic-{issue_number}-*.log"):
        m = re.match(rf"^critic-{issue_number}-(\d+)(?:-\w+)?$", path.stem)
        if m:
            stamps.add(m.group(1))
    return len(stamps)


def demote_sev3_if_stalled(
    report: CriticReport,
    *,
    round_number: int,
    threshold: int,
) -> CriticReport:
    """Triage, NOT standard erosion (Ch9 §9.5.2).

    Once a PR has stalled for ``threshold`` rounds, cosmetic (sev3) findings are
    moved out of the blocking ``findings`` set into ``follow_ups`` so rounds are
    not burned on nits. sev1/sev2 are NEVER touched — real defects always block,
    no matter the round count. A ``threshold`` of 0 disables demotion. If the
    demotion removes every blocking finding and the manifesto set is clean, an
    ``request_changes`` overall is relaxed to ``approve`` (nothing left blocks);
    a critic-emitted ``block`` is left intact (an explicit hard stop is not a nit).
    """
    if threshold <= 0 or round_number < threshold:
        return report
    blocking = [f for f in report.findings if f.severity != "sev3"]
    demoted = [f for f in report.findings if f.severity == "sev3"]
    if not demoted:
        return report
    overall = report.overall
    still_blocks = (
        bool(blocking)
        or any(v.severity in {"sev1", "sev2"} for v in report.manifesto_violations)
        or overall == "block"
    )
    if overall == "request_changes" and not still_blocks:
        overall = "approve"
    return CriticReport(
        overall=overall,
        findings=blocking,
        manifesto_violations=report.manifesto_violations,
        raw=report.raw,
        minimal_path_to_green=report.minimal_path_to_green,
        follow_ups=[*report.follow_ups, *demoted],
        round_number=report.round_number,
    )


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


def _repo_slug_from_pr_url(pr_url: str) -> str:
    """Extract ``owner/name`` from a GitHub PR URL.

    The critic's ``repo`` arg is the LOCAL checkout path, but the GitHub
    client addresses repos by ``owner/name``. The PR URL carries it:
    ``https://github.com/<owner>/<name>/pull/<n>``.
    """
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/(?:pull|issues)/\d+", pr_url)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return ""


def _fetch_pr_precommit_context(pr_url: str, _repo: Path) -> tuple[str, str]:
    """Return PR body plus commit metadata for deterministic checks.

    ``_repo`` (the local checkout path) is retained for call-site symmetry but
    unused — the data now comes from the GitHub API, addressed by the
    ``owner/name`` parsed out of ``pr_url``.
    """

    from forge_loop import gh_issues as gh

    slug = _repo_slug_from_pr_url(pr_url)
    if not slug:
        return "", ""
    return gh.pr_precommit_context(pr_url, slug)


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


def _fetch_pr_changed_files(pr_url: str, _repo: Path) -> list[str]:
    from forge_loop import gh_issues as gh

    slug = _repo_slug_from_pr_url(pr_url)
    if not slug:
        return []
    return gh.pr_changed_files(pr_url, slug)


def _with_deterministic_pip_editable_findings(
    report: CriticReport,
    *,
    pr_url: str,
    repo: Path,
    issue_number: int,
    logs_dir: Path,
) -> CriticReport:
    worker_text = _worker_command_context(issue_number, logs_dir)
    if not worker_text:
        return report
    changed_files = _fetch_pr_changed_files(pr_url, repo)
    deterministic = detect_pip_editable_poison(worker_text, changed_files=changed_files)
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


def _with_deterministic_findings(
    report: CriticReport,
    *,
    pr_url: str,
    repo: Path,
    issue_number: int,
    logs_dir: Path,
) -> CriticReport:
    """Apply all deterministic (non-LLM) critic rules to ``report``."""
    report = _with_deterministic_precommit_findings(
        report,
        pr_url=pr_url,
        repo=repo,
        issue_number=issue_number,
        logs_dir=logs_dir,
    )
    report = _with_deterministic_pip_editable_findings(
        report,
        pr_url=pr_url,
        repo=repo,
        issue_number=issue_number,
        logs_dir=logs_dir,
    )
    return report


def _round_guidance(round_number: int, sev3_demotion_round_threshold: int) -> str:
    """Round-aware instructions woven into the critic brief (Ch9 §9.5.1/.3).

    Early rounds stay terse — let the worker try. Later rounds escalate from
    *what is wrong* → *why* → *how* → *a concrete minimal patch sketch*, and
    once nits would otherwise burn rounds, instruct the critic to demote them.
    The text is deterministic given (round, threshold) so the behaviour is
    testable without invoking the model.
    """
    demote = sev3_demotion_round_threshold
    if round_number == 0:
        return (
            "ROUND 1 (first review of this PR). Keep BLOCKING findings TERSE: "
            "name what is wrong and where. Let the worker attempt the fix. Do "
            "NOT pre-write patches yet — that is for stalled rounds."
        )
    lines = [
        f"ROUND {round_number + 1} (this PR has already had {round_number} "
        "critic review(s) and has NOT converged — escalate specificity).",
        "For EVERY blocking (sev1/sev2) finding you carry or add, escalate: "
        "state (a) what is wrong, (b) WHY it matters, (c) HOW to fix it, and "
        "(d) a CONCRETE MINIMAL PATCH SKETCH — name the file + function and the "
        "specific change. The goal is to SHRINK what the worker must invent.",
        "DIAGNOSE THE META-CAUSE, do not just re-flag symptoms: if the diff is "
        "large and pure-addition (e.g. +N/-0), or the SAME class of finding "
        "recurs across rounds, the root cause is usually scope inflation / wrong "
        "approach / over-building. Say so explicitly and instruct the worker to "
        "CUT scope / simplify / split — not to add more code.",
    ]
    if demote > 0 and round_number >= demote:
        lines.append(
            f"This PR has stalled for {round_number} rounds (>= demotion "
            f"threshold {demote}). Move every COSMETIC (sev3) item OUT of "
            "`findings` and into `follow_ups` so it does NOT block — record it, "
            "do not grind on it. sev1/sev2 STILL BLOCK; never demote a real "
            "defect."
        )
    return "\n".join(lines)


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
    sev3_demotion_round_threshold: int = 3,
) -> CriticOutcome:
    """Spawn the critic subagent against an open PR. Synchronous.

    If the subagent's final JSON fails to parse, retry ONCE. If the retry
    also fails, emit ``critic_parse_failed`` via ``emit`` and surface an
    ``error`` verdict (the runner then leaves the PR alone — no auto-block,
    no auto-approve — so a human can intervene).

    ``sev3_demotion_round_threshold`` drives the teaching critic's triage
    (Ch9 §9.5.2): the round number is derived from the prior ``critic-*.log``
    files already on disk for this issue and woven into the brief, and once it
    reaches the threshold the parsed report's sev3 nits are demoted to
    non-blocking follow-ups.
    """
    template = brief_template or _default_brief()
    from forge_loop._critic_sdk import load_manifestos_text

    manifestos = load_manifestos_text(repo)
    # Round source: count prior critic reviews of this PR (0 == first review).
    # Computed BEFORE this review writes its own log so it is not self-counted.
    round_number = count_prior_critic_rounds(issue_number, logs_dir)
    brief = template.format(
        pr_url=pr_url,
        issue_number=issue_number,
        manifestos=manifestos,
        round_number=round_number,
        round_guidance=_round_guidance(round_number, sev3_demotion_round_threshold),
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
        report = _finalize_report(
            report,
            pr_url=pr_url,
            repo=repo,
            issue_number=issue_number,
            logs_dir=logs_dir,
            round_number=round_number,
            sev3_demotion_round_threshold=sev3_demotion_round_threshold,
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

    report = _finalize_report(
        report,
        pr_url=pr_url,
        repo=repo,
        issue_number=issue_number,
        logs_dir=logs_dir,
        round_number=round_number,
        sev3_demotion_round_threshold=sev3_demotion_round_threshold,
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


def _finalize_report(
    report: CriticReport,
    *,
    pr_url: str,
    repo: Path,
    issue_number: int,
    logs_dir: Path,
    round_number: int,
    sev3_demotion_round_threshold: int,
) -> CriticReport:
    """Apply deterministic rules, stamp the round, then triage sev3 nits.

    Order matters: deterministic precommit/pip-editable findings are added
    FIRST (so a sev1 they raise is never demoted), the round is stamped, and
    only then are cosmetic findings demoted once the PR has stalled. sev1/sev2
    pass through ``demote_sev3_if_stalled`` untouched.
    """
    report = _with_deterministic_findings(
        report,
        pr_url=pr_url,
        repo=repo,
        issue_number=issue_number,
        logs_dir=logs_dir,
    )
    report.round_number = round_number
    return demote_sev3_if_stalled(
        report,
        round_number=round_number,
        threshold=sev3_demotion_round_threshold,
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
        for e in read_events(log_path):
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

    # Teaching-critic: the explicit ordered must-fix set. Tolerant of a missing
    # field (back-compat with reports emitted before this field existed) and of
    # the model handing back a single string instead of a list.
    raw_mptg = obj.get("minimal_path_to_green")
    if isinstance(raw_mptg, str):
        raw_mptg = [raw_mptg]
    minimal_path_to_green = (
        [s.strip() for s in raw_mptg if isinstance(s, str) and s.strip()]
        if isinstance(raw_mptg, list)
        else []
    )

    raw_followups = obj.get("follow_ups") or []
    follow_ups: list[Finding] = []
    if isinstance(raw_followups, list):
        for item in raw_followups:
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
                follow_ups.append(f)

    return CriticReport(
        overall=overall,
        findings=findings,
        manifesto_violations=violations,
        raw=raw,
        minimal_path_to_green=minimal_path_to_green,
        follow_ups=follow_ups,
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
        for e in read_events(log_path):
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
