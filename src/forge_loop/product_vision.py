"""Product Brainstormer — typed discovery of the product "north star".

Reads two files from ``<repo_path>/.forge/``:

* ``product-vision.md`` — free-form markdown describing what the product
  is for and who it's for. We don't parse it; we just store the raw text
  so downstream prompts can quote it verbatim.
* ``axes.yaml`` — a strict, schema-validated list of "axes of value".
  Each axis says who the customer is, what counts as valuable, what kinds
  of work are acceptable, and what is rejected as cosmetic.

Either file missing or invalid raises :class:`MissingVisionError` with a
human-readable reason that includes the absolute file path. The
brainstormer must refuse to run when discovery fails — silent
fabrication of direction is the failure mode we are defending against.

See issue #122 (part of epic #121).
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = ["Axis", "ProductVision", "MissingVisionError", "discover"]


class MissingVisionError(RuntimeError):
    """Raised when ``.forge/product-vision.md`` or ``.forge/axes.yaml``
    is missing, unparseable, or schema-invalid.

    The message always contains an absolute path so operators can fix
    without grepping.
    """


class Axis(BaseModel):
    """A single axis of value. Extra keys are ignored for forward-compat."""

    model_config = ConfigDict(extra="ignore")

    name: str
    customer: str
    valuable_means: str
    acceptable_work: list[str] = Field(min_length=1)
    rejected_as_cosmetic: list[str] = Field(default_factory=list)


class ProductVision(BaseModel):
    """The brainstormer's "north star" — raw vision text plus axes."""

    model_config = ConfigDict(extra="ignore")

    vision_markdown: str
    axes: list[Axis] = Field(min_length=1)


def _format_validation_error(exc: ValidationError, axes_path: Path) -> str:
    """Render a pydantic ValidationError as a single operator-readable line."""
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        parts.append(f"{loc}: {err['msg']}")
    joined = "; ".join(parts) if parts else str(exc)
    return f"axes.yaml schema-invalid at {axes_path}: {joined}"


def discover(repo_path: Path) -> ProductVision:
    """Load ``.forge/product-vision.md`` and ``.forge/axes.yaml``.

    Returns a validated :class:`ProductVision`. Never returns ``None`` —
    missing or invalid inputs raise :class:`MissingVisionError`.
    """
    repo_path = Path(repo_path)
    forge_dir = (repo_path / ".forge").resolve() if (repo_path / ".forge").exists() else repo_path / ".forge"
    vision_path = (forge_dir / "product-vision.md")
    axes_path = (forge_dir / "axes.yaml")

    # Resolve to absolute paths for error messages, even when they don't exist.
    vision_abs = vision_path.resolve() if vision_path.exists() else vision_path.absolute()
    axes_abs = axes_path.resolve() if axes_path.exists() else axes_path.absolute()

    if not vision_path.exists():
        raise MissingVisionError(
            f"product-vision.md not found at {vision_abs}"
        )
    if not axes_path.exists():
        raise MissingVisionError(
            f"axes.yaml not found at {axes_abs}"
        )

    vision_text = vision_path.read_text(encoding="utf-8")
    if not vision_text.strip():
        raise MissingVisionError(
            f"vision markdown is empty at {vision_abs}"
        )

    try:
        axes_raw = yaml.safe_load(axes_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MissingVisionError(
            f"axes.yaml YAML parse failure at {axes_abs}: {exc}"
        ) from exc

    if not isinstance(axes_raw, dict) or "axes" not in axes_raw:
        raise MissingVisionError(
            f"axes.yaml schema-invalid at {axes_abs}: missing top-level 'axes' list"
        )

    try:
        vision = ProductVision(vision_markdown=vision_text, axes=axes_raw.get("axes") or [])
    except ValidationError as exc:
        # Distinguish empty axes list for a clearer reason.
        if axes_raw.get("axes") == []:
            raise MissingVisionError(
                f"axes.yaml schema-invalid at {axes_abs}: axes list cannot be empty"
            ) from exc
        raise MissingVisionError(_format_validation_error(exc, axes_abs)) from exc

    return vision
