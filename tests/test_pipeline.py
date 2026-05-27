"""Tests for the role-chain pipeline (issue #18)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from forge_loop.pipeline import (
    PipelineExecutor,
    PipelineLoadError,
    StepContext,
    StepOutcome,
    ValidationError,
    build_dag,
    load_pipeline,
    parse_pipeline,
)

# --------------------------------------------------------------------- loader


SAMPLE_YAML = """
default_chain:
  - role: po
    on: issue_labeled_ready
  - role: worker
    after: po
    parallel: 3
  - role: critic
    after: worker
  - role: security-reviewer
    after: critic
    condition:
      labels: ["security-sensitive"]
  - role: merge
    after: [critic, security-reviewer]
    condition:
      all_approve: true
"""


def test_parse_happy_yaml_produces_chain_with_correct_edges() -> None:
    spec = parse_pipeline(yaml.safe_load(SAMPLE_YAML))
    assert [s.role for s in spec.steps] == [
        "po", "worker", "critic", "security-reviewer", "merge",
    ]
    worker = spec.step("worker")
    assert worker.after == ("po",)
    assert worker.parallel == 3
    merge = spec.step("merge")
    assert merge.after == ("critic", "security-reviewer")
    assert merge.condition.all_approve is True

    dag = build_dag(spec)
    assert dag.roots == ("po",)
    assert dag.nodes["worker"].parents == ("po",)
    assert dag.nodes["merge"].parents == ("critic", "security-reviewer")
    # topo order: po then worker then critic then (security-reviewer
    # before merge because position breaks ties)
    assert dag.order.index("worker") > dag.order.index("po")
    assert dag.order.index("merge") > dag.order.index("critic")
    assert dag.order.index("merge") > dag.order.index("security-reviewer")
    # depth: po=0, worker=1, critic=2, security-reviewer=3, merge=4
    assert dag.nodes["po"].depth == 0
    assert dag.nodes["merge"].depth == 4


def test_load_pipeline_from_disk(tmp_path: Path) -> None:
    p = tmp_path / "pipeline.yaml"
    p.write_text(SAMPLE_YAML)
    spec = load_pipeline(p)
    assert spec.source_path == p
    assert len(spec.steps) == 5


def test_missing_default_chain_raises() -> None:
    with pytest.raises(PipelineLoadError, match="default_chain"):
        parse_pipeline({"chain": []})


def test_empty_chain_raises() -> None:
    with pytest.raises(PipelineLoadError, match="non-empty"):
        parse_pipeline({"default_chain": []})


def test_duplicate_role_raises() -> None:
    bad = {"default_chain": [
        {"role": "worker", "on": "x"},
        {"role": "worker", "after": "worker"},
    ]}
    with pytest.raises(PipelineLoadError, match="duplicate role"):
        parse_pipeline(bad)


def test_bad_parallel_raises() -> None:
    with pytest.raises(PipelineLoadError, match="parallel"):
        parse_pipeline({"default_chain": [{"role": "x", "on": "y", "parallel": 0}]})


def test_unknown_step_key_raises() -> None:
    bad = {"default_chain": [{"role": "x", "on": "y", "weight": 9}]}
    with pytest.raises(PipelineLoadError, match="unknown keys"):
        parse_pipeline(bad)


def test_load_pipeline_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PipelineLoadError, match="not found"):
        load_pipeline(tmp_path / "nope.yaml")


# ------------------------------------------------------------------ validation


def test_cyclic_chain_raises_with_cycle_path() -> None:
    bad = {"default_chain": [
        {"role": "a", "on": "x"},
        {"role": "b", "after": "a"},
        {"role": "c", "after": "b"},
    ]}
    spec = parse_pipeline(bad)
    # force a cycle by mutating spec via re-parse
    bad["default_chain"][0] = {"role": "a", "after": "c"}
    spec = parse_pipeline(bad)
    with pytest.raises(ValidationError) as exc:
        build_dag(spec)
    # cycle path must mention a, b, c
    msg = str(exc.value)
    assert "cycle" in msg.lower()
    for r in ("a", "b", "c"):
        assert r in msg


def test_unknown_role_reference_raises() -> None:
    bad = {"default_chain": [
        {"role": "po", "on": "ready"},
        {"role": "worker", "after": "nope"},
    ]}
    spec = parse_pipeline(bad)
    with pytest.raises(ValidationError) as exc:
        build_dag(spec)
    assert "nope" in str(exc.value)
    assert "worker" in str(exc.value)


def test_ambiguous_after_raises() -> None:
    """Non-head step without 'after:' and without 'on:' is ambiguous."""
    bad = {"default_chain": [
        {"role": "po", "on": "ready"},
        {"role": "worker"},  # no after, no on, position > 0 → ambiguous
    ]}
    spec = parse_pipeline(bad)
    with pytest.raises(ValidationError, match="ambiguous"):
        build_dag(spec)


# -------------------------------------------------------------------- execute


def _record_events(sink: list[dict[str, Any]]):
    return lambda e: sink.append(e)


def test_executor_runs_chain_in_order_with_conditional_skip() -> None:
    spec = parse_pipeline(yaml.safe_load(SAMPLE_YAML))
    dag = build_dag(spec)
    calls: list[str] = []

    def make_handler(role: str, ok: bool = True):
        def h(ctx: StepContext) -> StepOutcome:
            calls.append(role)
            return StepOutcome(role=role, status="ok" if ok else "failed")
        return h

    handlers = {r: make_handler(r) for r in ("po", "worker", "critic",
                                              "security-reviewer", "merge")}
    events: list[dict[str, Any]] = []
    ex = PipelineExecutor(dag, handlers, events=_record_events(events))

    # Issue lacks the "security-sensitive" label → security-reviewer skipped
    result = ex.run({"number": 1, "labels": []})
    assert calls == ["po", "worker", "critic", "merge"]
    assert result.outcomes["security-reviewer"].status == "skipped"
    assert result.outcomes["merge"].status == "ok"
    assert result.end_state == "ok"

    kinds = [e["kind"] for e in events]
    assert "step_skipped" in kinds
    assert any(e.get("role") == "security-reviewer" and e["kind"] == "step_skipped"
               for e in events)


def test_executor_runs_security_reviewer_when_label_present() -> None:
    spec = parse_pipeline(yaml.safe_load(SAMPLE_YAML))
    dag = build_dag(spec)
    calls: list[str] = []
    handlers = {
        r: (lambda role: lambda ctx: (calls.append(role),
                                       StepOutcome(role=role, status="ok"))[1])(r)
        for r in ("po", "worker", "critic", "security-reviewer", "merge")
    }
    ex = PipelineExecutor(dag, handlers)
    result = ex.run({"number": 7, "labels": [{"name": "security-sensitive"}]})
    assert "security-reviewer" in calls
    assert result.outcomes["security-reviewer"].status == "ok"


def test_executor_handler_exception_becomes_failed_outcome() -> None:
    spec = parse_pipeline({"default_chain": [{"role": "a", "on": "x"}]})
    dag = build_dag(spec)

    def boom(_ctx):
        raise RuntimeError("kaboom")

    ex = PipelineExecutor(dag, {"a": boom})
    result = ex.run({"number": 1})
    assert result.outcomes["a"].status == "failed"
    assert "kaboom" in result.outcomes["a"].detail
    assert result.end_state == "failed"


def test_executor_missing_handler_becomes_failed() -> None:
    spec = parse_pipeline({"default_chain": [
        {"role": "a", "on": "x"},
        {"role": "b", "after": "a"},
    ]})
    dag = build_dag(spec)
    ex = PipelineExecutor(dag, {"a": lambda c: StepOutcome("a", "ok")})
    result = ex.run({"number": 2})
    assert result.outcomes["b"].status == "failed"
    assert "no handler" in result.outcomes["b"].detail


def test_executor_timeout_yields_partial_end_state() -> None:
    """Adversarial: a role mid-chain that times out — downstream
    conditional roles still evaluated, end state is 'partial' not
    'crashed'.
    """
    spec = parse_pipeline({"default_chain": [
        {"role": "po", "on": "ready"},
        {"role": "worker", "after": "po"},
        {"role": "critic", "after": "worker"},
        {"role": "merge", "after": "critic",
         "condition": {"all_approve": True}},
    ]})
    dag = build_dag(spec)

    def slow(_ctx):
        time.sleep(2.0)
        return StepOutcome("worker", "ok")

    handlers = {
        "po": lambda c: StepOutcome("po", "ok"),
        "worker": slow,
        "critic": lambda c: StepOutcome("critic", "ok"),
        "merge": lambda c: StepOutcome("merge", "ok"),
    }
    ex = PipelineExecutor(dag, handlers, step_timeout_s=0.2)
    result = ex.run({"number": 9})
    assert result.outcomes["worker"].status == "failed"
    assert "timeout" in result.outcomes["worker"].detail.lower()
    # critic still ran (no condition blocks it)
    assert result.outcomes["critic"].status == "ok"
    # merge requires all_approve → since worker failed, merge skips
    assert result.outcomes["merge"].status == "skipped"
    # NOT 'crashed' — we use 'partial' or 'failed'
    assert result.end_state in {"partial", "failed"}
    assert result.end_state != "crashed"


def test_executor_handler_returns_non_outcome_becomes_failed() -> None:
    spec = parse_pipeline({"default_chain": [{"role": "x", "on": "y"}]})
    dag = build_dag(spec)
    ex = PipelineExecutor(dag, {"x": lambda c: "oops"})  # wrong return type
    r = ex.run({"number": 0})
    assert r.outcomes["x"].status == "failed"


# ------------------------------------------------------------------ ascii art


def test_dag_render_ascii_shape() -> None:
    spec = parse_pipeline(yaml.safe_load(SAMPLE_YAML))
    art = build_dag(spec).render_ascii()
    assert "po" in art
    assert "worker (parallel=3)" in art
    assert "security-sensitive" in art
    assert "all_approve" in art
    # has a connector line
    assert "│" in art


# ------------------------------------------------------------ integration test


def test_integration_chain_runs_end_to_end_with_conditional() -> None:
    """Integration: chain with conditional step, execution order matches
    and the condition-miss skips cleanly."""
    spec = parse_pipeline(yaml.safe_load(SAMPLE_YAML))
    dag = build_dag(spec)

    sequence: list[str] = []

    def mk(role: str):
        def h(ctx: StepContext) -> StepOutcome:
            sequence.append(role)
            # critic returns 'ok' so all_approve gate passes on merge
            return StepOutcome(role=role, status="ok")
        return h

    handlers = {r: mk(r) for r in ("po", "worker", "critic",
                                    "security-reviewer", "merge")}
    events: list[dict[str, Any]] = []
    ex = PipelineExecutor(dag, handlers, events=lambda e: events.append(e))

    # condition-miss path
    res = ex.run({"number": 11, "labels": ["routine"]})
    assert sequence == ["po", "worker", "critic", "merge"]
    assert res.outcomes["security-reviewer"].status == "skipped"
    skip_events = [e for e in events if e.get("kind") == "step_skipped"]
    assert len(skip_events) == 1
    assert skip_events[0]["role"] == "security-reviewer"

    # condition-hit path
    sequence.clear()
    ex.run({"number": 12, "labels": ["security-sensitive"]})
    assert "security-reviewer" in sequence
    # merge runs after both critic and security-reviewer
    assert sequence.index("merge") > sequence.index("security-reviewer")
