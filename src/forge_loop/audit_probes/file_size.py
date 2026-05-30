"""File-size probe — first state-rule probe (issue #156).

The quality manifesto carries soft caps for module size per language.
This probe walks the repository, measures LOC per source file
(non-blank, non-comment-only lines), and yields a :class:`Violation`
for every file that exceeds its language's threshold.

Why this is the first probe
===========================

The motivating example is ``src/forge_loop/cli.py`` at >1700 LOC, 3.4×
the Python soft-cap of 500. The per-PR critic never flagged it because
cli.py grew 50-100 LOC per PR — each diff was reasonable, the cumulative
state-violation was invisible. This probe makes the cumulative
violation visible AS a violation, on the same cadence as the
maintenance daemon.

Thresholds
==========

Defaults match the manifesto:

* Python: 500 LOC
* TypeScript / TSX / JS: 400 LOC
* Java: 600 LOC

Operators override via ``FileSizeProbe(thresholds=...)``. The
``hard_threshold_multiplier`` knob escalates severity once a file
crosses N× its soft cap — the cli.py case fires P1, not P2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from forge_loop.codebase_audit import Violation, walk_source_files


DEFAULT_THRESHOLDS: dict[str, int] = {
    ".py": 500,
    ".ts": 400,
    ".tsx": 400,
    ".js": 400,
    ".java": 600,
}


def _count_significant_lines(path: Path) -> int:
    """Count non-blank, non-comment-only lines.

    Pragmatic — no AST. A line that's entirely whitespace or starts
    (after lstrip) with ``#`` / ``//`` / ``/*`` / ``*`` is dropped. Good
    enough to distinguish a 1700-LOC business-logic file from a 1700-
    LOC docstring (none of which exist in the codebase today).

    Decode failures fall back to 0 — a binary file shouldn't trip the
    probe just because its suffix matched.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return 0
    n = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("#", "//", "/*", "*")):
            continue
        n += 1
    return n


@dataclass
class FileSizeProbe:
    """Yield one :class:`Violation` per oversized file.

    Construction parameters are kept simple — production wires defaults,
    tests override per-case.
    """

    name: str = "file-size"
    thresholds: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    hard_threshold_multiplier: float = 2.0

    def scan(self, repo: Path) -> Iterable[Violation]:
        suffixes = tuple(self.thresholds.keys())
        for path in walk_source_files(repo, suffixes=suffixes):
            soft = self.thresholds.get(path.suffix)
            if soft is None:
                continue
            loc = _count_significant_lines(path)
            if loc <= soft:
                continue
            rel = path.relative_to(repo.resolve()) if path.is_absolute() else path
            rel_str = str(rel).replace("\\", "/")
            multiplier = loc / soft if soft else float("inf")
            severity = 1 if multiplier >= self.hard_threshold_multiplier else 2
            ratio_str = f"{multiplier:.1f}×"
            title = (
                f"refactor({rel_str}): split oversized module "
                f"({loc} LOC > {soft} cap, {ratio_str})"
            )
            rationale = (
                f"`{rel_str}` is {loc} significant LOC, which exceeds the "
                f"quality-manifesto soft cap of {soft} for `{path.suffix}` "
                f"files ({ratio_str}).\n\n"
                f"The per-PR critic does not catch this class of accumulation "
                f"violation — modules grow 50-100 LOC at a time and each "
                f"individual diff looks reasonable. The audit probe (issue "
                f"#156) is the state-based gate that the manifesto rule "
                f"\"state-based rules need a state-based gate\" calls for.\n\n"
                f"Why it matters: oversized modules are correlated with the "
                f"boiling-frog failures the manifesto cites (#147 stringly-"
                f"typed boundaries, #128 silent fallthroughs) — once a "
                f"module is hard to read end-to-end, single-character bugs "
                f"survive review."
            )
            acceptance = [
                f"`{rel_str}` is split into ≥2 focused modules, none exceeding {soft} LOC.",
                "Public import surface preserved (no downstream breakage).",
                "Tests for the split modules remain green; no test logic moved.",
                "Manifesto rationale is honoured: each new module has a single, nameable responsibility.",
            ]
            yield Violation(
                probe=self.name,
                target=rel_str,
                severity=severity,
                title=title,
                rationale=rationale,
                acceptance=acceptance,
                metrics={"loc": loc, "soft_cap": soft, "ratio": round(multiplier, 2)},
            )


__all__ = ["FileSizeProbe", "DEFAULT_THRESHOLDS"]
