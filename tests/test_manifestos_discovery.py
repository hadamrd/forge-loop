"""Tests that the seed quality + testing manifestos exist, are
discoverable from the repo root, and parse as well-formed markdown
with the rule structure the rest of the system expects.

These tests are the meta-validation gate for issue #135 — they ensure
the manifestos themselves remain present and structurally sound. If
someone deletes or guts the manifestos, this test fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
FORGE_DIR = REPO_ROOT / ".forge"

QUALITY_PATH = FORGE_DIR / "quality-manifesto.md"
TESTING_PATH = FORGE_DIR / "testing-manifesto.md"


# ---------------------------------------------------------------------------
# Discovery — happy path
# ---------------------------------------------------------------------------

def test_forge_dir_exists() -> None:
    assert FORGE_DIR.is_dir(), f"missing {FORGE_DIR}"


def test_quality_manifesto_discoverable() -> None:
    assert QUALITY_PATH.is_file(), f"missing {QUALITY_PATH}"


def test_testing_manifesto_discoverable() -> None:
    assert TESTING_PATH.is_file(), f"missing {TESTING_PATH}"


# ---------------------------------------------------------------------------
# Parsing — both files have an H1 title and at least one rule heading
# ---------------------------------------------------------------------------

H1_RE = re.compile(r"^# .+", re.MULTILINE)
RULE_HEADING_RE = re.compile(r"^### [QT]\d+\. ", re.MULTILINE)


@pytest.mark.parametrize("path", [QUALITY_PATH, TESTING_PATH])
def test_manifesto_has_h1(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert H1_RE.search(text), f"{path} missing H1 title"


@pytest.mark.parametrize(
    "path,min_rules",
    [(QUALITY_PATH, 5), (TESTING_PATH, 6)],
)
def test_manifesto_has_rules(path: Path, min_rules: int) -> None:
    text = path.read_text(encoding="utf-8")
    rules = RULE_HEADING_RE.findall(text)
    assert len(rules) >= min_rules, (
        f"{path} expected ≥{min_rules} rule headings, got {len(rules)}"
    )


# ---------------------------------------------------------------------------
# Content — every rule has a rationale section, per the contract in
# both manifestos ("No rationale ⇒ no rule").
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [QUALITY_PATH, TESTING_PATH])
def test_every_rule_has_rationale(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    # Split on rule headings, drop preamble.
    chunks = re.split(r"(?m)^### [QT]\d+\. ", text)[1:]
    assert chunks, f"{path} parsed zero rule chunks"
    missing = [c.splitlines()[0] for c in chunks if "Rationale" not in c]
    assert not missing, (
        f"{path} rules missing rationale: {missing}"
    )


# ---------------------------------------------------------------------------
# Content — quality manifesto names the issues from the spec.
# ---------------------------------------------------------------------------

REQUIRED_QUALITY_REFS = ["#100", "#104", "#98", "#99", "#103", "#105"]


@pytest.mark.parametrize("ref", REQUIRED_QUALITY_REFS)
def test_quality_manifesto_references_spec_issue(ref: str) -> None:
    text = QUALITY_PATH.read_text(encoding="utf-8")
    assert ref in text, f"quality manifesto missing reference to {ref}"


# ---------------------------------------------------------------------------
# Content — testing manifesto names the iteration-probe bugs from the spec.
# ---------------------------------------------------------------------------

REQUIRED_TESTING_REFS = ["#97", "#120", "#128", "#102"]


@pytest.mark.parametrize("ref", REQUIRED_TESTING_REFS)
def test_testing_manifesto_references_iteration_probe_bug(ref: str) -> None:
    text = TESTING_PATH.read_text(encoding="utf-8")
    assert ref in text, f"testing manifesto missing reference to {ref}"


# ---------------------------------------------------------------------------
# Adversarial — manifestos are non-empty and not stub placeholders.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", [QUALITY_PATH, TESTING_PATH])
def test_manifesto_is_substantive(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert len(text) > 1000, f"{path} too short to be a real manifesto ({len(text)} bytes)"
    lowered = text.lower()
    for stub in ("tbd", "todo", "lorem ipsum", "coming soon"):
        assert stub not in lowered, f"{path} contains stub marker: {stub!r}"


# ---------------------------------------------------------------------------
# Adversarial — discovery must not silently succeed on a missing file.
# This guards the discovery helpers we'd build on top.
# ---------------------------------------------------------------------------

def test_missing_manifesto_path_is_detectable(tmp_path: Path) -> None:
    fake = tmp_path / ".forge" / "does-not-exist.md"
    assert not fake.is_file()
    with pytest.raises(FileNotFoundError):
        fake.read_text(encoding="utf-8")
