"""Codebase-state auditor (issue #156).

Per-PR critic catches **deltas**; this auditor catches **accumulation**.

Background — boiling-frog failure
=================================

``src/forge_loop/cli.py`` is >1700 LOC. The quality manifesto's soft cap
for Python modules is 500. The per-PR critic never flagged it because
cli.py grew 50-100 LOC at a time across many PRs — each increment was a
reasonable diff, but the cumulative state-violation slipped past every
per-PR review.

This module solves that gap by walking the repository, running a set of
**state-based probes** against it, and reporting (and optionally filing
tickets for) accumulation violations the per-PR critic structurally
cannot see.

Design
======

* :class:`Probe` is a typed ``Protocol`` — every concrete probe lives
  under :mod:`forge_loop.audit_probes` and emits zero or more
  :class:`Violation` objects.
* :class:`AuditReport` is the aggregate result of one audit pass.
* :func:`audit` walks a repository with the configured probes. The
  result is **dry-run by default**: the caller decides whether to file
  tickets.
* :func:`file_violations` does the gh side: one ticket per violation,
  axis-labeled ``modernization-gated``, with acceptance criteria
  scaffolded into the body. Idempotency is handled by listing the
  ``audit:probe-<name>`` label on each existing ticket and skipping
  duplicates (same probe + same target).
* Typed events: :class:`AuditViolationFiledEvent` / :class:`AuditCleanEvent`
  live in :mod:`forge_loop.events`.

Why a separate module (not absorbed into ``maintenance.py``)
============================================================

``maintenance.py`` is an LLM-as-PM grooming agent — qualitative
judgement calls (is this issue stale? which dupe is canonical?).
The auditor is the opposite shape: deterministic, regex/AST-driven,
zero LLM involvement. Keeping them apart means the audit pass runs in
<1s with no model spend and is easy to reason about in isolation.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


# Axis label for tickets the auditor files. Same shape as other
# loop-managed axis labels (``loop:ready`` etc.) — operators / dispatch
# can filter by it.
AUDIT_AXIS_LABEL = "modernization-gated"

# Per-probe label prefix used for idempotency. ``audit:probe-file-size``
# on an issue means "this ticket was filed by the file-size probe".
PROBE_LABEL_PREFIX = "audit:probe-"


@dataclass(frozen=True)
class Violation:
    """One state-rule violation surfaced by a probe.

    ``probe`` is the probe name (matches :attr:`Probe.name`); ``target``
    is a probe-specific identifier (file path, function FQN, module).
    The pair ``(probe, target)`` is the dedup key for ticket filing.

    ``severity`` is a 1-5 scale (1 = highest), purely informational.

    ``rationale`` should explain WHY this violation matters in
    manifesto terms (citing the rule, citing downstream axis work
    blocked) so the resulting ticket reads cleanly.

    ``acceptance`` is a markdown bullet list scaffolded into the ticket
    body so the worker that picks it up has unambiguous acceptance
    criteria.
    """

    probe: str
    target: str
    severity: int
    title: str
    rationale: str
    acceptance: list[str] = field(default_factory=list)
    metrics: dict[str, int | float | str] = field(default_factory=dict)

    @property
    def probe_label(self) -> str:
        return f"{PROBE_LABEL_PREFIX}{self.probe}"

    def dedup_key(self) -> tuple[str, str]:
        return (self.probe, self.target)


class Probe(Protocol):
    """Each probe inspects the repository and yields violations.

    Implementations live under :mod:`forge_loop.audit_probes`. They MUST:

    * Be pure with respect to the filesystem — no network, no state
      writes — so the audit pass is cheap and reproducible.
    * Yield zero violations on a clean repository.
    * Surface a stable ``name`` (used as the probe-label suffix; do not
      change without a migration).
    """

    name: str

    def scan(self, repo: Path) -> Iterable[Violation]: ...


@dataclass
class AuditReport:
    """Aggregate of one audit pass.

    ``violations`` is the flat list across every probe; ``by_probe`` is
    the same data grouped for convenient summarisation.
    ``probes_run`` lets the report distinguish "no violations because
    we didn't probe" from "no violations because the repo is clean".
    ``errors`` keys a probe name to the exception string if that probe
    crashed — one probe blowing up MUST NOT take down the rest.
    """

    violations: list[Violation] = field(default_factory=list)
    probes_run: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def is_clean(self) -> bool:
        return not self.violations

    def by_probe(self) -> dict[str, list[Violation]]:
        out: dict[str, list[Violation]] = {}
        for v in self.violations:
            out.setdefault(v.probe, []).append(v)
        return out


# ---------------------------------------------------------------------------
# Repo walk — single source of truth for "which files do we audit?"
# ---------------------------------------------------------------------------


_DEFAULT_IGNORE_DIR_NAMES: frozenset[str] = frozenset({
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    "target",
    ".tox",
    ".nox",
    "site-packages",
})


def walk_source_files(
    repo: Path,
    *,
    suffixes: tuple[str, ...] = (".py", ".ts", ".tsx", ".js", ".java"),
    ignore_dirs: frozenset[str] = _DEFAULT_IGNORE_DIR_NAMES,
) -> Iterable[Path]:
    """Yield every source file under ``repo`` whose suffix matches.

    Skips common vendor / cache / build directories so probes don't burn
    cycles on generated code. Probes that need a different filter should
    call this with their own ``suffixes``.

    The walk is intentionally simple — no .gitignore parsing — because
    the ignore set above already covers everything that matters in our
    layout, and adding pygit2 / pathspec for one feature is overkill.
    """
    repo = repo.resolve()
    suffix_set = {s if s.startswith(".") else f".{s}" for s in suffixes}
    stack: list[Path] = [repo]
    while stack:
        cur = stack.pop()
        try:
            entries = list(cur.iterdir())
        except (FileNotFoundError, PermissionError):
            continue
        for entry in entries:
            name = entry.name
            if entry.is_dir():
                # Skip hidden + ignored dirs but keep ``.github`` visible.
                if (name in ignore_dirs or name.startswith(".")) and name != ".github":
                    continue
                stack.append(entry)
            elif entry.is_file():
                if entry.suffix in suffix_set:
                    yield entry


# ---------------------------------------------------------------------------
# audit() — the entrypoint
# ---------------------------------------------------------------------------


def default_probes() -> list[Probe]:
    """Built-in probes shipped with forge-loop.

    A function (not a module-level list) so importing this module
    doesn't drag in every probe — and so tests can monkey-patch.
    """
    from forge_loop.audit_probes.file_size import FileSizeProbe
    from forge_loop.audit_probes.function_size import FunctionSizeProbe

    return [FileSizeProbe(), FunctionSizeProbe()]


def audit(repo: Path, probes: list[Probe] | None = None) -> AuditReport:
    """Run every probe against ``repo`` and aggregate the results.

    A probe that raises is recorded in ``report.errors`` and skipped —
    one bad probe MUST NOT prevent the others from running. This is the
    same belt-and-braces shape used by the stuck-sweep tick guard.

    The function is pure: no event emission, no gh calls, no ticket
    filing. Callers (CLI, tick loop) decide what to do with the report.
    """
    probes_list = probes if probes is not None else default_probes()
    report = AuditReport()
    for probe in probes_list:
        report.probes_run.append(probe.name)
        try:
            found = list(probe.scan(repo))
        except Exception as ex:  # noqa: BLE001 — one bad probe must not kill the pass
            report.errors[probe.name] = f"{type(ex).__name__}: {ex}"[:300]
            continue
        report.violations.extend(found)
    return report


# ---------------------------------------------------------------------------
# Ticket filing — gh side. Idempotent across runs.
# ---------------------------------------------------------------------------


# Loose protocol of what we need from the gh client. Matches both
# ``GithubkitClient`` and ``MockGhClient`` structurally without forcing
# callers to import the heavy module.
class _GhLike(Protocol):
    def issues_by_label(self, owner: str, repo: str, label: str, limit: int) -> list[Any]: ...

    def create_issue(
        self, owner: str, repo: str, title: str, body: str, labels: list[str]
    ) -> Any: ...


def render_ticket_body(v: Violation) -> str:
    """Render the markdown body of an audit-filed ticket.

    The body restates the rationale, lists acceptance criteria, and
    closes with a footer identifying the probe + target so a human
    reading the ticket can map back to the audit run that filed it.
    """
    lines = [
        "## Why",
        "",
        v.rationale.strip(),
        "",
        "## Acceptance",
    ]
    if v.acceptance:
        for crit in v.acceptance:
            lines.append(f"- {crit}")
    else:
        lines.append("- (probe did not scaffold acceptance criteria — fill in before working)")
    if v.metrics:
        lines.extend(["", "## Metrics", ""])
        for k, val in v.metrics.items():
            lines.append(f"- `{k}`: {val}")
    lines.extend([
        "",
        "---",
        f"_Filed by `forge-loop audit` — probe `{v.probe}`, target `{v.target}`._",
    ])
    return "\n".join(lines)


@dataclass
class FilingOutcome:
    """Result of one :func:`file_violations` call.

    ``filed`` lists the violations that produced new tickets;
    ``skipped`` lists those that matched an existing open ticket
    (idempotent path); ``errors`` keys violation dedup-keys to the
    exception string when create_issue blew up.
    """

    filed: list[tuple[Violation, int]] = field(default_factory=list)
    skipped: list[Violation] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def _existing_targets_for_probe(
    gh: _GhLike, owner: str, repo: str, probe_name: str
) -> set[str]:
    """Pull existing open issues for a probe and parse their target footers.

    Matches the ``probe `<name>`, target `<target>`.`` line we inject in
    :func:`render_ticket_body`. A missing footer (older ticket, hand-
    edited) is gracefully ignored — the worst case is one duplicate
    ticket, not a crash.
    """
    label = f"{PROBE_LABEL_PREFIX}{probe_name}"
    try:
        issues = gh.issues_by_label(owner, repo, label, 100)
    except Exception:  # noqa: BLE001 — best effort; on failure we may dup
        return set()
    pattern = re.compile(
        rf"probe `{re.escape(probe_name)}`, target `([^`]+)`"
    )
    out: set[str] = set()
    for issue in issues:
        body = getattr(issue, "body", "") or ""
        m = pattern.search(body)
        if m:
            out.add(m.group(1))
    return out


def file_violations(
    report: AuditReport,
    gh: _GhLike,
    *,
    owner: str,
    repo: str,
    extra_labels: tuple[str, ...] = (),
    emit_filed: Callable[[Violation, int], None] | None = None,
    emit_clean: Callable[[list[str]], None] | None = None,
) -> FilingOutcome:
    """File one GitHub issue per violation, deduping against existing.

    * ``extra_labels`` is appended to every created ticket — operators
      use this to inject org-specific labels (``team:platform`` etc.).
    * The ``audit:probe-<name>`` label is always added so the next
      audit pass can find the existing ticket and dedup.
    * ``AUDIT_AXIS_LABEL`` (``modernization-gated``) is always added.
    * If the report has zero violations, ``emit_clean`` is invoked with
      the probes-run list so the operator can see the auditor actually
      ran (and didn't just have no probes loaded).

    Idempotency: we list existing open tickets carrying the probe label
    and parse the target footer. A violation whose target is already
    present is recorded in ``outcome.skipped`` and NOT re-filed.
    """
    outcome = FilingOutcome()
    if report.is_clean:
        if emit_clean is not None:
            emit_clean(list(report.probes_run))
        return outcome

    # Group by probe so we issue one issues_by_label call per probe,
    # not one per violation.
    by_probe: dict[str, list[Violation]] = {}
    for v in report.violations:
        by_probe.setdefault(v.probe, []).append(v)

    for probe_name, violations in by_probe.items():
        existing = _existing_targets_for_probe(gh, owner, repo, probe_name)
        for v in violations:
            if v.target in existing:
                outcome.skipped.append(v)
                continue
            labels = [AUDIT_AXIS_LABEL, v.probe_label, *extra_labels]
            body = render_ticket_body(v)
            try:
                created = gh.create_issue(owner, repo, v.title, body, list(labels))
            except Exception as ex:  # noqa: BLE001
                outcome.errors[f"{v.probe}:{v.target}"] = (
                    f"{type(ex).__name__}: {ex}"[:200]
                )
                continue
            number = int(getattr(created, "number", 0) or 0)
            outcome.filed.append((v, number))
            if emit_filed is not None:
                emit_filed(v, number)
    return outcome


__all__ = [
    "AUDIT_AXIS_LABEL",
    "PROBE_LABEL_PREFIX",
    "AuditReport",
    "FilingOutcome",
    "Probe",
    "Violation",
    "audit",
    "default_probes",
    "file_violations",
    "render_ticket_body",
    "walk_source_files",
]
