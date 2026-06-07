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


def test_dangling_saga_reconciled_to_abandoned(tmp_path: Path) -> None:
    """A saga whose event trail ends non-terminally but which the control plane
    does NOT track as in-flight must read ABANDONED — never a live worker with an
    expired heartbeat (the zombie-worker bug)."""
    (tmp_path / ".forge").mkdir(parents=True, exist_ok=True)
    log = SqliteEventLog(tmp_path / ".forge" / "events.db")
    log.append(
        EventKind.TASK_DISPATCHED,
        {"issue": 7, "title": "Dangling", "worktree": "/wt/7"},
        task_id="issue:7",
        saga_id="tick:9",
    )
    # No terminal event AND no tasks.db → control plane doesn't track it in-flight.
    c = TestClient(build_console_api(repo=tmp_path, token=None))
    saga = next(s for s in c.get("/api/sagas").json() if s["saga_id"] == "issue:7")
    assert saga["state"] == "ABANDONED"
    assert c.get("/api/workers").json() == []


def test_auth_enforced_when_token_set(tmp_path: Path) -> None:
    _seed(tmp_path)
    c = TestClient(build_console_api(repo=tmp_path, token="secret"))
    assert c.get("/api/status").status_code == 401
    assert c.get("/api/status", headers={"authorization": "Bearer secret"}).status_code == 200
    assert c.get("/healthz").status_code == 200  # health stays open


def test_frontier_surfaces_okr(tmp_path: Path) -> None:
    """objective/key_result come straight from frontier.yaml (the cursor loader ignores them)."""
    (tmp_path / ".forge").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".forge" / "frontier.yaml").write_text(
        "product_goal: G\n"
        "objective: Close both return arcs\n"
        "key_result: KR holds across the window\n"
        "version: 3\n"
        "active_decisions:\n- a plain string decision\n"
        "rejected_paths:\n- idea: bad idea\n  reason: because\n  revisit_if: ''\n"
        "hot_files:\n- ref: src/x.py\n  why_hot: churny\n"
        "open_questions:\n- what now?\n"
    )
    c = TestClient(build_console_api(repo=tmp_path, token=None))
    f = c.get("/api/frontier").json()
    assert f["objective"] == "Close both return arcs"
    assert f["key_result"] == "KR holds across the window"
    assert f["version"] == 3
    assert f["active_decisions"][0]["text"] == "a plain string decision"
    assert f["rejected_paths"][0]["idea"] == "bad idea"
    assert f["hot_files"][0]["ref"] == "src/x.py"
    assert f["open_questions"] == ["what now?"]


def test_manifestos_parsed(tmp_path: Path) -> None:
    (tmp_path / ".forge").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".forge" / "quality-manifesto.md").write_text(
        "# quality\n\n"
        "### Q1. No shared mutable module-level state.\n\n"
        "Body prose.\n\n**Rationale:** see #100, the runner leaked state.\n\n"
        "### Q2. Typed boundaries behind a Protocol.\n\n**Rationale:** mocks drift.\n"
    )
    c = TestClient(build_console_api(repo=tmp_path, token=None))
    by_id = {r["id"]: r for r in c.get("/api/manifestos").json()}
    assert "Q1" in by_id and "Q2" in by_id
    assert by_id["Q1"]["manifesto"] == "quality"
    assert "module-level state" in by_id["Q1"]["rule"]
    assert by_id["Q1"]["source_pr"] == "#100"


def test_backlog_maps_axis_and_filters_labels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from forge_loop import gh_client
    from forge_loop.gh_client import Issue, OpenBacklog

    epic = Issue(number=1, title="Epic A", labels=["epic", "axis:durable-control-plane"])
    ticket = Issue(number=2, title="Ticket B", labels=["loop:ready", "axis:frontier-generation", "noise-label"])
    monkeypatch.setenv("LOOP_GITHUB_REPO", "o/r")
    monkeypatch.setattr(gh_client, "GithubkitClient", lambda *a, **k: object())
    monkeypatch.setattr(gh_client, "list_open_backlog", lambda *a, **k: OpenBacklog(epics=[epic], tickets=[ticket]))
    c = TestClient(build_console_api(repo=tmp_path, token=None))
    bl = {i["number"]: i for i in c.get("/api/backlog").json()}
    assert bl[1]["axis"] == "durable-control-plane"
    assert bl[2]["axis"] == "frontier-generation"
    assert "loop:ready" in bl[2]["labels"]
    assert "noise-label" not in bl[2]["labels"]
