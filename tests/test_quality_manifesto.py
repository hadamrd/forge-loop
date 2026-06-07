"""Regression guard for the quality manifesto's de-anchoring (issue #306).

The quality manifesto used to cite hardcoded, now-stale anchors that the
audit/groom path treated as live facts, generating phantom tickets for work
already done:

* "``src/forge_loop/cli.py`` is **1705 LOC**" — cli.py shrank.
* "``runner/tick.py::_tick()`` is **582 lines**" — ``_tick`` was decomposed.
* references to the deleted ``gh.py`` module.

A frozen number in a manifesto rule is a lie the moment the codebase moves.
These tests freeze the fix so a future edit cannot reintroduce a dead anchor,
and assert the manifesto still loads through its normal consumer.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from forge_loop.manifestos import discover_manifestos

# Repo root = parent of the ``tests/`` directory holding this file.
REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFESTO_PATH = REPO_ROOT / ".forge" / "quality-manifesto.md"

# Same expression as the acceptance-criteria grep: ``1705|582|gh\.py``.
# ``gh\.py`` is a literal dot, so ``gh_issues.py`` / ``gh_client.py`` must NOT
# match — that false-positive guard is asserted explicitly below.
STALE_ANCHOR_RE = re.compile(r"1705|582|gh\.py")


def test_manifesto_has_no_stale_anchors() -> None:
    """The three de-anchored citations stay gone — mirrors the grep gate."""
    text = MANIFESTO_PATH.read_text(encoding="utf-8")
    matches = STALE_ANCHOR_RE.findall(text)
    assert matches == [], f"stale manifesto anchors reintroduced: {matches!r}"


def test_manifesto_loads_via_normal_consumer() -> None:
    """The de-anchored file still parses through ``discover_manifestos``."""
    manifestos = discover_manifestos(REPO_ROOT, required=True)
    markdown = manifestos.quality.markdown
    assert markdown.strip(), "quality manifesto must be non-empty after edit"
    # The surviving live surfaces must still be named in the reuse rationale.
    assert "gh_issues.py" in markdown
    assert "gh_client.py" in markdown


def test_guard_bites_on_reverted_manifesto(tmp_path: Path) -> None:
    """Adversarial: a manifesto carrying the old anchors must FAIL the guard.

    Proves the regression test actually bites rather than passing vacuously.
    """
    reverted = (
        "## Rule\n"
        "`src/forge_loop/cli.py` is 1705 LOC.\n"
        "`runner/tick.py::_tick()` is 582 lines.\n"
        "duplicated in `gh.py::create_issue`.\n"
    )
    path = tmp_path / "quality-manifesto.md"
    path.write_text(reverted, encoding="utf-8")

    matches = STALE_ANCHOR_RE.findall(path.read_text(encoding="utf-8"))
    assert "1705" in matches
    assert "582" in matches
    assert "gh.py" in matches


@pytest.mark.parametrize("legit", ["gh_issues.py", "gh_client.py"])
def test_guard_does_not_false_positive_on_live_surfaces(legit: str) -> None:
    """``gh\\.py`` must not match the surviving issue-creation modules."""
    assert STALE_ANCHOR_RE.search(legit) is None
