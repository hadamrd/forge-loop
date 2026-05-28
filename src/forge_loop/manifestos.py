"""Quality + testing manifesto discovery and worker-prompt injection.

This module is the consumer-side of the manifesto contract introduced in
issues #130 (sibling discovery seed) and #132 (this ticket: inject into
worker system prompt).

A "manifesto" is a free-form markdown file under ``<repo>/.forge/`` that
the operator uses to nail down house rules every worker MUST follow:

    .forge/quality-manifesto.md  → "QUALITY RULES YOU MUST FOLLOW"
    .forge/testing-manifesto.md  → "TESTING RULES YOU MUST FOLLOW"

Both files are OPTIONAL. A repo with neither file behaves byte-identically
to the pre-feature baseline (back-compat is an acceptance criterion).

Public surface:

- :func:`load_manifestos` — discovery; returns a :class:`ManifestoBundle`.
- :func:`render_manifesto_block` — turns a bundle into the prompt prefix.
- :func:`inject_into_brief` — convenience: prepend block to an existing
  brief string, in front of all task-specific content.

The git-blob-style sha for each rendered manifesto is recorded so the
worker outcome telemetry can audit which manifesto version a given PR was
built against (acceptance criterion: ``manifesto_sha`` dict in outcome).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    # Worker-prompt injection API (issue #132).
    "ManifestoSide",
    "ManifestoBundle",
    "load_manifestos",
    "render_manifesto_block",
    "inject_into_brief",
    # Discovery API (issue #131).
    "Rule",
    "QualityManifesto",
    "TestingManifesto",
    "Manifestos",
    "MissingManifestoError",
    "discover_manifestos",
]

QUALITY_REL = ".forge/quality-manifesto.md"
TESTING_REL = ".forge/testing-manifesto.md"
QUALITY_RULES_REL = ".forge/quality-rules.yaml"
TESTING_RULES_REL = ".forge/testing-rules.yaml"

QUALITY_HEADER = "QUALITY RULES YOU MUST FOLLOW:"
TESTING_HEADER = "TESTING RULES YOU MUST FOLLOW:"
BLOCK_OPEN = "===== MANIFESTO ====="
BLOCK_CLOSE = "===== END MANIFESTO ====="


@dataclass(frozen=True)
class ManifestoSide:
    """A single manifesto (quality OR testing) after discovery.

    ``content`` is the verbatim file body, or ``None`` if the file was
    absent, whitespace-only, or unreadable. ``sha`` is the git-blob-style
    sha1 of the file body when content is present; ``None`` otherwise.
    Keeping the two fields locked together (both set, or both None) is
    enforced in :func:`_load_one`.
    """

    content: str | None
    sha: str | None

    @property
    def present(self) -> bool:
        return self.content is not None


@dataclass(frozen=True)
class ManifestoBundle:
    """Quality + testing manifestos, post-discovery."""

    quality: ManifestoSide
    testing: ManifestoSide

    @property
    def any_present(self) -> bool:
        return self.quality.present or self.testing.present

    def sha_payload(self) -> dict[str, str | None]:
        """Return the ``manifesto_sha`` field for outcome telemetry.

        Always a 2-key dict so downstream consumers can rely on the shape;
        missing sides are ``None`` (not absent keys).
        """
        return {"quality": self.quality.sha, "testing": self.testing.sha}


def _git_blob_sha(text: str) -> str:
    """Compute the git-blob sha1 of ``text``.

    Matches what ``git hash-object <file>`` returns, so an operator can
    cross-reference the recorded telemetry against the live file:

        $ git hash-object .forge/quality-manifesto.md
    """
    blob = text.encode("utf-8")
    h = hashlib.sha1()
    h.update(f"blob {len(blob)}\0".encode("ascii"))
    h.update(blob)
    return h.hexdigest()


def _load_one(path: Path) -> ManifestoSide:
    """Discover one manifesto file. Never raises.

    - File missing → both fields None.
    - File unreadable (permission, binary garbage that isn't decodable as
      utf-8) → both None. We deliberately swallow because the acceptance
      criterion says a corrupt manifesto MUST NOT crash the worker.
    - Whitespace-only body → treated as missing (no empty header rendered).
    - Otherwise → content + blob sha.
    """
    try:
        if not path.is_file():
            return ManifestoSide(None, None)
    except OSError:
        return ManifestoSide(None, None)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ManifestoSide(None, None)
    if not text.strip():
        return ManifestoSide(None, None)
    return ManifestoSide(text, _git_blob_sha(text))


def load_manifestos(repo: Path | str) -> ManifestoBundle:
    """Discover the quality + testing manifestos under ``<repo>/.forge/``.

    Pure I/O; no formatting, no injection. Always returns a bundle —
    consumers check ``bundle.any_present`` to decide whether to render the
    MANIFESTO block at all.
    """
    root = Path(repo)
    return ManifestoBundle(
        quality=_load_one(root / QUALITY_REL),
        testing=_load_one(root / TESTING_REL),
    )


def render_manifesto_block(bundle: ManifestoBundle) -> str:
    """Format the MANIFESTO prefix for a worker brief.

    Returns the empty string when BOTH sides are absent (back-compat: a
    repo with no manifestos sees a byte-identical prompt). When only one
    side is present, that subsection is rendered and the other is
    silently omitted.

    Order is fixed: QUALITY first, then TESTING — operators reading the
    brief should see quality framing before test framing because tests
    enforce quality, not the other way around.
    """
    if not bundle.any_present:
        return ""
    parts: list[str] = [BLOCK_OPEN]
    if bundle.quality.present:
        parts.append(QUALITY_HEADER)
        # body kept verbatim — no trimming, no wrapping; the operator
        # authored it deliberately.
        parts.append((bundle.quality.content or "").rstrip("\n"))
    if bundle.testing.present:
        if bundle.quality.present:
            parts.append("")  # blank line between subsections
        parts.append(TESTING_HEADER)
        parts.append((bundle.testing.content or "").rstrip("\n"))
    parts.append(BLOCK_CLOSE)
    parts.append("")  # trailing newline before downstream brief content
    return "\n".join(parts) + "\n"


def inject_into_brief(brief: str, bundle: ManifestoBundle) -> str:
    """Prepend the MANIFESTO block to a rendered worker brief.

    Injection happens BEFORE any task-specific content so the rules frame
    everything the worker reads after (acceptance criterion). When the
    bundle is empty, returns ``brief`` unchanged (no leading newline, no
    sentinel) so back-compat tests pass byte-for-byte.
    """
    block = render_manifesto_block(bundle)
    if not block:
        return brief
    return block + brief


# ===========================================================================
# Discovery API — issue #131
# ===========================================================================
#
# A separate, pydantic-typed view onto the same two files (plus optional
# ``*-rules.yaml`` adjuncts), built for the brainstormer/worker startup
# path. Mirrors :mod:`forge_loop.product_vision` discovery semantics, with
# soft-default behaviour: a missing markdown file produces an empty
# manifesto and a warning, not an exception, unless ``required=True``.
#
# This API is intentionally separate from :func:`load_manifestos` /
# :class:`ManifestoBundle` above (which exists for worker-prompt
# injection, #132). Both live in this module because they share the same
# file layout — but their consumers are different (#132 wants verbatim
# text + sha for telemetry; #131 wants schema validation + warnings).


class MissingManifestoError(RuntimeError):
    """Raised by :func:`discover_manifestos` when ``required=True`` and a
    required manifesto markdown file is absent. The message always
    contains an absolute path so operators can fix without grepping.

    Mirrors :class:`forge_loop.product_vision.MissingVisionError` in spirit
    and message style.
    """


class Rule(BaseModel):
    """A single rule entry in a ``*-rules.yaml`` adjunct.

    Minimal schema on purpose — richer fields (owner, scope, examples)
    are deferred per the #131 out-of-scope clause.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    description: str
    severity: str | None = None


class _ManifestoBase(BaseModel):
    """Common shape for quality + testing manifestos."""

    model_config = ConfigDict(extra="ignore")

    markdown: str
    rules: list[Rule] | None = None


class QualityManifesto(_ManifestoBase):
    """Discovered quality manifesto (raw markdown + optional rules)."""


class TestingManifesto(_ManifestoBase):
    """Discovered testing manifesto (raw markdown + optional rules)."""

    # Avoid pytest mistaking this for a Test* collection class.
    __test__ = False


class Manifestos(BaseModel):
    """Bundle returned by :func:`discover_manifestos`."""

    model_config = ConfigDict(extra="ignore")

    quality: QualityManifesto
    testing: TestingManifesto
    warnings: list[str] = Field(default_factory=list)


def _format_rules_validation_error(exc: ValidationError, path: Path) -> str:
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        parts.append(f"{loc}: {err['msg']}")
    joined = "; ".join(parts) if parts else str(exc)
    return f"rules.yaml schema-invalid at {path}: {joined}"


def _check_forge_dir(forge_dir: Path) -> None:
    """Defensive check that ``.forge/`` is a real directory.

    Catches the symlink-to-nowhere and "file at .forge" footguns up front
    so callers see ``MissingManifestoError`` rather than a raw
    :class:`OSError` from deeper pathlib calls.
    """
    if not forge_dir.exists():
        # Distinguish dangling-symlink from "just doesn't exist": the
        # former needs operator attention, the latter is the soft default.
        if forge_dir.is_symlink():
            raise MissingManifestoError(
                f".forge is a symlink to a non-existent target at {forge_dir.absolute()}"
            )
        return
    if not forge_dir.is_dir():
        raise MissingManifestoError(
            f".forge exists but is not a directory at {forge_dir.absolute()}"
        )


def _load_markdown(
    md_path: Path,
    *,
    kind: Literal["quality", "testing"],
    required: bool,
    warnings: list[str],
) -> str:
    """Read a manifesto markdown file with soft-default semantics.

    Returns the file body verbatim, or ``""`` if the file is absent
    (soft) / empty (always soft). Appends a warning to ``warnings`` when
    the file is absent and ``required`` is False. Raises
    :class:`MissingManifestoError` when the file is absent and
    ``required`` is True, OR when the path exists but is not a regular
    file (e.g., someone made ``quality-manifesto.md/`` a directory).
    """
    abs_path = md_path.absolute()
    # exists() follows symlinks; we want to treat dangling symlinks as
    # "missing" so the soft-default branch applies uniformly.
    if not md_path.exists():
        if required:
            raise MissingManifestoError(
                f"{kind}-manifesto.md not found at {abs_path}"
            )
        warnings.append(
            f"{kind} manifesto missing at {abs_path} (soft default: empty)"
        )
        return ""
    if not md_path.is_file():
        # Adversarial: someone made the markdown path a directory. Always
        # raise — silent treatment as missing would be a footgun.
        raise MissingManifestoError(
            f"{kind}-manifesto.md is not a regular file at {abs_path}"
        )
    try:
        return md_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MissingManifestoError(
            f"{kind}-manifesto.md unreadable at {abs_path}: {exc}"
        ) from exc


def _load_rules(
    rules_path: Path,
    *,
    kind: Literal["quality", "testing"],
) -> list[Rule] | None:
    """Read an optional ``*-rules.yaml`` adjunct.

    Returns ``None`` if the file is absent (a manifesto without a rules
    adjunct is normal). Always raises on YAML parse error or schema
    invalidity — per acceptance criterion, silent corruption is worse
    than absence.
    """
    if not rules_path.exists():
        return None
    if not rules_path.is_file():
        raise MissingManifestoError(
            f"{kind}-rules.yaml is not a regular file at {rules_path.absolute()}"
        )
    try:
        raw = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MissingManifestoError(
            f"{kind}-rules.yaml YAML parse failure at {rules_path.absolute()}: {exc}"
        ) from exc

    # Accept two shapes: a bare list, or a dict with top-level ``rules:``.
    if raw is None:
        return []
    if isinstance(raw, dict):
        rules_raw = raw.get("rules")
        if rules_raw is None:
            raise MissingManifestoError(
                f"rules.yaml schema-invalid at {rules_path.absolute()}: "
                f"missing top-level 'rules' list"
            )
    elif isinstance(raw, list):
        rules_raw = raw
    else:
        raise MissingManifestoError(
            f"rules.yaml schema-invalid at {rules_path.absolute()}: "
            f"top-level must be a list or a mapping with 'rules:', got {type(raw).__name__}"
        )

    try:
        return [Rule.model_validate(item) for item in rules_raw]
    except ValidationError as exc:
        raise MissingManifestoError(
            _format_rules_validation_error(exc, rules_path.absolute())
        ) from exc


def discover_manifestos(
    repo_path: Path | str,
    *,
    required: bool = False,
) -> Manifestos:
    """Discover ``.forge/quality-manifesto.md`` + ``.forge/testing-manifesto.md``.

    Soft-default semantics: a missing markdown file produces an empty
    manifesto (``markdown=""``, ``rules=None``) and a warning is appended
    to the returned ``Manifestos.warnings`` list. The caller decides
    whether to log, surface to the operator, or ignore.

    When ``required=True``, a missing markdown file raises
    :class:`MissingManifestoError` instead — mirroring the hard-fail
    semantics of :func:`forge_loop.product_vision.discover`.

    A malformed ``*-rules.yaml`` ALWAYS raises, regardless of the
    ``required`` flag — silent corruption of a structured adjunct is
    worse than absence (per #131 acceptance criterion).

    An empty markdown file is treated as "present but empty": no
    warning, ``markdown=""``, ``rules=None`` (unless the adjunct exists).
    """
    root = Path(repo_path)
    forge_dir = root / ".forge"
    _check_forge_dir(forge_dir)

    warnings: list[str] = []

    quality_md_path = forge_dir / "quality-manifesto.md"
    testing_md_path = forge_dir / "testing-manifesto.md"
    quality_rules_path = forge_dir / "quality-rules.yaml"
    testing_rules_path = forge_dir / "testing-rules.yaml"

    quality_md = _load_markdown(
        quality_md_path, kind="quality", required=required, warnings=warnings
    )
    testing_md = _load_markdown(
        testing_md_path, kind="testing", required=required, warnings=warnings
    )

    quality_rules = _load_rules(quality_rules_path, kind="quality")
    testing_rules = _load_rules(testing_rules_path, kind="testing")

    return Manifestos(
        quality=QualityManifesto(markdown=quality_md, rules=quality_rules),
        testing=TestingManifesto(markdown=testing_md, rules=testing_rules),
        warnings=warnings,
    )
