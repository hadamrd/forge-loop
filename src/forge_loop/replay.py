"""Replay (time-travel): re-run a past tick with a modified brief.

Issue #24. Operators iterate on briefs by editing and waiting for the next
tick. This module answers: "if I had used THIS brief on tick 42, would
the outcome have been different?"

Surface area:

- :func:`find_tick_workers` — walk the events log, return the workers that
  ran in tick N (issue, title, original status/pr/cost).
- :func:`assemble_replay_invocations` — pure: turn those into
  :class:`ReplayInvocation` records carrying the new brief.
- :func:`replay_from_fixture` — fixture-backed replay (zero-cost). Uses
  the :mod:`forge_loop._testing.replayer` machinery from issue #9 to feed
  recorded events back through `_extract_outcome` and capture a
  :class:`DryRunCapture` with the recorded diff / cost.
- :func:`run_replay_tick` — orchestrator: assemble + replay each
  invocation, append ``replay=True`` events, write capture log.
- :func:`build_diff_report` — side-by-side per-issue comparison between
  the original tick and the replay tick.
- :func:`make_replay_tick_id` — derives the synthetic tick id
  (``"42r"`` for tick 42).

Dry-run guarantees: replay code paths NEVER push branches or open PRs.
The worker's dry-run flag (:func:`forge_loop.worker.make_brief` with
``dry_run=True``) replaces the push/PR steps with a diff-capture step;
and fixture-backed replay doesn't spawn the worker at all.

Out of scope (per the issue): replay across role changes, live A/B.
"""


from __future__ import annotations


# Experimental gate (issue #39): refuse to import unless the [experimental]
# extra is installed. Stable surface only in the default install.
from forge_loop._extras import require_experimental as _require_experimental
_require_experimental('replay')
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from forge_loop.replay_fixture import extract_diff_from_events


class ReplayError(RuntimeError):
    """Raised when a replay cannot proceed (corrupt fixture, no such tick, etc).

    Distinct from :class:`forge_loop._testing.replayer.FixtureCorruptError`
    so the CLI can map it to a non-zero exit cleanly without leaking the
    test-only import path into user-facing error messages.
    """


@dataclass(frozen=True)
class WorkerRecord:
    """One worker that ran during a historical tick."""

    issue: int
    title: str
    original_pr_url: str | None
    original_status: str
    original_cost_usd: float
    original_error: str | None = None


@dataclass(frozen=True)
class ReplayInvocation:
    """A planned re-dispatch of a worker with a substituted brief."""

    issue: int
    title: str
    role: str
    brief: str
    fixture_path: Path | None  # set if a recorded SDK session is available


@dataclass
class DryRunCapture:
    """Result of replaying one invocation in dry-run mode.

    Holds enough information for the side-by-side diff report:
    - ``commit_hash`` / ``diff_text``: what the worker would have committed
    - ``cost_usd``: zero for fixture-backed replays, real for live replays
    - ``status``: outcome status the worker reported
    - ``source``: ``"fixture"`` or ``"live-dry-run"``
    """

    issue: int
    title: str
    status: str
    commit_hash: str | None
    diff_text: str
    cost_usd: float
    source: str
    error: str | None = None
    pr_url: str | None = None  # for fixture-backed replays we surface the recorded PR
    event_count: int = 0
    fixture_path: str | None = None


# ---------------------------------------------------------------- discovery


def find_tick_workers(events_path: Path, tick: int) -> list[WorkerRecord]:
    """Walk the JSONL events log; return workers that ran during ``tick``.

    Preference order:
    1. ``tick_done`` event for the tick — carries full outcomes (title,
       status, cost, etc.).
    2. ``tick_start`` event — has issue numbers only; we fall back to
       partial records (title="") if no tick_done was written (mid-tick
       crash, manually inspected events).

    Returns empty list if no such tick exists (caller decides whether to
    treat that as an error).
    """
    if not events_path.exists():
        return []

    tick_start_issues: list[int] = []
    tick_done_outcomes: list[dict[str, Any]] = []
    for raw in events_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            e = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if e.get("kind") == "tick_start" and int(e.get("tick", -1)) == tick:
            tick_start_issues = [int(n) for n in (e.get("issues") or [])]
        elif e.get("kind") == "tick_done" and int(e.get("tick", -1)) == tick:
            tick_done_outcomes = list(e.get("outcomes") or [])

    if tick_done_outcomes:
        return [
            WorkerRecord(
                issue=int(o.get("issue", 0)),
                title=str(o.get("title", "")),
                original_pr_url=o.get("pr_url"),
                original_status=str(o.get("status", "unknown")),
                original_cost_usd=float(o.get("cost_usd") or 0.0),
                original_error=o.get("error"),
            )
            for o in tick_done_outcomes
            if int(o.get("issue", 0)) > 0
        ]

    # No tick_done — fall back to tick_start.
    return [
        WorkerRecord(
            issue=n,
            title="",
            original_pr_url=None,
            original_status="unknown",
            original_cost_usd=0.0,
        )
        for n in tick_start_issues
    ]


# --------------------------------------------------------------- assembly


def assemble_replay_invocations(
    workers: list[WorkerRecord],
    new_brief: str,
    role: str,
    fixtures_dir: Path | None = None,
    *,
    original_tick: int | None = None,
) -> list[ReplayInvocation]:
    """Pure: turn worker records into invocations with the new brief.

    If ``fixtures_dir`` is given, looks for a session recording at
    ``{fixtures_dir}/tick-{tick}-issue-{n}.jsonl`` (or, when ``original_tick``
    is None, ``{fixtures_dir}/issue-{n}.jsonl``) and attaches its path. The
    invocation's ``fixture_path`` is None when no recording exists — the
    caller decides whether to live-dispatch or skip.

    ``role`` is captured verbatim for tagging; only ``"worker"`` runs
    actually exist today (PO/critic replay is out of scope per the issue).
    """
    if role != "worker":
        raise ReplayError(
            f"unsupported replay role: {role!r}. Only 'worker' is implemented "
            f"(issue #24 out-of-scope: cross-role replay)."
        )

    invocations: list[ReplayInvocation] = []
    for w in workers:
        fixture: Path | None = None
        if fixtures_dir is not None:
            candidates = (
                [fixtures_dir / f"tick-{original_tick}-issue-{w.issue}.jsonl"]
                if original_tick is not None
                else []
            )
            candidates.append(fixtures_dir / f"issue-{w.issue}.jsonl")
            for cand in candidates:
                if cand.exists():
                    fixture = cand
                    break
        invocations.append(ReplayInvocation(
            issue=w.issue,
            title=w.title,
            role=role,
            brief=new_brief,
            fixture_path=fixture,
        ))
    return invocations


# ---------------------------------------------------------------- fixtures


def replay_from_fixture(invocation: ReplayInvocation) -> DryRunCapture:
    """Replay ``invocation`` from its attached fixture (zero cost).

    Pre: ``invocation.fixture_path`` is set. Raises :class:`ReplayError`
    if the fixture is missing or :class:`FixtureCorruptError` propagates
    from the underlying replayer (the CLI catches this).
    """
    from forge_loop._testing.replayer import (
        FixtureCorruptError,
        SessionReplayer,
    )

    if invocation.fixture_path is None:
        raise ReplayError(
            f"no fixture for issue #{invocation.issue}; cannot replay "
            f"without spawning a live worker"
        )
    try:
        session = SessionReplayer(invocation.fixture_path).load()
    except FixtureCorruptError as exc:
        raise ReplayError(
            f"corrupt recording for issue #{invocation.issue} "
            f"({invocation.fixture_path}): {exc}"
        ) from exc

    diff_text, commit_hash = extract_diff_from_events(session.events)
    return DryRunCapture(
        issue=invocation.issue,
        title=invocation.title or str(session.header.get("title", "")),
        status=session.status,
        commit_hash=commit_hash,
        diff_text=diff_text,
        cost_usd=0.0,
        source="fixture",
        pr_url=session.pr_url,
        event_count=len(session.events),
        fixture_path=str(invocation.fixture_path),
    )


# --------------------------------------------------------------- dry-run brief


_DRY_RUN_BANNER = """\

DRY-RUN MODE (replay):
- DO NOT run `git push`, `gh pr create`, or `gh pr merge`. Replay must
  not mutate origin or open PRs.
- After committing locally, run `git diff origin/trunk -- ':!.claude'` and
  print it as the final result, then run `git rev-parse HEAD` and print
  that on its own line. The replay harness parses both.
- Emit your final JSON with `"status": "dry_run"` and `"pr": null`.
"""


def apply_dry_run_to_brief(brief: str) -> str:
    """Prepend a dry-run banner so the worker captures a diff instead of pushing.

    Idempotent — calling twice leaves a single banner. Kept as a tiny
    helper so unit tests can assert the contract without touching the
    worker module.
    """
    if _DRY_RUN_BANNER.strip() in brief:
        return brief
    return _DRY_RUN_BANNER + "\n" + brief


# ------------------------------------------------------------- tick id + emit


def make_replay_tick_id(tick: int, suffix: str = "r") -> str:
    """Synthetic id for replay output: ``42`` -> ``"42r"``.

    Lets the events log distinguish replay activity from real ticks
    without polluting the integer tick counter (which the runner advances
    monotonically).
    """
    return f"{tick}{suffix}"


def emit_replay_events(
    events_path: Path,
    *,
    original_tick: int,
    replay_tick: str,
    role: str,
    captures: list[DryRunCapture],
) -> None:
    """Append a ``replay_tick_done`` event (+ per-capture events) to the log.

    Every replay event carries ``replay: True`` so downstream consumers
    (status, eventdb) can filter them out of normal-tick aggregates.
    """
    events_path.parent.mkdir(parents=True, exist_ok=True)
    from forge_loop.state import now_iso

    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": now_iso(),
            "kind": "replay_tick_start",
            "replay": True,
            "original_tick": original_tick,
            "replay_tick": replay_tick,
            "role": role,
            "issues": [c.issue for c in captures],
        }) + "\n")
        for c in captures:
            f.write(json.dumps({
                "ts": now_iso(),
                "kind": "replay_worker_done",
                "replay": True,
                "original_tick": original_tick,
                "replay_tick": replay_tick,
                "issue": c.issue,
                "title": c.title,
                "status": c.status,
                "cost_usd": round(c.cost_usd, 6),
                "commit_hash": c.commit_hash,
                "diff_chars": len(c.diff_text),
                "source": c.source,
                "fixture_path": c.fixture_path,
                "error": c.error,
                "pr_url": c.pr_url,
            }, default=str) + "\n")
        f.write(json.dumps({
            "ts": now_iso(),
            "kind": "replay_tick_done",
            "replay": True,
            "original_tick": original_tick,
            "replay_tick": replay_tick,
            "role": role,
            "captures": [asdict(c) for c in captures],
        }, default=str) + "\n")


# --------------------------------------------------------------- orchestrator


@dataclass
class ReplayPlan:
    original_tick: int
    replay_tick: str
    role: str
    workers: list[WorkerRecord]
    invocations: list[ReplayInvocation]
    new_brief: str
    fixtures_dir: Path | None = None
    captures: list[DryRunCapture] = field(default_factory=list)


def plan_replay(
    events_path: Path,
    *,
    tick: int,
    role: str,
    new_brief: str,
    fixtures_dir: Path | None,
    replay_suffix: str = "r",
) -> ReplayPlan:
    """Build a :class:`ReplayPlan` without executing it.

    Kept separate from :func:`run_replay_tick` so the CLI can show
    operators what would happen (and tests can assert the assembled
    invocations) without spawning anything.
    """
    workers = find_tick_workers(events_path, tick)
    if not workers:
        raise ReplayError(
            f"no workers found for tick {tick} in {events_path}. "
            f"(Looked for `tick_done` / `tick_start` events.)"
        )
    invocations = assemble_replay_invocations(
        workers, new_brief, role,
        fixtures_dir=fixtures_dir,
        original_tick=tick,
    )
    return ReplayPlan(
        original_tick=tick,
        replay_tick=make_replay_tick_id(tick, suffix=replay_suffix),
        role=role,
        workers=workers,
        invocations=invocations,
        new_brief=new_brief,
        fixtures_dir=fixtures_dir,
    )


def run_replay_tick(
    plan: ReplayPlan,
    *,
    events_path: Path,
    live_executor: Any = None,  # callable(invocation) -> DryRunCapture; optional
) -> list[DryRunCapture]:
    """Execute the plan. Fixture-backed invocations replay locally;
    others go through ``live_executor`` if provided, or are recorded as
    skipped captures (status=``"skipped_no_fixture"``) otherwise.

    Replay events are appended to ``events_path`` with ``replay=True``.
    """
    captures: list[DryRunCapture] = []
    for inv in plan.invocations:
        if inv.fixture_path is not None:
            cap = replay_from_fixture(inv)
        elif live_executor is not None:
            cap = live_executor(inv)
            if not isinstance(cap, DryRunCapture):
                raise ReplayError(
                    f"live_executor returned {type(cap).__name__}, expected DryRunCapture"
                )
        else:
            cap = DryRunCapture(
                issue=inv.issue,
                title=inv.title,
                status="skipped_no_fixture",
                commit_hash=None,
                diff_text="",
                cost_usd=0.0,
                source="skipped",
                error="no recorded fixture and no live_executor supplied",
            )
        captures.append(cap)

    plan.captures = captures
    emit_replay_events(
        events_path,
        original_tick=plan.original_tick,
        replay_tick=plan.replay_tick,
        role=plan.role,
        captures=captures,
    )
    return captures


# ---------------------------------------------------------------- diff report


@dataclass
class DiffRow:
    issue: int
    title: str
    original_status: str
    replay_status: str
    original_pr_url: str | None
    replay_pr_url: str | None
    original_cost_usd: float
    replay_cost_usd: float
    replay_commit_hash: str | None
    replay_diff_chars: int
    replay_source: str


def build_diff_report(
    events_path: Path,
    *,
    tick: int,
    replay_tick: str,
) -> dict[str, Any]:
    """Build the side-by-side report comparing original tick vs replay tick.

    Reads the events log: pulls original ``tick_done`` outcomes for
    ``tick`` and the ``replay_tick_done`` event for ``replay_tick``.
    Joins them by issue number.

    Raises :class:`ReplayError` if either anchor event is missing — the
    report is meaningless without both sides.
    """
    original = {w.issue: w for w in find_tick_workers(events_path, tick)}
    if not original:
        raise ReplayError(f"no original tick_done found for tick {tick}")

    replay_done: dict[str, Any] | None = None
    for raw in events_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            e = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if (
            e.get("kind") == "replay_tick_done"
            and str(e.get("replay_tick")) == replay_tick
        ):
            replay_done = e  # keep last one (most recent replay re-run)

    if replay_done is None:
        raise ReplayError(
            f"no replay_tick_done event found for replay_tick {replay_tick!r}. "
            f"Did you forget to run `forge-loop replay --tick {tick} ...` first?"
        )

    captures = {int(c["issue"]): c for c in (replay_done.get("captures") or [])}

    rows: list[DiffRow] = []
    for issue, orig in sorted(original.items()):
        c = captures.get(issue)
        rows.append(DiffRow(
            issue=issue,
            title=orig.title,
            original_status=orig.original_status,
            replay_status=str(c.get("status", "missing")) if c else "missing",
            original_pr_url=orig.original_pr_url,
            replay_pr_url=(c.get("pr_url") if c else None),
            original_cost_usd=orig.original_cost_usd,
            replay_cost_usd=float(c.get("cost_usd") or 0.0) if c else 0.0,
            replay_commit_hash=(c.get("commit_hash") if c else None),
            replay_diff_chars=(
                int(c.get("diff_chars") or len(c.get("diff_text") or "")) if c else 0
            ),
            replay_source=str(c.get("source", "missing")) if c else "missing",
        ))

    total_original_cost = sum(r.original_cost_usd for r in rows)
    total_replay_cost = sum(r.replay_cost_usd for r in rows)
    return {
        "original_tick": tick,
        "replay_tick": replay_tick,
        "role": replay_done.get("role"),
        "rows": [asdict(r) for r in rows],
        "totals": {
            "original_cost_usd": round(total_original_cost, 6),
            "replay_cost_usd": round(total_replay_cost, 6),
            "savings_usd": round(total_original_cost - total_replay_cost, 6),
            "issues": len(rows),
        },
    }


def render_diff_report_text(report: dict[str, Any]) -> str:
    """Human-readable rendering of :func:`build_diff_report` output."""
    lines: list[str] = []
    lines.append(
        f"== replay diff: tick {report['original_tick']} → "
        f"{report['replay_tick']} (role={report['role']}) =="
    )
    for r in report["rows"]:
        lines.append(
            f"  #{r['issue']:<5} {r['title'][:48]:<48}  "
            f"orig={r['original_status']:<10}  "
            f"replay={r['replay_status']:<10}  "
            f"cost ${r['original_cost_usd']:.4f} → ${r['replay_cost_usd']:.4f}  "
            f"diff={r['replay_diff_chars']}b  src={r['replay_source']}"
        )
    t = report["totals"]
    lines.append(
        f"  TOTAL  {t['issues']} issue(s)  "
        f"cost ${t['original_cost_usd']:.4f} → ${t['replay_cost_usd']:.4f}  "
        f"(savings ${t['savings_usd']:.4f})"
    )
    return "\n".join(lines) + "\n"
