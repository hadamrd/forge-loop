"""Tests for the operator-console JSON API (forge_loop.console_api).

Seeds a temp durable event log, then drives the FastAPI app via TestClient to
prove the endpoints reconstruct console-shaped data from the real event store —
including the load-bearing edge case that ``pr`` payloads may be full GitHub URLs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from forge_loop.console_api import build_console_api  # noqa: E402
from forge_loop.eventlog.models import EventKind  # noqa: E402
from forge_loop.eventlog.sqlite import SqliteEventLog  # noqa: E402


def _seed(repo: Path) -> None:
    (repo / ".forge").mkdir(parents=True, exist_ok=True)
    log = SqliteEventLog(repo / ".forge" / "events.db")
    log.append(
        EventKind.TASK_DISPATCHED,
        {"issue": 999, "title": "Test issue", "branch": "loop/999", "worktree": "/wt/999", "model": "x"},
        task_id="issue:999",
        saga_id="tick:1",
    )
    # pr payload as a FULL URL — must be coerced to an int PR number.
    log.append(
        EventKind.PR_OPENED,
        {"pr": "https://github.com/o/r/pull/4242", "title": "Test PR", "additions": 10, "deletions": 2},
        task_id="issue:999",
        saga_id="tick:1",
    )
    log.append(
        EventKind.CRITIQUE_ISSUED,
        {"pr": 4242, "round": 1, "verdict": "changes_requested", "sev2": 2},
        task_id="issue:999",
        saga_id="tick:1",
    )
    log.append(
        EventKind.PR_MERGED,
        {"pr": 4242, "branch": "loop/999", "cost_usd": 1.5},
        task_id="issue:999",
        saga_id="tick:1",
    )
    log.append(EventKind.TASK_COMPLETED, {"issue": 999}, task_id="issue:999", saga_id="tick:1")


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    _seed(tmp_path)
    return TestClient(build_console_api(repo=tmp_path, token=None))


def test_status_shape(client: TestClient) -> None:
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["sequence"] >= 5
    assert {"boot", "event_log", "projections", "frontier", "memory"} <= set(body)


def test_events_page(client: TestClient) -> None:
    r = client.get("/api/events?limit=10")
    assert r.status_code == 200
    page = r.json()
    kinds = {e["kind"] for e in page["events"]}
    assert {"task.dispatched", "pr.opened", "critique.issued", "pr.merged"} <= kinds
    # envelope shape the console depends on
    e = page["events"][0]
    assert {"sequence", "event_id", "kind", "occurred_at", "payload"} <= set(e)


def test_sagas_reconstructed_from_events(client: TestClient) -> None:
    sagas = client.get("/api/sagas").json()
    assert len(sagas) == 1
    saga = sagas[0]
    assert saga["saga_id"] == "issue:999"
    assert saga["issue"]["number"] == 999
    assert saga["issue"]["title"] == "Test issue"
    assert saga["state"] == "MERGED"
    assert saga["repair_rounds"] == 1
    assert saga["cost_usd"] == 1.5
    assert saga["branch"] == "loop/999"


def test_prs_coerce_url_pr_number(client: TestClient) -> None:
    prs = client.get("/api/prs").json()
    assert len(prs) == 1
    pr = prs[0]
    assert pr["number"] == 4242  # coerced from the full PR URL
    assert pr["state"] == "merged"
    assert pr["review"]["history"][0]["sev2"] == 2


def test_critic_review_for_pr(client: TestClient) -> None:
    review = client.get("/api/prs/4242/critic").json()
    assert review["verdict"] in {"changes_requested", "approved", "error"}
    assert review["sev_counts"]["sev2"] == 2
    assert client.get("/api/prs/123456/critic").status_code == 404


def test_scorecard_is_honest_nulls(client: TestClient) -> None:
    sc = client.get("/api/scorecard").json()
    # No projection wired → null metrics + a designed "nulls" explanation map.
    assert sc["first_pass_critic_acceptance_rate"] is None
    assert sc["sev2_regeneration_rate"] is None
    assert "first_pass_critic_acceptance_rate" in sc["nulls"]
    assert sc["history"] == []


def test_list_endpoints_return_arrays(client: TestClient) -> None:
    for ep in ("workers", "memory", "backlog", "manifestos", "pipeline"):
        r = client.get(f"/api/{ep}")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


def test_budget_shape(client: TestClient) -> None:
    b = client.get("/api/budget").json()
    assert {"points", "spend_today", "cumulative", "cost_per_merged_pr"} <= set(b)
    assert b["cumulative"] == 1.5  # the one merged PR's cost_usd


def test_auth_enforced_when_token_set(tmp_path: Path) -> None:
    _seed(tmp_path)
    c = TestClient(build_console_api(repo=tmp_path, token="secret"))
    assert c.get("/api/status").status_code == 401
    assert c.get("/api/status", headers={"authorization": "Bearer secret"}).status_code == 200
    assert c.get("/healthz").status_code == 200  # health stays open
