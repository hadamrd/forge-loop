"""Tests for worker.py — outcome parsing + branch naming + brief shape.

The subprocess call to `claude -p` is NOT exercised here (that's an
integration / smoke concern). Focus on the deterministic parsing logic.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from forge_loop import _worker_sdk
from forge_loop.worker import (
    _branch_name,
    _extract_outcome,
    _prep_worktree,
    _read_subagent_events,
    _tail,
    make_brief,
    make_repair_brief,
    run_repair_worker,
    run_worker,
)


def test_branch_name_slugifies_title() -> None:
    b = _branch_name(942, "fix(api): webhook HMAC fail-closed on blank secret")
    assert b.startswith("loop/942-")
    assert "fix" in b
    assert "api" in b
    assert " " not in b
    # ≤40-char suffix after the issue number
    suffix = b.split("-", 1)[1]
    assert len(suffix) <= 50


def test_branch_name_empty_title_falls_back() -> None:
    assert _branch_name(1, "") == "loop/1-fix"
    assert _branch_name(2, "!!!") == "loop/2-fix"


def test_make_brief_includes_issue_number_and_body(tmp_path: Path) -> None:
    issue = {"number": 947, "title": "feat(pdl): onFailure", "body": "Some body text"}
    brief = make_brief(issue, tmp_path / "wt-947")
    assert "#947" in brief
    assert "feat(pdl): onFailure" in brief
    assert "Some body text" in brief
    assert str(tmp_path / "wt-947") in brief
    assert "CONTRACT" in brief


def test_make_brief_caps_body_at_6000_chars(tmp_path: Path) -> None:
    long_body = "a" * 10000
    brief = make_brief({"number": 1, "title": "x", "body": long_body}, tmp_path / "w")
    assert brief.count("a") <= 6500  # body truncation in effect


def _write_stream_log(path: Path, result_text: str) -> None:
    """Synthesise a claude stream-json log with a single `result` event."""
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": "thinking"},
        {"type": "result", "subtype": "success", "result": result_text},
    ]
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def test_extract_outcome_parses_trailing_json_object(tmp_path: Path) -> None:
    log = tmp_path / "w.log"
    _write_stream_log(
        log,
        'Some narrative...\n{"issue": 933, "pr": "https://github.com/h/r/pull/942", '
        '"status": "merged", "note": "shipped"}',
    )
    pr, status = _extract_outcome(log)
    assert pr == "https://github.com/h/r/pull/942"
    assert status == "merged"


def test_extract_outcome_regex_fallback_when_no_trailing_json(tmp_path: Path) -> None:
    log = tmp_path / "w.log"
    _write_stream_log(log, "PR opened: https://github.com/foo/bar/pull/123 ready for review")
    pr, status = _extract_outcome(log)
    assert pr == "https://github.com/foo/bar/pull/123"
    assert status == "open"


def test_extract_outcome_empty_result_returns_no_pr(tmp_path: Path) -> None:
    log = tmp_path / "w.log"
    _write_stream_log(log, "")
    pr, status = _extract_outcome(log)
    assert pr is None
    assert status == "no_pr"


def test_extract_outcome_picks_last_json_when_multiple_present(tmp_path: Path) -> None:
    log = tmp_path / "w.log"
    _write_stream_log(
        log,
        '{"issue": 1, "pr": "https://github.com/a/b/pull/1", "status": "open"}\n'
        '{"issue": 1, "pr": "https://github.com/a/b/pull/2", "status": "merged"}',
    )
    pr, status = _extract_outcome(log)
    assert pr == "https://github.com/a/b/pull/2"
    assert status == "merged"


def test_extract_outcome_handles_bad_json_gracefully(tmp_path: Path) -> None:
    log = tmp_path / "w.log"
    _write_stream_log(log, "{this isn't json}\n{also broken")
    pr, status = _extract_outcome(log)
    assert pr is None
    assert status == "no_pr"


def test_tail_reads_last_n_bytes(tmp_path: Path) -> None:
    p = tmp_path / "log"
    p.write_text("a" * 1000 + "Z")
    assert _tail(p, 5).endswith("Z")
    assert len(_tail(p, 5)) == 5


def test_tail_missing_file_returns_empty(tmp_path: Path) -> None:
    assert _tail(tmp_path / "nope", 100) == ""


def test_read_subagent_events_parses_jsonl(tmp_path: Path) -> None:
    (tmp_path / "sprint-events.jsonl").write_text(
        '{"ts":"2026-01-01T00:00:00Z","kind":"bug_found","detail":"X"}\n'
        '{"ts":"2026-01-01T00:00:05Z","kind":"pr_opened","url":"https://github.com/h/r/pull/1"}\n'
        "not-json-line\n"
        '{"ts":"2026-01-01T00:00:10Z","kind":"merged"}\n'
    )
    events = _read_subagent_events(tmp_path)
    assert len(events) == 3  # bad line skipped
    assert events[0]["kind"] == "bug_found"
    assert events[1]["url"] == "https://github.com/h/r/pull/1"
    assert events[2]["kind"] == "merged"


def test_read_subagent_events_no_file_returns_empty(tmp_path: Path) -> None:
    assert _read_subagent_events(tmp_path) == []


def test_make_brief_includes_history_section_when_past_attempts(tmp_path: Path) -> None:
    issue = {"number": 942, "title": "fix x", "body": "Do the thing."}
    past = [
        {"ts": "2026-05-26T10:00:00Z", "status": "failed", "note": "test missing", "pr_url": None},
        {
            "ts": "2026-05-26T11:00:00Z",
            "status": "merged",
            "note": "shipped",
            "pr_url": "https://github.com/h/r/pull/9",
        },
    ]
    brief = make_brief(issue, tmp_path / "w", past_attempts=past)
    assert "PREVIOUS ATTEMPTS" in brief
    assert "test missing" in brief
    assert "https://github.com/h/r/pull/9" in brief


def test_make_brief_no_history_section_when_empty(tmp_path: Path) -> None:
    issue = {"number": 942, "title": "fix x", "body": ""}
    brief = make_brief(issue, tmp_path / "w", past_attempts=[])
    assert "PREVIOUS ATTEMPTS" not in brief


def test_make_brief_risk_gated_disables_automerge(tmp_path: Path) -> None:
    issue = {"number": 942, "title": "fix x", "body": ""}
    brief = make_brief(issue, tmp_path / "w", risk_gated=True)
    assert "DO NOT enable auto-merge" in brief
    assert "ready for human review" in brief
    assert '"status": "open"' in brief


def test_make_brief_default_keeps_automerge(tmp_path: Path) -> None:
    issue = {"number": 942, "title": "fix x", "body": ""}
    brief = make_brief(issue, tmp_path / "w")
    assert "gh pr merge" in brief
    assert "--auto" in brief
    assert "DO NOT enable auto-merge" not in brief


def test_make_repair_brief_keeps_same_pr_contract(tmp_path: Path) -> None:
    issue = {"number": 42, "title": "fix blocked pr", "body": "Acceptance"}
    pr = {
        "number": 7,
        "url": "https://github.com/o/r/pull/7",
        "headRefName": "loop/42-fix-blocked-pr",
    }
    brief = make_repair_brief(
        issue,
        tmp_path / "wt",
        pr=pr,
        review_context="[sev1] fix the real consumer",
    )
    assert "Repair the EXISTING PR branch" in brief
    assert "Do not create a new branch" in brief
    assert "https://github.com/o/r/pull/7" in brief
    assert "[sev1] fix the real consumer" in brief
    assert '"pr": "https://github.com/o/r/pull/7"' in brief


def test_run_repair_worker_codex_uses_existing_pr_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from forge_loop import agent_backend

    worktree = tmp_path / "repair"
    worktree.mkdir()

    def fake_prep(_repo: Path, issue: int, branch: str) -> tuple[Path, None]:
        assert issue == 42
        assert branch == "loop/42-fix-blocked-pr"
        return worktree, None

    def fake_codex(**kwargs: Any) -> agent_backend.AgentRunResult:
        assert kwargs["cwd"] == worktree
        assert "Do not create a new branch" in kwargs["prompt"]
        return agent_backend.AgentRunResult(
            provider="codex",
            log_path=kwargs["log_path"],
            last_message=(
                '{"issue": 42, "pr": "https://github.com/o/r/pull/7", '
                '"status": "open", "note": "repair pushed"}'
            ),
            duration_s=2.0,
        )

    monkeypatch.setattr("forge_loop.worker._prep_repair_worktree", fake_prep)
    monkeypatch.setattr(agent_backend, "run_codex_exec", fake_codex)

    out = run_repair_worker(
        {"number": 42, "title": "fix blocked pr", "body": "Acceptance"},
        {
            "number": 7,
            "url": "https://github.com/o/r/pull/7",
            "headRefName": "loop/42-fix-blocked-pr",
        },
        "[sev1] finding",
        tmp_path,
        tmp_path / "logs",
        30,
        provider="codex",
    )
    assert out.status == "open"
    assert out.pr_url == "https://github.com/o/r/pull/7"


def test_run_worker_codex_provider_maps_final_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from forge_loop import agent_backend

    worktree = tmp_path / "wt"
    worktree.mkdir()

    def fake_prep(_repo: Path, _n: int, _branch: str, **_kwargs: Any) -> tuple[Path, None]:
        return worktree, None

    def fake_codex(**kwargs: Any) -> agent_backend.AgentRunResult:
        assert kwargs["cwd"] == worktree
        assert kwargs["model"] == "gpt-5-codex"
        return agent_backend.AgentRunResult(
            provider="codex",
            log_path=kwargs["log_path"],
            last_message=(
                'Done.\n{"issue": 12, "pr": "https://github.com/o/r/pull/9", "status": "open"}'
            ),
            duration_s=1.5,
        )

    monkeypatch.setattr("forge_loop.worker._prep_worktree", fake_prep)
    monkeypatch.setattr(agent_backend, "run_codex_exec", fake_codex)
    out = run_worker(
        {"number": 12, "title": "ship codex", "body": "body"},
        tmp_path,
        tmp_path / "logs",
        30,
        provider="codex",
        model="gpt-5-codex",
    )
    assert out.status == "open"
    assert out.pr_url == "https://github.com/o/r/pull/9"
    assert out.model == "gpt-5-codex"


def test_prep_worktree_uses_configured_base_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class _Completed:
        returncode = 0
        stderr = ""

    def fake_run(cmd: list[str], **kwargs: Any) -> _Completed:
        calls.append(cmd)
        return _Completed()

    monkeypatch.setattr("forge_loop.worker.subprocess.run", fake_run)
    monkeypatch.setattr("forge_loop.worker._drop_permissive_settings", lambda _wt: None)

    worktree, err = _prep_worktree(tmp_path, 12, "loop/12-demo", base_branch="main")

    assert err is None
    assert str(worktree).endswith("/tmp/wt-loop-12")
    assert [
        "git",
        "fetch",
        "--prune",
        "origin",
        "+refs/heads/main:refs/remotes/origin/main",
    ] in calls
    assert ["git", "worktree", "add", str(worktree), "-B", "loop/12-demo", "origin/main"] in calls


# Gradle/WSL-OOM guard tests removed: forge-loop is stack-agnostic; the
# generic brief now says "avoid full-suite runs" without project-specific
# JVM flag pinning. Operators add their own gates via project tooling.


# ---------------------------------------------------------------------------
# SDK-based worker path (issue #2). These tests inject a fake SDK message
# stream into `_worker_sdk.run_sdk_session` and assert the typed-event →
# WorkerOutcome mapping covers the happy path, timeout, tool-error, and
# no-PR cases. They also pin the contract that the new module never imports
# `subprocess` and that a 429 mid-stream surfaces as an `error` event with
# a retry hint instead of crashing the loop.
# ---------------------------------------------------------------------------


@dataclass
class _FakeTextBlock:
    text: str


@dataclass
class _FakeToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class _FakeToolResultBlock:
    tool_use_id: str
    content: Any
    is_error: bool = False


@dataclass
class _FakeSystemMessage:
    subtype: str = "init"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FakeAssistantMessage:
    content: list[Any] = field(default_factory=list)
    model: str = "claude-sonnet-4-6"
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FakeUserMessage:
    content: list[Any] = field(default_factory=list)


@dataclass
class _FakeResultMessage:
    result: str = ""
    total_cost_usd: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False
    num_turns: int = 1


class _FakeOptions:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


@pytest.fixture
def patch_sdk_types(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap claude_agent_sdk in sys.modules for a stub the tests can drive."""
    import types as _types

    fake = _types.ModuleType("claude_agent_sdk")
    fake.AssistantMessage = _FakeAssistantMessage  # type: ignore[attr-defined]
    fake.ResultMessage = _FakeResultMessage  # type: ignore[attr-defined]
    fake.SystemMessage = _FakeSystemMessage  # type: ignore[attr-defined]
    fake.UserMessage = _FakeUserMessage  # type: ignore[attr-defined]
    fake.TextBlock = _FakeTextBlock  # type: ignore[attr-defined]
    fake.ToolUseBlock = _FakeToolUseBlock  # type: ignore[attr-defined]
    fake.ToolResultBlock = _FakeToolResultBlock  # type: ignore[attr-defined]
    fake.ClaudeAgentOptions = _FakeOptions  # type: ignore[attr-defined]

    async def _stub_query(**_kw: Any) -> Any:  # pragma: no cover
        if False:
            yield None

    fake.query = _stub_query  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)


def _make_stream(messages: list[Any]) -> Any:
    """Return a callable matching SDK `query(prompt=..., options=...)`."""

    async def _q(*, prompt: str, options: Any) -> Any:
        for m in messages:
            yield m

    return _q


def _run(messages: list[Any], **kw: Any) -> _worker_sdk.SDKRunResult:
    import anyio

    return anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "irrelevant brief",
            cwd=Path("/tmp"),
            query_fn=_make_stream(messages),
            options_cls=_FakeOptions,
            **kw,
        )
    )


def test_sdk_happy_path_extracts_pr_and_merged(patch_sdk_types: None) -> None:
    messages = [
        _FakeSystemMessage(subtype="init", data={"session_id": "s1"}),
        _FakeAssistantMessage(
            content=[
                _FakeTextBlock(text="Reading the issue…"),
                _FakeToolUseBlock(id="t1", name="Read", input={"file_path": "/x"}),
            ],
            model="claude-sonnet-4-6",
        ),
        _FakeUserMessage(
            content=[
                _FakeToolResultBlock(
                    tool_use_id="t1",
                    content="file contents",
                    is_error=False,
                )
            ]
        ),
        _FakeAssistantMessage(
            content=[
                _FakeTextBlock(
                    text='Done.\n{"issue":2,"pr":"https://github.com/o/r/pull/77","status":"merged"}'
                )
            ],
        ),
        _FakeResultMessage(
            result='Done.\n{"issue":2,"pr":"https://github.com/o/r/pull/77","status":"merged"}',
            total_cost_usd=0.42,
            usage={"input_tokens": 100, "output_tokens": 50},
            num_turns=4,
        ),
    ]
    captured: list[dict[str, Any]] = []
    res = _run(messages, on_event=captured.append)

    assert res.pr_url == "https://github.com/o/r/pull/77"
    assert res.status == "merged"
    assert res.cost_usd == pytest.approx(0.42)
    assert res.error is None
    assert res.model == "claude-sonnet-4-6"

    kinds = [e["kind"] for e in captured]
    # The MCP filter (issue #60) injects a ``worker_mcp_filtered`` event
    # immediately after ``turn_start``; the original event order is
    # otherwise preserved.
    assert kinds == [
        "turn_start",
        "worker_mcp_filtered",
        "assistant_text",
        "tool_use",
        "tool_result",
        "assistant_text",
        "final_result",
    ]
    # seq is monotonic
    assert [e["seq"] for e in captured] == list(range(1, len(captured) + 1))
    # tool_result content survives
    tr = next(e for e in captured if e["kind"] == "tool_result")
    assert tr["content"] == "file contents"
    assert tr["is_error"] is False


def test_sdk_no_pr_when_result_empty(patch_sdk_types: None) -> None:
    messages = [
        _FakeSystemMessage(subtype="init"),
        _FakeResultMessage(result="", total_cost_usd=0.01, num_turns=1),
    ]
    res = _run(messages)
    assert res.pr_url is None
    assert res.status == "no_pr"
    assert res.error is None


def test_sdk_tool_error_event_preserved(patch_sdk_types: None) -> None:
    messages = [
        _FakeAssistantMessage(
            content=[_FakeToolUseBlock(id="t9", name="Bash", input={"command": "git push"})],
        ),
        _FakeUserMessage(
            content=[
                _FakeToolResultBlock(
                    tool_use_id="t9",
                    content="permission denied",
                    is_error=True,
                )
            ]
        ),
        _FakeResultMessage(result="failed: cannot push", total_cost_usd=0.05),
    ]
    events: list[dict[str, Any]] = []
    res = _run(messages, on_event=events.append)
    assert res.status == "no_pr"
    tool_results = [e for e in events if e["kind"] == "tool_result"]
    assert len(tool_results) == 1
    assert tool_results[0]["is_error"] is True
    assert "permission denied" in tool_results[0]["content"]


def test_sdk_rate_limit_mid_stream_emits_error_not_crash(patch_sdk_types: None) -> None:
    """Adversarial: SDK raises 429 mid-iteration — worker must absorb it."""

    async def _q(**_kw: Any) -> Any:
        yield _FakeSystemMessage(subtype="init")
        yield _FakeAssistantMessage(content=[_FakeTextBlock(text="working")])
        raise RuntimeError("HTTP 429: rate_limit_error — retry-after: 30s")

    import anyio

    events: list[dict[str, Any]] = []
    res = anyio.run(
        lambda: _worker_sdk.run_sdk_session(
            "x",
            cwd=Path("/tmp"),
            query_fn=_q,
            options_cls=_FakeOptions,
            on_event=events.append,
        )
    )
    # Loop survives — no exception propagates
    err_events = [e for e in events if e["kind"] == "error"]
    assert len(err_events) == 1
    assert err_events[0]["error_type"] == "rate_limit"
    assert "retry" in (err_events[0]["retry_hint"] or "").lower()
    # status reflects failure since no PR was produced
    assert res.status == "failed"
    assert res.error is not None and "rate_limit" in res.error


def test_sdk_path_does_not_import_subprocess() -> None:
    """Acceptance criterion: the new worker path is subprocess-free.

    We re-import the SDK driver module fresh and verify ``subprocess`` is
    not in its globals. Worker.py itself still imports subprocess for the
    git worktree management helpers — that's intentional (out of scope per
    issue #2's 'shrink or disappear' wording).
    """
    import importlib

    mod = importlib.reload(_worker_sdk)
    assert "subprocess" not in mod.__dict__, "_worker_sdk must remain subprocess-free per issue #2"
    # And the source text itself doesn't reference it
    src = Path(mod.__file__).read_text()
    # only allowed reference is the explanatory docstring/comment
    code_lines = [
        ln
        for ln in src.splitlines()
        if not ln.strip().startswith("#") and not ln.strip().startswith('"')
    ]
    code_blob = "\n".join(code_lines)
    assert "import subprocess" not in code_blob
    assert "from subprocess" not in code_blob


def test_extract_pr_status_handles_trailing_object() -> None:
    pr, status = _worker_sdk._extract_pr_status(
        'preamble\n{"issue":1,"pr":"https://github.com/o/r/pull/5","status":"open"}'
    )
    assert pr == "https://github.com/o/r/pull/5"
    assert status == "open"


def test_extract_pr_status_regex_fallback() -> None:
    pr, status = _worker_sdk._extract_pr_status("see https://github.com/foo/bar/pull/9 for review")
    assert pr == "https://github.com/foo/bar/pull/9"
    assert status == "open"


def test_extract_pr_status_no_pr() -> None:
    assert _worker_sdk._extract_pr_status("nothing to see") == (None, "no_pr")


def test_classify_error_known_categories() -> None:
    assert _worker_sdk._classify_error(RuntimeError("HTTP 429 rate limited"))[0] == "rate_limit"
    assert _worker_sdk._classify_error(RuntimeError("401 Unauthorized"))[0] == "auth"
    assert _worker_sdk._classify_error(TimeoutError("read timed out"))[0] == "timeout"
    # Unknown → exception class name preserved
    et, hint = _worker_sdk._classify_error(ValueError("oops"))
    assert et == "ValueError"
    assert hint is None


def test_run_sdk_session_writes_typed_events_to_callback(patch_sdk_types: None) -> None:
    """Smoke check: every kind in WORKER_EVENT_KINDS can appear."""
    from forge_loop.eventdb import WORKER_EVENT_KINDS

    messages = [
        _FakeSystemMessage(subtype="init"),
        _FakeAssistantMessage(
            content=[
                _FakeTextBlock(text="hi"),
                _FakeToolUseBlock(id="t", name="Read", input={"p": "/x"}),
            ]
        ),
        _FakeUserMessage(
            content=[
                _FakeToolResultBlock(
                    tool_use_id="t",
                    content="ok",
                    is_error=False,
                )
            ]
        ),
        _FakeResultMessage(result="{}", total_cost_usd=0.001),
    ]
    seen: list[str] = []
    _run(messages, on_event=lambda e: seen.append(e["kind"]))
    for k in ("turn_start", "assistant_text", "tool_use", "tool_result", "final_result"):
        assert k in seen, f"missing {k}; got {seen}"
        assert k in WORKER_EVENT_KINDS
