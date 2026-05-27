"""Worker dispatch: spin up a worktree + run `claude -p` on a single issue."""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def ensure_subagent_trusted(target_dir: Path) -> None:
    """Plant `.claude/settings.json` in target_dir if missing.

    Belt-and-suspenders to the static `.claude/settings.json` checked in to
    the main repo: on fresh checkouts / CI / new operator machines where the
    static file might be missing or stale, we still want subagents to start
    in a trusted state. Idempotent — never overwrites an existing file
    (operators may have customised it).

    Without this trust-marker, the harness applies the "untrusted project"
    gate (anthropics/claude-code#58663) and denies Edit/Write/most Bash
    actions even with --allow-dangerously-skip-permissions.
    """
    cdir = target_dir / ".claude"
    settings_path = cdir / "settings.json"
    if settings_path.exists():
        return
    cdir.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(_PERMISSIVE_WORKTREE_SETTINGS)


def _subagent_env() -> dict[str, str]:
    """Env for spawned claude subagents.

    The `claude` CLI checks ``CLAUDECODE=1`` and refuses to run as a fresh
    autonomous session if it's set (because it thinks it's inside an existing
    Claude Code session — see anthropics/claude-code#37442 and
    anthropics/claude-agent-sdk-python#573). When the loop runner ITSELF is
    spawned from inside a Claude Code session (e.g. an operator running it
    via the IDE's Bash tool), CLAUDECODE=1 propagates to the workers and they
    can't acquire Edit/Write permissions.

    Clear the var so the subprocess starts a clean session. Same fix the
    Anthropic SDK recommends.
    """
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_SSE_PORT", None)  # also leaked by some IDE integrations
    return env


@dataclass
class WorkerOutcome:
    issue: int
    title: str
    pr_url: str | None
    status: str  # merged | open | failed | timeout | no_pr | budget_exceeded
    duration_s: float
    stdout_tail: str
    error: str | None = None
    events: list[dict[str, Any]] | None = None  # appended by subagent via sprint-events.jsonl
    cost_usd: float = 0.0
    usage: dict[str, Any] | None = None
    model: str = ""
    budget_usd: float = 0.0


def make_brief(
    issue: dict[str, Any],
    worktree: Path,
    *,
    risk_gated: bool = False,
    past_attempts: list[dict[str, Any]] | None = None,
    lumen_top_k: int = 3,
    lumen_test_pattern: str = "**/*Test.*",
    coauthor: str = "",
) -> str:
    """Render the worker brief for an issue.

    ``risk_gated`` (True): the worker opens the PR but DOES NOT enable
    auto-merge. It posts a comment "ready for human review" and exits.

    ``past_attempts`` (non-empty): includes a "PREVIOUS ATTEMPTS" section
    so the worker can learn from prior tries.
    """
    body = (issue.get("body") or "")[:6000]
    n = issue["number"]

    history_section = ""
    if past_attempts:
        rendered = "\n".join(
            f"- {a.get('ts', '?')} → {a.get('status', '?')}"
            + (f" (note: {a['note']})" if a.get("note") else "")
            + (f"; PR={a['pr_url']}" if a.get("pr_url") else "")
            for a in past_attempts[-10:]
        )
        history_section = (
            "\nPREVIOUS ATTEMPTS ON THIS ISSUE (oldest first):\n"
            f"{rendered}\n"
            "Use these to avoid repeating the same dead-ends.\n"
        )

    merge_step_renumbered = (
        "10. `gh pr create` with a clear title + body, then\n"
        "    STOP. DO NOT enable auto-merge. The `risk:high` label on this issue\n"
        "    means a human must review. Post a comment on the PR: 'Risk-gated;\n"
        "    ready for human review.' Your status is `open` (not `merged`)."
        if risk_gated else
        "10. `gh pr create` with a clear title + body (the body should restate\n"
        "    the acceptance criteria and how they're tested).\n"
        "11. `gh pr merge <N> --squash --auto --delete-branch`."
    )

    lumen_total = lumen_top_k + 1

    final_status = (
        f'{{"issue": {n}, "pr": "<url>", "status": "open", "note": "risk-gated"}}'
        if risk_gated else
        f'{{"issue": {n}, "pr": "<url-or-null>", "status": "merged|open|failed", "note": "<short>"}}'
    )

    coauthor_line = f"Sign as: Co-Authored-By: {coauthor}" if coauthor else ""

    return f"""You are an autonomous worker in a sprint loop. Fix issue #{n} end-to-end.

WORKTREE (already created): {worktree}
cd there. Stay there. Don't touch the main checkout.

ISSUE #{n}: {issue['title']}
---
{body}
---
{history_section}
CONTRACT (content-grade — NOT the smallest possible diff):
1. **Read the spec.** The issue body is the contract. Look for sections like
   `## Acceptance criteria`, `## Test matrix`, `## Out of scope`, `## File pointers`.
   If those are missing, best-effort is fine — lean toward COVERAGE OF THE
   STATED INTENT, not the minimum touch.
2. **Read the surrounding context BEFORE coding.** Skim your project's
   contributing/architecture docs, read the surrounding code and tests in
   your project before changing it, and re-use existing patterns over
   inventing new ones.
3. **Write COMPLETE tests, not just the test that lights up your one new line:**
   - **Happy path** unit test for every new method.
   - **Adversarial / sad-path** test — bad input, missing config,
     concurrent calls, partial failure, empty list, null value. AT LEAST ONE.
   - **Integration test** for changes that cross a module boundary — look
     for integration tests under the standard location for your stack.
   - **End-to-end** for any user-facing feature behavior.
   - Tests must HARD-FAIL when the production code is broken — no tautologies,
     no swallowed assertions.
4. **Scope-appropriate size.** If the issue is a feature, ship the feature.
   Multi-file PRs are fine if the spec calls for it. Don't pad with unrelated
   refactors, BUT don't underdeliver.
5. **Discover dependent tests via Lumen semantic search** (between editing
   files and running tests). Call `mcp__lumen__semantic_search` with a query
   composed of the issue title + first ~500 chars of the issue body + the
   list of changed file paths, scoped to `{lumen_test_pattern}`. Take the
   TOP {lumen_top_k} returned test classes. Then run BOTH the test you authored
   AND those discovered classes via your project's test runner, one invocation
   per class (e.g. `--tests 'fully.qualified.MyTest'` or `pytest path::ClassName`).

   **Hard caps & contract:**
   - Cap at K={lumen_top_k} discovered + 1 authored = {lumen_total} max test-class
     invocations per sprint. Do NOT run more even if Lumen returns 30 hits.
   - **Dedup**: if a Lumen result equals the test class you wrote, do not
     run it twice.
   - **Graceful degrade**: if `mcp__lumen__semantic_search` is unavailable,
     times out, errors, or returns empty, SKIP this step with a one-line
     `echo "lumen: discovery skipped (<reason>)"` and continue. Sprint MUST
     NOT fail because Lumen is offline. Lumen is a HINT, not a contract.
   - If a discovered class produces "no tests found matching" from the test
     runner, surface it as a soft warning and move on — do not fail the sprint.
   - **Never** fall back to a full-suite test run. Avoid full-module test runs;
     target the specific tests you touched.
6. **Run YOUR added tests** (foreground): the specific class you authored +
   the discovered ones. Avoid full-suite runs; target the specific tests
   you touched.
7. Run your project's pre-commit gates (lint, format, type-check).
8. git add -A; git commit referencing #{n}. {coauthor_line}
   The commit message MUST have a body (the "why"), not just a one-line title.
9. git push -u origin <branch>.
{merge_step_renumbered}

LOOP INFRASTRUCTURE — DO NOT TOUCH:
- `{worktree}/.claude/settings.json` is loop-planted (read-only). Required
  for your permissions to work. Do NOT `git clean`, `rm`, or chmod it.
- Don't run `git clean -fdx` (would delete it). If you must reset, use
  `git restore <path>` for tracked files only.

WATCHDOG — KEEP YOUR LOG TICKING:
A liveness watchdog kills you if your own session log goes silent for 15+
minutes. Long-running commands (gradle, npm, docker) MUST run FOREGROUND
in Bash so their stdout streams into your log. DO NOT use
`run_in_background: true` for builds and then `Monitor` the exit file —
that pattern shows zero log activity until the build finishes and the
watchdog will kill you. If a command genuinely takes >10min, foreground it
with an appropriate `timeout` (Bash tool's `timeout` arg goes up to
600000ms / 10min) and let stdout stream.

COMMIT-BEFORE-QUITTING — non-negotiable:
If you have made ANY file changes, you MUST `git add -A && git commit && git push -u`
BEFORE you exit, even if:
- a test you tried to run silently produced no output,
- you're uncertain whether a gate will pass,
- you were going to write "one more test" and ran out of time,
- you think the work is incomplete.
A committed-and-pushed branch (even WIP) is recoverable; an uncommitted worktree gets
nuked at the start of the next tick (`_prep_worktree` force-removes it). If genuinely
unfinished, open the PR as a DRAFT (`gh pr create --draft`) and comment on the PR with
the remaining TODOs. Your status is then "open" not "merged" — the loop's critic will
read your draft and a future tick can finish it. NEVER exit with uncommitted changes.

DEEP-WORK BUDGET — you have time:
Wall ceiling is 2h; idle-kill is 15min. That gives you room for real
multi-file features + adversarial tests + a Spotless / spotbugs pass on
the module you touched. Don't rush a shallow PR to stay under an
imaginary clock — ship the feature.

EVENT REPORTING: to emit a significant event the master loop should
see, append a single-line JSON object to `{worktree}/sprint-events.jsonl`:
    echo '{{"ts":"<iso>","kind":"investigation_blocked","detail":"..."}}' >> {worktree}/sprint-events.jsonl
Useful kinds: investigation_started, bug_found, test_failed, gate_failed,
pr_opened, pr_merged, blocked.

FINAL LINE OF YOUR OUTPUT MUST BE A JSON OBJECT (no prose after it):
{final_status}

If you genuinely cannot ship (blocked), set status="failed" and put the blocker in `note`.
Do NOT investigate forever — make decisions and ship."""


def brief_template_hash() -> str:
    """Stable digest of the worker brief template.

    Used by ``attempts.compute_fingerprint`` so that a meaningful change to
    the worker's instructions (e.g. a new contract clause) invalidates the
    in-flight/cooldown skip — the next dispatch is for materially different
    work even if the issue body hasn't changed.

    Hashes the source of ``make_brief`` so any code change to the template
    bumps the digest. Cheap (called once per dispatch).
    """
    src = inspect.getsource(make_brief)
    return hashlib.sha256(src.encode("utf-8")).hexdigest()


def _branch_name(n: int, title: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", title.lower())[:40].strip("-")
    return f"loop/{n}-{slug or 'fix'}"


_PERMISSIVE_WORKTREE_SETTINGS = """{
  "permissions": {
    "defaultMode": "bypassPermissions",
    "allow": ["Bash(*)", "Edit(*)", "Write(*)", "Read(*)", "Grep(*)", "Glob(*)", "WebFetch(*)", "WebSearch(*)", "Task(*)", "TodoWrite(*)", "NotebookEdit(*)", "mcp__*"],
    "deny": []
  },
  "hasTrustDialogAccepted": true,
  "hasCompletedProjectOnboarding": true
}
"""


def _drop_permissive_settings(worktree: Path) -> None:
    """Plant a .claude/settings.json in the worktree so the worker subprocess
    doesn't get blocked by the harness 'untrusted project' gate.

    Observed: when workers run `git clean -fd` or similar (common when
    inspecting a worktree to "reset"), they delete the planted file and
    Claude re-evaluates permissions, locking the worker out. Fix:

    1. Chmod 444 the file so a naive `rm` triggers a permission warning
       (workers tend to skip protected files rather than `rm -f`).
    2. The brief calls this out explicitly so the agent doesn't fight it.

    Ref: anthropics/claude-code#58663 + observed deletion behavior in
    parallel sprint runs (PR #993 was the only one of 3 that survived).
    """
    cdir = worktree / ".claude"
    cdir.mkdir(parents=True, exist_ok=True)
    settings_path = cdir / "settings.json"
    settings_path.write_text(_PERMISSIVE_WORKTREE_SETTINGS)
    # Read-only — workers shouldn't be removing this file.
    settings_path.chmod(0o444)
    cdir.chmod(0o555)


def _prep_worktree(repo: Path, n: int, branch: str) -> tuple[Path, str | None]:
    wt = Path(f"/tmp/wt-loop-{n}")
    # chmod the planted .claude/ back to writable so worktree remove can
    # delete it (we set it read-only at the end of last run to prevent worker
    # tampering).
    claude_dir = wt / ".claude"
    if claude_dir.exists():
        subprocess.run(["chmod", "-R", "u+w", str(claude_dir)], capture_output=True)
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt)],
        cwd=repo, capture_output=True,
    )
    # If a previous failed attempt left a local branch lying around, delete
    # it so `git worktree add -B` can recreate it cleanly off the freshest
    # origin/trunk. `-B` would overwrite anyway, but we use plain `-b` after
    # an explicit delete to fail loudly if the branch is still in use.
    subprocess.run(
        ["git", "branch", "-D", branch],
        cwd=repo, capture_output=True,
    )
    # Force-update origin/trunk so the worktree always starts at the freshest
    # commit, even if many PRs landed during the prior tick. `+refs/heads/...`
    # makes the fetch force the ref update (defensive — non-FF should never
    # happen for trunk, but if it does we want the upstream view).
    subprocess.run(
        ["git", "fetch", "--prune", "origin", "+refs/heads/trunk:refs/remotes/origin/trunk"],
        cwd=repo, capture_output=True,
    )
    r = subprocess.run(
        ["git", "worktree", "add", str(wt), "-B", branch, "origin/trunk"],
        cwd=repo, capture_output=True, text=True,
    )
    if r.returncode != 0:
        return wt, r.stderr
    _drop_permissive_settings(wt)
    return wt, None


def _extract_outcome(log_path: Path) -> tuple[str | None, str]:
    """Parse the final `result` event from a claude stream-json log."""
    last_result_text = ""
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
                last_result_text = e.get("result", "") or ""

    pr_url: str | None = None
    status = "no_pr"
    for chunk in reversed(last_result_text.strip().splitlines()):
        chunk = chunk.strip()
        if chunk.startswith("{") and chunk.endswith("}"):
            try:
                obj = json.loads(chunk)
                pr_url = obj.get("pr")
                status = obj.get("status", status)
                break
            except json.JSONDecodeError:
                continue

    if pr_url is None and last_result_text:
        m = re.search(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+", last_result_text)
        if m:
            pr_url = m.group(0)
            status = "open"
    return pr_url, status


def _tail(path: Path, n_chars: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n_chars))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def run_worker(
    issue: dict[str, Any],
    repo: Path,
    logs_dir: Path,
    timeout_s: int,
    *,
    risk_gated: bool = False,
    past_attempts: list[dict[str, Any]] | None = None,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
    lumen_top_k: int = 3,
    lumen_test_pattern: str = "**/*Test.*",
    coauthor: str = "",
    ticket_budget_usd: float | None = None,
    spend_ledger: Path | None = None,
    tick: int | None = None,
) -> WorkerOutcome:
    """Run one claude-code worker against an issue.

    ``emit(kind, payload)`` is the bus emitter — used for watchdog events.
    Passed in by the runner; if omitted, watchdog events are silently dropped.
    """
    from forge_loop.budget import (
        SpendRecord,
        TicketBudgetTracker,
        append_spend,
        ticket_budget_for,
        utc_now_iso,
    )
    from forge_loop.watchdog import WorkerWatchdog

    n = issue["number"]
    title = issue["title"]
    branch = _branch_name(n, title)
    labels = issue.get("labels") or []
    resolved_budget = ticket_budget_for(labels, default=ticket_budget_usd)
    tracker = TicketBudgetTracker(ceiling_usd=resolved_budget)

    worktree, err = _prep_worktree(repo, n, branch)
    if err is not None:
        return WorkerOutcome(
            issue=n, title=title, pr_url=None, status="failed",
            duration_s=0.0, stdout_tail=err[-500:],
            error="worktree-create-failed",
        )

    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"worker-{n}-{int(time.time())}.log"
    brief = make_brief(
        issue, worktree,
        risk_gated=risk_gated, past_attempts=past_attempts,
        lumen_top_k=lumen_top_k,
        lumen_test_pattern=lumen_test_pattern,
        coauthor=coauthor,
    )

    started = time.time()
    timed_out = False
    proc_returncode = -1

    # Popen so the watchdog has a handle to terminate the subprocess
    # if the worker stalls (no events written, no log progress).
    with open(log_path, "wb") as logf:
        proc = subprocess.Popen(
            [
                "claude", "-p", brief,
                "--max-turns", "120",
                "--allow-dangerously-skip-permissions",
                "--add-dir", str(worktree),
                "--output-format", "stream-json",
                "--verbose",
            ],
            cwd=worktree,
            stdout=logf,
            stderr=subprocess.STDOUT,
            env=_subagent_env(),
        )

        watchdog: WorkerWatchdog | None = None
        if emit is not None:
            watchdog = WorkerWatchdog(
                proc=proc, worktree=worktree, log_path=log_path,
                emit=emit, issue=n,
            )
            watchdog.start()

        budget_killer = _BudgetWatcher(
            proc=proc, log_path=log_path, tracker=tracker,
            emit=emit, issue=n,
        )
        budget_killer.start()

        try:
            proc.wait(timeout=timeout_s)
            proc_returncode = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.terminate()
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                proc.kill()
            proc_returncode = -1
        finally:
            if watchdog is not None:
                watchdog.stop()
            budget_killer.stop()
            budget_killer.scan_once()  # final pass — catch usage on the tail

    duration = time.time() - started

    snap = tracker.snapshot
    usage_summary = {
        "input_tokens": snap.input_tokens,
        "output_tokens": snap.output_tokens,
        "cache_creation_input_tokens": snap.cache_creation_input_tokens,
        "cache_read_input_tokens": snap.cache_read_input_tokens,
        "events": snap.events,
        "fallbacks": snap.fallbacks,
    }
    model_seen = budget_killer.last_model or ""

    def _record_spend(status_for_ledger: str) -> None:
        if spend_ledger is None:
            return
        append_spend(spend_ledger, SpendRecord(
            ts=utc_now_iso(),
            issue=n,
            cost_usd=snap.cost_usd,
            input_tokens=snap.input_tokens,
            output_tokens=snap.output_tokens,
            cache_creation_input_tokens=snap.cache_creation_input_tokens,
            cache_read_input_tokens=snap.cache_read_input_tokens,
            status=status_for_ledger,
            model=model_seen,
            tick=tick,
            fallbacks=snap.fallbacks,
        ))

    if budget_killer.tripped:
        if emit is not None:
            emit("budget_exceeded", {
                "issue": n, "cost_usd": round(snap.cost_usd, 4),
                "ceiling_usd": round(resolved_budget, 4),
                "fallbacks": snap.fallbacks,
            })
        _record_spend("budget_exceeded")
        return WorkerOutcome(
            issue=n, title=title, pr_url=None, status="budget_exceeded",
            duration_s=duration,
            stdout_tail=_tail(log_path, 500),
            error=f"ticket budget ${resolved_budget:.4f} exceeded "
                  f"(spent ${snap.cost_usd:.4f})",
            cost_usd=snap.cost_usd, usage=usage_summary, model=model_seen,
            budget_usd=resolved_budget,
        )

    if timed_out:
        _record_spend("timeout")
        return WorkerOutcome(
            issue=n, title=title, pr_url=None, status="timeout",
            duration_s=duration, stdout_tail="(timeout)",
            error=f"worker exceeded {timeout_s}s",
            cost_usd=snap.cost_usd, usage=usage_summary, model=model_seen,
            budget_usd=resolved_budget,
        )

    pr_url, status = _extract_outcome(log_path)
    if pr_url is None and proc_returncode != 0:
        status = "failed"
    events = _read_subagent_events(worktree)
    _record_spend(status)
    return WorkerOutcome(
        issue=n, title=title, pr_url=pr_url, status=status,
        duration_s=duration, stdout_tail=_tail(log_path, 500),
        events=events,
        cost_usd=snap.cost_usd, usage=usage_summary, model=model_seen,
        budget_usd=resolved_budget,
    )


class _BudgetWatcher:
    """Tail the worker's stream-json log, accumulate cost, kill on ceiling.

    The Claude Agent SDK emits a ``usage`` block on each ``assistant`` message
    event and a final ``result`` event with totals. We tail the log file
    every poll interval, push every new usage event into the TicketBudgetTracker,
    and SIGTERM the subprocess as soon as the configured ceiling is crossed.

    Belt-and-suspenders against missing-data: the tracker treats malformed
    usage as worst-case, so a streamer bug cannot silently undercount the
    budget and let a runaway worker burn through $30.
    """

    poll_interval_s: float = 5.0

    def __init__(
        self, *,
        proc: subprocess.Popen[bytes],
        log_path: Path,
        tracker: Any,  # TicketBudgetTracker
        emit: Callable[[str, dict[str, Any]], None] | None,
        issue: int,
    ) -> None:
        self._proc = proc
        self._log_path = log_path
        self._tracker = tracker
        self._emit = emit
        self._issue = issue
        self._stop = False
        self._thread: Any = None
        self._read_offset = 0
        self._buf = b""
        self.tripped = False
        self.last_model: str | None = None

    def start(self) -> None:
        import threading as _t
        self._thread = _t.Thread(target=self._loop, name=f"budget-{self._issue}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop:
            self.scan_once()
            if self.tripped:
                return
            time.sleep(self.poll_interval_s)

    def scan_once(self) -> None:
        from forge_loop.budget import extract_usage
        try:
            size = self._log_path.stat().st_size
        except OSError:
            return
        if size <= self._read_offset:
            return
        try:
            with open(self._log_path, "rb") as f:
                f.seek(self._read_offset)
                chunk = f.read(size - self._read_offset)
        except OSError:
            return
        self._read_offset = size
        self._buf += chunk
        # Split on newlines; keep the trailing partial line for the next scan.
        lines = self._buf.split(b"\n")
        self._buf = lines[-1]
        crossed = False
        for raw in lines[:-1]:
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            model, usage = extract_usage(event)
            if model:
                self.last_model = model
            if usage is None:
                continue
            if self._tracker.add(model, usage):
                crossed = True
        if crossed and not self.tripped:
            self.tripped = True
            if self._emit is not None:
                snap = self._tracker.snapshot
                self._emit("budget_worker_killed", {
                    "issue": self._issue,
                    "cost_usd": round(snap.cost_usd, 4),
                    "ceiling_usd": round(self._tracker.ceiling_usd, 4),
                })
            with contextlib.suppress(OSError, ProcessLookupError):
                self._proc.terminate()


def _read_subagent_events(worktree: Path) -> list[dict[str, Any]]:
    """Read sprint-events.jsonl that the subagent (claude -p worker) may have written."""
    path = worktree / "sprint-events.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
