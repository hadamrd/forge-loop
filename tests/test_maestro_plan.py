"""Maestro tick: frontier+memory-driven dispatch planning."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from forge_loop.frontier import FrontierCursor, FrontierStore, HotArtifact, RejectedPath
from forge_loop.memory import MemoryItem, MemoryKind, MemoryProvenance, SqliteMemoryStore
from forge_loop.memory.models import REJECTED_PATH_TAG
from forge_loop.runner import dispatch as dispatch_mod
from forge_loop.runner.maestro import build_maestro_plan, load_maestro_inputs
from forge_loop.worker import WorkerOutcome
from tests.test_persistent_dispatch import _issue, _make_cfg, _meta


def _frontier(**kw: Any) -> FrontierCursor:
    base = {
        "product_goal": "make the loop resumable",
        "current_problem": "c",
        "next_expansion": "n",
        "why_now": "w",
    }
    base.update(kw)
    return FrontierCursor(**base)


def _issues(*specs: tuple[int, str]) -> list[dict[str, Any]]:
    return [{"number": n, "title": t, "body": "", "labels": []} for n, t in specs]


# --- planner -------------------------------------------------------------


def test_prioritizes_issue_matching_next_expansion() -> None:
    issues = _issues((1, "polish docs"), (2, "add a rate limiter to the api"))
    plan = build_maestro_plan(
        issues, frontier=_frontier(next_expansion="rate limiter"), rejected_path_titles=()
    )
    assert plan.prioritized_issue_numbers == (2, 1)


def test_hot_file_ref_prioritizes() -> None:
    issues = _issues((1, "unrelated"), (2, "fix dispatch.py race"))
    plan = build_maestro_plan(
        issues,
        frontier=_frontier(hot_files=(HotArtifact(ref="dispatch.py", why_hot="churny"),)),
        rejected_path_titles=(),
    )
    assert plan.prioritized_issue_numbers == (2, 1)


def test_rejected_path_deprioritized_not_dropped() -> None:
    issues = _issues((1, "rewrite everything in rust"), (2, "small fix"))
    plan = build_maestro_plan(
        issues,
        frontier=_frontier(
            rejected_paths=(RejectedPath(idea="rewrite everything in rust", reason="too big"),)
        ),
        rejected_path_titles=(),
    )
    # Still present (never dropped), but pushed last.
    assert set(plan.prioritized_issue_numbers) == {1, 2}
    assert plan.prioritized_issue_numbers[-1] == 1
    assert plan.deprioritized_issue_numbers == (1,)


def test_empty_inputs_preserve_order_and_context() -> None:
    issues = _issues((3, "a"), (1, "b"), (2, "c"))
    plan = build_maestro_plan(issues, frontier=None, rejected_path_titles=())
    assert plan.prioritized_issue_numbers == (3, 1, 2)
    assert plan.brief_context == ""
    assert plan.event_payload()["context_applied"] is False


def test_brief_context_names_goal_and_rejected() -> None:
    plan = build_maestro_plan(
        _issues((1, "x")),
        frontier=_frontier(next_expansion="ship recovery"),
        rejected_path_titles=("polling every second",),
    )
    ctx = plan.brief_context
    assert "make the loop resumable" in ctx
    assert "ship recovery" in ctx
    assert "polling every second" in ctx


# --- loader (best-effort I/O) -------------------------------------------


def test_load_inputs_none_on_uninitialised_repo(tmp_path: Path) -> None:
    cfg: Any = type("C", (), {"repo": tmp_path})()
    assert load_maestro_inputs(cfg) == (None, ())


def test_load_inputs_reads_real_stores(tmp_path: Path) -> None:
    forge = tmp_path / ".forge"
    FrontierStore(forge / "frontier.yaml").save(_frontier(next_expansion="recovery"))
    mem = SqliteMemoryStore(forge / "memory.db")
    mem.put(
        MemoryItem(
            memory_id="m1",
            kind=MemoryKind.PROCEDURAL,
            title="never poll the API every second",
            body="b",
            tags=(REJECTED_PATH_TAG,),
            provenance=MemoryProvenance(
                source_event=None, authored_by="t", source_task_ref="task:#1"
            ),
        )
    )
    cfg: Any = type("C", (), {"repo": tmp_path})()
    frontier, rejected = load_maestro_inputs(cfg)
    assert frontier is not None and frontier.next_expansion == "recovery"
    assert "never poll the API every second" in rejected


# --- dispatch threading --------------------------------------------------


def test_dispatch_forwards_maestro_context_to_worker(monkeypatch: Any, tmp_path: Any) -> None:
    cfg = _make_cfg(tmp_path)
    seen: dict[str, Any] = {}

    def fake_run_worker(*args: Any, **kwargs: Any) -> WorkerOutcome:
        seen["maestro_context"] = kwargs.get("maestro_context")
        return WorkerOutcome(
            issue=7, title="t", pr_url=None, status="open", duration_s=1.0, stdout_tail=""
        )

    monkeypatch.setattr(dispatch_mod, "run_worker", fake_run_worker)
    dispatch_mod._dispatch_one_worker(
        cfg,
        _issue(7),
        _meta(),
        tick=1,
        bus_emit=lambda *a, **k: None,
        store=None,
        maestro_context="=== MAESTRO CONTEXT ===",
    )
    assert seen["maestro_context"] == "=== MAESTRO CONTEXT ==="
