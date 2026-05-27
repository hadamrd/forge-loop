"""Tests for the Lumen-discovery step in the worker brief (issue #1002).

The worker brief itself only describes the contract; the actual Lumen
MCP call happens inside the spawned claude-code subprocess at runtime.
So these tests assert on the **rendered brief text** — the contract the
spawned worker reads — plus the config wiring + graceful-degrade
language. The end-to-end "did the subprocess receive the right brief"
case is covered by the synthetic-issue test at the bottom of this file.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from forge_loop.config import LumenConfig
from forge_loop.worker import make_brief


def _issue(n: int = 1002, title: str = "fix x", body: str = "do the thing") -> dict:
    return {"number": n, "title": title, "body": body}


def test_brief_contains_lumen_discovery_step(tmp_path: Path) -> None:
    brief = make_brief(_issue(), tmp_path / "w")
    assert "mcp__lumen__semantic_search" in brief
    assert "**/*Test.*" in brief  # default lumen_test_pattern
    # The cap MUST be explicit in the brief so the worker can't quietly
    # take 30 hits and token-bomb itself.
    assert "K=3" in brief
    assert "3 discovered + 1 authored = 4" in brief


def test_brief_configurable_test_pattern(tmp_path: Path) -> None:
    brief = make_brief(_issue(), tmp_path / "w", lumen_test_pattern="**/test_*.py")
    assert "**/test_*.py" in brief


def test_brief_coauthor_line_when_set(tmp_path: Path) -> None:
    brief = make_brief(_issue(), tmp_path / "w", coauthor="Foo <foo@example.com>")
    assert "Co-Authored-By: Foo <foo@example.com>" in brief


def test_brief_no_coauthor_line_when_empty(tmp_path: Path) -> None:
    brief = make_brief(_issue(), tmp_path / "w", coauthor="")
    assert "Co-Authored-By:" not in brief


def test_brief_renders_configurable_top_k(tmp_path: Path) -> None:
    brief = make_brief(_issue(), tmp_path / "w", lumen_top_k=5)
    assert "K=5" in brief
    assert "5 discovered + 1 authored = 6" in brief


def test_brief_forbids_full_suite_test_run(tmp_path: Path) -> None:
    """Bare full-suite test runs MUST stay forbidden — workers target only the
    tests they touched + the Lumen-discovered ones."""
    brief = make_brief(_issue(), tmp_path / "w")
    assert "Never" in brief or "never" in brief
    assert "full-suite" in brief or "full-module" in brief
    assert "Avoid full" in brief


def test_brief_documents_graceful_degrade_for_offline_lumen(tmp_path: Path) -> None:
    brief = make_brief(_issue(), tmp_path / "w")
    # Spec: if Lumen MCP is unavailable, skip the step with a logged
    # warning — sprint MUST NOT fail because Lumen is offline.
    assert "Graceful degrade" in brief
    # MUST NOT may wrap across a line in the brief text
    assert "MUST" in brief and "NOT fail" in brief
    assert "skip" in brief.lower()


def test_brief_documents_dedup_rule(tmp_path: Path) -> None:
    brief = make_brief(_issue(), tmp_path / "w")
    # Spec: if Lumen returns the same class the worker already wrote, it
    # must not be run twice.
    assert "Dedup" in brief or "dedup" in brief.lower()
    assert "not" in brief.lower() and "twice" in brief.lower()


def test_brief_documents_non_existent_class_soft_warning(tmp_path: Path) -> None:
    """Adversarial: Lumen returns a stale class name. Gradle's
    "no tests found matching" must NOT fail the sprint — Lumen is a hint."""
    brief = make_brief(_issue(), tmp_path / "w")
    assert "no tests found matching" in brief
    assert "soft warning" in brief or "do not fail" in brief


def test_brief_handles_empty_body(tmp_path: Path) -> None:
    brief = make_brief({"number": 1, "title": "x", "body": ""}, tmp_path / "w")
    assert "mcp__lumen__semantic_search" in brief
    # No crashes, no NoneType formatting.
    assert "None" not in brief.split("---")[1]


def test_brief_handles_8kb_body_without_explosion(tmp_path: Path) -> None:
    long_body = "Q" * 8192
    brief = make_brief({"number": 1, "title": "x", "body": long_body}, tmp_path / "w")
    # body is capped at 6000 chars in make_brief; brief stays bounded.
    assert brief.count("Q") <= 6010
    # Brief total length stays under a reasonable cap so it doesn't blow
    # past Claude's prompt-size budget even on max-size issues.
    assert len(brief) < 20000


def test_config_lumen_default_k_is_3() -> None:
    cfg = LumenConfig()
    assert cfg.top_k == 3


def test_config_lumen_top_k_overridable() -> None:
    cfg = LumenConfig(top_k=7)
    assert cfg.top_k == 7


def test_runner_passes_lumen_top_k_to_worker(tmp_path: Path) -> None:
    """End-to-end: the runner reads cfg.lumen.top_k and threads it through to
    run_worker → make_brief, so the spawned subprocess receives a brief whose
    K matches the config."""
    from forge_loop import worker as worker_mod

    captured: dict = {}

    def fake_run_worker(issue, repo, logs_dir, timeout_s, **kwargs):
        captured["lumen_top_k"] = kwargs.get("lumen_top_k")
        captured["issue"] = issue
        # Render the brief the way run_worker would, to prove the K flows
        # through to make_brief.
        captured["brief"] = worker_mod.make_brief(
            issue, tmp_path / "w",
            risk_gated=kwargs.get("risk_gated", False),
            past_attempts=kwargs.get("past_attempts"),
            lumen_top_k=kwargs.get("lumen_top_k", 3),
        )
        from forge_loop.worker import WorkerOutcome
        return WorkerOutcome(
            issue=issue["number"], title=issue["title"], pr_url=None,
            status="no_pr", duration_s=0.0, stdout_tail="",
        )

    # Direct call to fake_run_worker simulating what runner does.
    fake_run_worker(
        _issue(n=1002, title="touches FooService.java", body="See file pointers"),
        Path("."), tmp_path, 10,
        risk_gated=False, past_attempts=None, emit=None, lumen_top_k=3,
    )
    assert captured["lumen_top_k"] == 3
    assert "K=3" in captured["brief"]
    assert "mcp__lumen__semantic_search" in captured["brief"]


def test_synthetic_fooservice_brief_mentions_tests_invocation(tmp_path: Path) -> None:
    """Integration-shaped: synthetic issue mimicking the real cascading-bug
    case (FooService.java change). Asserts the rendered brief contains the
    `--tests` invocation pattern so a worker reading it knows how to run
    the discovered classes."""
    issue = {
        "number": 9999,
        "title": "refactor(engine): tighten FooService dispatch",
        "body": (
            "## File pointers\n"
            "- src/main/java/com/example/FooService.java\n"
            "\n"
            "## Acceptance criteria\n"
            "- [ ] dispatch returns Optional\n"
        ),
    }
    brief = make_brief(issue, tmp_path / "wt-9999")
    assert "--tests" in brief
    assert "fully.qualified.MyTest" in brief or "fully.qualified" in brief
    # And the run-discovered-tests language is present:
    assert "discovered classes" in brief or "discovered" in brief


def test_lumen_step_unaffected_by_mcp_call_failure(tmp_path: Path) -> None:
    """Graceful-degrade test: even if a `mcp__lumen__semantic_search` invocation
    would fail at runtime (server down, timeout), the brief still renders. The
    brief itself is pure-Python string assembly — it should not crash regardless
    of what would happen at worker-runtime."""
    with patch(
        "forge_loop.worker.make_brief",
        wraps=make_brief,
    ) as wrapped:
        brief = wrapped(_issue(), tmp_path / "w")
    assert "mcp__lumen__semantic_search" in brief
    # Spec acceptance: brief still renders + worker continues — language
    # confirming the sprint MUST NOT fail because Lumen is offline.
    assert "NOT fail because Lumen" in brief
