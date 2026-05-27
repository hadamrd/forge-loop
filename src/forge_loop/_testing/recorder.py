"""SessionRecorder — capture a real Claude Agent SDK session to a JSONL fixture.

Recording is OUT-OF-BAND from the production worker path: we shell out to the
`claude` CLI exactly the same way :func:`forge_loop.worker.run_worker` does,
but tee its `--output-format stream-json` stdout into a fixture file instead
of throwing it away after outcome extraction.

Fixture schema (forge-loop-session/v1):

    line 1   : header  {"schema": "forge-loop-session/v1", "issue": int,
                        "title": str, "recorded_at": iso8601,
                        "claude_argv": [..], "duration_s": float}
    line 2..N: events  one stream-json event per line, in capture order,
                       each annotated with a monotonic "seq" key
    line N+1 : trailer {"type": "outcome", "pr": str|null, "status": str,
                        "returncode": int}

The trailer is what makes a fixture *replayable*: SessionReplayer asserts
that feeding the events back through the worker's outcome-extraction path
yields the same (pr, status) pair the recorder observed live.

SECRETS: recordings are NOT auto-redacted. Convention (per issue #9 out-of-
scope note): hand-scrub any PR comments / tool outputs that contain tokens
before committing a fixture. The recorder writes a `# SECRETS` banner in
the header to remind you.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "forge-loop-session/v1"


@dataclass
class RecordingResult:
    fixture_path: Path
    event_count: int
    duration_s: float
    pr_url: str | None
    status: str


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class SessionRecorder:
    """Wraps an invocation of `claude -p` and captures every stream-json event.

    Use via :meth:`record`. The recorder does NOT mutate worker.py — it
    duplicates the subprocess-spawning code on purpose so production stays
    untouched by test machinery.
    """

    def __init__(self, *, issue: dict[str, Any], worktree: Path, brief: str) -> None:
        if "number" not in issue or "title" not in issue:
            raise ValueError("issue must have 'number' and 'title'")
        self._issue = issue
        self._worktree = worktree
        self._brief = brief

    def record(self, fixture_path: Path, *, timeout_s: int = 600) -> RecordingResult:
        from forge_loop.worker import _extract_outcome, _subagent_env

        fixture_path.parent.mkdir(parents=True, exist_ok=True)
        argv = [
            "claude", "-p", self._brief,
            "--max-turns", "120",
            "--allow-dangerously-skip-permissions",
            "--add-dir", str(self._worktree),
            "--output-format", "stream-json",
            "--verbose",
        ]

        scratch_log = fixture_path.with_suffix(".raw.log")
        started = time.time()
        with open(scratch_log, "wb") as logf:
            proc = subprocess.Popen(
                argv, cwd=self._worktree, stdout=logf,
                stderr=subprocess.STDOUT, env=_subagent_env(),
            )
            try:
                proc.wait(timeout=timeout_s)
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                returncode = -1
        duration = time.time() - started

        pr_url, status = _extract_outcome(scratch_log)

        # Re-write as a clean fixture: header, seq-annotated events, trailer.
        header = {
            "schema": SCHEMA_VERSION,
            "issue": int(self._issue["number"]),
            "title": str(self._issue["title"]),
            "recorded_at": _now_iso(),
            "claude_argv": argv[:2] + ["<brief>"] + argv[3:],  # redact brief
            "duration_s": round(duration, 3),
            "_note": "SECRETS not auto-redacted; scrub before committing",
        }
        events: list[dict[str, Any]] = []
        with open(scratch_log, "rb") as f:
            for seq, raw in enumerate(f, start=1):
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                e["seq"] = seq
                events.append(e)

        trailer = {
            "type": "outcome",
            "pr": pr_url,
            "status": status,
            "returncode": returncode,
        }

        with open(fixture_path, "w", encoding="utf-8") as out:
            out.write(json.dumps(header) + "\n")
            for e in events:
                out.write(json.dumps(e) + "\n")
            out.write(json.dumps(trailer) + "\n")

        scratch_log.unlink(missing_ok=True)

        return RecordingResult(
            fixture_path=fixture_path,
            event_count=len(events),
            duration_s=duration,
            pr_url=pr_url,
            status=status,
        )
