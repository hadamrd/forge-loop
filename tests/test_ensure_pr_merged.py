"""Tests for the auto-merge → direct-squash fallback (issue #255).

The loop never self-landed anything because ``enablePullRequestAutoMerge``
could fail (the repo / PR may not accept auto-merge) and the failure was
swallowed: the method returned a bare ``False`` and the caller logged a
``*_automerge_failed`` event with an EMPTY reason. The fix:

1. ``enable_pr_auto_merge`` returns a typed ``AutoMergeResult`` carrying the
   real GraphQL error message — never an empty reason.
2. ``ensure_pr_merged`` falls back to a DIRECT squash merge (REST) + branch
   delete when auto-merge can't be enabled but the PR is gated-clean.
3. Both-fail surfaces a NON-EMPTY reason naming both causes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from forge_loop import gh_issues
from forge_loop.config import (
    AttemptsConfig,
    Briefs,
    Config,
    CriticConfig,
    Labels,
    LumenConfig,
    POConfig,
)
from forge_loop.gh_client import (
    AutoMergeResult,
    GhError,
    MergeResult,
    MockGhClient,
    PullRequest,
)
from forge_loop.gh_issues import MergeOutcome, ensure_pr_merged
from forge_loop.runner.tick import _enable_automerge_for_reviewed_outcomes
from forge_loop.worker import WorkerOutcome

PR_URL = "https://github.com/o/r/pull/42"


@pytest.fixture(autouse=True)
def _reset_client() -> Any:
    gh_issues.set_client(None)
    yield
    gh_issues.set_client(None)


def _mock(**kwargs: Any) -> MockGhClient:
    client = MockGhClient(**kwargs)
    # The fallback path looks up the head ref to delete the branch.
    client.pulls[("o", "r", 42)] = PullRequest(
        number=42, title="t", head_ref="loop/42-fix"
    )
    gh_issues.set_client(client)
    return client


# ---------------------------------------------------------------------------
# (c) approved + clean PR → actually merges (the happy path)
# ---------------------------------------------------------------------------


def test_ensure_pr_merged_enables_auto_merge_on_happy_path() -> None:
    client = _mock()
    out = ensure_pr_merged(PR_URL, repo="o/r")
    assert out == MergeOutcome(merged=True, method="auto")
    # Only auto-merge was attempted — no direct merge needed.
    methods = [c[0] for c in client.calls]
    assert "enable_pr_auto_merge" in methods
    assert "merge_pull_request" not in methods


# ---------------------------------------------------------------------------
# (a) auto-merge enable fails → falls back to direct squash merge + branch delete
# ---------------------------------------------------------------------------


def test_ensure_pr_merged_falls_back_to_direct_squash() -> None:
    client = _mock(auto_merge_fail_reason="Allow auto-merge must be enabled")
    out = ensure_pr_merged(PR_URL, repo="o/r")

    assert out.merged is True
    assert out.method == "squash"
    assert out.branch_deleted is True
    # Reason names the auto-merge cause AND records the direct-merge fallback.
    assert "Allow auto-merge must be enabled" in out.reason
    assert "merged directly via squash" in out.reason

    calls = {c[0]: c[1] for c in client.calls}
    assert calls["merge_pull_request"]["merge_method"] == "squash"
    assert calls["merge_pull_request"]["number"] == 42
    assert calls["delete_branch"]["branch"] == "loop/42-fix"


def test_fallback_merge_succeeds_even_if_branch_delete_fails() -> None:
    # Branch cleanup is best-effort: the PR is already merged.
    client = _mock(
        auto_merge_fail_reason="auto-merge off", delete_branch_fails=True
    )
    out = ensure_pr_merged(PR_URL, repo="o/r")
    assert out.merged is True
    assert out.method == "squash"
    assert out.branch_deleted is False
    assert "merge_pull_request" in {c[0] for c in client.calls}


# ---------------------------------------------------------------------------
# (b) both fail → event reason is NON-EMPTY with the real error
# ---------------------------------------------------------------------------


def test_ensure_pr_merged_both_fail_carries_nonempty_reason() -> None:
    client = _mock(
        auto_merge_fail_reason="Allow auto-merge must be enabled",
        merge_fail_reason="HTTP 405: Pull Request is not mergeable",
    )
    out = ensure_pr_merged(PR_URL, repo="o/r")

    assert out.merged is False
    assert out.method == "none"
    assert out.reason  # NON-EMPTY — no silent degradation (#255)
    assert "Allow auto-merge must be enabled" in out.reason
    assert "Pull Request is not mergeable" in out.reason
    # We did NOT try to delete a branch for a PR that never merged.
    assert "delete_branch" not in {c[0] for c in client.calls}
    # mock unused-arg guard
    assert isinstance(client, MockGhClient)


# ---------------------------------------------------------------------------
# Caller wiring: the *_automerge_failed event must carry the real reason, and a
# successful fallback merge must mark the outcome merged + emit success.
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path) -> Config:
    cfg = Config(
        repo=tmp_path,
        github_repo="o/r",
        parallel=1,
        tick_interval_s=0,
        max_ticks=1,
        worker_timeout_s=60,
        deploy_task="",
        labels=Labels(),
        briefs=Briefs(),
        critic=CriticConfig(enabled=False, timeout_s=10),
        po=POConfig(enabled=False, timeout_s=10, max_to_expand_per_tick=0),
        attempts=AttemptsConfig(enabled=True, max_history_in_brief=5),
        lumen=LumenConfig(),
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.events_file.touch()
    return cfg


def _events(cfg: Config) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in cfg.events_file.read_text().splitlines()
        if line.strip()
    ]


def _outcome() -> WorkerOutcome:
    return WorkerOutcome(
        issue=42,
        title="t42",
        pr_url=PR_URL,
        status="open",
        duration_s=1.0,
        stdout_tail="",
        error=None,
    )


def test_caller_fallback_merge_marks_outcome_merged(tmp_path: Path) -> None:
    _mock(auto_merge_fail_reason="Allow auto-merge must be enabled")
    cfg = _cfg(tmp_path)
    outcome = _outcome()

    _enable_automerge_for_reviewed_outcomes(
        cfg, [outcome], risk_gated_issues=set(), refused_issues=set()
    )

    assert outcome.status == "merged"  # the loop self-landed via the fallback
    enabled = [
        e for e in _events(cfg) if e["kind"] == "post_critic_automerge_enabled"
    ]
    assert len(enabled) == 1
    assert enabled[0]["method"] == "squash"
    assert "merged directly via squash" in enabled[0]["detail"]


def test_caller_logs_nonempty_reason_when_both_fail(tmp_path: Path) -> None:
    _mock(
        auto_merge_fail_reason="Allow auto-merge must be enabled",
        merge_fail_reason="HTTP 405: Pull Request is not mergeable",
    )
    cfg = _cfg(tmp_path)
    outcome = _outcome()

    _enable_automerge_for_reviewed_outcomes(
        cfg, [outcome], risk_gated_issues=set(), refused_issues=set()
    )

    assert outcome.status == "open"  # not merged
    failed = [
        e for e in _events(cfg) if e["kind"] == "post_critic_automerge_failed"
    ]
    assert len(failed) == 1
    # The empty-reason bug (#255) is gone: the event names both real causes.
    assert failed[0]["reason"]
    assert "Allow auto-merge must be enabled" in failed[0]["reason"]
    assert "Pull Request is not mergeable" in failed[0]["reason"]


# ---------------------------------------------------------------------------
# gh_client level: enable_pr_auto_merge captures the GraphQL error (no swallow).
# ---------------------------------------------------------------------------


class _StubGh:
    """A githubkit-like object: scripted ``.graphql`` + a ``.rest`` shim."""

    def __init__(
        self,
        *,
        graphql_exc: Exception | None = None,
        node_id: str = "PR_node",
    ) -> None:
        self._graphql_exc = graphql_exc
        self._node_id = node_id
        outer = self

        class _Pulls:
            def get(self, **_kw: Any) -> Any:
                class _Resp:
                    status_code = 200
                    parsed_data = type("PD", (), {"node_id": outer._node_id})()

                return _Resp()

        class _Rest:
            pulls = _Pulls()

        self.rest = _Rest()

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> Any:
        if self._graphql_exc is not None:
            raise self._graphql_exc
        return {"data": {"enablePullRequestAutoMerge": {"pullRequest": {"id": "x"}}}}


def _real_client(stub: _StubGh) -> Any:
    from forge_loop.gh_client import GithubkitClient

    client = GithubkitClient.__new__(GithubkitClient)
    client._auth_source = "test"  # type: ignore[attr-defined]
    client._gh = stub  # type: ignore[attr-defined]
    return client


def test_enable_pr_auto_merge_captures_graphql_error_message() -> None:
    from githubkit.exception import GraphQLFailed
    from githubkit.graphql import GraphQLResponse

    resp = GraphQLResponse.model_validate(
        {"data": None, "errors": [{"message": "Allow auto-merge must be enabled"}]}
    )
    client = _real_client(_StubGh(graphql_exc=GraphQLFailed(resp)))

    result = client.enable_pr_auto_merge("o", "r", 42)
    assert isinstance(result, AutoMergeResult)
    assert result.enabled is False
    assert result.reason == "Allow auto-merge must be enabled"


def test_enable_pr_auto_merge_success_returns_no_reason() -> None:
    client = _real_client(_StubGh())
    result = client.enable_pr_auto_merge("o", "r", 42)
    assert result == AutoMergeResult(True, "")


def test_enable_pr_auto_merge_falls_back_to_str_for_opaque_error() -> None:
    client = _real_client(_StubGh(graphql_exc=RuntimeError("boom")))
    result = client.enable_pr_auto_merge("o", "r", 42)
    assert result.enabled is False
    assert "boom" in result.reason


# ---------------------------------------------------------------------------
# MockGhClient contract for the new methods.
# ---------------------------------------------------------------------------


def test_mock_merge_pull_request_records_and_defaults_squash() -> None:
    client = MockGhClient()
    assert client.merge_pull_request("o", "r", 7) == MergeResult(True)
    method, kwargs = client.calls[-1]
    assert method == "merge_pull_request"
    assert kwargs["merge_method"] == "squash"


def test_mock_merge_pull_request_can_simulate_failure() -> None:
    client = MockGhClient(merge_fail_reason="not mergeable")
    assert client.merge_pull_request("o", "r", 7) == MergeResult(False, "not mergeable")


def test_mock_enable_auto_merge_can_simulate_feature_off() -> None:
    client = MockGhClient(auto_merge_fail_reason="auto-merge off")
    assert client.enable_pr_auto_merge("o", "r", 7) == AutoMergeResult(
        False, "auto-merge off"
    )


def test_mock_delete_branch_reports_failure_flag() -> None:
    ok = MockGhClient()
    assert ok.delete_branch("o", "r", "b") is True
    bad = MockGhClient(delete_branch_fails=True)
    assert bad.delete_branch("o", "r", "b") is False
    # Touch GhError import so it is exercised (mock raise_on path elsewhere).
    assert issubclass(GhError, RuntimeError)
