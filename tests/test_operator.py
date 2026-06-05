"""Tests for the operator-checkpoint module (issue #8).

Covers:
- Happy path: marker matched → ``ask`` returns the operator's choice.
- Timeout: no marker → :class:`OperatorTimeout` raised, event emitted.
- Channel posting: GH issue + webhook + Slack; partial failure OK.
- ``operator_question`` event shape.
- Marker matching: case-insensitive, skips stale comments, ignores prose,
  rejects unknown options.
- MCP wrapper: returns ``{"status": "timeout"}`` on timeout, not a raise.
- Bad input: empty options, whitespace in option token.

Integration: fixture GH-issue flow simulated with a fake ``runner`` that
serves issue comments after a few polls, end-to-end through ``ask``.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

import pytest

from forge_loop import operator as op


def _ask_request(**overrides: Any) -> op.AskRequest:
    base: dict[str, Any] = dict(
        question="Delete the 0042_drop_users migration?",
        options=["yes", "no"],
        context="The migration drops a column referenced by a view.",
        issue=42,
        repo="acme/forge",
        timeout_s=5,
        poll_interval_s=0,
    )
    base.update(overrides)
    return op.AskRequest(**base)


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def mono(self) -> float:
        return self.t

    def sleep(self, secs: float) -> None:
        self.t += max(secs, 0.001)


@pytest.fixture(autouse=True)
def _reset_gh_client() -> Any:
    """Reset the gh_issues client singleton around every operator test."""
    from forge_loop import gh_issues

    gh_issues.set_client(None)
    yield
    gh_issues.set_client(None)


class _FakeOperatorClient:
    """Stateful GhClient double for the operator flow.

    ``issue_comments`` returns successive ``comments_by_call`` payloads (one per
    poll); ``add_comment`` records the post and raises iff ``comment_post_ok``
    is False (so ``post_github_issue`` reports failure). Only the methods the
    operator path uses are implemented.
    """

    auth_source = "fake"

    def __init__(
        self,
        comments_by_call: list[list[dict[str, Any]]],
        *,
        comment_post_ok: bool = True,
    ) -> None:
        self.comments_by_call = comments_by_call
        self.comment_post_ok = comment_post_ok
        self.posted: list[dict[str, Any]] = []
        self.view_count = 0

    def add_comment(self, owner: str, repo: str, number: int, body: str) -> None:
        self.posted.append({"owner": owner, "repo": repo, "number": number, "body": body})
        if not self.comment_post_ok:
            raise RuntimeError("comment post failed")

    def issue_comments(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        seq = self.comments_by_call
        i = self.view_count
        self.view_count = min(i + 1, len(seq) - 1) if seq else 0
        return list(seq[i]) if i < len(seq) else []


def _seed_operator_client(
    comments_by_call: list[list[dict[str, Any]]],
    *,
    comment_post_ok: bool = True,
) -> _FakeOperatorClient:
    from forge_loop import gh_issues

    client = _FakeOperatorClient(comments_by_call, comment_post_ok=comment_post_ok)
    gh_issues.set_client(client)
    return client


# ── happy path ──────────────────────────────────────────────────────────────


def test_ask_returns_chosen_option_when_marker_matches() -> None:
    """Operator replies with /forge-answer yes — ask returns 'yes'."""
    comments = [
        # first poll: no reply yet
        [],
        # second poll: operator answered
        [
            {
                "createdAt": "2999-01-01T00:00:01+00:00",
                "body": "Let's keep it.\n/forge-answer no",
            }
        ],
    ]
    events: list[tuple[str, dict[str, Any]]] = []
    clock = _FakeClock()
    client = _seed_operator_client(comments)

    result = op.ask(
        _ask_request(),
        emit=lambda k, p: events.append((k, p)),
        sleep=clock.sleep,
        monotonic=clock.mono,
        now_iso=lambda: "1970-01-01T00:00:00+00:00",
    )

    assert result.status == "answered"
    assert result.answer == "no"
    assert "/forge-answer no" in (result.raw_reply or "")
    assert result.posted_channels == ["github"]

    # operator_question emitted exactly once with the right shape
    questions = [e for e in events if e[0] == "operator_question"]
    assert len(questions) == 1
    qp = questions[0][1]
    assert qp["issue"] == 42
    assert qp["options"] == ["yes", "no"]
    assert qp["channels"] == ["github"]
    assert qp["timeout_s"] == 5

    # operator_answer emitted, no timeout
    answers = [e for e in events if e[0] == "operator_answer"]
    timeouts = [e for e in events if e[0] == "operator_timeout"]
    assert len(answers) == 1 and not timeouts
    assert answers[0][1]["answer"] == "no"

    # The GH comment that was posted carries the question + options.
    assert len(client.posted) == 1
    body = client.posted[0]["body"]
    assert client.posted[0]["number"] == 42
    assert "Delete the 0042_drop_users migration" in body
    assert "/forge-answer" in body
    assert "`yes`" in body and "`no`" in body


def test_ask_normalises_option_casing_and_strips_trailing_prose() -> None:
    """Operator may answer with weird casing / a trailing comment."""
    comments = [
        [
            {
                "createdAt": "2999-01-01T00:00:01+00:00",
                "body": "/forge-answer YES, do it.",
            }
        ]
    ]
    clock = _FakeClock()
    _seed_operator_client(comments)
    result = op.ask(
        _ask_request(),
        sleep=clock.sleep,
        monotonic=clock.mono,
        now_iso=lambda: "1970-01-01T00:00:00+00:00",
    )
    assert result.answer == "yes"


# ── timeout / sad path ──────────────────────────────────────────────────────


def test_ask_raises_timeout_when_no_reply() -> None:
    """No matching marker appears before deadline → OperatorTimeout."""
    # Reply comment was posted BEFORE our question — must be ignored.
    comments = [
        [
            {
                "createdAt": "1970-01-01T00:00:00+00:00",
                "body": "/forge-answer yes",
            }
        ]
    ]
    events: list[tuple[str, dict[str, Any]]] = []
    clock = _FakeClock()
    req = _ask_request(timeout_s=2, poll_interval_s=1)
    _seed_operator_client(comments)

    with pytest.raises(op.OperatorTimeout):
        op.ask(
            req,
            emit=lambda k, p: events.append((k, p)),
            sleep=clock.sleep,
            monotonic=clock.mono,
            now_iso=lambda: "2999-01-01T00:00:00+00:00",
        )

    kinds = [k for (k, _p) in events]
    assert "operator_question" in kinds
    assert "operator_timeout" in kinds
    assert "operator_answer" not in kinds


def test_ask_rejects_unknown_option() -> None:
    """An operator typo like ``/forge-answer maybe`` is NOT a match."""
    comments = [
        [
            {
                "createdAt": "2999-01-01T00:00:01+00:00",
                "body": "/forge-answer maybe",
            }
        ]
    ]
    clock = _FakeClock()
    _seed_operator_client(comments)
    with pytest.raises(op.OperatorTimeout):
        op.ask(
            _ask_request(timeout_s=1, poll_interval_s=1),
            sleep=clock.sleep,
            monotonic=clock.mono,
            now_iso=lambda: "1970-01-01T00:00:00+00:00",
        )


def test_ask_rejects_empty_options() -> None:
    with pytest.raises(op.OperatorError):
        op.ask(_ask_request(options=[]))


def test_ask_rejects_whitespace_in_option_token() -> None:
    """Options must be single tokens so the /forge-answer marker is unambiguous."""
    with pytest.raises(op.OperatorError):
        op.ask(_ask_request(options=["yes please", "no"]))


def test_ask_errors_when_no_channels_configured() -> None:
    """Issue+webhook+slack all unset → can't post the question anywhere."""
    req = _ask_request(issue=None, repo=None)
    with pytest.raises(op.OperatorError):
        op.ask(req)


# ── channel adapters ────────────────────────────────────────────────────────


def test_post_webhook_and_slack_send_json_payloads() -> None:
    captured: list[tuple[str, dict[str, Any]]] = []

    class _FakeResp:
        status = 200

        def getcode(self) -> int:
            return 200

    def _opener(request: urllib.request.Request) -> Any:
        body = request.data.decode("utf-8") if request.data else ""
        captured.append((request.full_url, json.loads(body)))
        return _FakeResp()

    req = _ask_request()
    assert op.post_webhook(req, "https://example.com/hook", opener=_opener) is True
    assert op.post_slack(req, "https://hooks.slack.com/abc", opener=_opener) is True

    urls = [c[0] for c in captured]
    assert "https://example.com/hook" in urls
    assert "https://hooks.slack.com/abc" in urls

    webhook_payload = next(p for (u, p) in captured if "example.com" in u)
    assert webhook_payload["kind"] == "operator_question"
    assert webhook_payload["options"] == ["yes", "no"]

    slack_payload = next(p for (u, p) in captured if "slack.com" in u)
    assert "text" in slack_payload
    assert "forge-answer" in slack_payload["text"]


def test_post_webhook_returns_false_on_network_error() -> None:
    def _opener(_r: urllib.request.Request) -> Any:
        raise urllib.error.URLError("conn refused")

    assert op.post_webhook(_ask_request(), "https://x", opener=_opener) is False


def test_ask_continues_when_webhook_fails_but_github_succeeds() -> None:
    """Partial channel failure must NOT block the operator question."""

    def _opener(_r: urllib.request.Request) -> Any:
        raise urllib.error.URLError("boom")

    comments = [
        [
            {
                "createdAt": "2999-01-01T00:00:01+00:00",
                "body": "/forge-answer yes",
            }
        ]
    ]
    clock = _FakeClock()
    _seed_operator_client(comments)
    result = op.ask(
        _ask_request(),
        webhook_url="https://broken.example/hook",
        opener=_opener,
        sleep=clock.sleep,
        monotonic=clock.mono,
        now_iso=lambda: "1970-01-01T00:00:00+00:00",
    )
    assert result.answer == "yes"
    assert "github" in result.posted_channels
    assert "webhook" not in result.posted_channels


# ── marker matching ─────────────────────────────────────────────────────────


def test_match_answer_anchors_to_line_start() -> None:
    """Prose like 'I would say /forge-answer yes' must NOT match — the
    marker is anchored to the start of a line so quoted text in replies
    doesn't trigger a false positive."""
    comments = [{"createdAt": "x", "body": "I would say /forge-answer yes"}]
    ans, _ = op.match_answer(comments, ["yes", "no"])
    assert ans is None


def test_match_answer_skips_stale_comments_by_timestamp() -> None:
    comments = [
        {"createdAt": "1970-01-01T00:00:00+00:00", "body": "/forge-answer yes"},
        {"createdAt": "2999-01-01T00:00:00+00:00", "body": "/forge-answer no"},
    ]
    ans, _ = op.match_answer(
        comments,
        ["yes", "no"],
        posted_after_iso="2000-01-01T00:00:00+00:00",
    )
    assert ans == "no"


def test_match_answer_returns_none_on_empty_list() -> None:
    ans, raw = op.match_answer([], ["yes", "no"])
    assert ans is None and raw is None


# ── env helpers ─────────────────────────────────────────────────────────────


def test_env_helpers_read_loop_operator_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOP_OPERATOR_TIMEOUT_S", "120")
    monkeypatch.setenv("LOOP_OPERATOR_WEBHOOK", "https://wh.example")
    monkeypatch.setenv("LOOP_OPERATOR_SLACK_WEBHOOK", "https://sl.example")
    monkeypatch.setenv("LOOP_OPERATOR_ISSUE", "777")
    assert op.env_timeout_s() == 120
    assert op.env_webhook() == "https://wh.example"
    assert op.env_slack() == "https://sl.example"
    assert op.env_issue() == 777


def test_env_timeout_falls_back_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOP_OPERATOR_TIMEOUT_S", "not-an-int")
    assert op.env_timeout_s(default=300) == 300


# ── MCP wrapper integration ─────────────────────────────────────────────────


def test_mcp_ask_operator_returns_timeout_dict_not_raise(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """End-to-end through the MCP tool: timeout becomes a dict, not an exception.
    Worker code can branch on ``status`` without try/except."""
    from forge_loop import mcp_server

    fake_cfg = type(
        "C",
        (),
        {
            "github_repo": "acme/forge",
            "events_file": tmp_path / "events.jsonl",
        },
    )()
    monkeypatch.setattr(mcp_server, "load_config", lambda: fake_cfg)
    monkeypatch.setenv("LOOP_OPERATOR_ISSUE", "42")

    def _fake_ask(req: op.AskRequest, **kwargs: Any) -> op.AskResult:
        # simulate timeout
        emit = kwargs.get("emit")
        if emit is not None:
            emit("operator_timeout", {"issue": req.issue, "elapsed_s": 1})
        raise op.OperatorTimeout("no reply")

    monkeypatch.setattr(mcp_server._operator, "ask", _fake_ask)

    out = mcp_server.ask_operator("delete?", ["yes", "no"], "context", 30)
    assert out["status"] == "timeout"
    assert out["answer"] is None
    assert "operator_no_response" in out["note"]

    # event was persisted
    assert fake_cfg.events_file.exists()
    lines = fake_cfg.events_file.read_text().splitlines()
    kinds = [json.loads(line)["kind"] for line in lines]
    assert "operator_timeout" in kinds


def test_mcp_ask_operator_returns_answer_on_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from forge_loop import mcp_server

    fake_cfg = type(
        "C",
        (),
        {
            "github_repo": "acme/forge",
            "events_file": tmp_path / "events.jsonl",
        },
    )()
    monkeypatch.setattr(mcp_server, "load_config", lambda: fake_cfg)
    monkeypatch.setenv("LOOP_OPERATOR_ISSUE", "9")

    def _fake_ask(req: op.AskRequest, **kwargs: Any) -> op.AskResult:
        return op.AskResult(
            status="answered",
            answer="yes",
            raw_reply="/forge-answer yes",
            elapsed_s=1.5,
            posted_channels=["github"],
        )

    monkeypatch.setattr(mcp_server._operator, "ask", _fake_ask)
    out = mcp_server.ask_operator("delete?", ["yes", "no"])
    assert out["status"] == "answered"
    assert out["answer"] == "yes"
    assert out["posted_channels"] == ["github"]


# ── simulated end-to-end (integration-flavoured) ────────────────────────────


def test_end_to_end_via_fixture_gh_issue_simulated_reply() -> None:
    """Simulate the full GH flow: post question, poll twice, operator replies.

    No real ``gh`` invocation — runner is faked, but the orchestration
    (post, poll, match, return) is exercised end-to-end.
    """
    # First two polls: empty. Third poll: operator answered.
    comments_sequence = [
        [],
        [],
        [
            {
                "createdAt": "2999-06-01T00:00:00+00:00",
                "body": "Let's proceed.\n\n/forge-answer yes\n",
            }
        ],
    ]
    client = _seed_operator_client(comments_sequence)
    clock = _FakeClock()
    events: list[tuple[str, dict[str, Any]]] = []

    result = op.ask(
        _ask_request(timeout_s=60, poll_interval_s=1),
        emit=lambda k, p: events.append((k, p)),
        sleep=clock.sleep,
        monotonic=clock.mono,
        now_iso=lambda: "1970-01-01T00:00:00+00:00",
    )

    assert result.status == "answered"
    assert result.answer == "yes"

    # Sanity: a question was posted, then the issue was polled multiple
    # times before the reply landed.
    assert len(client.posted) == 1
    assert client.view_count >= 2

    kinds = [k for (k, _p) in events]
    assert kinds[0] == "operator_question"
    assert kinds[-1] == "operator_answer"
