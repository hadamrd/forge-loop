"""Backlog-maintenance tick — the AI-as-PM subagent.

Every Nth tick (per ``Config.maintenance_every_n_ticks``) we skip the normal
issue-dispatch path and instead spawn a single ``claude -p`` worker with the
maintenance brief. The brief instructs it to triage / retitle / dedupe / close
stale items via ``gh`` calls.

Why a subagent (vs builtin Python logic): the judgment calls (is this issue
stale? is THIS the canonical dupe?) are LLM-shaped. Keep the deterministic
plumbing (subprocess, log capture, outcome parsing) in Python; let the LLM
do the qualitative work.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge_loop.events import read_events
from forge_loop.worker import _subagent_env, ensure_subagent_trusted

DEFAULT_BRIEF = """You are the backlog-maintenance subagent for the forge-loop sprint loop.

Your job is to keep the loop FED. The worker pool starves if `loop:ready` is empty.
A `acted_on: 0` outcome is a FAILURE of this role — you must always act.

STEP 1 — measure the queue
  Run: gh issue list --label "loop:ready" --state open --json number --limit 50
  Note the count (call it READY_COUNT).

STEP 2 — if READY_COUNT < 3, you MUST add at least (3 - READY_COUNT) issues
List candidate issues:
  gh issue list --state open --limit 50 --json number,title,body,labels,createdAt,updatedAt
For each issue, evaluate:
  - Is the title clear ("area(scope): action" or close)? If not → retitle via `gh issue edit`.
  - Is the body well-formed (problem statement + acceptance / hints)? If empty → SKIP for ready, add `loop:triage`.
  - Is the scope small (single-file, single-test, mirror of an existing pattern)? Prefer these.
  - Is it labeled `loop:blocked` or `epic`? SKIP.
  - Does it touch infra (k8s, helm, registry, terraform)? SKIP for now — workers can't deploy.
Pick the best (3 - READY_COUNT) candidates and `gh issue edit <N> --add-label "loop:ready"`.
These count as added_ready.

STEP 3 — close obvious dupes (≤3 per run)
For pairs with ≥80% title match, close the OLDER:
  `gh issue close <older-N> --comment "Duplicate of #<newer>. Closing."`

STEP 4 — retitle ≤3 bad titles (vague, all-caps, "fix bug", "issue", etc.)
  `gh issue edit <N> --title "area(scope): action"`

STEP 5 — final accountability line (one JSON object, NO prose after):
  {"acted_on": <N>, "added_ready": [<numbers>], "closed_dupes": [<numbers>], "retitled": [<numbers>], "ready_count_before": <X>, "ready_count_after": <Y>}

CRITICAL:
- If READY_COUNT >= 3 you may still groom (close 1-3 dupes / retitle 1-3) but
  added_ready can be empty.
- If READY_COUNT < 3 AND you return added_ready=[], that's a hard failure.
  Pick SOMETHING — even an imperfect candidate. The loop self-heals via the
  worker's "blocked" outcome path; a marginally-bad pick is recoverable.
  An empty queue is not.
- Do NOT create code PRs. Do NOT touch open PRs.
- Cap total actions at 12 per run (be decisive, not exhaustive)."""


@dataclass
class MaintenanceOutcome:
    duration_s: float
    acted_on: int
    added_ready: list[int]
    closed_dupes: list[int]
    retitled: list[int]
    raw: dict[str, Any]
    stdout_tail: str


def run_maintenance(
    repo: Path,
    logs_dir: Path,
    timeout_s: int = 1800,
    brief: str = DEFAULT_BRIEF,
) -> MaintenanceOutcome:
    """Spawn a single claude-code subagent with the maintenance brief."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    ensure_subagent_trusted(repo)
    log_path = logs_dir / f"maintenance-{int(time.time())}.log"
    started = time.time()

    try:
        with open(log_path, "wb") as logf:
            subprocess.run(
                [
                    "claude", "-p", brief,
                    "--max-turns", "30",
                    "--allow-dangerously-skip-permissions",
                    "--add-dir", str(repo),
                    "--output-format", "stream-json",
                    "--verbose",
                ],
                cwd=repo,
                stdout=logf,
                stderr=subprocess.STDOUT,
                timeout=timeout_s,
                env=_subagent_env(),
            )
    except subprocess.TimeoutExpired:
        return MaintenanceOutcome(
            duration_s=time.time() - started,
            acted_on=0, added_ready=[], closed_dupes=[], retitled=[],
            raw={"error": "timeout"}, stdout_tail="(timeout)",
        )

    duration = time.time() - started
    parsed = _parse_outcome(log_path)
    return MaintenanceOutcome(
        duration_s=duration,
        acted_on=int(parsed.get("acted_on", 0)),
        added_ready=parsed.get("added_ready", []) or [],
        closed_dupes=parsed.get("closed_dupes", []) or [],
        retitled=parsed.get("retitled", []) or [],
        raw=parsed,
        stdout_tail=_tail(log_path, 800),
    )


def _parse_outcome(log_path: Path) -> dict[str, Any]:
    """Pull the final JSON object from claude's last `result` event."""
    last = ""
    for e in read_events(log_path):
        if e.get("type") == "result":
            last = e.get("result", "") or ""

    for chunk in reversed(last.strip().splitlines()):
        chunk = chunk.strip()
        if chunk.startswith("{") and chunk.endswith("}"):
            try:
                obj: dict[str, Any] = json.loads(chunk)
                return obj
            except json.JSONDecodeError:
                continue

    # Fallback: regex an "acted_on" count from anywhere
    m = re.search(r"acted_on[\"\s:]+(\d+)", last)
    if m:
        return {"acted_on": int(m.group(1))}
    return {"acted_on": 0}


def _tail(path: Path, n: int) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
