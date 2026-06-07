"""Function-size probe — per-function LOC state-gate (Q8, issue #306).

The quality manifesto's Q8 rule ("No god-functions. A function over 80 LOC
must be decomposed") is a *state* rule: the per-PR critic catches a function
that grows over-cap in a single diff, but a function that accretes 5-10 LOC
per PR over many merges slips past every per-PR review — the same boiling-frog
shape that let ``cli.py`` reach several times its module cap. Per the
manifesto meta-rule "state-based rules need a state-based gate", that rule
needs a probe; this is it, mirroring :mod:`forge_loop.audit_probes.file_size`.

Why this mirrors file_size, not an AST complexity analyzer
==========================================================

The motivating example is ``runner/tick.py::_tick()``, which once ballooned to
many times the 80-LOC cap before it was decomposed. This probe re-measures
function length on every audit pass so the manifesto can reference a *live*
gate instead of a frozen line range. It intentionally stays pragmatic: it uses
stdlib ``ast`` only to find function boundaries (start/end line) — far more
robust than indent heuristics for decorators and multiline signatures — and
then counts significant lines with the exact same rule as ``file_size.py``
(non-blank, non-comment-only). It does NOT compute cyclomatic complexity; a
heavier analyzer is a separate ticket.

Thresholds
==========

Default cap is 80 logical lines (the Q8 number). The
``hard_threshold_multiplier`` knob escalates severity once a function crosses
N× the cap, exactly as ``FileSizeProbe`` does for modules. Only Python is
scanned — the Q8 anchor (``_tick``) is Python, and reliable per-function LOC
for other languages needs their own parsers.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from forge_loop.codebase_audit import Violation, walk_source_files

DEFAULT_MAX_LINES = 80


def _count_significant_lines(lines: list[str]) -> int:
    """Count non-blank, non-comment-only lines.

    Same pragmatic rule as ``file_size._count_significant_lines`` — a line
    that is entirely whitespace or starts (after lstrip) with ``#`` is
    dropped. Kept in lockstep so a function's LOC and its module's LOC are
    measured the same way.
    """
    n = 0
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        n += 1
    return n


def _function_spans(source: str) -> Iterable[tuple[str, int, int]]:
    """Yield ``(qualified_name, start_lineno, end_lineno)`` for every def.

    Uses ``ast`` for boundary detection only. Names are qualified by their
    enclosing class/function (``Foo.bar``, ``outer.inner``) so the violation
    target is unambiguous. A file that does not parse yields nothing — a
    syntactically broken file should not crash the audit pass.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return

    def walk(node: ast.AST, prefix: str) -> Iterable[tuple[str, int, int]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                qualname = f"{prefix}{child.name}"
                end = getattr(child, "end_lineno", None) or child.lineno
                yield (qualname, child.lineno, end)
                yield from walk(child, f"{qualname}.")
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, f"{prefix}{child.name}.")
            else:
                yield from walk(child, prefix)

    yield from walk(tree, "")


@dataclass
class FunctionSizeProbe:
    """Yield one :class:`Violation` per over-cap function.

    Construction parameters mirror :class:`FileSizeProbe`: production wires
    defaults, tests override per-case.
    """

    name: str = "function-size"
    max_lines: int = DEFAULT_MAX_LINES
    hard_threshold_multiplier: float = 2.0

    def scan(self, repo: Path) -> Iterable[Violation]:
        for path in walk_source_files(repo, suffixes=(".py",)):
            try:
                source = path.read_text(encoding="utf-8", errors="strict")
            except (OSError, UnicodeDecodeError):
                continue
            file_lines = source.splitlines()
            for qualname, start, end in _function_spans(source):
                body = file_lines[start - 1 : end]
                loc = _count_significant_lines(body)
                if loc <= self.max_lines:
                    continue
                rel = (
                    path.relative_to(repo.resolve())
                    if path.is_absolute()
                    else path
                )
                rel_str = str(rel).replace("\\", "/")
                target = f"{rel_str}::{qualname}"
                multiplier = loc / self.max_lines if self.max_lines else float("inf")
                severity = 1 if multiplier >= self.hard_threshold_multiplier else 2
                ratio_str = f"{multiplier:.1f}×"
                title = (
                    f"refactor({rel_str}): decompose god-function "
                    f"`{qualname}` ({loc} LOC > {self.max_lines} cap, {ratio_str})"
                )
                rationale = (
                    f"`{target}` is {loc} significant LOC, which exceeds the "
                    f"quality-manifesto Q8 cap of {self.max_lines} logical lines "
                    f"({ratio_str}).\n\n"
                    f"The per-PR critic does not catch this class of accumulation "
                    f"violation — functions grow a handful of lines at a time and "
                    f"each diff looks reasonable. This audit probe is the "
                    f"state-based gate the manifesto rule \"state-based rules need "
                    f"a state-based gate\" calls for, mirroring the module-level "
                    f"`file-size` probe one scope down.\n\n"
                    f"Why it matters: god-functions have no unit tests of their "
                    f"branches (only end-to-end coverage), so single-character "
                    f"bugs in a rarely-hit arm survive review — the exact "
                    f"failure mode behind the manifesto's fallthrough incidents."
                )
                acceptance = [
                    f"`{qualname}` is decomposed into named helpers, none over "
                    f"{self.max_lines} LOC.",
                    "Each extracted helper has a single, nameable responsibility.",
                    "Behaviour preserved; existing tests for the function stay green.",
                    "New helpers are individually unit-testable (and tested).",
                ]
                yield Violation(
                    probe=self.name,
                    target=target,
                    severity=severity,
                    title=title,
                    rationale=rationale,
                    acceptance=acceptance,
                    metrics={
                        "loc": loc,
                        "max_lines": self.max_lines,
                        "ratio": round(multiplier, 2),
                    },
                )


__all__ = ["FunctionSizeProbe", "DEFAULT_MAX_LINES"]
