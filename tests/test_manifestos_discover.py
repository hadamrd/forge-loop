"""Unit tests for :func:`forge_loop.manifestos.discover_manifestos`.

These tests exercise the issue #131 discovery API end-to-end against
fixture repos under ``tests/fixtures/manifestos/``. The discovery API is
distinct from the worker-prompt injection API (#132, ``load_manifestos``
and ``ManifestoBundle``) — different consumers, same files. See module
docstring in :mod:`forge_loop.manifestos` for the contract.

Test matrix mirrors the acceptance criteria in #131:

* ``full``                    — both markdowns + both rules.yaml.
* ``missing_quality``         — only testing files.
* ``missing_testing``         — only quality files.
* ``missing_both``            — empty ``.forge/``.
* ``markdown_only``           — markdowns present, no rules.yaml.
* ``rules_yaml_malformed``    — invalid YAML in a rules file.
* ``rules_yaml_schema_invalid`` — YAML parses, schema rejects.
* ``required_flag``           — soft-default vs hard-fail switch.

Plus two adversarial cases (symlink to nowhere, markdown-is-a-directory)
called out in the acceptance criteria.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from forge_loop.manifestos import (
    Manifestos,
    MissingManifestoError,
    QualityManifesto,
    Rule,
    TestingManifesto,
    discover_manifestos,
)


# ---------------------------------------------------------------------------
# Fixture builder — one tmp_path per case, written inline so the test file
# is self-contained and a future contributor doesn't have to grep for a
# yaml seed somewhere on disk.
# ---------------------------------------------------------------------------


QUALITY_MD = "# Quality\n\nNever silently swallow exceptions.\n"
TESTING_MD = "# Testing\n\nEvery state machine gets a fallthrough test.\n"

QUALITY_RULES_YAML = """\
rules:
  - id: Q1
    description: No shared mutable module-level state.
    severity: blocker
  - id: Q2
    description: Every I/O boundary lives behind a typed Protocol.
"""

TESTING_RULES_YAML = """\
rules:
  - id: T1
    description: One test per edge plus one fallthrough adversarial test.
    severity: blocker
"""


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _build_full(root: Path) -> None:
    _write(root / ".forge" / "quality-manifesto.md", QUALITY_MD)
    _write(root / ".forge" / "testing-manifesto.md", TESTING_MD)
    _write(root / ".forge" / "quality-rules.yaml", QUALITY_RULES_YAML)
    _write(root / ".forge" / "testing-rules.yaml", TESTING_RULES_YAML)


# ---------------------------------------------------------------------------
# Happy path — full bundle.
# ---------------------------------------------------------------------------


def test_full_bundle_returns_populated_models(tmp_path: Path) -> None:
    _build_full(tmp_path)
    result = discover_manifestos(tmp_path)

    assert isinstance(result, Manifestos)
    assert isinstance(result.quality, QualityManifesto)
    assert isinstance(result.testing, TestingManifesto)
    assert result.quality.markdown == QUALITY_MD
    assert result.testing.markdown == TESTING_MD
    assert result.warnings == []

    assert result.quality.rules is not None
    assert len(result.quality.rules) == 2
    assert result.quality.rules[0] == Rule(
        id="Q1",
        description="No shared mutable module-level state.",
        severity="blocker",
    )
    # severity is optional and the second rule omits it.
    assert result.quality.rules[1].severity is None

    assert result.testing.rules is not None
    assert result.testing.rules[0].id == "T1"


# ---------------------------------------------------------------------------
# Missing-one-side cases.
# ---------------------------------------------------------------------------


def test_missing_quality_warns_and_empties(tmp_path: Path) -> None:
    _write(tmp_path / ".forge" / "testing-manifesto.md", TESTING_MD)

    result = discover_manifestos(tmp_path)

    assert result.quality.markdown == ""
    assert result.quality.rules is None
    assert result.testing.markdown == TESTING_MD
    assert len(result.warnings) == 1
    assert "quality" in result.warnings[0]
    # Absolute path must appear so operators can fix without grepping.
    assert str((tmp_path / ".forge" / "quality-manifesto.md").absolute()) in result.warnings[0]


def test_missing_testing_warns_and_empties(tmp_path: Path) -> None:
    _write(tmp_path / ".forge" / "quality-manifesto.md", QUALITY_MD)

    result = discover_manifestos(tmp_path)

    assert result.testing.markdown == ""
    assert result.testing.rules is None
    assert result.quality.markdown == QUALITY_MD
    assert len(result.warnings) == 1
    assert "testing" in result.warnings[0]


def test_missing_both_warns_twice(tmp_path: Path) -> None:
    (tmp_path / ".forge").mkdir()

    result = discover_manifestos(tmp_path)

    assert result.quality.markdown == ""
    assert result.testing.markdown == ""
    assert len(result.warnings) == 2
    assert any("quality" in w for w in result.warnings)
    assert any("testing" in w for w in result.warnings)


def test_no_forge_dir_at_all_warns_twice(tmp_path: Path) -> None:
    # No .forge directory at all — soft-default still applies.
    result = discover_manifestos(tmp_path)
    assert result.quality.markdown == ""
    assert result.testing.markdown == ""
    assert len(result.warnings) == 2


# ---------------------------------------------------------------------------
# markdown_only — rules absent is normal, not a warning.
# ---------------------------------------------------------------------------


def test_markdown_only_returns_none_rules_no_warning(tmp_path: Path) -> None:
    _write(tmp_path / ".forge" / "quality-manifesto.md", QUALITY_MD)
    _write(tmp_path / ".forge" / "testing-manifesto.md", TESTING_MD)

    result = discover_manifestos(tmp_path)

    assert result.quality.rules is None
    assert result.testing.rules is None
    assert result.warnings == []


# ---------------------------------------------------------------------------
# Empty markdown — "present but empty", no warning.
# ---------------------------------------------------------------------------


def test_empty_markdown_no_warning(tmp_path: Path) -> None:
    _write(tmp_path / ".forge" / "quality-manifesto.md", "")
    _write(tmp_path / ".forge" / "testing-manifesto.md", "")

    result = discover_manifestos(tmp_path)
    assert result.quality.markdown == ""
    assert result.testing.markdown == ""
    assert result.warnings == []


# ---------------------------------------------------------------------------
# Malformed rules.yaml → always raises, even with required=False.
# ---------------------------------------------------------------------------


def test_rules_yaml_malformed_raises_with_path(tmp_path: Path) -> None:
    _write(tmp_path / ".forge" / "quality-manifesto.md", QUALITY_MD)
    _write(tmp_path / ".forge" / "testing-manifesto.md", TESTING_MD)
    bad_path = tmp_path / ".forge" / "quality-rules.yaml"
    _write(bad_path, "rules:\n  - id: Q1\n  bad indentation :: garbage\n   {{}}")

    with pytest.raises(MissingManifestoError) as exc:
        discover_manifestos(tmp_path)
    assert str(bad_path.absolute()) in str(exc.value)


def test_rules_yaml_schema_invalid_raises(tmp_path: Path) -> None:
    _write(tmp_path / ".forge" / "quality-manifesto.md", QUALITY_MD)
    _write(tmp_path / ".forge" / "testing-manifesto.md", TESTING_MD)
    bad_path = tmp_path / ".forge" / "quality-rules.yaml"
    # Missing required "description" field on the rule.
    _write(bad_path, "rules:\n  - id: Q1\n    severity: blocker\n")

    with pytest.raises(MissingManifestoError) as exc:
        discover_manifestos(tmp_path)
    msg = str(exc.value)
    assert "schema-invalid" in msg
    assert "description" in msg


def test_rules_yaml_top_level_not_mapping_raises(tmp_path: Path) -> None:
    _write(tmp_path / ".forge" / "quality-manifesto.md", QUALITY_MD)
    _write(tmp_path / ".forge" / "testing-manifesto.md", TESTING_MD)
    bad_path = tmp_path / ".forge" / "quality-rules.yaml"
    _write(bad_path, "just-a-string\n")  # scalar at top, not list or dict

    with pytest.raises(MissingManifestoError):
        discover_manifestos(tmp_path)


def test_rules_yaml_as_bare_list_is_accepted(tmp_path: Path) -> None:
    """A bare top-level list of rules is the lenient shape."""
    _write(tmp_path / ".forge" / "quality-manifesto.md", QUALITY_MD)
    _write(tmp_path / ".forge" / "testing-manifesto.md", TESTING_MD)
    _write(
        tmp_path / ".forge" / "quality-rules.yaml",
        "- id: Q1\n  description: foo\n",
    )

    result = discover_manifestos(tmp_path)
    assert result.quality.rules == [Rule(id="Q1", description="foo")]


# ---------------------------------------------------------------------------
# required=True flag.
# ---------------------------------------------------------------------------


def test_required_flag_raises_on_missing_quality(tmp_path: Path) -> None:
    _write(tmp_path / ".forge" / "testing-manifesto.md", TESTING_MD)
    with pytest.raises(MissingManifestoError) as exc:
        discover_manifestos(tmp_path, required=True)
    assert str((tmp_path / ".forge" / "quality-manifesto.md").absolute()) in str(exc.value)


def test_required_flag_raises_on_missing_testing(tmp_path: Path) -> None:
    _write(tmp_path / ".forge" / "quality-manifesto.md", QUALITY_MD)
    with pytest.raises(MissingManifestoError) as exc:
        discover_manifestos(tmp_path, required=True)
    assert str((tmp_path / ".forge" / "testing-manifesto.md").absolute()) in str(exc.value)


def test_required_flag_passes_when_both_present(tmp_path: Path) -> None:
    _build_full(tmp_path)
    result = discover_manifestos(tmp_path, required=True)
    assert result.quality.markdown == QUALITY_MD
    assert result.warnings == []


# ---------------------------------------------------------------------------
# Adversarial cases called out in the spec.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="symlink semantics differ")
def test_forge_is_dangling_symlink_raises_clean_error(tmp_path: Path) -> None:
    forge = tmp_path / ".forge"
    forge.symlink_to(tmp_path / "nowhere-dir")
    assert forge.is_symlink()
    assert not forge.exists()

    with pytest.raises(MissingManifestoError) as exc:
        discover_manifestos(tmp_path)
    assert "symlink" in str(exc.value).lower()
    # Must NOT be a raw OSError / FileNotFoundError leaking through.
    assert not isinstance(exc.value, OSError)


def test_forge_is_a_regular_file_raises_clean_error(tmp_path: Path) -> None:
    forge_as_file = tmp_path / ".forge"
    forge_as_file.write_text("oops, not a dir", encoding="utf-8")

    with pytest.raises(MissingManifestoError) as exc:
        discover_manifestos(tmp_path)
    assert "not a directory" in str(exc.value)


def test_manifesto_md_is_a_directory_raises_clean_error(tmp_path: Path) -> None:
    # Adversarial: someone created ``quality-manifesto.md/`` as a directory.
    (tmp_path / ".forge" / "quality-manifesto.md").mkdir(parents=True)
    _write(tmp_path / ".forge" / "testing-manifesto.md", TESTING_MD)

    with pytest.raises(MissingManifestoError) as exc:
        discover_manifestos(tmp_path)
    assert "not a regular file" in str(exc.value)
    # Path is absolute in the message.
    assert str((tmp_path / ".forge" / "quality-manifesto.md").absolute()) in str(exc.value)


# ---------------------------------------------------------------------------
# Integration — mirror the import path the brainstormer/worker startup
# would use. ``product_vision.discover`` is imported from the top-level
# module path; do the same here.
# ---------------------------------------------------------------------------


def test_integration_import_path_matches_product_vision_pattern(tmp_path: Path) -> None:
    # Same import shape used by callers of product_vision.discover.
    from forge_loop import manifestos as m  # noqa: WPS433 — intentional

    _build_full(tmp_path)
    result = m.discover_manifestos(tmp_path)
    assert isinstance(result, m.Manifestos)
    assert result.quality.markdown == QUALITY_MD
    assert result.warnings == []


def test_repo_path_accepts_str(tmp_path: Path) -> None:
    _build_full(tmp_path)
    result = discover_manifestos(str(tmp_path))
    assert result.quality.markdown == QUALITY_MD
