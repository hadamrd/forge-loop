"""Tests for the #144 critic rule: pip-editable-poison.

The critic flags **sev1** when a PR diff touches packaging files
(``pyproject.toml`` / ``setup.py`` / ``setup.cfg``) AND the worker session
log contains a ``pip install -e`` (or root ``pip install .``) invocation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge_loop import critic as critic_mod
from forge_loop.critic import (
    PIP_EDITABLE_POISON_TAG,
    detect_pip_editable_poison,
)

PACKAGING = ["pyproject.toml", "src/forge_loop/cli.py"]


# --------------------------------------------------------------------------
# Pure detection
# --------------------------------------------------------------------------


def test_editable_install_plus_packaging_diff_is_sev1() -> None:
    report = detect_pip_editable_poison("pip install -e .", changed_files=PACKAGING)
    assert report.has_sev1()
    finding = report.findings[0]
    assert finding.severity == "sev1"
    assert PIP_EDITABLE_POISON_TAG in finding.message


def test_plain_pip_install_plus_packaging_diff_is_clean() -> None:
    # Negative AC: a regular dependency install must NOT trip the rule.
    report = detect_pip_editable_poison("pip install requests", changed_files=PACKAGING)
    assert not report.has_sev1()
    assert report.findings == []


def test_editable_install_without_packaging_diff_is_clean() -> None:
    # Both conditions are required: no packaging file touched → no finding.
    report = detect_pip_editable_poison(
        "pip install -e .", changed_files=["docs/readme.md", "src/forge_loop/cli.py"]
    )
    assert not report.has_sev1()


def test_python_m_pip_editable_form_is_flagged() -> None:
    report = detect_pip_editable_poison("python -m pip install -e .", changed_files=PACKAGING)
    assert report.has_sev1()


def test_root_pip_install_dot_is_flagged() -> None:
    report = detect_pip_editable_poison("pip install .", changed_files=PACKAGING)
    assert report.has_sev1()


def test_editable_install_with_setup_py_diff_is_flagged() -> None:
    report = detect_pip_editable_poison(
        "pip install -e /tmp/wt-loop-1", changed_files=["setup.py"]
    )
    assert report.has_sev1()


def test_empty_inputs_are_clean() -> None:
    assert not detect_pip_editable_poison("", changed_files=[]).has_sev1()
    assert not detect_pip_editable_poison("pip install -e .", changed_files=[]).has_sev1()


@pytest.mark.parametrize(
    "command",
    ["pip install requests", "pip install numpy pandas", "pip download -e .", "pip uninstall foo"],
)
def test_non_editable_commands_never_flag(command: str) -> None:
    assert not detect_pip_editable_poison(command, changed_files=PACKAGING).has_sev1()


def test_setup_cfg_path_with_subdir_matches_on_basename() -> None:
    report = detect_pip_editable_poison(
        "pip install -e .", changed_files=["packages/x/setup.cfg"]
    )
    assert report.has_sev1()


# --------------------------------------------------------------------------
# Adversarial / known-limitation: indirection through Makefile / shell
# --------------------------------------------------------------------------


def test_indirect_editable_via_makefile_is_known_limitation() -> None:
    """A worker can hide `pip install -e .` behind a Makefile target.

    The session-log scan keys on the literal command string, so a
    ``make install`` whose recipe runs ``pip install -e .`` (the recipe text
    never appearing in the command log) is NOT caught. This is an explicit,
    documented known limitation (full coverage is out of scope for #144). If
    the recipe text DOES appear in the log, it is caught — asserted below.
    """
    # `make install` alone — recipe body not in the log → not caught.
    assert not detect_pip_editable_poison("make install", changed_files=PACKAGING).has_sev1()
    # But if the expanded recipe shows up in the log, the rule still fires.
    expanded = "make install\n\tpip install -e ."
    assert detect_pip_editable_poison(expanded, changed_files=PACKAGING).has_sev1()


# --------------------------------------------------------------------------
# Integration through the deterministic-findings merge path
# --------------------------------------------------------------------------


def _write_worker_log(logs_dir: Path, issue: int, command: str) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / f"worker-{issue}-1.log").write_text(
        json.dumps(
            {"type": "item.started", "item": {"type": "command_execution", "command": command}}
        )
        + "\n",
        encoding="utf-8",
    )


def test_merge_path_adds_sev1_when_log_and_diff_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forge_loop.critic import CriticReport, _with_deterministic_pip_editable_findings

    logs = tmp_path / "logs"
    _write_worker_log(logs, 144, "pip install -e .")
    monkeypatch.setattr(
        critic_mod, "_fetch_pr_changed_files", lambda _pr, _repo: ["pyproject.toml"]
    )

    base = CriticReport(overall="approve", findings=[])
    out = _with_deterministic_pip_editable_findings(
        base, pr_url="x", repo=tmp_path, issue_number=144, logs_dir=logs
    )
    assert out.has_sev1()
    assert out.overall == "request_changes"


def test_merge_path_clean_when_only_plain_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forge_loop.critic import CriticReport, _with_deterministic_pip_editable_findings

    logs = tmp_path / "logs"
    _write_worker_log(logs, 144, "pip install requests")
    monkeypatch.setattr(
        critic_mod, "_fetch_pr_changed_files", lambda _pr, _repo: ["pyproject.toml"]
    )

    base = CriticReport(overall="approve", findings=[])
    out = _with_deterministic_pip_editable_findings(
        base, pr_url="x", repo=tmp_path, issue_number=144, logs_dir=logs
    )
    assert not out.has_sev1()
    assert out.overall == "approve"


def test_merge_path_no_worker_log_is_noop(tmp_path: Path) -> None:
    from forge_loop.critic import CriticReport, _with_deterministic_pip_editable_findings

    logs = tmp_path / "logs"
    logs.mkdir()
    base = CriticReport(overall="approve", findings=[])
    out = _with_deterministic_pip_editable_findings(
        base, pr_url="x", repo=tmp_path, issue_number=144, logs_dir=logs
    )
    assert out is base
