"""Pre-merge safety gate: refuse to merge a worker's PR when its source
issue was closed mid-flight.

Issue #65 — real correctness gap observed in dogfood: operator closed
issue #47 as dup-of-#55, a worker that started 3min earlier kept running,
opened PR #62, critic approved, runner auto-merged. The closed source
issue did NOT block the merge — operator's intent to STOP was silently
overridden.

Fix: AFTER the critic runs, BEFORE auto-merge can fire, the runner
re-fetches each PR's source issue state via ``gh issue view``. If the
issue is CLOSED (or the gh call fails — conservative), the runner:
  * disables auto-merge on the PR
  * posts an explanatory comment on the PR
  * emits a ``merge_refused_issue_closed`` event
  * leaves the PR open (do not close — operator may still want it)
  * flips the worker outcome status from ``merged`` to ``open`` so
    attempts history reflects the truth.

Adversarial: operator closes AND reopens within the worker's run →
final state OPEN → merge proceeds. We only check ONCE, at gate time,
so the last gh check wins.
"""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from forge_loop.state import append_event
from forge_loop.worker import WorkerOutcome
from forge_loop.worker_env import missing_tools


class GhMergeGateClient(Protocol):
    """The slice of ``forge_loop.gh`` this gate needs. Lets tests inject a
    spy without monkey-patching the global module."""

    def get_issue_state(self, issue: int,
                        repo: str | None = None) -> str | None: ...

    def disable_pr_auto_merge(self, pr: int | str,
                              repo: str | None = None) -> bool: ...

    def pr_comment(self, pr: int | str, body: str,
                   repo: str | None = None) -> bool: ...


def _refusal_comment(issue_num: int, issue_state: str | None) -> str:
    """The PR comment we leave on a refused merge. Surfaces enough context
    that an operator skimming notifications knows WHY the loop stopped."""
    if issue_state is None:
        state_blurb = "could not be fetched (gh call failed)"
    else:
        state_blurb = f"state: {issue_state.lower()}"
    return (
        f"Source issue #{issue_num} was closed mid-flight ({state_blurb}). "
        "Loop refusing auto-merge. Reopen the issue OR merge manually."
    )


def check_issue_closed_gate(
    outcome: WorkerOutcome,
    *,
    gh: GhMergeGateClient,
    repo: str | None,
    events_file: Any | None = None,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
) -> bool:
    """Re-check the source issue's state for a single outcome.

    Returns True iff the gate REFUSED the merge (issue closed or gh
    unreachable). Mutates ``outcome.status`` from ``merged`` → ``open``
    when refusing so the attempts ledger reflects reality.

    Outcomes without a ``pr_url`` are skipped (nothing to gate).
    """
    if not outcome.pr_url:
        return False

    state = gh.get_issue_state(outcome.issue, repo=repo)

    # OPEN → proceed (the normal happy path).
    if state == "OPEN":
        return False

    # CLOSED or unknown → refuse. Conservative on unknown: we'd rather
    # leave a PR open for human triage than auto-merge while we're not
    # sure the operator still wants it.
    body = _refusal_comment(outcome.issue, state)
    # Best-effort side effects; the event + status flip below MUST still fire.
    with contextlib.suppress(Exception):
        gh.disable_pr_auto_merge(outcome.pr_url, repo=repo)
    with contextlib.suppress(Exception):
        gh.pr_comment(outcome.pr_url, body, repo=repo)

    payload = {
        "issue": outcome.issue,
        "pr": outcome.pr_url,
        "issue_state": state,  # None = gh fetch failed; CLOSED = explicit
    }
    if events_file is not None:
        append_event(events_file, "merge_refused_issue_closed", **payload)
    if emit is not None:
        emit("merge_refused_issue_closed", payload)

    # Reflect truth in attempts history: the worker may have raced ahead
    # of the close and reported "merged", but we just blocked it.
    if outcome.status == "merged":
        outcome.status = "open"

    return True


def apply_issue_closed_gate(
    outcomes: list[WorkerOutcome],
    *,
    gh: GhMergeGateClient,
    repo: str | None,
    events_file: Any | None = None,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
) -> list[int]:
    """Run the gate over every outcome. Returns the list of issue numbers
    whose merge was refused."""
    refused: list[int] = []
    for o in outcomes:
        if check_issue_closed_gate(
            o, gh=gh, repo=repo, events_file=events_file, emit=emit,
        ):
            refused.append(o.issue)
    return refused


# ---------------------------------------------------------------------------
# Verify-clean ratchet (issue #241).
#
# The issue-closed gate above answers "did the operator change their mind?".
# THIS gate answers "is the codebase still clean?". forge-loop has no GitHub
# Actions CI (Taskfile.yml documents that decision on purpose) — it is its own
# CI — so the only thing standing between a worker that self-reported
# "definition of done met" and a merge is this deterministic, repo-wide check
# of the configured ``worker.verify`` commands (``ruff check src/ tests/``,
# ``pyright src/forge_loop``). Per the quality-manifesto boiling-frog meta-rule
# ("a metric with no gate drifts"), the prose verify list injected into the
# worker brief is decoration until something programmatically blocks the merge.
#
# It mirrors the issue-closed flow exactly: refuse → disable auto-merge, post a
# comment, emit a typed event, flip ``merged`` → ``open`` so the attempts
# ledger reflects the truth. It reuses the SAME ``GhMergeGateClient`` slice.
#
# Tool resolution is via the DECLARED ``worker.env`` contract (manifesto Q11),
# NOT ambient PATH: a missing ``ruff`` / ``pyright`` fails LOUD (refuse) rather
# than silently passing the gate.
# ---------------------------------------------------------------------------

# Keep event/comment payloads bounded — a pyright run can emit thousands of
# lines; we only need the tail to tell the operator WHICH command failed.
_VERIFY_TAIL_CHARS = 2000


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of one verify command run repo-wide.

    ``returncode == 0`` ⇒ clean. Any non-zero (including the synthetic 127 we
    use for a missing tool and the synthetic -1 we use for a crashed/timed-out
    runner) ⇒ non-clean, fail-closed.
    """

    command: str
    returncode: int
    output_tail: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class VerifyRunner(Protocol):
    """The external I/O boundary for running a verify command (manifesto Q2).

    Production wires :class:`SubprocessVerifyRunner`; tests inject a fake. The
    runner is handed the EXACT env the gate built from the declared
    ``worker.env`` contract — it MUST NOT reach for ambient PATH itself.
    """

    def run_verify(
        self, command: str, *, cwd: str, env: Mapping[str, str]
    ) -> VerifyResult: ...


def _missing_tool_result(tool: str) -> VerifyResult:
    """Fail-loud synthetic result for a tool absent from the declared PATH."""
    return VerifyResult(
        command=tool,
        returncode=127,
        output_tail=(
            f"worker toolchain unavailable: {tool!r} did not resolve on the "
            "declared worker.env PATH (manifesto Q11). Refusing the merge "
            "rather than silently passing the verify gate."
        ),
    )


def run_verify_suite(
    commands: Iterable[str],
    *,
    runner: VerifyRunner,
    cwd: str,
    env: Mapping[str, str],
    require: Iterable[str] = (),
) -> VerifyResult | None:
    """Run each configured verify command repo-wide against ``cwd``.

    Returns the FIRST failing :class:`VerifyResult`, or ``None`` if every
    command is clean. The check is repo-wide, NOT diff-scoped — the commands
    (``ruff check src/ tests/`` / ``pyright src/forge_loop``) span the whole
    tree, which is the whole point of the ratchet.

    Two fail-closed behaviours:

    * **Preflight (manifesto Q11):** the declared ``require`` tools are resolved
      against the declared ``env`` PATH BEFORE running anything. A missing tool
      short-circuits to a refusal — we never run a half-present toolchain and
      we never silently pass.
    * **Runner crash / timeout:** a runner that raises (subprocess timeout,
      OSError, …) is treated as a non-clean result, not a crashed tick.
    """
    missing = missing_tools(env, require)
    if missing:
        return _missing_tool_result(missing[0])

    for cmd in commands:
        if not cmd or not cmd.strip():
            continue
        try:
            res = runner.run_verify(cmd, cwd=cwd, env=env)
        except Exception as ex:  # noqa: BLE001 — fail-closed, never crash the tick
            return VerifyResult(
                command=cmd,
                returncode=-1,
                output_tail=f"verify runner error: {ex}"[-_VERIFY_TAIL_CHARS:],
            )
        if not res.ok:
            # Defensive copy so the tail we surface is always bounded.
            return VerifyResult(
                command=res.command,
                returncode=res.returncode,
                output_tail=res.output_tail[-_VERIFY_TAIL_CHARS:],
            )
    return None


def _verify_refusal_comment(result: VerifyResult) -> str:
    """Operator-facing PR comment for a verify-gate refusal."""
    tail = result.output_tail[-1200:]
    return (
        f"Pre-merge verify gate FAILED — repo-wide `{result.command}` is not "
        f"clean (exit {result.returncode}). The loop is refusing auto-merge "
        "until the lint/type debt is fixed (anti-slop ratchet, issue #241). "
        f"Fix the violations or address the failing command.\n\n"
        f"```\n{tail}\n```"
    )


def check_verify_clean_gate(
    outcome: WorkerOutcome,
    result: VerifyResult,
    *,
    gh: GhMergeGateClient,
    repo: str | None,
    events_file: Any | None = None,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
) -> bool:
    """Refuse a single merge-eligible outcome because verify is non-clean.

    Mirrors :func:`check_issue_closed_gate`: disables auto-merge, posts a
    comment, emits ``merge_refused_verify_unclean`` (typed, manifesto Q4),
    flips ``merged`` → ``open``. Side effects are best-effort; the event +
    status flip MUST still fire. Returns ``True`` (always refused — the caller
    only invokes this when ``result`` is already known non-clean).

    Outcomes without a ``pr_url`` are skipped (nothing to gate).
    """
    if not outcome.pr_url:
        return False

    body = _verify_refusal_comment(result)
    with contextlib.suppress(Exception):
        gh.disable_pr_auto_merge(outcome.pr_url, repo=repo)
    with contextlib.suppress(Exception):
        gh.pr_comment(outcome.pr_url, body, repo=repo)

    payload = {
        "issue": outcome.issue,
        "pr": outcome.pr_url,
        "command": result.command,
        "returncode": result.returncode,
        "output_tail": result.output_tail[-_VERIFY_TAIL_CHARS:],
    }
    if events_file is not None:
        append_event(events_file, "merge_refused_verify_unclean", **payload)
    if emit is not None:
        emit("merge_refused_verify_unclean", payload)

    if outcome.status == "merged":
        outcome.status = "open"

    return True


def apply_verify_clean_gate(
    outcomes: list[WorkerOutcome],
    *,
    runner: VerifyRunner,
    gh: GhMergeGateClient,
    repo: str | None,
    commands: Iterable[str],
    cwd: str,
    env: Mapping[str, str],
    require: Iterable[str] = (),
    enabled: bool = True,
    events_file: Any | None = None,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
) -> list[int]:
    """Repo-wide verify ratchet over a tick's outcomes (issue #241).

    Runs the verify suite ONCE against ``cwd`` (the commands are repo-wide, so
    one run gates every outcome). If non-clean, refuses every merge-eligible
    outcome (``pr_url`` set, status ``open``/``merged``). Returns the refused
    issue numbers — the caller folds these into the auto-merge ``refused_issues``
    set so the PRs stay off the conveyor.

    ``enabled=False`` ⇒ no-op even when the repo is red. This is the
    dependency-ordering escape hatch from the issue: the gate ships behind a
    flag defaulting to off so it can merge BEFORE the lint/pyright cleanup
    tickets land, then be flipped to enforce once the baseline is green.
    """
    if not enabled:
        return []

    eligible = [
        o for o in outcomes if o.pr_url and o.status in {"open", "merged"}
    ]
    if not eligible:
        return []

    cmd_list = [c for c in commands if c and c.strip()]
    require_list = [t for t in require if t and t.strip()]
    if not cmd_list and not require_list:
        # Nothing declared to verify → nothing to enforce. (The brief still
        # carries the prose instruction; this layer is purely additive.)
        return []

    result = run_verify_suite(
        cmd_list, runner=runner, cwd=cwd, env=env, require=require_list
    )
    if result is None:
        return []  # clean → proceed

    refused: list[int] = []
    for o in eligible:
        if check_verify_clean_gate(
            o, result, gh=gh, repo=repo, events_file=events_file, emit=emit,
        ):
            refused.append(o.issue)
    return refused


class SubprocessVerifyRunner:
    """Production :class:`VerifyRunner` — runs a verify command via subprocess.

    Ruff / pyright / pytest have no Python SDK, so a subprocess is the only way
    to drive them; manifesto Q5 (no ``subprocess.run`` for *SDK-able* services)
    does not apply. The command runs with ``cwd`` and the EXACT ``env`` the gate
    built from the declared ``worker.env`` contract — never ambient PATH.

    A non-zero return code OR a timeout maps to a non-clean :class:`VerifyResult`
    (fail-closed); the runner never raises for a normal command failure.
    """

    def __init__(self, timeout_s: float = 600.0) -> None:
        self._timeout_s = timeout_s

    def run_verify(
        self, command: str, *, cwd: str, env: Mapping[str, str]
    ) -> VerifyResult:
        try:
            proc = subprocess.run(  # noqa: S602 — verify commands are operator-declared
                command,
                shell=True,
                cwd=str(Path(cwd)),
                env=dict(env),
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
            )
        except subprocess.TimeoutExpired as ex:
            tail = (ex.stdout or "") + (ex.stderr or "")
            return VerifyResult(
                command=command,
                returncode=124,  # conventional timeout code
                output_tail=(f"verify TIMEOUT after {self._timeout_s}s\n{tail}")[
                    -_VERIFY_TAIL_CHARS:
                ],
            )
        combined = (proc.stdout or "") + (proc.stderr or "")
        return VerifyResult(
            command=command,
            returncode=proc.returncode,
            output_tail=combined[-_VERIFY_TAIL_CHARS:],
        )
