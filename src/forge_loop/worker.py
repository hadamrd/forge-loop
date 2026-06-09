"""Worker dispatch: spin up a worktree + run `claude -p` on a single issue."""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge_loop.events import read_events
from forge_loop.sandbox import CapabilityPolicy
from forge_loop.worker_brief import (
    brief_template_hash as _brief_template_hash,
)
from forge_loop.worker_brief import make_brief, make_repair_brief
from forge_loop.worker_worktree import ensure_subagent_trusted as _ensure_subagent_trusted
from forge_loop.worker_worktree import prep_repair_worktree as _prep_repair_worktree
from forge_loop.worker_worktree import prep_worktree as _prep_worktree
from forge_loop.worker_worktree import subagent_env as _worktree_subagent_env
from forge_loop.worker_worktree import worktree_path as _worktree_path

__all__ = [
    "WorkerOutcome",
    "brief_template_hash",
    "ensure_subagent_trusted",
    "make_brief",
    "make_repair_brief",
    "recover_orphaned_pr_url",
    "run_repair_worker",
    "run_worker",
]


def brief_template_hash() -> str:
    """Compatibility export for callers that fingerprint worker briefs."""
    return _brief_template_hash()


def ensure_subagent_trusted(target_dir: Path) -> None:
    """Compatibility export for callers that prepare SDK/CLI trust files."""
    _ensure_subagent_trusted(target_dir)


def _subagent_env() -> dict[str, str]:
    """Compatibility export for legacy subprocess workers."""
    return _worktree_subagent_env()


@dataclass
class WorkerOutcome:
    issue: int
    title: str
    pr_url: str | None
    status: str  # merged | open | failed | timeout | no_pr
    duration_s: float
    stdout_tail: str
    error: str | None = None
    # Issue #267 (safety): the critic's verdict for this outcome's PR, as set by
    # the legacy ``_run_critic_for_outcomes`` review pass. ``None`` means "no
    # legacy critic verdict was recorded" (critic disabled, pipeline-driven, or
    # the PR was never reviewable) — the merge gate then falls back to its
    # pre-critic behaviour. A non-``None`` value is one of the critic verdict
    # tokens (``approved | changes_requested | blocked | error``); auto-merge
    # requires an AFFIRMATIVE ``"approved"`` and is withheld for every other
    # token. This closes the #267 hole where a verdict=error (a crashed review)
    # fell through the deny-list and auto-merged an UNREVIEWED PR.
    critic_verdict: str | None = None
    events: list[dict[str, Any]] | None = None  # appended by subagent via sprint-events.jsonl
    cost_usd: float = 0.0
    usage: dict[str, Any] | None = None
    model: str = ""
    # Issue #132: which manifesto versions were prepended to the worker
    # system prompt for this run. Always a 2-key dict ({"quality": ...,
    # "testing": ...}) when at least one side was present; ``None`` when
    # the repo had no manifestos at all (back-compat baseline).
    manifesto_sha: dict[str, str | None] | None = None


def _branch_name(n: int, title: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", title.lower())[:40].strip("-")
    return f"loop/{n}-{slug or 'fix'}"


def _current_branch(worktree: Path) -> str:
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return branch.stdout.strip() if branch.returncode == 0 else ""


def _extract_outcome(log_path: Path) -> tuple[str | None, str]:
    """Parse the final `result` event from a claude stream-json log."""
    last_result_text = ""
    for e in read_events(log_path):
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


_PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


def _recover_pr_url_from_events(events: list[dict[str, Any]]) -> str | None:
    """Scan subagent sprint-events for a PR URL (newest first).

    The worker subagent emits ``pr_opened`` events (and a trailing result
    object) carrying the PR it created. When the worker is cancelled at the
    deadline *after* opening the PR, those events are the most reliable
    surviving record of the URL — the SDK ResultMessage never arrived.
    """
    for ev in reversed(events):
        if not isinstance(ev, dict):
            continue
        for key in ("pr", "pr_url", "url", "detail"):
            value = ev.get(key)
            if isinstance(value, str):
                match = _PR_URL_RE.search(value)
                if match:
                    return match.group(0)
    return None


def _recover_pr_url_from_log(log_path: Path) -> str | None:
    """Scan a worker stream-json log for the last PR URL it printed."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    matches = _PR_URL_RE.findall(text)
    return matches[-1] if matches else None


def recover_orphaned_pr_url(
    worktree: Path | None = None,
    log_path: Path | None = None,
) -> str | None:
    """Best-effort recovery of a PR URL after a worker was cancelled.

    Issue #213: a worker can open its PR and then hit ``worker_timeout_s``
    (``CancelledError``/``TimeoutError``) before reporting the URL on its
    ``WorkerOutcome``. The PR is then orphaned — no tick learns it exists.
    This recovers the URL from the structured sprint-events first (most
    reliable) and falls back to scanning the raw worker log. Returns
    ``None`` when nothing PR-shaped is found — callers keep their existing
    ``pr_url=None`` semantics in that case.
    """
    if worktree is not None:
        url = _recover_pr_url_from_events(_read_subagent_events(worktree))
        if url:
            return url
    if log_path is not None and log_path.exists():
        return _recover_pr_url_from_log(log_path)
    return None


def _tail(path: Path, n_chars: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n_chars))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _emit_worker_event(
    emit: Callable[[str, dict[str, Any]], None] | None,
    kind: str,
    **payload: Any,
) -> None:
    if emit is None:
        return
    with contextlib.suppress(Exception):
        emit(kind, payload)


def _preflight_worker_env(
    *,
    repo: Path,
    n: int,
    title: str,
    tick: int | None,
    worktree: Path,
    emit: Callable[[str, dict[str, Any]], None] | None,
    env_path_prepend: tuple[str, ...],
    env_vars: dict[str, str],
    env_require: tuple[str, ...],
) -> WorkerOutcome | None:
    """Provision + preflight the worker's declared toolchain contract.

    The 2026-06-05 silent-toolchain incident: a worker inherited the
    orchestrator's ambient env (system python, no project ``.venv`` on PATH)
    so ``pyright`` / ``pytest`` failed with ``command not found`` and the
    worker burned ~20 min retrying variants with NO error surfaced. This makes
    the toolchain an EXPLICIT, verified dependency:

    * No env block declared at all → emit a ``worker_env_undeclared`` warning
      (visible gap) and proceed.
    * ``env_require`` non-empty and a tool is missing on the PROVISIONED PATH →
      emit ``worker_toolchain_unavailable`` and return a failed WorkerOutcome
      so the caller ABORTS before starting the doomed SDK session.

    Returns ``None`` to proceed, or a terminal failed :class:`WorkerOutcome`.
    """
    if not (env_path_prepend or env_vars or env_require):
        _emit_worker_event(
            emit,
            "worker_env_undeclared",
            issue=n,
            title=title,
            tick=tick,
            worktree=str(worktree),
        )
        return None

    if not env_require:
        return None

    from forge_loop._worker_sdk import _clean_sdk_env
    from forge_loop.worker_env import build_worker_env, missing_tools

    preflight_env = build_worker_env(
        _clean_sdk_env(),
        repo=repo,
        path_prepend=env_path_prepend,
        vars=env_vars,
    )
    missing = missing_tools(preflight_env, env_require)
    if not missing:
        return None

    _emit_worker_event(
        emit,
        "worker_toolchain_unavailable",
        issue=n,
        title=title,
        tick=tick,
        missing=missing,
        path=preflight_env.get("PATH", ""),
        venv=preflight_env.get("VIRTUAL_ENV", ""),
        worktree=str(worktree),
    )
    return WorkerOutcome(
        issue=n,
        title=title,
        pr_url=None,
        status="failed",
        duration_s=0.0,
        stdout_tail=(
            f"required worker toolchain missing: {', '.join(missing)} "
            f"(PATH={preflight_env.get('PATH', '')})"
        ),
        error=f"worker_toolchain_unavailable: {', '.join(missing)}",
    )


def run_worker(
    issue: dict[str, Any],
    repo: Path,
    logs_dir: Path,
    timeout_s: int,
    *,
    risk_gated: bool = False,
    past_attempts: list[dict[str, Any]] | None = None,
    blocking_comments: list[str] | None = None,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
    lumen_top_k: int = 3,
    lumen_test_pattern: str = "**/*Test.*",
    coauthor: str = "",
    tick: int | None = None,
    model: str | None = None,
    thinking: str | None = None,
    provider: str = "claude",
    allowed_mcp_servers: tuple[str, ...] | None = None,
    load_timeout_ms: int | None = None,
    strict_mcp_config: bool = False,
    mcp_servers: dict[str, Any] | None = None,
    base_branch: str = "trunk",
    brief_override: str | None = None,
    capability_policy: CapabilityPolicy | None = None,
    events_file: Path | None = None,
    maestro_context: str = "",
    permissions: str = "full",
    env_path_prepend: tuple[str, ...] = (),
    env_vars: dict[str, str] | None = None,
    env_require: tuple[str, ...] = (),
    verify_commands: tuple[str, ...] = (),
    scope_soft_loc_cap: int = 150,
    reuse_existing_worktree: bool = False,
) -> WorkerOutcome:
    """Run one claude-code worker against an issue.

    ``emit(kind, payload)`` is the bus emitter — used for watchdog events.
    Passed in by the runner; if omitted, watchdog events are silently dropped.

    ``model`` / ``thinking`` (issue #34) are threaded through to the SDK so
    each role can be tuned independently of the Claude Code CLI default.

    ``load_timeout_ms`` / ``strict_mcp_config`` / ``mcp_servers`` defend the
    worker session against operator-global MCP config slowness (~250 tools
    enumerated at init can blow the SDK's default 60s timeout). Operators
    set these via ``worker.load_timeout_ms`` / ``worker.strict_mcp_config`` /
    ``worker.mcp_servers`` in forge-loop.yaml or the matching env vars.
    """
    n = issue["number"]
    title = issue["title"]
    branch = _branch_name(n, title)

    if reuse_existing_worktree and (existing := _worktree_path(repo, n)).exists():
        existing_branch = _current_branch(existing)
        if existing_branch != branch:
            return WorkerOutcome(
                issue=n,
                title=title,
                pr_url=None,
                status="failed",
                duration_s=0.0,
                stdout_tail=(
                    f"existing worktree {existing} is on branch {existing_branch!r}, "
                    f"expected {branch!r}; refusing to reset follow-up work"
                ),
                error="followup-worktree-branch-mismatch",
            )
        worktree, err = existing, None
    else:
        worktree, err = _prep_worktree(
            repo,
            n,
            branch,
            base_branch=base_branch,
            emit=emit,
            capability_policy=capability_policy,
            events_file=events_file,
        )
    if err is not None:
        return WorkerOutcome(
            issue=n,
            title=title,
            pr_url=None,
            status="failed",
            duration_s=0.0,
            stdout_tail=err[-500:],
            error="worktree-create-failed",
        )

    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"worker-{n}-{int(time.time())}.log"
    _emit_worker_event(
        emit,
        "worker_start",
        issue=n,
        title=title,
        tick=tick,
        provider=provider,
        model=model or "",
        worktree=str(worktree),
        log_path=str(log_path),
        branch=branch,
    )
    # Worker environment contract — PREFLIGHT before driving a doomed session
    # (the 2026-06-05 silent-toolchain incident). Abort loud if a required tool
    # is missing on the provisioned PATH; warn if no env block was declared.
    env_vars = env_vars or {}
    aborted = _preflight_worker_env(
        repo=repo,
        n=n,
        title=title,
        tick=tick,
        worktree=worktree,
        emit=emit,
        env_path_prepend=env_path_prepend,
        env_vars=env_vars,
        env_require=env_require,
    )
    if aborted is not None:
        return aborted

    # Issue #132 — discover the active manifestos once at dispatch time.
    # The bundle threads into BOTH the brief renderer (prepends MANIFESTO
    # block) AND the outcome telemetry (``manifesto_sha`` audit field).
    # Discovery sources from the repo checkout, NOT the worktree — the
    # worktree's .forge/ exists post-branch but the manifestos live on
    # the canonical checkout that controls house rules.
    from forge_loop.manifestos import load_manifestos

    manifesto_bundle = load_manifestos(repo)
    manifesto_sha = manifesto_bundle.sha_payload() if manifesto_bundle.any_present else None

    # Iteration loop (issue #78) passes a focused follow-up brief that
    # short-circuits ``make_brief`` — the follow-up session reuses the same
    # worktree + branch and just gets told "your ONLY job is X".
    if brief_override is not None:
        # Even on follow-up runs, prepend the manifesto block so iteration 2
        # is held to the same house rules as iteration 1.
        from forge_loop.manifestos import inject_into_brief

        brief = inject_into_brief(brief_override, manifesto_bundle)
    else:
        brief = make_brief(
            issue,
            worktree,
            risk_gated=risk_gated,
            past_attempts=past_attempts,
            blocking_comments=blocking_comments,
            lumen_top_k=lumen_top_k,
            lumen_test_pattern=lumen_test_pattern,
            coauthor=coauthor,
            manifesto_bundle=manifesto_bundle,
            capability_policy=capability_policy,
            verify_commands=verify_commands,
            scope_soft_loc_cap=scope_soft_loc_cap,
        )

    # Maestro advisory context (frontier + memory) rides on top of the brief.
    # Prepended here — downstream of fingerprint/template-hash computation — so
    # it never perturbs attempt fingerprints or skip-guards. Empty = no-op.
    if maestro_context:
        brief = f"{maestro_context}\n\n{brief}"

    from forge_loop.worker_permissions import claude_permission_options, codex_sandbox_args

    if provider == "codex":
        outcome = _run_worker_codex(
            issue=issue,
            worktree=worktree,
            log_path=log_path,
            brief=brief,
            timeout_s=timeout_s,
            model=model,
            sandbox_args=codex_sandbox_args(permissions, capability_policy),
        )
        outcome.manifesto_sha = manifesto_sha
        _emit_worker_event(
            emit,
            "worker_done",
            issue=n,
            title=title,
            tick=tick,
            status=outcome.status,
            pr_url=outcome.pr_url,
            duration_s=round(outcome.duration_s, 1),
            error=outcome.error,
            worktree=str(worktree),
            log_path=str(log_path),
        )
        return outcome

    _claude_opts = claude_permission_options(permissions, capability_policy)
    try:
        outcome = _run_worker_sdk(
            issue=issue,
            worktree=worktree,
            log_path=log_path,
            brief=brief,
            timeout_s=timeout_s,
            emit=emit,
            tick=tick,
            model=model,
            thinking=thinking,
            allowed_mcp_servers=allowed_mcp_servers,
            load_timeout_ms=load_timeout_ms,
            strict_mcp_config=strict_mcp_config,
            mcp_servers=mcp_servers,
            permission_mode=_claude_opts["permission_mode"],
            sandbox=_claude_opts.get("sandbox"),
            repo=repo,
            env_path_prepend=env_path_prepend,
            env_vars=env_vars,
            secret_names=capability_policy.secret_names if capability_policy else (),
            capability_policy=capability_policy,
        )
        outcome.manifesto_sha = manifesto_sha
        _emit_worker_event(
            emit,
            "worker_done",
            issue=n,
            title=title,
            tick=tick,
            status=outcome.status,
            pr_url=outcome.pr_url,
            duration_s=round(outcome.duration_s, 1),
            error=outcome.error,
            worktree=str(worktree),
            log_path=str(log_path),
        )
        return outcome
    except BaseException as exc:
        _emit_worker_event(
            emit,
            "worker_failed",
            issue=n,
            title=title,
            tick=tick,
            error=f"{type(exc).__name__}: {exc!s:.200}",
            worktree=str(worktree),
            log_path=str(log_path),
        )
        raise


def run_repair_worker(
    issue: dict[str, Any],
    pr: dict[str, Any],
    review_context: str,
    repo: Path,
    logs_dir: Path,
    timeout_s: int,
    *,
    emit: Callable[[str, dict[str, Any]], None] | None = None,
    lumen_top_k: int = 3,
    lumen_test_pattern: str = "**/*Test.*",
    coauthor: str = "",
    tick: int | None = None,
    model: str | None = None,
    thinking: str | None = None,
    provider: str = "claude",
    allowed_mcp_servers: tuple[str, ...] | None = None,
    load_timeout_ms: int | None = None,
    strict_mcp_config: bool = False,
    mcp_servers: dict[str, Any] | None = None,
    capability_policy: CapabilityPolicy | None = None,
    events_file: Path | None = None,
    permissions: str = "full",
    env_path_prepend: tuple[str, ...] = (),
    env_vars: dict[str, str] | None = None,
    env_require: tuple[str, ...] = (),
    verify_commands: tuple[str, ...] = (),
    scope_soft_loc_cap: int = 150,
) -> WorkerOutcome:
    """Repair an existing blocked PR by pushing to its head branch."""
    n = issue["number"]
    title = issue["title"]
    branch = pr.get("headRefName") or ""
    pr_url = pr.get("url")
    if not branch:
        return WorkerOutcome(
            issue=n,
            title=title,
            pr_url=pr_url,
            status="failed",
            duration_s=0.0,
            stdout_tail="missing PR headRefName",
            error="repair-missing-branch",
        )
    worktree, err = _prep_repair_worktree(
        repo,
        n,
        branch,
        emit=emit,
        capability_policy=capability_policy,
        events_file=events_file,
    )
    if err is not None:
        return WorkerOutcome(
            issue=n,
            title=title,
            pr_url=pr_url,
            status="failed",
            duration_s=0.0,
            stdout_tail=err[-500:],
            error="repair-worktree-create-failed",
        )
    # Same toolchain preflight as the fresh-worker path: a repair worker runs
    # the same verify gates (pyright/pytest) and would silently degrade on a
    # missing toolchain just the same (2026-06-05 incident).
    env_vars = env_vars or {}
    aborted = _preflight_worker_env(
        repo=repo,
        n=n,
        title=title,
        tick=tick,
        worktree=worktree,
        emit=emit,
        env_path_prepend=env_path_prepend,
        env_vars=env_vars,
        env_require=env_require,
    )
    if aborted is not None:
        aborted.pr_url = pr_url
        return aborted

    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"repair-{n}-{int(time.time())}.log"
    brief = make_repair_brief(
        issue,
        worktree,
        pr=pr,
        review_context=review_context,
        lumen_top_k=lumen_top_k,
        lumen_test_pattern=lumen_test_pattern,
        coauthor=coauthor,
        verify_commands=verify_commands,
        scope_soft_loc_cap=scope_soft_loc_cap,
    )
    from forge_loop.worker_permissions import claude_permission_options, codex_sandbox_args

    if provider == "codex":
        return _run_worker_codex(
            issue=issue,
            worktree=worktree,
            log_path=log_path,
            brief=brief,
            timeout_s=timeout_s,
            model=model,
            sandbox_args=codex_sandbox_args(permissions, capability_policy),
        )
    _claude_opts = claude_permission_options(permissions, capability_policy)
    return _run_worker_sdk(
        issue=issue,
        worktree=worktree,
        log_path=log_path,
        brief=brief,
        timeout_s=timeout_s,
        emit=emit,
        tick=tick,
        model=model,
        thinking=thinking,
        allowed_mcp_servers=allowed_mcp_servers,
        load_timeout_ms=load_timeout_ms,
        strict_mcp_config=strict_mcp_config,
        mcp_servers=mcp_servers,
        permission_mode=_claude_opts["permission_mode"],
        sandbox=_claude_opts.get("sandbox"),
        repo=repo,
        env_path_prepend=env_path_prepend,
        env_vars=env_vars,
        secret_names=capability_policy.secret_names if capability_policy else (),
        capability_policy=capability_policy,
    )


def _run_worker_codex(
    *,
    issue: dict[str, Any],
    worktree: Path,
    log_path: Path,
    brief: str,
    timeout_s: int,
    model: str | None = None,
    sandbox_args: list[str] | None = None,
) -> WorkerOutcome:
    """Drive a worker through ``codex exec`` and map it to WorkerOutcome."""
    from forge_loop.agent_backend import (
        extract_github_pr,
        extract_last_json_object,
        run_codex_exec,
    )

    n = issue["number"]
    title = issue["title"]
    result = run_codex_exec(
        prompt=brief,
        cwd=worktree,
        log_path=log_path,
        timeout_s=timeout_s,
        model=model,
        add_dirs=[worktree],
        sandbox_args=sandbox_args,
    )
    if result.timed_out:
        return WorkerOutcome(
            issue=n,
            title=title,
            pr_url=None,
            status="timeout",
            duration_s=result.duration_s,
            stdout_tail="(timeout)",
            error=result.error,
            usage={},
            model=model or "",
        )
    obj = extract_last_json_object(result.last_message) or {}
    pr_url = obj.get("pr") if isinstance(obj.get("pr"), str) else None
    raw_status = obj.get("status")
    status: str = raw_status if isinstance(raw_status, str) else "no_pr"
    if pr_url is None:
        pr_url = extract_github_pr(result.last_message)
        if pr_url:
            status = "open"
    if result.error and pr_url is None:
        status = "failed"
    return WorkerOutcome(
        issue=n,
        title=title,
        pr_url=pr_url,
        status=status,
        duration_s=result.duration_s,
        stdout_tail=_tail(log_path, 500),
        events=_read_subagent_events(worktree),
        cost_usd=0.0,
        usage={},
        model=model or "",
        error=result.error,
    )


def _run_worker_sdk(
    *,
    issue: dict[str, Any],
    worktree: Path,
    log_path: Path,
    brief: str,
    timeout_s: int,
    emit: Callable[[str, dict[str, Any]], None] | None,
    tick: int | None,
    model: str | None = None,
    thinking: str | None = None,
    allowed_mcp_servers: tuple[str, ...] | None = None,
    load_timeout_ms: int | None = None,
    strict_mcp_config: bool = False,
    mcp_servers: dict[str, Any] | None = None,
    permission_mode: str = "bypassPermissions",
    sandbox: dict[str, Any] | None = None,
    repo: Path | None = None,
    env_path_prepend: tuple[str, ...] = (),
    env_vars: dict[str, str] | None = None,
    secret_names: tuple[str, ...] = (),
    capability_policy: CapabilityPolicy | None = None,
) -> WorkerOutcome:
    """Drive the SDK session, emit typed WorkerEvents, build a WorkerOutcome.

    The SDK delivers typed messages and a final ResultMessage carries the
    grand-total cost + usage. The events.jsonl format (one JSON dict per
    line, ``kind`` + ``ts`` discriminant) is preserved for backward compat
    with anything that tails the worker log.
    """
    from forge_loop._worker_sdk import run_sdk_session

    n = issue["number"]
    title = issue["title"]
    started = time.time()

    with open(log_path, "w", encoding="utf-8") as log_fh:

        def _on_event(ev: dict[str, Any]) -> None:
            with contextlib.suppress(OSError):
                log_fh.write(json.dumps(ev, default=str) + "\n")
                log_fh.flush()

        async def _drive() -> Any:
            return await run_sdk_session(
                brief,
                cwd=worktree,
                max_turns=120,
                repo=repo,
                env_path_prepend=env_path_prepend,
                env_vars=env_vars or {},
                add_dirs=[worktree],
                permission_mode=permission_mode,
                sandbox=sandbox,
                on_event=_on_event,
                model=model,
                thinking_budget=thinking,
                allowed_mcp_servers=allowed_mcp_servers,
                load_timeout_ms=load_timeout_ms,
                strict_mcp_config=strict_mcp_config,
                mcp_servers=mcp_servers,
                secret_names=secret_names,
                capability_policy=capability_policy,
            )

        timed_out = False
        result: Any = None
        try:
            result = _run_with_timeout(_drive, timeout_s)
        except TimeoutError:
            timed_out = True

    duration = time.time() - started

    if timed_out:
        # Record the *requested* model so the audit trail is informative
        # even when no response ever arrived (issue #34).
        #
        # Issue #213: a worker can open its PR and THEN trip the deadline
        # before the SDK ResultMessage arrives. Recover the URL from the
        # worker log / sprint-events so the next tick's adoption scan can
        # re-critic + merge it instead of orphaning the PR forever. The
        # status stays ``timeout`` (the truth) — only ``pr_url`` is filled.
        recovered_pr = recover_orphaned_pr_url(worktree, log_path)
        return WorkerOutcome(
            issue=n,
            title=title,
            pr_url=recovered_pr,
            status="timeout",
            duration_s=duration,
            stdout_tail="(timeout)",
            error=f"worker exceeded {timeout_s}s",
            cost_usd=0.0,
            usage={},
            model=model or "",
        )

    assert result is not None
    # ``result.model`` already falls back to the requested model when the
    # response carried none (see _worker_sdk.run_sdk_session).
    model_seen = result.model or (model or "")
    cost_usd = result.cost_usd

    pr_url = result.pr_url
    status = result.status
    if result.error is not None and pr_url is None:
        status = "failed"

    events = _read_subagent_events(worktree)
    return WorkerOutcome(
        issue=n,
        title=title,
        pr_url=pr_url,
        status=status,
        duration_s=duration,
        stdout_tail=_tail(log_path, 500),
        events=events,
        cost_usd=cost_usd,
        usage=dict(result.usage or {}),
        model=model_seen,
        error=result.error,
    )


def _run_with_timeout(coro_factory: Callable[[], Any], timeout_s: int) -> Any:
    """Run an async coroutine factory under a wall-clock timeout.

    Uses ``anyio.run`` + ``anyio.fail_after`` so we share the SDK's event
    loop. Raises :class:`TimeoutError` on deadline expiry — the caller
    converts that to a ``timeout`` WorkerOutcome.
    """
    import anyio

    async def _wrap() -> Any:
        with anyio.fail_after(float(timeout_s)):
            return await coro_factory()

    try:
        return anyio.run(_wrap)
    except TimeoutError:
        raise


def _read_subagent_events(worktree: Path) -> list[dict[str, Any]]:
    """Read sprint-events.jsonl that the subagent (claude -p worker) may have written."""
    path = worktree / "sprint-events.jsonl"
    if not path.exists():
        return []
    return list(read_events(path))
