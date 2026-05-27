"""Issue #49 — pipeline-driven dispatch path in runner._tick.

The chain ``po → worker → custom-reviewer → critic`` defined in
``.forge/pipeline.yaml`` MUST actually fire every role's handler when
``LOOP_PIPELINE_DRIVEN=1``; the prior wiring built the DAG at boot and
emitted ``pipeline_loaded`` but never invoked the executor — these
tests fail loudly if that regression returns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from forge_loop.pipeline.executor import StepContext, StepOutcome
from forge_loop.runner import _pipeline_driver
from forge_loop.runner._pipeline_driver import (
    dispatch_via_pipeline,
    pipeline_driven_enabled,
)
from forge_loop.worker import WorkerOutcome

CHAIN_YAML = """
default_chain:
  - role: po
    on: issue_labeled_ready
  - role: worker
    after: po
  - role: custom-reviewer
    after: worker
  - role: critic
    after: custom-reviewer
"""

CONDITIONAL_YAML = """
default_chain:
  - role: po
    on: issue_labeled_ready
  - role: worker
    after: po
  - role: security-reviewer
    after: worker
    condition:
      labels: ["security-sensitive"]
  - role: critic
    after: worker
"""


# --------------------------------------------------------------- helpers / cfg


class _Sub:
    """Lightweight stand-in for cfg sub-namespaces (cfg.lumen.top_k, …)."""

    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _FakeCfg:
    def __init__(self, repo: Path, events_file: Path) -> None:
        self.repo = repo
        self.logs_dir = repo / "logs"
        self.logs_dir.mkdir(exist_ok=True)
        self.events_file = events_file
        self.worker_timeout_s = 60
        self.lumen_test_pattern = "**/*Test.*"
        self.coauthor = ""
        self.lumen = _Sub(top_k=3)
        self.worker = _Sub(model="claude-opus-4-7", thinking="off")
        self.critic = _Sub(
            enabled=True, timeout_s=60, model="claude-opus-4-7",
            block_on_sev2=False, min_findings_for_approve=0,
        )


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "pipeline.yaml").write_text(CHAIN_YAML)
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_extra_handlers():
    _pipeline_driver.EXTRA_HANDLERS.clear()
    yield
    _pipeline_driver.EXTRA_HANDLERS.clear()


# ------------------------------------------------------------- gating tests


def test_pipeline_driven_disabled_when_env_unset(fake_repo, monkeypatch):
    monkeypatch.delenv("LOOP_PIPELINE_DRIVEN", raising=False)
    cfg = _FakeCfg(fake_repo, fake_repo / "events.jsonl")
    assert pipeline_driven_enabled(cfg) is False


def test_pipeline_driven_disabled_when_yaml_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOP_PIPELINE_DRIVEN", "1")
    cfg = _FakeCfg(tmp_path, tmp_path / "events.jsonl")
    # No .forge/pipeline.yaml in fixture → must remain disabled.
    assert pipeline_driven_enabled(cfg) is False


def test_pipeline_driven_enabled_when_both_present(fake_repo, monkeypatch):
    monkeypatch.setenv("LOOP_PIPELINE_DRIVEN", "1")
    cfg = _FakeCfg(fake_repo, fake_repo / "events.jsonl")
    assert pipeline_driven_enabled(cfg) is True


# ----------------------------------------------------------- happy-path test


def test_custom_role_between_worker_and_critic_actually_fires(fake_repo):
    """The headline acceptance criterion: a 4th role declared between
    worker and critic in pipeline.yaml gets its handler invoked — not
    just validated at boot."""
    cfg = _FakeCfg(fake_repo, fake_repo / "events.jsonl")

    calls: list[str] = []
    events: list[tuple[str, dict[str, Any]]] = []

    def custom_handler(ctx: StepContext) -> StepOutcome:
        calls.append("custom-reviewer")
        # The custom role MUST see the issue payload and the upstream
        # worker step's outcome — otherwise it's useless.
        assert ctx.issue["number"] == 7
        assert "worker" in ctx.upstream_outcomes
        return StepOutcome(role="custom-reviewer", status="ok", detail="lgtm")

    _pipeline_driver.register_handler("custom-reviewer", custom_handler)

    def fake_worker(*_a, **_kw) -> WorkerOutcome:
        calls.append("worker")
        return WorkerOutcome(
            issue=7, title="t", pr_url="https://example.invalid/pr/7",
            status="open", duration_s=1.0, stdout_tail="",
        )

    def fake_critic(pr_url, issue, *_a, **_kw):
        calls.append("critic")
        # Minimal duck-typed critic outcome
        return _Sub(verdict="approve", reasons=[], duration_s=0.1, report=None,
                    parse_retries=0)

    def bus_emit(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))

    issues = [{"number": 7, "title": "t", "labels": []}]
    workers_meta = [{"risk_gated": False, "past_attempts": [],
                     "brief_fingerprint": "fp", "forced": False}]

    outcomes = dispatch_via_pipeline(
        cfg, issues, workers_meta, tick=1,
        master_log_path=cfg.logs_dir / "master.log",
        bus_emit=bus_emit,
        run_worker_fn=fake_worker,
        critic_review_fn=fake_critic,
    )

    # The four roles fired in order. (po is a no-op placeholder so
    # it's not in `calls`, but the per-step events prove it ran.)
    assert calls == ["worker", "custom-reviewer", "critic"]

    # And the executor emitted the per-step events through the bus —
    # this is how operators see the chain progress in events.jsonl.
    kinds = [k for k, _ in events]
    assert "pipeline_run_start" in kinds
    assert "pipeline_run_done" in kinds
    # step_started must have fired for every declared role.
    started_roles = {p.get("role") for k, p in events if k == "pipeline_step_started"}
    assert {"po", "worker", "custom-reviewer", "critic"} <= started_roles

    # And the worker's WorkerOutcome propagated back as the legacy return.
    assert len(outcomes) == 1
    assert outcomes[0].pr_url == "https://example.invalid/pr/7"
    assert outcomes[0].status == "open"


# ---------------------------------------------------------- conditional test


def test_conditional_step_skipped_when_label_missing(tmp_path):
    """``condition.labels`` on a step must short-circuit the handler
    when the issue doesn't carry the label, but downstream steps still
    run. This proves the executor evaluates conditions inside the
    runner's pipeline path (not just in the unit test fixtures)."""
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "pipeline.yaml").write_text(CONDITIONAL_YAML)
    cfg = _FakeCfg(tmp_path, tmp_path / "events.jsonl")

    sec_called = {"n": 0}

    def sec_handler(ctx: StepContext) -> StepOutcome:
        sec_called["n"] += 1
        return StepOutcome(role="security-reviewer", status="ok")

    _pipeline_driver.register_handler("security-reviewer", sec_handler)

    def fake_worker(*_a, **_kw) -> WorkerOutcome:
        return WorkerOutcome(
            issue=11, title="t", pr_url="https://example.invalid/pr/11",
            status="open", duration_s=1.0, stdout_tail="",
        )

    def fake_critic(*_a, **_kw):
        return _Sub(verdict="approve", reasons=[], duration_s=0.0, report=None,
                    parse_retries=0)

    events: list[tuple[str, dict[str, Any]]] = []
    issues = [{"number": 11, "title": "t", "labels": []}]
    meta = [{"risk_gated": False, "past_attempts": [],
             "brief_fingerprint": "fp", "forced": False}]
    dispatch_via_pipeline(
        cfg, issues, meta, tick=1,
        master_log_path=cfg.logs_dir / "master.log",
        bus_emit=lambda k, p: events.append((k, p)),
        run_worker_fn=fake_worker, critic_review_fn=fake_critic,
    )
    assert sec_called["n"] == 0, "security-reviewer must skip when label missing"
    # The executor must have emitted a step_skipped for it.
    skipped_roles = {p.get("role") for k, p in events
                     if k == "pipeline_step_skipped"}
    assert "security-reviewer" in skipped_roles


def test_conditional_step_runs_when_label_present(tmp_path):
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "pipeline.yaml").write_text(CONDITIONAL_YAML)
    cfg = _FakeCfg(tmp_path, tmp_path / "events.jsonl")
    sec_called = {"n": 0}

    def sec_handler(ctx: StepContext) -> StepOutcome:
        sec_called["n"] += 1
        return StepOutcome(role="security-reviewer", status="ok")

    _pipeline_driver.register_handler("security-reviewer", sec_handler)

    def fake_worker(*_a, **_kw) -> WorkerOutcome:
        return WorkerOutcome(
            issue=12, title="t", pr_url="https://example.invalid/pr/12",
            status="open", duration_s=1.0, stdout_tail="",
        )

    def fake_critic(*_a, **_kw):
        return _Sub(verdict="approve", reasons=[], duration_s=0.0, report=None,
                    parse_retries=0)

    issues = [{"number": 12, "title": "t",
               "labels": [{"name": "security-sensitive"}]}]
    meta = [{"risk_gated": False, "past_attempts": [],
             "brief_fingerprint": "fp", "forced": False}]
    dispatch_via_pipeline(
        cfg, issues, meta, tick=1,
        master_log_path=cfg.logs_dir / "master.log",
        bus_emit=lambda *a, **k: None,
        run_worker_fn=fake_worker, critic_review_fn=fake_critic,
    )
    assert sec_called["n"] == 1, "security-reviewer must fire when label present"


# ------------------------------------------------------------ adversarial


def test_unknown_role_in_yaml_does_not_crash_tick(fake_repo):
    """A pipeline.yaml referencing a role with no registered handler
    must produce a `failed` step for that role but still return a
    flat WorkerOutcome list — the loop is more important than the
    chain validity."""
    cfg = _FakeCfg(fake_repo, fake_repo / "events.jsonl")
    # Do NOT register custom-reviewer this time — handler is missing.

    def fake_worker(*_a, **_kw) -> WorkerOutcome:
        return WorkerOutcome(
            issue=8, title="t", pr_url=None, status="failed",
            duration_s=0.1, stdout_tail="", error="boom",
        )

    issues = [{"number": 8, "title": "t", "labels": []}]
    meta = [{"risk_gated": False, "past_attempts": [],
             "brief_fingerprint": "fp", "forced": False}]
    outcomes = dispatch_via_pipeline(
        cfg, issues, meta, tick=1,
        master_log_path=cfg.logs_dir / "master.log",
        bus_emit=lambda *a, **k: None,
        run_worker_fn=fake_worker,
        critic_review_fn=lambda *a, **k: _Sub(
            verdict="reject", reasons=[], duration_s=0.0,
            report=None, parse_retries=0,
        ),
    )
    # The worker outcome still propagated back.
    assert len(outcomes) == 1
    assert outcomes[0].issue == 8


def test_worker_handler_propagates_outcome_payload(fake_repo):
    """The worker handler MUST place the live WorkerOutcome on the
    step's payload — otherwise the runner can't reconstruct the
    per-issue list it needs for attempts.record / redeploy / drift."""
    cfg = _FakeCfg(fake_repo, fake_repo / "events.jsonl")

    _pipeline_driver.register_handler(
        "custom-reviewer",
        lambda ctx: StepOutcome(role="custom-reviewer", status="ok"),
    )

    captured: dict[str, Any] = {}

    def fake_worker(issue, repo, logs_dir, timeout_s, **kw) -> WorkerOutcome:
        captured.update(kw)
        captured["issue"] = issue
        return WorkerOutcome(
            issue=issue["number"], title=issue["title"],
            pr_url=f"https://example.invalid/pr/{issue['number']}",
            status="merged", duration_s=2.5, stdout_tail="",
        )

    issues = [{"number": 22, "title": "t", "labels": []}]
    meta = [{"risk_gated": True, "past_attempts": [{"ts": "x"}],
             "brief_fingerprint": "fp", "forced": False}]
    outcomes = dispatch_via_pipeline(
        cfg, issues, meta, tick=4,
        master_log_path=cfg.logs_dir / "master.log",
        bus_emit=lambda *a, **k: None,
        run_worker_fn=fake_worker,
        critic_review_fn=lambda *a, **k: _Sub(
            verdict="approve", reasons=[], duration_s=0.0,
            report=None, parse_retries=0,
        ),
    )
    assert outcomes[0].status == "merged"
    assert outcomes[0].duration_s == 2.5
    # And the meta plumbed through to the worker.
    assert captured["risk_gated"] is True
    assert captured["past_attempts"] == [{"ts": "x"}]
    assert captured["tick"] == 4
