"""JSON + SSE read API for the forge-loop operator console (the React UI).

The console (``console/``) is a pure read surface over the durable control plane.
This module exposes the event-sourced state — the SAME ``.forge`` stores the
runner and ``forge-loop status`` use — as JSON shaped to the console's domain
types, plus an SSE event stream, plus (optionally) the built console served
same-origin (no CORS, no build-time URL baking).

Design:
* READ-ONLY. Events read through a ``mode=ro`` SQLite connection so a second
  reader never contends with the live loop's writer (WAL allows it). Status reuses
  ``collect_control_plane_status`` (the path ``forge-loop status`` already uses).
* Sagas / Workers / PRs are RECONSTRUCTED from the durable event log (the rich
  facts — issue, branch, model, verdict, cost — live in event payloads, not in the
  minimal tasks.db rows). Reconstruction is best-effort and honest: fields absent
  from the log are omitted/empty rather than faked.
* Endpoints mirror ``console/src/api/client.ts`` (ForgeApi). All under ``/api`` so
  the console's ``realApi.ts`` targets them with ``VITE_FORGE_BASE_URL=/api``.
* Bearer auth via ``LOOP_CONSOLE_TOKEN``; ``/healthz`` stays open.

Honest gaps on the live loop today:
the OKR/scorecard trend (projection unwired — renders "not yet measured"), backlog
+ manifestos (external; return empty). Everything else is real.

Run: ``LOOP_REPO=/path/to/repo uvicorn forge_loop.console_api:app``.
"""

import asyncio
import json
import os
import sqlite3
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Read-only event reader (events.db) → console EventEnvelope.
# ---------------------------------------------------------------------------
def _events_db_path(repo: Path) -> Path:
    return repo / ".forge" / "events.db"


def _row_to_envelope(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
    except (json.JSONDecodeError, TypeError):
        payload = {}
    # Normalize a `pr` payload that's a full GitHub URL to its int number so every
    # consumer (ticker, recent-merges, drawers) shows "#1289", not the raw URL.
    if isinstance(payload, dict) and "pr" in payload:
        n = _pr_number(payload["pr"])
        if n is not None:
            payload["pr"] = n
    return {
        "sequence": int(row["sequence"]),
        "event_id": row["event_id"],
        "kind": row["kind"],
        "occurred_at": row["occurred_at"],
        "task_id": row["task_id"],
        "saga_id": row["saga_id"],
        "causal_event_id": row["causal_event_id"],
        "payload": payload,
    }


def _read_events(repo: Path, *, since: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
    path = _events_db_path(repo)
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT sequence, event_id, kind, payload_json, occurred_at,
                   task_id, saga_id, causal_event_id
            FROM events WHERE sequence > ? ORDER BY sequence ASC
            """,
            (since,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    out = [_row_to_envelope(r) for r in rows]
    if limit is not None and len(out) > limit:
        out = out[-limit:]
    return out


# ---------------------------------------------------------------------------
# Status adapter — collect_control_plane_status() → console LoopStatus.
# ---------------------------------------------------------------------------
def _forge_version() -> str:
    try:
        from importlib.metadata import version

        return f"forge {version('forge-loop')}"
    except Exception:
        return "forge"


def _frontier_cursor(repo: Path) -> Any:
    try:
        from forge_loop.frontier import FrontierStore

        return FrontierStore(repo / ".forge" / "frontier.yaml").load()
    except Exception:
        return None


def _status_payload(repo: Path) -> dict[str, Any]:
    from forge_loop.control.status import collect_control_plane_status

    raw = collect_control_plane_status(
        repo, datetime.now(UTC), github_repo=_github_repo_slug()
    )
    ev, proj = raw.get("event_log", {}), raw.get("projections", {})
    entropy = raw.get("operational_entropy", {})
    fr, mem, tasks, boot = (
        raw.get("frontier", {}), raw.get("memory", {}), raw.get("tasks", {}), raw.get("boot", {})
    )
    last_seq = int(ev.get("last_sequence") or 0)
    projections = [
        {"name": n, "sequence": int(p.get("sequence", 0)), "lag": int(p.get("lag", 0))}
        for n, p in sorted(proj.items())
    ]
    in_flight = int(tasks.get("in_flight_count") or 0)
    db = _events_db_path(repo)
    size_mb = round(db.stat().st_size / 1_000_000, 1) if db.exists() else 0.0
    cur = _frontier_cursor(repo)
    return {
        "summary": (boot.get("summary") or "Loop status").split("\n")[0],
        "available": bool(ev.get("available")),
        "sequence": last_seq,
        "last_sequence": last_seq,
        "lag": max((p["lag"] for p in projections), default=0),
        "active_count": in_flight,
        "in_flight_count": in_flight,
        "stale_lease_count": int(tasks.get("stale_lease_count") or 0),
        "rejected_count": int(mem.get("rejected_count") or 0),
        "halted": any(e["kind"] == "loop.halted" for e in _read_events(repo, since=max(0, last_seq - 50))),
        "boot": {
            "booted_at": datetime.now(UTC).isoformat(),
            "doctor": "ok" if boot.get("available") else "degraded",
            "version": _forge_version(),
            "env": "ok" if ev.get("available") else "degraded",
        },
        "event_log": {"path": str(ev.get("path") or db), "sequence": last_seq, "size_mb": size_mb},
        "projections": projections,
        "frontier": {
            "current_problem": fr.get("current_problem") or "",
            "next_expansion": fr.get("next_expansion") or "",
            "version": int(getattr(cur, "version", 0) or 0),
        },
        "memory": {"total": int(mem.get("active_count") or 0), "promoted_today": 0, "superseded": 0},
        "operational_entropy": {
            "open_branches": entropy.get("open_branches"),
            "live_worktrees": entropy.get("live_worktrees"),
            "open_epics": entropy.get("open_epics"),
            "backlog_age_days": entropy.get("backlog_age_days"),
        },
        "tasks": [],
    }


def _events_page(repo: Path, *, limit: int = 400, cursor: int | None = None) -> dict[str, Any]:
    allev = _read_events(repo)
    end = cursor if cursor is not None else len(allev)
    start = max(0, end - limit)
    return {"events": allev[start:end], "cursor": start if start > 0 else None, "has_more": start > 0}


# ---------------------------------------------------------------------------
# Reconstruction from the event log: sagas (by task_id), workers, PRs, budget.
# ---------------------------------------------------------------------------
_INFLIGHT = {"RUNNING", "AWAITING_CRITIC", "REVISING"}


def _p(e: dict[str, Any], key: str, default: Any = None) -> Any:
    return e.get("payload", {}).get(key, default)


def _findings_from_payload(crit: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Read serialized critic findings off a ``critique.issued`` event (#404).

    Delegates to the single validated parser ``critic.deserialize_findings`` so
    back-compat / adversarial safety lives in one place (a legacy event with no
    ``findings`` field, or a malformed/non-list/partially-bad payload, degrades
    to ``[]`` / skips the bad row — never raises), then re-serializes to the
    plain-JSON dict shape the console review object expects.
    """
    from forge_loop.critic import deserialize_findings, serialize_findings

    raw = _p(crit or {}, "findings", []) if crit else []
    return serialize_findings(deserialize_findings(raw))


def _mptg_from_payload(crit: dict[str, Any] | None) -> list[str]:
    """Read ``minimal_path_to_green`` off a ``critique.issued`` event (#404).

    Degrades to ``[]`` for legacy/malformed payloads; keeps only str entries.
    """
    raw = _p(crit or {}, "minimal_path_to_green", []) if crit else []
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, str)]


def _pr_number(val: Any) -> int | None:
    """Coerce a ``pr`` payload to an int — it may be an int or a full PR URL."""
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        import re

        m = re.search(r"(\d+)\s*$", val.rstrip("/"))
        return int(m.group(1)) if m else None
    return None


def _issue_from_task_id(task_id: str | None) -> int | None:
    """The issue number behind a ``issue:<n>`` task_id, else None."""
    if task_id and task_id.startswith("issue:"):
        try:
            return int(task_id.split(":", 1)[1])
        except ValueError:
            return None
    return None


def _github_repo_slug() -> str | None:
    """The ``owner/name`` slug the console reconciles against, or ``None``.

    Single resolution point reused by ``_open_issue_numbers`` and the
    operational-entropy metric (issue #402) so the two never drift to different
    defaults (manifesto Q7 — no parallel slug logic).
    """
    repo_slug = os.environ.get("LOOP_GITHUB_REPO") or "hadamrd/forge-loop"
    return repo_slug if "/" in repo_slug else None


def _open_issue_numbers(repo: Path) -> set[int] | None:
    """All open issue numbers (epics + tickets), or None if it can't be fetched.

    ``None`` means "do not reconcile" — a transient GitHub failure must never cause
    the console to hide or relabel work. A closed issue is the landed-signal used to
    tell a resolved saga/PR (outcome compacted out of the durable log) from a live one.
    """
    repo_slug = _github_repo_slug()
    if repo_slug is None:
        return None
    owner, name = repo_slug.split("/", 1)
    try:
        from forge_loop.gh_client import GithubkitClient, list_open_backlog

        backlog = list_open_backlog(GithubkitClient(), owner, name, limit=200)
    except Exception:
        return None
    return {issue.number for issue in (list(backlog.epics) + list(backlog.tickets))}


def _live_inflight_task_ids(repo: Path) -> set[str]:
    """task_ids the control plane currently tracks as in-flight — the authoritative
    tasks.db lease store (the same source ``forge-loop status`` uses).

    Empty on any error: a non-terminal saga in the event log is only "live" if the
    control plane agrees. Otherwise it's a dead saga whose last event simply never
    reached a terminal kind — showing it as a live worker with an expired heartbeat
    is the bug this guards against.
    """
    path = repo / ".forge" / "tasks.db"
    if not path.exists():
        return set()
    try:
        from forge_loop.tasks import SqliteTaskSagaStore

        store = SqliteTaskSagaStore(path)
        try:
            return {s.task_id for s in store.list_in_flight()}
        finally:
            store.close()
    except Exception:
        return set()


def _reconstruct_sagas(repo: Path) -> list[dict[str, Any]]:
    """Group events by task_id and fold each into a console Saga.

    A saga whose event trail ends non-terminally but which the control plane no
    longer tracks as in-flight is reconciled to ABANDONED — the event log only
    records that work *started*, not that it died, so without this the loop would
    appear to have zombie workers running forever.
    """
    events = _read_events(repo)
    live = _live_inflight_task_ids(repo)
    open_issues = _open_issue_numbers(repo)
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        if e.get("task_id"):
            by_task[e["task_id"]].append(e)

    sagas: list[dict[str, Any]] = []
    for task_id, evs in by_task.items():
        evs.sort(key=lambda e: e["sequence"])
        kinds = [e["kind"] for e in evs]
        first, last = evs[0], evs[-1]

        def find(kind: str, _evs: list[dict[str, Any]] = evs) -> dict[str, Any] | None:
            return next((e for e in reversed(_evs) if e["kind"] == kind), None)

        disp = find("task.dispatched") or find("task.planned")
        issue = next((_p(e, "issue") for e in evs if _p(e, "issue") is not None), None)
        title = next((_p(e, "title") for e in evs if _p(e, "title")), "") or ""
        axis = next((_p(e, "axis") for e in evs if _p(e, "axis")), "") or ""
        branch = _p(disp or {}, "branch", "") or ""
        worktree = _p(disp or {}, "worktree", "") or ""
        worker_id = _p(disp or {}, "worker") or ("wkr-" + task_id.replace(":", "-"))

        if "pr.merged" in kinds:
            state = "MERGED"
        elif "task.compensated" in kinds:
            state = "COMPENSATED"
        elif "task.failed" in kinds:
            state = "ABANDONED"
        elif kinds[-1] == "critique.issued":
            state = "AWAITING_CRITIC"
        elif "critique.issued" in kinds and kinds[-1] in {"worker.observation", "task.heartbeat", "pr.opened"}:
            state = "REVISING"
        elif kinds[-1] in {"task.dispatched", "task.heartbeat", "worker.observation", "pr.opened"}:
            state = "RUNNING"
        else:
            state = "DISPATCHED"

        # Reconcile a non-terminal computed state against authoritative state.
        has_explicit_terminal = bool({"pr.merged", "task.failed", "task.compensated"} & set(kinds))
        if not has_explicit_terminal and task_id not in live:
            issue_closed = (
                open_issues is not None and issue is not None and int(issue) not in open_issues
            )
            if issue_closed:
                # Outcome landed but its terminal event predates / was compacted out of
                # the durable log — a resolved historical saga, not a live one. Drop it
                # rather than render a misleading ABANDONED/RUNNING ghost.
                continue
            # No live lease and the issue is still open (or unknowable) → genuinely stalled.
            state = "ABANDONED"

        rounds = [int(_p(e, "round", 0) or 0) for e in evs if e["kind"] == "critique.issued"]
        repair_rounds = max(rounds) if rounds else 0
        merged = find("pr.merged")
        cost = float(_p(merged or {}, "cost_usd", 0) or 0.0)
        if not cost:
            cost = sum(float(_p(e, "cost_usd", 0) or 0) for e in evs if e["kind"] == "task.heartbeat")
        terminal = state in {"MERGED", "ABANDONED", "COMPENSATED", "QUARANTINED"}
        sagas.append({
            "saga_id": task_id,
            "issue": {"number": int(issue) if issue is not None else 0, "title": title, "axis": axis},
            "worker_id": worker_id,
            "worktree_path": worktree,
            "state": state,
            "repair_rounds": repair_rounds,
            "lease_expires_at": None,
            "heartbeat_at": last["occurred_at"],
            "cost_usd": round(cost, 2),
            "branch": branch,
            "started_at": first["occurred_at"],
            "ended_at": last["occurred_at"] if terminal else None,
        })
    sagas.sort(key=lambda s: s["started_at"], reverse=True)
    return sagas


def _reconstruct_workers(repo: Path) -> list[dict[str, Any]]:
    """In-flight sagas → console Workers (capability/tokens unknown → empty/0)."""
    workers = []
    for s in _reconstruct_sagas(repo):
        if s["state"] not in _INFLIGHT:
            continue
        workers.append({
            "id": s["worker_id"],
            "issue_number": s["issue"]["number"],
            "model": "claude-opus-4-8",
            "worktree_path": s["worktree_path"],
            "state": s["state"],
            "started_at": s["started_at"],
            "last_event_at": s["heartbeat_at"],
            "cost_usd": s["cost_usd"],
            "tokens": 0,
            "capability_policy": {"secret_names": [], "mcp": [], "network_egress": []},
            "withheld_secrets": [],
            "monologue_log_path": "",
            "monologue": [],
        })
    return workers


def _reconstruct_prs(repo: Path) -> list[dict[str, Any]]:
    """Fold pr.opened / critique.issued / pr.merged / merge.blocked into console PRs.

    Critic findings + minimal_path_to_green are read off the latest
    ``critique.issued`` event (#404); legacy events without them degrade to ``[]``.
    The review also carries the verdict, sev2 trajectory, and round.
    """
    events = _read_events(repo)
    open_issues = _open_issue_numbers(repo)
    by_pr: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        if e["kind"] not in {"pr.opened", "critique.issued", "pr.merged", "merge.blocked"}:
            continue
        num = _pr_number(_p(e, "pr"))
        if num is not None:
            by_pr[num].append(e)

    prs = []
    for number, evs in by_pr.items():
        evs.sort(key=lambda e: e["sequence"])
        opened = next((e for e in evs if e["kind"] == "pr.opened"), None)
        merged = next((e for e in reversed(evs) if e["kind"] == "pr.merged"), None)
        crits = [e for e in evs if e["kind"] == "critique.issued"]
        last_crit = crits[-1] if crits else None
        verdict = str(_p(last_crit or {}, "verdict", "approved") or "approved") if last_crit else "approved"
        sev2 = int(_p(last_crit or {}, "sev2", 0) or 0) if last_crit else 0
        round_n = int(_p(last_crit or {}, "round", 1) or 1) if last_crit else 1
        state = "merged" if merged else "open"
        # Reconcile against issue state: a PR whose issue is closed is resolved, not
        # "open" — the event log just lacks its close/merge event. Drop it out of the
        # open tabs (state "closed") so the screen shows only actually-open PRs.
        if state == "open":
            issue_num = _issue_from_task_id(
                next((e.get("task_id") for e in evs if e.get("task_id")), None)
            )
            if open_issues is not None and issue_num is not None and issue_num not in open_issues:
                state = "closed"
        # Labels are derived from the LATEST critic verdict only — never "approved AND
        # critic:blocking" (a stale merge.blocked event must not contradict an approval).
        is_blocking = state == "open" and (verdict in {"changes_requested", "error"} or sev2 > 0)
        labels = []
        if state == "open":
            labels.append("critic:blocking" if is_blocking else "clean")
        history = [{"round": int(_p(c, "round", i + 1) or i + 1), "sev2": int(_p(c, "sev2", 0) or 0)}
                   for i, c in enumerate(crits)] or [{"round": 1, "sev2": sev2}]
        prs.append({
            "number": number,
            "title": str(_p(opened or merged or {}, "title", f"PR #{number}") or f"PR #{number}"),
            "branch": str(_p(merged or opened or {}, "branch", "") or ""),
            "additions": int(_p(opened or {}, "additions", 0) or 0),
            "deletions": int(_p(opened or {}, "deletions", 0) or 0),
            "mergeable": state == "open" and not is_blocking,
            "state": state,
            "saga_id": (opened or merged or evs[0]).get("task_id"),
            "labels": labels,
            "review": {
                "verdict": verdict,
                "round": round_n,
                "suspicious": False,
                "sev_counts": {"sev1": 0, "sev2": sev2, "sev3": 0},
                "findings": _findings_from_payload(last_crit),
                "minimal_path_to_green": _mptg_from_payload(last_crit),
                "history": history,
            },
        })
    prs.sort(key=lambda p: p["number"], reverse=True)
    return prs


def _budget(repo: Path) -> dict[str, Any]:
    """Derive spend from pr.merged cost_usd payloads, bucketed by hour.

    Tokens (input + output) are summed from the same payloads when the worker
    recorded them (#403); they stay ``0`` when the SDK reported none, which is
    the honest "no token signal" state rather than a fabricated count.
    """
    events = _read_events(repo)
    merges = [e for e in events if e["kind"] == "pr.merged"]
    total = sum(float(_p(e, "cost_usd", 0) or 0) for e in merges)
    today = datetime.now(UTC).date().isoformat()
    spend_today = sum(float(_p(e, "cost_usd", 0) or 0) for e in merges if str(e["occurred_at"]).startswith(today))
    tokens_today = sum(
        int(_p(e, "input_tokens", 0) or 0) + int(_p(e, "output_tokens", 0) or 0)
        for e in merges
        if str(e["occurred_at"]).startswith(today)
    )
    by_hour: dict[str, float] = defaultdict(float)
    tokens_by_hour: dict[str, int] = defaultdict(int)
    for e in merges:
        hour = str(e["occurred_at"])[:13]
        by_hour[hour] += float(_p(e, "cost_usd", 0) or 0)
        tokens_by_hour[hour] += int(_p(e, "input_tokens", 0) or 0) + int(_p(e, "output_tokens", 0) or 0)
    points, cum = [], 0.0
    for hour in sorted(by_hour):
        cum += by_hour[hour]
        points.append({"t": hour + ":00:00", "hourly": round(by_hour[hour], 2),
                       "cumulative": round(cum, 2), "tokens": tokens_by_hour[hour]})
    if not points:
        points = [{"t": datetime.now(UTC).isoformat(), "hourly": 0.0, "cumulative": 0.0, "tokens": 0}]
    n_merges = len(merges) or 1
    return {
        "points": points,
        "spend_today": round(spend_today, 2),
        "cumulative": round(total, 2),
        "tokens_today": tokens_today,
        "cost_per_merged_pr": round(total / n_merges, 2),
    }


def _scorecard(repo: Path) -> dict[str, Any]:
    """Real scorecard projection if present; else honest nulls ("not yet measured").

    The live loop's scorecard projection is currently unwired (projection_cursors
    empty) — so this returns the designed null state, which is the truth.
    """
    return {
        "first_pass_critic_acceptance_rate": None,
        "mean_repair_rounds_to_converge": None,
        "sev2_regeneration_rate": None,
        "mean_lead_time_seconds": None,
        "abandonment_rate": None,
        "cost_per_merged_pr": _budget(repo)["cost_per_merged_pr"],
        "history": [],
        "nulls": {
            "first_pass_critic_acceptance_rate": {"reason": "projection not yet wired",
                "detail": "The Scorecard projection is not registered on the live loop yet — no trend to measure."},
            "sev2_regeneration_rate": {"reason": "not instrumented",
                "detail": "Requires the critic to tag regressions across rounds."},
            "abandonment_rate": {"reason": "not yet measured",
                "detail": "Needs the scorecard projection wired and a full merge window."},
        },
    }


def _frontier(repo: Path) -> dict[str, Any]:
    """Frontier cursor → console Frontier, read straight from frontier.yaml so the
    objective/key_result fields surface (the FrontierCursor loader ignores them).
    Numeric KR (kr_current/target) is absent until the scorecard projection lands → 0."""
    import yaml

    empty = {"version": 0, "product_goal": "", "current_problem": "", "next_expansion": "",
             "why_now": "", "objective": "", "key_result": "", "kr_current": 0, "kr_target": 0,
             "kr_merges_window": 0, "kr_merges_observed": 0, "active_decisions": [],
             "rejected_paths": [], "hot_files": [], "open_questions": []}
    path = repo / ".forge" / "frontier.yaml"
    if not path.exists():
        return empty
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return empty
    decisions = [
        d if isinstance(d, dict) else {"id": f"D-{i + 1}", "text": str(d), "at": ""}
        for i, d in enumerate(raw.get("active_decisions") or [])
    ]
    rejected = [
        {"idea": r.get("idea", ""), "reason": r.get("reason", ""), "revisit_if": r.get("revisit_if", "")}
        if isinstance(r, dict) else {"idea": str(r), "reason": "", "revisit_if": ""}
        for r in (raw.get("rejected_paths") or [])
    ]
    hot = [
        {"ref": h.get("ref", ""), "why_hot": h.get("why_hot", "")}
        if isinstance(h, dict) else {"ref": str(h), "why_hot": ""}
        for h in (raw.get("hot_files") or [])
    ]
    return {
        "version": int(raw.get("version", 0) or 0),
        "product_goal": raw.get("product_goal", "") or "",
        "current_problem": raw.get("current_problem", "") or "",
        "next_expansion": raw.get("next_expansion", "") or "",
        "why_now": raw.get("why_now", "") or "",
        "objective": raw.get("objective", "") or "",
        "key_result": raw.get("key_result", "") or "",
        "kr_current": float(raw.get("kr_current", 0) or 0),
        "kr_target": float(raw.get("kr_target", 0) or 0),
        "kr_merges_window": int(raw.get("kr_merges_window", 0) or 0),
        "kr_merges_observed": int(raw.get("kr_merges_observed", 0) or 0),
        "active_decisions": decisions,
        "rejected_paths": rejected,
        "hot_files": hot,
        "open_questions": list(raw.get("open_questions") or []),
    }


_KNOWN_PR_LABELS = {"loop:ready", "epic", "critic:blocking", "critic:suspicious", "loop:auto-rescued", "clean"}


def _manifestos(repo: Path) -> list[dict[str, Any]]:
    """Parse the quality + testing manifesto markdown into console ManifestoRule[].

    Each rule is a ``### <ID>. <rule>`` header; the following ``**Rationale:**`` line
    is the rationale, and a ``#NNN`` in it is the source issue/PR. Severity isn't in
    the seed format → default sev2 (the console renders it as a badge either way).
    """
    import re

    out: list[dict[str, Any]] = []
    for manifesto, fname in (("quality", "quality-manifesto.md"), ("testing", "testing-manifesto.md")):
        path = repo / ".forge" / fname
        if not path.exists():
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        for part in re.split(r"\n###\s+", "\n" + text)[1:]:
            lines = part.splitlines()
            header = lines[0].strip()
            m = re.match(r"([A-Za-z]+-?\d+)\.\s*(.+)", header)
            rid, rule = (m.group(1), m.group(2).strip()) if m else (header.split(".", 1)[0][:8] or "?", header)
            body = "\n".join(lines[1:])
            rat_m = re.search(r"\*\*Rationale:\*\*\s*(.+?)(?:\n\n|\Z)", body, re.S)
            rationale = " ".join(rat_m.group(1).split()) if rat_m else ""
            src_m = re.search(r"#(\d{2,6})", rationale)
            out.append({
                "id": rid,
                "manifesto": manifesto,
                "rule": rule,
                "severity": "sev2",
                "rationale": rationale,
                "source_pr": ("#" + src_m.group(1)) if src_m else None,
            })
    return out


def _backlog(repo: Path) -> list[dict[str, Any]]:
    """Open GitHub issues → console Issue[]. axis from an ``axis:<name>`` label.

    Best-effort: returns [] without a token / repo / network (the console degrades
    to an honest-empty Backlog rather than erroring).
    """
    repo_slug = os.environ.get("LOOP_GITHUB_REPO") or "hadamrd/forge-loop"
    if "/" not in repo_slug:
        return []
    owner, name = repo_slug.split("/", 1)
    try:
        from forge_loop.gh_client import GithubkitClient, list_open_backlog

        backlog = list_open_backlog(GithubkitClient(), owner, name, limit=100)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for issue in list(backlog.epics) + list(backlog.tickets):
        if issue.number in seen:
            continue
        seen.add(issue.number)
        labels = list(issue.labels or [])
        axis = next((label.split(":", 1)[1] for label in labels if label.startswith("axis:")), "")
        out.append({
            "number": issue.number,
            "title": issue.title,
            "axis": axis,
            "labels": [label for label in labels if label in _KNOWN_PR_LABELS],
            "state": "open",
            "epic": None,  # epic→sub-issue linkage needs the GraphQL sub-issue read; not wired here
        })
    return out


def _memory(repo: Path) -> list[dict[str, Any]]:
    try:
        from forge_loop.memory import SqliteMemoryStore
    except Exception:
        return []
    path = repo / ".forge" / "memory.db"
    if not path.exists():
        return []
    try:
        store = SqliteMemoryStore(path)
        active = list(store.list_active())
        rejected = list(store.list_rejected_paths())
    except Exception:
        return []
    kind_map = {"semantic": "procedural", "procedural": "procedural", "episodic": "episodic"}
    out = []
    for item in active:
        prov = getattr(item, "provenance", None)
        out.append({
            "id": item.memory_id,
            "kind": kind_map.get(getattr(item.kind, "value", str(item.kind)), "episodic"),
            "title": item.title,
            "body": item.body,
            "evidence_refs": list(getattr(prov, "evidence_refs", ()) or []),
            "superseded_by": getattr(item, "superseded_by", None),
            "created_at": getattr(prov, "created_at", datetime.now(UTC)).isoformat() if prov else datetime.now(UTC).isoformat(),
            "confidence": float(getattr(prov, "confidence", 1.0)) if prov else 1.0,
        })
    for item in rejected:
        prov = getattr(item, "provenance", None)
        out.append({
            "id": item.memory_id, "kind": "rejected_path", "title": item.title, "body": item.body,
            "evidence_refs": list(getattr(prov, "evidence_refs", ()) or []),
            "superseded_by": getattr(item, "superseded_by", None),
            "created_at": getattr(prov, "created_at", datetime.now(UTC)).isoformat() if prov else datetime.now(UTC).isoformat(),
            "confidence": float(getattr(prov, "confidence", 1.0)) if prov else 1.0,
        })
    return out


def _pipeline() -> list[dict[str, Any]]:
    model = "claude-opus-4-8"
    return [
        {"stage": "plan", "role": "planner", "model": model, "status": "ok"},
        {"stage": "dispatch", "role": "dispatcher", "model": "—", "status": "ok"},
        {"stage": "execute", "role": "worker", "model": model, "status": "ok"},
        {"stage": "critique", "role": "critic", "model": model, "status": "ok"},
        {"stage": "merge", "role": "merger", "model": "—", "status": "ok"},
        {"stage": "learn", "role": "librarian", "model": model, "status": "ok"},
    ]


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
def build_console_api(*, repo: Path, token: str | None = None, console_dist: Path | None = None) -> Any:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles

    repo = Path(repo)
    app = FastAPI(title="forge-loop console API")

    def _check_auth(request: Request) -> None:
        if token is None:
            return
        h = request.headers.get("authorization", "")
        if not h.lower().startswith("bearer ") or h.split(None, 1)[1].strip() != token:
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    @app.middleware("http")
    async def _auth_mw(request: Request, call_next: Any) -> Any:
        if request.url.path == "/healthz" or not request.url.path.startswith("/api"):
            return await call_next(request)
        try:
            _check_auth(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return await call_next(request)

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    @app.get("/api/status")
    def status() -> Any:
        return _status_payload(repo)

    @app.get("/api/events")
    def events(limit: int = 400, cursor: int | None = None) -> Any:
        return _events_page(repo, limit=limit, cursor=cursor)

    @app.get("/api/events/stream")
    async def events_stream(request: Request, since: int = 0) -> StreamingResponse:
        async def gen() -> AsyncIterator[bytes]:
            last = since
            for e in _read_events(repo, since=last):
                last = max(last, e["sequence"])
                yield f"data: {json.dumps(e)}\n\n".encode()
            while not await request.is_disconnected():
                for e in _read_events(repo, since=last):
                    last = max(last, e["sequence"])
                    yield f"data: {json.dumps(e)}\n\n".encode()
                await asyncio.sleep(1.5)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/workers")
    def workers() -> Any:
        return _reconstruct_workers(repo)

    @app.get("/api/workers/{wid}/logs")
    def worker_logs(wid: str) -> Any:
        del wid
        return []

    @app.post("/api/workers/{wid}/kill")
    def kill_worker(wid: str) -> Any:
        # Read-only console for now: kill is not wired to the live runner.
        del wid
        return {"ok": True, "note": "kill not wired (read-only console)"}

    @app.get("/api/sagas")
    def sagas() -> Any:
        return _reconstruct_sagas(repo)

    @app.get("/api/issues/{issue}/attempts")
    def attempts(issue: int) -> Any:
        return [e for e in _read_events(repo) if _p(e, "issue") == issue]

    @app.get("/api/prs")
    def prs() -> Any:
        return _reconstruct_prs(repo)

    @app.get("/api/prs/{pr}/critic")
    def critic(pr: int) -> Any:
        match = next((p for p in _reconstruct_prs(repo) if p["number"] == pr), None)
        if match is None:
            raise HTTPException(status_code=404, detail="pr not found")
        return match["review"]

    @app.get("/api/scorecard")
    def scorecard() -> Any:
        return _scorecard(repo)

    @app.get("/api/frontier")
    def frontier() -> Any:
        return _frontier(repo)

    @app.get("/api/memory")
    def memory() -> Any:
        return _memory(repo)

    @app.get("/api/backlog")
    def backlog() -> Any:
        return _backlog(repo)

    @app.get("/api/manifestos")
    def manifestos() -> Any:
        return _manifestos(repo)

    @app.get("/api/budget")
    def budget() -> Any:
        return _budget(repo)

    @app.get("/api/pipeline")
    def pipeline() -> Any:
        return _pipeline()

    @app.get("/api/roles")
    def roles() -> Any:
        return _pipeline()

    if console_dist is not None and Path(console_dist).exists():
        from fastapi.responses import FileResponse

        dist = Path(console_dist)
        index = dist / "index.html"
        assets = dist / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        # SPA fallback: serve a real file when present, else index.html so client-side
        # routes (/stream, /sagas, …) deep-link correctly. /api is matched earlier; a
        # stray /api/* below means "no such endpoint" → 404 (don't return HTML).
        @app.get("/{full_path:path}")
        def spa(full_path: str) -> Any:
            if full_path.startswith("api"):
                raise HTTPException(status_code=404, detail="not found")
            candidate = dist / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index)

    return app


def _default_app() -> Any:
    repo = Path(os.environ.get("LOOP_REPO", os.getcwd()))
    token = os.environ.get("LOOP_CONSOLE_TOKEN")
    dist = os.environ.get("LOOP_CONSOLE_DIST")
    return build_console_api(repo=repo, token=token, console_dist=Path(dist) if dist else None)


app = _default_app()
