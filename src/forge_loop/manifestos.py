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

QUALITY_REL = ".forge/quality-manifesto.md"
TESTING_REL = ".forge/testing-manifesto.md"

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
