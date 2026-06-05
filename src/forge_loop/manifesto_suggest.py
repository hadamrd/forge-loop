"""Close the manifesto feedback loop: PR fix → proposed house rule (#134).

The manifesto system's whole point (see ``docs/GUIDE.md`` → "The feedback
loop") is *every bug → manifesto update → permanent gate*. Today that loop is
manual: a human reads a bug fix and hand-edits ``.forge/quality-manifesto.md``
or ``.forge/testing-manifesto.md``. Most fixes never become permanent gates, so
the same failure shape recurs.

This module powers ``forge-loop manifesto suggest --from-pr <N>``: it reads a
bug PR's body + commits + unified diff + linked-issue body + added test files,
seeds **one** focused SDK session with the current manifestos plus that bug
context, and parses a structured :class:`ManifestoSuggestion` of proposed
adds/edits — each carrying a ``rationale`` that cites the source PR / issue.

Dry-run (default) prints the suggestion and exits 0 with zero GitHub writes.
``--apply`` (driven from the CLI) opens a reviewable PR against the relevant
``.forge/*-manifesto.md`` file(s) carrying the markdown delta — never an
in-place edit, never an auto-merge (out of scope per the ticket).

Design mirrors :mod:`forge_loop.brainstormer`: a :class:`BaseModel` with
``ConfigDict(extra="ignore")`` for forward-compat, a one-shot SDK driver with an
injectable ``sdk_fn``/``query_fn`` for tests, and swallow-and-degrade context
assembly so a missing/unfetchable piece never crashes the command.

Cross-module discriminators (``target``, ``kind``) are ``str`` enums per the
quality manifesto's "no stringly-typed cross-module event boundaries" rule —
compared with ``is``, never string literals.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from forge_loop import gh_issues as _gh_module
from forge_loop.log import get_logger
from forge_loop.manifestos import QUALITY_REL, TESTING_REL, discover_manifestos

__all__ = [
    "ManifestoTarget",
    "EditKind",
    "ProposedManifestoEdit",
    "ManifestoSuggestion",
    "BugContext",
    "ManifestoSuggester",
    "ManifestoPrPlan",
    "InsufficientContextError",
    "SuggestionParseError",
    "extract_linked_issue",
    "assemble_bug_context",
    "build_prompt",
    "parse_suggestion",
    "render_suggestion_text",
    "build_markdown_delta",
    "build_pr_plan",
    "open_manifesto_pr",
    "run_manifesto_suggest_sdk",
]

_log = get_logger("forge_loop.manifesto_suggest")


# ---------------------------------------------------------------------------
# Discriminators (str enums — manifesto: no stringly-typed cross-module labels)
# ---------------------------------------------------------------------------


class ManifestoTarget(StrEnum):
    """Which manifesto a proposed rule belongs to."""

    QUALITY = "quality"
    TESTING = "testing"


class EditKind(StrEnum):
    """Whether the proposal adds a brand-new rule or edits an existing one."""

    ADD = "add"
    EDIT = "edit"


# ---------------------------------------------------------------------------
# Structured output model — mirrors brainstormer.BrainstormReport style.
# ---------------------------------------------------------------------------


class ProposedManifestoEdit(BaseModel):
    """A single proposed manifesto add/edit.

    Extra keys are ignored for forward-compat with future SDK output
    versions (same convention as :class:`brainstormer.BrainstormReport`).
    """

    model_config = ConfigDict(extra="ignore")

    target: ManifestoTarget = ManifestoTarget.QUALITY
    kind: EditKind = EditKind.ADD
    rule_text: str = ""
    rationale: str = ""


class ManifestoSuggestion(BaseModel):
    """Structured suggestion of manifesto adds/edits derived from a bug PR."""

    model_config = ConfigDict(extra="ignore")

    summary: str = ""
    edits: list[ProposedManifestoEdit] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.edits


class InsufficientContextError(RuntimeError):
    """Raised when a PR yields too little context to suggest a rule.

    Drives a clear non-zero exit on the sad path (non-existent /
    closed-without-fix / network-failing PR) without crashing.
    """


class SuggestionParseError(RuntimeError):
    """Raised when the SDK final message can't be parsed into a suggestion.

    Fails closed: a malformed / non-JSON / empty final message yields a
    clear error and non-zero exit rather than a garbage suggestion.
    """


# ---------------------------------------------------------------------------
# Bug context assembly — swallow-and-degrade per gh.py convention.
# ---------------------------------------------------------------------------


@dataclass
class BugContext:
    """Everything the SDK session needs to know about the source bug."""

    pr_number: int
    pr_body: str = ""
    commit_metadata: str = ""
    diff: str = ""
    linked_issue_number: int | None = None
    linked_issue_body: str = ""
    test_files: tuple[str, ...] = ()

    @property
    def is_sufficient(self) -> bool:
        """True when there's enough signal to ask the model for a rule.

        A PR with no body, no diff, and no commit metadata (e.g. a
        non-existent / network-failing PR whose gh reads all returned "")
        is *insufficient* — we refuse rather than hallucinate a rule.
        """
        return bool(self.pr_body.strip() or self.diff.strip() or self.commit_metadata.strip())


_LINKED_ISSUE_VERB = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[:\s]+#(\d+)",
    re.IGNORECASE,
)
_ANY_ISSUE_REF = re.compile(r"#(\d+)")


def extract_linked_issue(text: str) -> int | None:
    """Best-effort: pull the linked bug-issue number out of PR text.

    Prefers an explicit ``Fixes/Closes/Resolves #N`` (GitHub auto-link
    keywords); falls back to the first bare ``#N`` reference. Returns
    ``None`` when nothing matches — the caller then skips the linked-issue
    fetch entirely (degrade gracefully).
    """
    if not text:
        return None
    m = _LINKED_ISSUE_VERB.search(text)
    if m:
        return int(m.group(1))
    m = _ANY_ISSUE_REF.search(text)
    if m:
        return int(m.group(1))
    return None


def _is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py") or "/tests/" in f"/{path}"


def assemble_bug_context(
    pr_number: int,
    *,
    repo_path: Path,
    owner: str = "",
    repo: str = "",
    gh_client: Any = None,
    gh_module: Any = None,
) -> BugContext:
    """Assemble :class:`BugContext` from the GitHub layer.

    Every fetch is wrapped so a missing/unfetchable piece degrades to an
    empty value rather than raising — mirroring the swallow-and-return
    convention in :mod:`forge_loop.gh_issues`. PR data is fetched via the
    GhClient, addressed by ``owner/repo``; ``repo_path`` is retained for
    call-site back-compat (used for the local checkout in the apply step).
    """
    gh = gh_module or _gh_module
    pr_ref = str(pr_number)
    # The GhClient-backed PR helpers address the repo by ``owner/name``; the
    # local checkout ``repo_path`` is no longer how PR data is fetched (#223).
    repo_slug = f"{owner}/{repo}" if owner and repo else ""

    body = ""
    commit_metadata = ""
    try:
        body, commit_metadata = gh.pr_precommit_context(pr_ref, repo_slug)
    except Exception as exc:  # noqa: BLE001 — boundary; degrade gracefully
        _log.warning("manifesto_suggest_pr_context_unavailable", pr=pr_number, error=str(exc))

    diff = ""
    try:
        diff = gh.pr_diff(pr_ref, repo_slug)
    except Exception as exc:  # noqa: BLE001 — boundary; degrade gracefully
        _log.warning("manifesto_suggest_pr_diff_unavailable", pr=pr_number, error=str(exc))

    changed: list[str] = []
    try:
        changed = gh.pr_changed_files(pr_ref, repo_slug)
    except Exception as exc:  # noqa: BLE001 — boundary; degrade gracefully
        _log.warning("manifesto_suggest_pr_files_unavailable", pr=pr_number, error=str(exc))
    test_files = tuple(p for p in changed if isinstance(p, str) and _is_test_file(p))

    linked_issue_number = extract_linked_issue(body or "")
    linked_issue_body = ""
    if linked_issue_number is not None and gh_client is not None and owner and repo:
        try:
            issue = gh_client.get_issue(owner, repo, linked_issue_number)
            if issue is not None:
                linked_issue_body = getattr(issue, "body", "") or ""
        except Exception as exc:  # noqa: BLE001 — boundary; degrade gracefully
            _log.warning(
                "manifesto_suggest_issue_unavailable",
                issue=linked_issue_number,
                error=str(exc),
            )

    return BugContext(
        pr_number=pr_number,
        pr_body=body or "",
        commit_metadata=commit_metadata or "",
        diff=diff or "",
        linked_issue_number=linked_issue_number,
        linked_issue_body=linked_issue_body,
        test_files=test_files,
    )


# ---------------------------------------------------------------------------
# Prompt rendering — self-contained template (no briefs/ coupling).
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """\
You are the MANIFESTO-FEEDBACK agent in a forge-loop sprint system.

Your job: read ONE bug PR that has already been fixed, then propose the house
rule(s) that — had they existed — would have caught this bug at review time so
the same failure shape never recurs. This closes the feedback loop:
every bug -> manifesto update -> permanent gate.

# Current manifestos (the rules already in force)

## quality-manifesto.md

{quality_md}

## testing-manifesto.md

{testing_md}

# The bug (source PR #{pr_number})

## PR body

{pr_body}

## Commit metadata

{commit_metadata}

## Linked bug issue {linked_issue_ref}

{linked_issue_body}

## Test files added/changed in the PR

{test_files}

## Unified diff

{diff}

# Your task

Propose the minimal set of manifesto rule(s) that would have prevented or caught
this bug. For EACH proposal emit:
  * target: "quality" or "testing" (which manifesto the rule belongs in)
  * kind: "add" (a brand-new rule) or "edit" (tighten an existing one)
  * rule_text: the proposed rule, in the imperative house-rule voice already
    used in the manifestos above (a short title line + a rationale paragraph)
  * rationale: WHY this rule follows from this bug. It MUST reference the source
    bug — cite PR #{pr_number}{linked_issue_clause}.

Do NOT restate a rule that already exists verbatim in the manifestos above.
Prefer one or two sharp rules over many vague ones.

# Output

Emit ONE JSON object on the LAST line of your reply, no markdown fence, no prose
after it:

{{"summary": "<one line>",
  "edits": [
    {{"target": "quality",
      "kind": "add",
      "rule_text": "...",
      "rationale": "... references PR #{pr_number} ..."}}
  ]}}

An empty "edits" list is valid if the bug genuinely implies no general rule.
"""


def _or_none(text: str) -> str:
    text = (text or "").strip()
    return text if text else "(none)"


def build_prompt(manifestos: Any, bug: BugContext) -> str:
    """Render the one-shot SDK prompt from the manifestos + bug context."""
    linked_ref = f"#{bug.linked_issue_number}" if bug.linked_issue_number is not None else "(none)"
    linked_clause = (
        f" and/or linked issue #{bug.linked_issue_number}"
        if bug.linked_issue_number is not None
        else ""
    )
    test_files = "\n".join(f"- {p}" for p in bug.test_files) if bug.test_files else "(none)"
    return _PROMPT_TEMPLATE.format(
        quality_md=_or_none(getattr(getattr(manifestos, "quality", None), "markdown", "")),
        testing_md=_or_none(getattr(getattr(manifestos, "testing", None), "markdown", "")),
        pr_number=bug.pr_number,
        pr_body=_or_none(bug.pr_body),
        commit_metadata=_or_none(bug.commit_metadata),
        linked_issue_ref=linked_ref,
        linked_issue_body=_or_none(bug.linked_issue_body),
        test_files=test_files,
        diff=_or_none(bug.diff),
        linked_issue_clause=linked_clause,
    )


# ---------------------------------------------------------------------------
# Suggestion parsing — fail closed.
# ---------------------------------------------------------------------------


def _extract_json_object(last_message: str) -> dict[str, Any]:
    """Extract the trailing JSON object from the SDK's last message.

    Same defensive strategy as :func:`brainstormer._parse_sdk_payload`:
    whole-message fast path, then scan for the last balanced ``{...}``.
    Raises :class:`ValueError` on malformed/empty input.
    """
    text = (last_message or "").strip()
    if not text:
        raise ValueError("empty SDK output")
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    depth = 0
    start = -1
    best: str | None = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                best = text[start : i + 1]
    if best is None:
        raise ValueError("no JSON object found in SDK output")
    try:
        obj = json.loads(best)
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON in SDK output: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("SDK output JSON is not an object")
    return obj


def parse_suggestion(last_message: str) -> ManifestoSuggestion:
    """Parse the SDK final message into a :class:`ManifestoSuggestion`.

    Raises :class:`SuggestionParseError` on any malformed / non-JSON /
    empty input or schema-invalid payload — never returns a partial or
    garbage suggestion.
    """
    try:
        payload = _extract_json_object(last_message)
        return ManifestoSuggestion.model_validate(payload)
    except (ValueError, ValidationError) as exc:
        raise SuggestionParseError(
            f"could not parse manifesto suggestion from SDK output ({exc}); "
            f"last_message={last_message!r}"
        ) from exc


# ---------------------------------------------------------------------------
# Human-readable rendering.
# ---------------------------------------------------------------------------


def render_suggestion_text(suggestion: ManifestoSuggestion, *, pr_number: int) -> str:
    """Render a human-readable view of the suggestion for the dry-run path."""
    lines: list[str] = [f"Manifesto suggestion from PR #{pr_number}:"]
    if suggestion.summary.strip():
        lines.append(f"  summary: {suggestion.summary.strip()}")
    if suggestion.is_empty:
        lines.append("  (no rule proposed — bug implies no general gate)")
        return "\n".join(lines)
    for i, edit in enumerate(suggestion.edits, start=1):
        lines.append("")
        lines.append(f"  [{i}] target={edit.target.value} kind={edit.kind.value}")
        lines.append(f"      rule: {edit.rule_text.strip()}")
        lines.append(f"      rationale: {edit.rationale.strip()}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown-delta logic → PR plan.
# ---------------------------------------------------------------------------


def _append_rules(existing_md: str, items: list[ProposedManifestoEdit], pr_number: int) -> str:
    """Append proposed rules to a manifesto body (best-effort, never in-place).

    Back-compat: when ``existing_md`` is empty (the repo has no such
    manifesto yet) we seed a top-level header so the produced file is a
    valid standalone manifesto.
    """
    base = existing_md.rstrip("\n")
    if not base.strip():
        base = "# Manifesto\n\n(Seeded by `forge-loop manifesto suggest`.)"
    parts = [base]
    for edit in items:
        verb = "added" if edit.kind is EditKind.ADD else "edited"
        rule = edit.rule_text.strip() or "(rule text missing)"
        rationale = edit.rationale.strip() or f"Derived from PR #{pr_number}."
        parts.append(
            f"\n## Rule (suggested from PR #{pr_number}, {verb})\n\n"
            f"{rule}\n\n"
            f"**Rationale.** {rationale}"
        )
    return "\n".join(parts) + "\n"


def build_markdown_delta(
    suggestion: ManifestoSuggestion,
    manifestos: Any,
    *,
    pr_number: int,
) -> dict[str, str]:
    """Return ``{relative_path: new_full_content}`` for each touched manifesto.

    Only manifestos that have at least one proposed edit appear in the
    result. Each value is the FULL new file body (existing + appended rule
    sections) so the caller can write it verbatim.
    """
    out: dict[str, str] = {}
    sides = (
        (ManifestoTarget.QUALITY, QUALITY_REL, getattr(manifestos, "quality", None)),
        (ManifestoTarget.TESTING, TESTING_REL, getattr(manifestos, "testing", None)),
    )
    for target, rel, side in sides:
        items = [e for e in suggestion.edits if e.target is target]
        if not items:
            continue
        existing = getattr(side, "markdown", "") or ""
        out[rel] = _append_rules(existing, items, pr_number)
    return out


@dataclass
class ManifestoPrPlan:
    """A reviewable PR-open plan: branch + file deltas + title/body."""

    pr_number: int
    branch: str
    title: str
    body: str
    commit_message: str
    file_contents: dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.file_contents


def build_pr_plan(
    suggestion: ManifestoSuggestion,
    manifestos: Any,
    *,
    pr_number: int,
) -> ManifestoPrPlan:
    """Turn a suggestion into a :class:`ManifestoPrPlan` (pure / testable)."""
    file_contents = build_markdown_delta(suggestion, manifestos, pr_number=pr_number)
    targets = ", ".join(sorted({Path(p).name for p in file_contents})) or "manifestos"
    title = f"chore(manifesto): codify house rule from PR #{pr_number}"
    rationale_lines = [
        f"- [{e.target.value}/{e.kind.value}] {e.rationale.strip()}"
        for e in suggestion.edits
        if e.rationale.strip()
    ]
    body = (
        f"Proposed manifesto update derived from the bug fixed in PR #{pr_number}.\n\n"
        f"{suggestion.summary.strip()}\n\n"
        "## Proposed rules\n\n" + ("\n".join(rationale_lines) or "(none)") + "\n\n"
        f"Touches: {targets}\n\n"
        "Generated by `forge-loop manifesto suggest --from-pr "
        f"{pr_number} --apply`."
    )
    commit_message = (
        f"chore(manifesto): codify house rule from PR #{pr_number}\n\n"
        f"Automatically proposed by `forge-loop manifesto suggest` to close the\n"
        f"feedback loop: the bug fixed in PR #{pr_number} should have been caught\n"
        f"by a house rule. {suggestion.summary.strip()}".rstrip()
    )
    return ManifestoPrPlan(
        pr_number=pr_number,
        branch=f"manifesto/suggest-from-pr-{pr_number}",
        title=title,
        body=body,
        commit_message=commit_message,
        file_contents=file_contents,
    )


def open_manifesto_pr(
    plan: ManifestoPrPlan,
    *,
    repo_path: Path,
    github_repo: str,
    base_branch: str = "trunk",
    runner: Callable[..., Any] | None = None,
) -> str:
    """Write the delta, branch/commit/push, and open a PR. Returns the URL.

    ``runner`` is an injection point (defaults to ``subprocess.run``) so
    tests can drive the git/gh dance without a real repo. Raises
    :class:`ValueError` when ``github_repo`` is unset (the ``--apply``
    guard) or :class:`RuntimeError` when a step fails.
    """
    if not github_repo:
        raise ValueError("manifesto suggest --apply requires a configured github_repo (owner/name)")
    if plan.is_empty:
        raise ValueError("manifesto suggest --apply: nothing to apply (empty suggestion)")

    import subprocess as _subprocess

    run = runner or _subprocess.run

    # Write the new manifesto bodies.
    rel_paths = sorted(plan.file_contents)
    for rel in rel_paths:
        dest = Path(repo_path) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(plan.file_contents[rel], encoding="utf-8")

    def _run(cmd: list[str]) -> Any:
        result = run(cmd, cwd=repo_path, capture_output=True, text=True, check=False)
        if getattr(result, "returncode", 1) != 0:
            stderr = getattr(result, "stderr", "") or ""
            raise RuntimeError(f"manifesto suggest --apply: `{' '.join(cmd[:3])}` failed: {stderr}")
        return result

    _run(["git", "checkout", "-b", plan.branch])
    _run(["git", "add", *rel_paths])
    _run(["git", "commit", "-m", plan.commit_message])
    _run(["git", "push", "-u", "origin", plan.branch])
    # The branch is pushed; open the PR through the GitHub SDK client (git
    # stays on the injectable ``runner``; the PR open does not shell out).
    return _gh_module.create_pull(
        plan.title,
        plan.body,
        plan.branch,
        base_branch,
        github_repo,
    )


# ---------------------------------------------------------------------------
# One-shot SDK driver — reuse the brainstormer/critic shim.
# ---------------------------------------------------------------------------


def run_manifesto_suggest_sdk(
    prompt: str,
    *,
    cwd: Path,
    timeout_s: int,
    model: str | None = None,
    query_fn: Any = None,
    options_cls: Any = None,
) -> Any:
    """One-shot SDK session for manifesto suggestion.

    Thin pass-through to :func:`forge_loop._critic_sdk.run_critic_sdk` —
    the same single-session driver the critic / brainstormer / PO use.
    """
    from forge_loop._critic_sdk import run_critic_sdk

    return run_critic_sdk(
        prompt,
        cwd=cwd,
        timeout_s=timeout_s,
        model=model,
        query_fn=query_fn,
        options_cls=options_cls,
    )


@dataclass
class ManifestoSuggester:
    """Drive one SDK session and return a :class:`ManifestoSuggestion`.

    Mirrors :class:`forge_loop.brainstormer.Brainstormer`: an injectable
    ``sdk_fn`` keeps the SDK module out of unit tests, and the GitHub layer
    (``gh_module`` for PR reads, ``gh_client`` for the linked issue) is
    injected so context assembly is fully fakeable.
    """

    repo_path: Path = Path(".")
    owner: str = ""
    repo: str = ""
    gh_client: Any = None
    gh_module: Any = None
    sdk_fn: Callable[..., Any] | None = None
    timeout_s: int = 300
    model: str | None = None

    def assemble_context(self, pr_number: int) -> BugContext:
        return assemble_bug_context(
            pr_number,
            repo_path=self.repo_path,
            owner=self.owner,
            repo=self.repo,
            gh_client=self.gh_client,
            gh_module=self.gh_module,
        )

    def run(self, pr_number: int) -> ManifestoSuggestion:
        bug = self.assemble_context(pr_number)
        if not bug.is_sufficient:
            raise InsufficientContextError(
                f"could not read PR #{pr_number} / insufficient context "
                f"(no body, diff, or commit metadata) — wrote nothing"
            )

        # Back-compat: a repo with no .forge/ manifestos still produces a
        # suggestion. discover_manifestos(required=False) never raises for a
        # missing dir/file — it returns empty manifestos + warnings.
        manifestos = discover_manifestos(self.repo_path, required=False)
        prompt = build_prompt(manifestos, bug)

        sdk_fn = self.sdk_fn or run_manifesto_suggest_sdk
        result = sdk_fn(prompt, cwd=self.repo_path, timeout_s=self.timeout_s, model=self.model)
        timed_out = getattr(result, "timed_out", False)
        err = getattr(result, "error", None)
        if timed_out or err == "timeout":
            raise SuggestionParseError("manifesto suggest SDK session timed out — no suggestion")
        if err:
            raise SuggestionParseError(f"manifesto suggest SDK session failed: {err}")

        return parse_suggestion(getattr(result, "last_message", "") or "")
