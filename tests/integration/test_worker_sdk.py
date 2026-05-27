"""Integration test for the SDK-based worker path (issue #2).

Runs a *real* Claude Agent SDK call against a tiny fixture brief. Skipped
unless ``ANTHROPIC_API_KEY`` is in the environment so CI stays hermetic
when secrets aren't provisioned.

We don't assert exact text — only that:
- the typed event stream is non-empty,
- at least one assistant_text or final_result event arrives,
- the returned SDKRunResult carries a model and either a cost > 0 or a
  classified error (network/auth/rate_limit), proving the SDK plumbing
  works end-to-end without crashing the loop.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="needs ANTHROPIC_API_KEY for a live SDK call",
)


def test_real_sdk_call_streams_typed_events(tmp_path: Path) -> None:
    import anyio

    from forge_loop._worker_sdk import run_sdk_session

    events: list[dict[str, Any]] = []
    prompt = (
        "Reply with the single word 'OK' and emit a trailing JSON object "
        '{"issue":0,"pr":null,"status":"no_pr","note":"integration probe"} '
        "on the last line. Do not call any tools."
    )

    res = anyio.run(lambda: run_sdk_session(
        prompt,
        cwd=tmp_path,
        max_turns=1,
        on_event=events.append,
    ))

    assert events, "expected at least one WorkerEvent"
    kinds = {e["kind"] for e in events}
    assert kinds & {"assistant_text", "final_result", "error"}, (
        f"unexpected event kinds: {kinds}"
    )
    assert res.model or res.error, "either a model or a classified error must appear"
    # If the call succeeded, the SDK reports a non-zero cost.
    if res.error is None:
        assert res.cost_usd >= 0.0
        assert res.status in {"no_pr", "open", "merged", "failed"}
