"""Tests for the codebase-state auditor (issue #156).

Covers the framework + the first probe (file-size) + ticket-filing
idempotency. Hits both the manifesto T1 (state-machine edges) and T2
(adversarial / sad path) rules:

* Happy paths: clean repo emits an ``audit_clean`` filing; a planted
  oversized file produces a violation that becomes a ticket.
* Adversarial: probe crashes are isolated; ``--apply`` against an empty
  report is a no-op; second-run dedup skips already-filed targets;
  empty / binary / unreadable files don't crash the LOC counter.
* Plus a CLI integration that drives ``cmd_audit`` against a planted
  worktree.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from forge_loop.audit_probes.file_size import (
    DEFAULT_THRESHOLDS,
    FileSizeProbe,
    _count_significant_lines,
)
from forge_loop.audit_probes.function_size import (
    DEFAULT_MAX_LINES,
    FunctionSizeProbe,
)
from forge_loop.codebase_audit import (
    AUDIT_AXIS_LABEL,
    PROBE_LABEL_PREFIX,
    AuditReport,
    Violation,
    audit,
    file_violations,
    render_ticket_body,
    walk_source_files,
)
from forge_loop.events import AuditCleanEvent, AuditViolationFiledEvent
from forge_loop.gh_client import Issue, MockGhClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plant_py_file(path: Path, n_significant: int) -> None:
    """Write a .py file with exactly ``n_significant`` significant lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"x_{i} = {i}" for i in range(n_significant)]
    path.write_text("\n".join(lines) + "\n")


@dataclass
class StubProbe:
    name: str = "stub"
    output: list[Violation] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.output is None:
            self.output = []

    def scan(self, repo: Path) -> Iterable[Violation]:  # noqa: ARG002
        return list(self.output)


@dataclass
class BoomProbe:
    name: str = "boom"

    def scan(self, repo: Path) -> Iterable[Violation]:  # noqa: ARG002
        raise RuntimeError("probe blew up")


def _violation(probe: str = "file-size", target: str = "src/x.py") -> Violation:
    return Violation(
        probe=probe,
        target=target,
        severity=2,
        title=f"refactor({target}): split",
        rationale="manifesto cap exceeded",
        acceptance=["split it"],
        metrics={"loc": 1000, "soft_cap": 500},
    )


# ---------------------------------------------------------------------------
# walk_source_files
# ---------------------------------------------------------------------------


def test_walk_source_files_skips_vendor_dirs(tmp_path: Path) -> None:
    _plant_py_file(tmp_path / "src" / "ok.py", 5)
    _plant_py_file(tmp_path / ".venv" / "lib" / "skip.py", 5)
    _plant_py_file(tmp_path / "node_modules" / "skip.py", 5)
    _plant_py_file(tmp_path / "__pycache__" / "skip.py", 5)

    found = {p.name for p in walk_source_files(tmp_path)}
    assert found == {"ok.py"}


def test_walk_source_files_suffix_filter(tmp_path: Path) -> None:
    _plant_py_file(tmp_path / "a.py", 1)
    (tmp_path / "b.md").write_text("# not source")
    found = {p.suffix for p in walk_source_files(tmp_path)}
    assert ".md" not in found
    assert ".py" in found


# ---------------------------------------------------------------------------
# _count_significant_lines — T5 (small property surface) + adversarial
# ---------------------------------------------------------------------------


def test_count_significant_lines_skips_blanks_and_comments(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text(
        "\n".join(
            [
                "# header comment",
                "",
                "x = 1",
                "    ",
                "// not python but still a comment-shape",
                "y = 2",
                "  # indented comment",
            ]
        )
    )
    assert _count_significant_lines(f) == 2


def test_count_significant_lines_handles_missing_file(tmp_path: Path) -> None:
    # No crash on a path that doesn't exist; just returns 0.
    assert _count_significant_lines(tmp_path / "nope.py") == 0


def test_count_significant_lines_handles_binary(tmp_path: Path) -> None:
    f = tmp_path / "binary.py"
    f.write_bytes(b"\xff\xfe\x00\x01garbage\x80\x90")
    # Either 0 (decode failure) or a positive count — the contract is
    # "doesn't raise". Assert no exception and a non-negative result.
    assert _count_significant_lines(f) >= 0


# ---------------------------------------------------------------------------
# FileSizeProbe — happy + threshold edges
# ---------------------------------------------------------------------------


def test_file_size_probe_clean_repo_yields_nothing(tmp_path: Path) -> None:
    _plant_py_file(tmp_path / "src" / "small.py", 10)
    probe = FileSizeProbe(thresholds={".py": 500})
    assert list(probe.scan(tmp_path)) == []


def test_file_size_probe_flags_oversized_file(tmp_path: Path) -> None:
    _plant_py_file(tmp_path / "src" / "huge.py", 1700)
    probe = FileSizeProbe(thresholds={".py": 500})
    violations = list(probe.scan(tmp_path))
    assert len(violations) == 1
    v = violations[0]
    assert v.probe == "file-size"
    assert v.target == "src/huge.py"
    assert v.metrics["loc"] == 1700
    assert v.metrics["soft_cap"] == 500
    # 1700/500 = 3.4 ≥ default hard multiplier (2.0) → severity 1.
    assert v.severity == 1


def test_file_size_probe_at_threshold_is_not_flagged(tmp_path: Path) -> None:
    _plant_py_file(tmp_path / "edge.py", 500)
    probe = FileSizeProbe(thresholds={".py": 500})
    assert list(probe.scan(tmp_path)) == []


def test_file_size_probe_severity_2_below_hard_multiplier(tmp_path: Path) -> None:
    _plant_py_file(tmp_path / "med.py", 700)
    probe = FileSizeProbe(thresholds={".py": 500}, hard_threshold_multiplier=2.0)
    [v] = list(probe.scan(tmp_path))
    assert v.severity == 2


def test_file_size_probe_per_language_thresholds(tmp_path: Path) -> None:
    # Java soft cap is 600, TS is 400.
    (tmp_path / "Big.java").write_text(
        "\n".join(f"int v{i} = {i};" for i in range(601))
    )
    (tmp_path / "big.ts").write_text(
        "\n".join(f"const v{i} = {i};" for i in range(401))
    )
    (tmp_path / "small.ts").write_text("const x = 1;\n")
    probe = FileSizeProbe(thresholds=DEFAULT_THRESHOLDS)
    targets = {v.target for v in probe.scan(tmp_path)}
    assert "Big.java" in targets
    assert "big.ts" in targets
    assert "small.ts" not in targets


# ---------------------------------------------------------------------------
# FunctionSizeProbe — Q8 god-function state-gate (issue #306)
# ---------------------------------------------------------------------------


def _plant_py_function(path: Path, body_lines: int, name: str = "big_func") -> None:
    """Write a .py file with one function whose body has ``body_lines`` sig lines.

    Significant LOC counted by the probe = 1 (the ``def`` line) + ``body_lines``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"def {name}():"]
    lines.extend(f"    y_{i} = {i}" for i in range(body_lines))
    path.write_text("\n".join(lines) + "\n")


def test_function_size_probe_small_file_yields_nothing(tmp_path: Path) -> None:
    _plant_py_function(tmp_path / "src" / "small.py", body_lines=10)
    probe = FunctionSizeProbe()
    assert list(probe.scan(tmp_path)) == []


def test_function_size_probe_flags_god_function(tmp_path: Path) -> None:
    # 1 def line + 90 body lines = 91 significant LOC > 80 cap.
    _plant_py_function(tmp_path / "src" / "huge.py", body_lines=90)
    probe = FunctionSizeProbe()
    violations = list(probe.scan(tmp_path))
    assert len(violations) == 1
    v = violations[0]
    assert v.probe == "function-size"
    assert v.target == "src/huge.py::big_func"
    assert v.metrics["loc"] == 91
    assert v.metrics["max_lines"] == DEFAULT_MAX_LINES
    # 91/80 = 1.14 < default hard multiplier (2.0) → severity 2.
    assert v.severity == 2


def test_function_size_probe_at_cap_is_not_flagged(tmp_path: Path) -> None:
    # 1 def line + 79 body lines = 80 significant LOC == cap → not flagged.
    _plant_py_function(tmp_path / "edge.py", body_lines=79)
    probe = FunctionSizeProbe()
    assert list(probe.scan(tmp_path)) == []


def test_function_size_probe_severity_1_above_hard_multiplier(tmp_path: Path) -> None:
    # 1 def line + 200 body lines = 201 LOC; 201/80 = 2.5 ≥ 2.0 → severity 1.
    _plant_py_function(tmp_path / "monster.py", body_lines=200)
    [v] = list(FunctionSizeProbe().scan(tmp_path))
    assert v.severity == 1


def test_function_size_probe_qualifies_nested_names(tmp_path: Path) -> None:
    src = (
        "class Foo:\n"
        "    def method(self):\n"
        + "".join(f"        z_{i} = {i}\n" for i in range(90))
    )
    (tmp_path / "nested.py").write_text(src)
    [v] = list(FunctionSizeProbe().scan(tmp_path))
    assert v.target == "nested.py::Foo.method"


def test_function_size_probe_ignores_unparseable_file(tmp_path: Path) -> None:
    """Adversarial: a syntactically broken .py must not crash the scan."""
    (tmp_path / "broken.py").write_text("def oops(:\n    pass\n")
    # No exception, no violations.
    assert list(FunctionSizeProbe().scan(tmp_path)) == []


def test_function_size_probe_registered_in_audit(tmp_path: Path) -> None:
    """Integration: audit() runs the probe and aggregates its violations."""
    _plant_py_function(tmp_path / "huge.py", body_lines=90)
    report = audit(tmp_path)  # default_probes()
    assert "function-size" in report.probes_run
    assert any(v.probe == "function-size" for v in report.violations)


# ---------------------------------------------------------------------------
# audit() — orchestration + error isolation
# ---------------------------------------------------------------------------


def test_audit_runs_every_probe_and_aggregates(tmp_path: Path) -> None:
    v1 = _violation(probe="a", target="x")
    v2 = _violation(probe="b", target="y")
    report = audit(tmp_path, probes=[StubProbe(name="a", output=[v1]), StubProbe(name="b", output=[v2])])
    assert report.probes_run == ["a", "b"]
    assert report.violations == [v1, v2]
    assert report.errors == {}
    assert not report.is_clean


def test_audit_isolates_probe_crash(tmp_path: Path) -> None:
    # Adversarial — one probe blows up, the other still runs.
    good = _violation(probe="good", target="z")
    report = audit(tmp_path, probes=[BoomProbe(), StubProbe(name="good", output=[good])])
    assert "boom" in report.errors
    assert "RuntimeError" in report.errors["boom"]
    assert report.violations == [good]
    assert report.probes_run == ["boom", "good"]


def test_audit_clean_repo_with_real_probe(tmp_path: Path) -> None:
    _plant_py_file(tmp_path / "small.py", 5)
    report = audit(tmp_path, probes=[FileSizeProbe(thresholds={".py": 500})])
    assert report.is_clean
    assert report.probes_run == ["file-size"]


# ---------------------------------------------------------------------------
# Ticket filing — happy + idempotent dedup + adversarial create failure
# ---------------------------------------------------------------------------


def test_file_violations_files_one_ticket_per_violation() -> None:
    report = AuditReport(
        violations=[_violation(target="a.py"), _violation(target="b.py")],
        probes_run=["file-size"],
    )
    gh = MockGhClient()
    filed: list[tuple[Violation, int]] = []

    outcome = file_violations(
        report, gh, owner="o", repo="r",
        emit_filed=lambda v, n: filed.append((v, n)),
    )

    assert len(outcome.filed) == 2
    assert outcome.skipped == []
    assert outcome.errors == {}
    assert len(filed) == 2
    # Labels must include axis + probe-label.
    create_calls = [kw for (m, kw) in gh.calls if m == "create_issue"]
    assert len(create_calls) == 2
    for kw in create_calls:
        assert AUDIT_AXIS_LABEL in kw["labels"]
        assert f"{PROBE_LABEL_PREFIX}file-size" in kw["labels"]


def test_file_violations_clean_report_emits_clean_event_only() -> None:
    report = AuditReport(violations=[], probes_run=["file-size"])
    gh = MockGhClient()
    cleans: list[list[str]] = []

    outcome = file_violations(
        report, gh, owner="o", repo="r",
        emit_clean=cleans.append,
    )

    # No tickets created.
    assert not [m for (m, _) in gh.calls if m == "create_issue"]
    assert outcome.filed == [] and outcome.skipped == [] and outcome.errors == {}
    assert cleans == [["file-size"]]


def test_file_violations_is_idempotent_via_dedup() -> None:
    """A second filing run with the same target must not re-create the ticket."""
    v = _violation(target="src/huge.py")
    body = render_ticket_body(v)
    # Seed the mock with an existing open ticket carrying both labels +
    # the footer the dedup logic parses.
    existing = Issue(
        number=42,
        title=v.title,
        body=body,
        state="open",
        labels=[AUDIT_AXIS_LABEL, v.probe_label],
    )
    gh = MockGhClient(
        issues={("o", "r", 42): existing},
        issues_by_label_response=[existing],
    )

    report = AuditReport(violations=[v], probes_run=["file-size"])
    outcome = file_violations(report, gh, owner="o", repo="r")

    assert outcome.filed == []
    assert outcome.skipped == [v]
    assert not [m for (m, _) in gh.calls if m == "create_issue"]


def test_file_violations_surfaces_create_failure() -> None:
    from forge_loop.gh_client import GhError

    v = _violation(target="src/x.py")
    gh = MockGhClient(
        raise_on_create_titles={v.title: GhError("create_issue", 422, "boom")},
    )
    report = AuditReport(violations=[v], probes_run=["file-size"])

    outcome = file_violations(report, gh, owner="o", repo="r")

    assert outcome.filed == []
    assert outcome.skipped == []
    assert any("file-size:src/x.py" in k for k in outcome.errors)


def test_render_ticket_body_contains_footer_and_acceptance() -> None:
    v = _violation(target="src/huge.py")
    body = render_ticket_body(v)
    # Footer is the dedup contract — assert exact shape.
    assert "probe `file-size`, target `src/huge.py`" in body
    # Acceptance bullets surface in the body.
    assert "## Acceptance" in body
    assert "- split it" in body


# ---------------------------------------------------------------------------
# Typed events — schema validation
# ---------------------------------------------------------------------------


def test_audit_events_are_typed_and_validated() -> None:
    e = AuditViolationFiledEvent(
        probe="file-size", target="x.py", severity=2,
        issue_number=99, title="t",
    )
    rec = e.to_record()
    assert rec["kind"] == "audit_violation_filed"
    assert rec["issue_number"] == 99

    # Out-of-range severity is rejected by pydantic.
    with pytest.raises(ValidationError):
        AuditViolationFiledEvent(severity=99)  # ge=1, le=5

    clean = AuditCleanEvent(probes_run=["a", "b"])
    assert clean.to_record()["probes_run"] == ["a", "b"]


# ---------------------------------------------------------------------------
# CLI integration — drives `_cmd_audit` end-to-end with a planted repo
# ---------------------------------------------------------------------------


def test_cli_audit_dry_run_no_gh_calls(tmp_path: Path, monkeypatch) -> None:
    """`forge-loop audit` default path must not hit gh — even with violations."""
    from forge_loop import cli

    _plant_py_file(tmp_path / "src" / "huge.py", 600)

    monkeypatch.chdir(tmp_path)
    # Make load() fail so we exercise the "no config" branch — audit must
    # still succeed on the dry-run path.
    monkeypatch.setattr(cli, "load", lambda: (_ for _ in ()).throw(RuntimeError("nope")))

    gh_calls: list = []

    def _no_gh():
        gh_calls.append("called")
        raise AssertionError("gh client must not be constructed on dry-run")

    monkeypatch.setattr(cli, "_gh_client_factory", _no_gh)

    rc = cli._cmd_audit(SimpleNamespace(apply=False, json=False))
    assert rc == 0
    assert gh_calls == []


def test_cli_audit_json_emits_violations(tmp_path: Path, monkeypatch, capsys) -> None:
    from forge_loop import cli

    _plant_py_file(tmp_path / "src" / "huge.py", 800)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load", lambda: (_ for _ in ()).throw(RuntimeError("nope")))

    rc = cli._cmd_audit(SimpleNamespace(apply=False, json=True))
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["probes_run"] == ["file-size", "function-size"]
    assert any(
        v["target"] == "src/huge.py" for v in payload["violations"]
    )


def test_cli_audit_apply_requires_github_repo(tmp_path: Path, monkeypatch) -> None:
    from forge_loop import cli

    _plant_py_file(tmp_path / "src" / "huge.py", 800)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "load", lambda: (_ for _ in ()).throw(RuntimeError("nope")))

    rc = cli._cmd_audit(SimpleNamespace(apply=True, json=False))
    # No owner/repo configured -> exit 2 with a clear message.
    assert rc == 2


def test_cli_audit_apply_files_tickets_via_mock_gh(
    tmp_path: Path, monkeypatch
) -> None:
    """End-to-end --apply path: plant a violation, drive CLI, see one create_issue call."""
    from forge_loop import cli

    _plant_py_file(tmp_path / "src" / "huge.py", 800)
    monkeypatch.chdir(tmp_path)

    cfg = SimpleNamespace(
        repo=tmp_path,
        github_repo="o/r",
        events_file=tmp_path / "events.jsonl",
    )
    monkeypatch.setattr(cli, "load", lambda: cfg)

    gh = MockGhClient()
    monkeypatch.setattr(cli, "_gh_client_factory", lambda: gh)

    rc = cli._cmd_audit(SimpleNamespace(apply=True, json=False))
    assert rc == 0
    create_calls = [kw for (m, kw) in gh.calls if m == "create_issue"]
    assert len(create_calls) == 1
    assert AUDIT_AXIS_LABEL in create_calls[0]["labels"]
    # Event was emitted to the configured file.
    assert cfg.events_file.exists()
    body = cfg.events_file.read_text()
    assert "audit_violation_filed" in body
