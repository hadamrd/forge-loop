"""Tests for ``forge_loop.product_vision`` — issue #122."""

from __future__ import annotations

from pathlib import Path

import pytest

from forge_loop.product_vision import (
    Axis,
    MissingVisionError,
    ProductVision,
    discover,
)

FIXTURES = Path(__file__).parent / "fixtures" / "product_vision"


# -- Happy paths ------------------------------------------------------------


def test_valid_minimal_returns_product_vision() -> None:
    pv = discover(FIXTURES / "valid_minimal")
    assert isinstance(pv, ProductVision)
    assert len(pv.axes) == 1
    axis = pv.axes[0]
    assert isinstance(axis, Axis)
    assert axis.name == "shipping"
    assert axis.customer == "solo operator"
    assert axis.valuable_means == "feature reaches production"
    assert axis.acceptable_work == ["write code"]
    assert axis.rejected_as_cosmetic == []
    # vision_markdown is the raw file content verbatim
    assert (
        pv.vision_markdown
        == (FIXTURES / "valid_minimal" / ".forge" / "product-vision.md").read_text(
            encoding="utf-8"
        )
    )


def test_valid_full_multi_axis() -> None:
    pv = discover(FIXTURES / "valid_full")
    assert len(pv.axes) == 2
    names = [a.name for a in pv.axes]
    assert names == ["throughput", "reliability"]
    assert pv.axes[0].rejected_as_cosmetic == ["rename variables", "reflow whitespace"]
    assert "## Who" in pv.vision_markdown
    assert "## How" in pv.vision_markdown


# -- Sad paths --------------------------------------------------------------


def test_missing_vision_file() -> None:
    with pytest.raises(MissingVisionError) as ei:
        discover(FIXTURES / "missing_vision")
    msg = str(ei.value)
    assert "product-vision.md" in msg
    assert str((FIXTURES / "missing_vision" / ".forge" / "product-vision.md").absolute()) in msg


def test_missing_axes_file() -> None:
    with pytest.raises(MissingVisionError) as ei:
        discover(FIXTURES / "missing_axes")
    msg = str(ei.value)
    assert "axes.yaml" in msg
    assert "missing_axes" in msg


def test_empty_vision() -> None:
    with pytest.raises(MissingVisionError) as ei:
        discover(FIXTURES / "empty_vision")
    assert "vision markdown is empty" in str(ei.value)


def test_invalid_axes_yaml_parse_failure() -> None:
    with pytest.raises(MissingVisionError) as ei:
        discover(FIXTURES / "invalid_axes_yaml")
    msg = str(ei.value)
    assert "YAML parse" in msg or "parse failure" in msg
    assert "axes.yaml" in msg


def test_invalid_axes_schema_missing_customer() -> None:
    with pytest.raises(MissingVisionError) as ei:
        discover(FIXTURES / "invalid_axes_schema")
    msg = str(ei.value)
    assert "customer" in msg


def test_empty_axes_list() -> None:
    with pytest.raises(MissingVisionError) as ei:
        discover(FIXTURES / "empty_axes_list")
    msg = str(ei.value)
    assert "axes" in msg
    assert "empty" in msg.lower()


def test_acceptable_work_empty() -> None:
    with pytest.raises(MissingVisionError) as ei:
        discover(FIXTURES / "acceptable_work_empty")
    msg = str(ei.value)
    assert "acceptable_work" in msg


# -- Adversarial -----------------------------------------------------------


def test_missing_forge_dir_raises_missing_vision_error(tmp_path: Path) -> None:
    """No ``.forge/`` at all → MissingVisionError, not FileNotFoundError leak."""
    with pytest.raises(MissingVisionError):
        discover(tmp_path)


def test_symlinked_forge_dir_resolves(tmp_path: Path) -> None:
    """A repo whose ``.forge/`` is a symlink to a valid fixture must validate."""
    target = (FIXTURES / "valid_minimal" / ".forge").resolve()
    link = tmp_path / ".forge"
    link.symlink_to(target, target_is_directory=True)
    pv = discover(tmp_path)
    assert pv.axes[0].name == "shipping"


def test_extra_keys_on_axis_are_ignored() -> None:
    """``valid_full`` includes ``extra_unknown_key`` on an axis — must be ignored."""
    pv = discover(FIXTURES / "valid_full")
    # If we got here, extra keys did not blow up validation.
    assert pv.axes[0].name == "throughput"
