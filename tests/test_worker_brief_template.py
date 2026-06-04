"""Guard test (#144): the worker brief must forbid worktree pip-editable installs.

A future template refactor must not silently drop the prohibition rule, so we
render the real brief and assert the prohibition + recommended alternative are
present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_loop.briefs import load_template
from forge_loop.worker import make_brief


def test_rendered_brief_forbids_pip_editable_against_worktree(tmp_path: Path) -> None:
    issue = {"number": 144, "title": "guardrails", "body": "body"}
    brief = make_brief(issue, tmp_path / "wt-144")
    assert "pip install -e" in brief
    assert "pip install ." in brief
    # The prohibition must be unambiguous, not a passing mention.
    assert "NEVER run" in brief
    assert "poison" in brief.lower()


def test_rendered_brief_names_uv_venv_alternative(tmp_path: Path) -> None:
    issue = {"number": 144, "title": "guardrails", "body": "body"}
    brief = make_brief(issue, tmp_path / "wt-144")
    assert "uv venv" in brief
    assert "uv pip install -e" in brief


def test_raw_template_carries_the_rule() -> None:
    # Belt-and-suspenders: assert against the source template too, so the rule
    # survives even if make_brief stops injecting capability sections etc.
    template = load_template("worker")
    assert "ENVIRONMENT SAFETY" in template
    assert "pip install -e" in template


@pytest.mark.parametrize(
    "issue",
    [
        {"number": 1, "title": "t", "body": ""},
        {"number": 2, "title": "t", "body": None},
        {"number": 3, "title": "t", "body": "x" * 20000},
    ],
)
def test_rule_present_across_varied_issue_bodies(issue: dict, tmp_path: Path) -> None:
    # Adversarial: empty / None / oversized bodies must not displace the rule.
    brief = make_brief(issue, tmp_path / "wt")
    assert "pip install -e" in brief
    assert "ENVIRONMENT SAFETY" in brief
