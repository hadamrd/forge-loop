"""Tests for the typed GitHub client (issue #83).

Pins the MockGhClient contract + the auth resolution + the GhError
shape. The githubkit-backed real impl is exercised only at the
boundary — full integration testing against api.github.com belongs
in a recorded-fixture pass, deferred to follow-up.
"""

from __future__ import annotations

import pytest

from forge_loop.gh_client import (
    GhError,
    GhTokenSource,
    Issue,
    MockGhClient,
    PullRequest,
    resolve_token,
)

# ---------------------------------------------------------------------------
# Auth resolution — env-only (GITHUB_TOKEN > GH_TOKEN). No gh-CLI fallback
# after #223; the gh CLI is not used by forge-loop.
# ---------------------------------------------------------------------------


def test_resolve_token_prefers_github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "from-gh")
    monkeypatch.setenv("GITHUB_TOKEN", "from-actions")
    # GITHUB_TOKEN wins (GitHub Actions convention checked first).
    assert resolve_token() == "from-actions"


def test_resolve_token_falls_back_to_gh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "from-gh")
    assert resolve_token() == "from-gh"


class _FakeTokenSource:
    """Env-only token source double (the TokenSource Protocol post-#223)."""

    def __init__(self, *, env: dict[str, str | None]) -> None:
        self.env = env

    def env_token(self, name: str) -> str | None:
        return self.env.get(name)


def test_resolve_token_uses_injected_source() -> None:
    source = _FakeTokenSource(env={"GITHUB_TOKEN": None, "GH_TOKEN": "from-env"})
    assert resolve_token(source) == "from-env"


def test_real_token_source_returns_none_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert GhTokenSource().env_token("GH_TOKEN") is None
    assert GhTokenSource().env_token("GITHUB_TOKEN") is None


def test_resolve_token_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert resolve_token() is None


def test_constructing_client_without_token_raises_naming_both_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env-only auth must fail fast with a clear, actionable error (#223)."""
    from forge_loop.gh_client import GithubkitClient

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(RuntimeError) as exc:
        GithubkitClient()
    msg = str(exc.value)
    assert "GITHUB_TOKEN" in msg and "GH_TOKEN" in msg


# ---------------------------------------------------------------------------
# MockGhClient — call recording + canned responses
# ---------------------------------------------------------------------------


def test_mock_records_calls_with_kwargs() -> None:
    gh = MockGhClient()
    gh.add_comment(owner="o", repo="r", number=1, body="hi")
    gh.add_labels(owner="o", repo="r", number=1, labels=["x", "y"])
    assert [c[0] for c in gh.calls] == ["add_comment", "add_labels"]
    assert gh.calls[0][1] == {"owner": "o", "repo": "r", "number": 1, "body": "hi"}
    assert gh.calls[1][1] == {"owner": "o", "repo": "r", "number": 1, "labels": ["x", "y"]}


def test_mock_get_issue_returns_preloaded() -> None:
    gh = MockGhClient(
        issues={
            ("o", "r", 42): Issue(number=42, title="t", body="b", labels=["a"]),
        }
    )
    issue = gh.get_issue("o", "r", 42)
    assert issue is not None
    assert issue.title == "t"
    assert issue.labels == ["a"]


def test_mock_get_issue_returns_none_when_missing() -> None:
    gh = MockGhClient()
    assert gh.get_issue("o", "r", 999) is None


def test_mock_get_pull_returns_preloaded() -> None:
    gh = MockGhClient(
        pulls={
            ("o", "r", 5): PullRequest(
                number=5,
                title="pr",
                state="open",
                draft=True,
                head_ref="feat/x",
                additions=10,
                deletions=2,
            ),
        }
    )
    pr = gh.get_pull("o", "r", 5)
    assert pr is not None
    assert pr.draft is True
    assert pr.head_ref == "feat/x"
    assert pr.additions == 10


def test_mock_issues_by_label_respects_limit() -> None:
    gh = MockGhClient(issues_by_label_response=[Issue(number=i, title=f"i{i}") for i in range(10)])
    out = gh.issues_by_label("o", "r", "ready", limit=3)
    assert len(out) == 3
    assert out[0].number == 0


def test_mock_raise_on_fires_typed_error() -> None:
    err = GhError("add_comment", 403, "rate limited")
    gh = MockGhClient(raise_on={"add_comment": err})
    with pytest.raises(GhError) as excinfo:
        gh.add_comment(owner="o", repo="r", number=1, body="x")
    assert excinfo.value.status == 403
    assert "rate limited" in excinfo.value.body_tail


def test_mock_check_auth_raises_configured_auth_error() -> None:
    gh = MockGhClient(raise_on={"check_auth": GhError("check_auth", 401, "bad credentials")})
    with pytest.raises(GhError) as excinfo:
        gh.check_auth()
    assert excinfo.value.method == "check_auth"
    assert gh.calls == [("check_auth", {})]


# ---------------------------------------------------------------------------
# GhError — message shape carries diagnostics
# ---------------------------------------------------------------------------


def test_gh_error_message_includes_method_status_body() -> None:
    e = GhError("create_issue", 422, "validation failed: title too long")
    msg = str(e)
    assert "create_issue" in msg
    assert "422" in msg
    assert "validation failed" in msg


def test_gh_error_truncates_long_body() -> None:
    long_body = "x" * 1000
    e = GhError("get_pull", 500, long_body)
    msg = str(e)
    # Body is truncated to <= 300 chars in the message
    assert len(msg) < 500


# ---------------------------------------------------------------------------
# close_pull (#272) — mirrors close_issue: state="closed" + bool contract
# ---------------------------------------------------------------------------


def test_mock_close_pull_records_call_and_returns_true() -> None:
    gh = MockGhClient()
    assert gh.close_pull("o", "r", 5) is True
    assert gh.calls == [("close_pull", {"owner": "o", "repo": "r", "number": 5})]


def test_mock_close_pull_returns_false_when_configured_to_fail() -> None:
    gh = MockGhClient(close_pull_fails=True)
    assert gh.close_pull("o", "r", 5) is False


class _UpdateRecorder:
    """Minimal githubkit ``_gh`` double recording ``rest.pulls.update`` calls."""

    def __init__(self, *, raises: bool = False) -> None:
        from types import SimpleNamespace

        self.calls: list[dict[str, object]] = []
        self._raises = raises

        def _update(**kwargs: object) -> object:
            self.calls.append(kwargs)
            if raises:
                raise RuntimeError("boom")
            return SimpleNamespace(status_code=200)

        self.rest = SimpleNamespace(pulls=SimpleNamespace(update=_update))


def test_real_close_pull_issues_state_closed_update() -> None:
    from forge_loop.gh_client import GithubkitClient

    client = GithubkitClient(token="x")
    recorder = _UpdateRecorder()
    client._gh = recorder  # type: ignore[assignment]
    assert client.close_pull("o", "r", 7) is True
    assert recorder.calls == [{"owner": "o", "repo": "r", "pull_number": 7, "state": "closed"}]


def test_real_close_pull_returns_false_on_failure() -> None:
    """Adversarial: a transport failure yields the False bool contract, not a raise."""
    from forge_loop.gh_client import GithubkitClient

    client = GithubkitClient(token="x")
    client._gh = _UpdateRecorder(raises=True)  # type: ignore[assignment]
    assert client.close_pull("o", "r", 7) is False


# ---------------------------------------------------------------------------
# Issue / PullRequest dataclasses — defaults sane
# ---------------------------------------------------------------------------


def test_issue_defaults() -> None:
    i = Issue(number=1, title="t")
    assert i.body == ""
    assert i.state == "open"
    assert i.labels == []


def test_pull_request_defaults() -> None:
    p = PullRequest(number=1, title="t")
    assert p.draft is False
    assert p.changed_files == 0
    assert p.labels == []
