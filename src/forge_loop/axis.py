"""Axis label namespace helpers (issue #126).

The brainstormer/PO emits issues across many concerns: CLI, runner,
dispatch, observability, etc. Operators want to focus a sprint on a
single concern — but a flat ``loop:ready`` queue gives them no way to
group or filter. Issue #126 introduces the ``axis:<slug>`` label
namespace as plumbing:

* Any label whose name (lowercased) starts with ``axis:`` is parsed
  into a free-form axis slug. No registry, no validation, no enum.
* ``forge-loop status`` groups open ready-issues by axis; issues with
  no axis label go into the ``unaligned`` bucket and a soft warning
  is surfaced.
* ``forge-loop run --axis <name>`` (repeatable) narrows dispatch to
  issues carrying any of those axis labels. Omitting the flag is a
  no-op — behaviour is byte-identical to the pre-#126 loop.

The helpers in this module are intentionally pure: they read labels
off in-memory dicts, normalise case, and never touch GitHub. Callers
fetch issues; we just bucket them.
"""

from __future__ import annotations

import os
from typing import Any, Iterable


AXIS_PREFIX = "axis:"
UNALIGNED_BUCKET = "unaligned"
AXIS_FILTER_ENV = "LOOP_AXIS_FILTER"


def extract_axes(labels: Iterable[Any]) -> set[str]:
    """Pull the ``axis:*`` slugs off a label list.

    Accepts either raw strings (``"axis:dispatch"``) or dicts shaped
    like the ``gh issue list --json labels`` payload (``{"name": ...}``).
    Comparison is case-insensitive: ``Axis:Dispatch`` normalises to
    ``dispatch``.

    Empty slugs (the literal label ``axis:`` with nothing after the
    colon) are dropped — the acceptance criteria call this out as
    "treated as unaligned".
    """
    out: set[str] = set()
    for raw in labels or []:
        if isinstance(raw, dict):
            name = str(raw.get("name") or "")
        else:
            name = str(raw or "")
        low = name.strip().lower()
        if not low.startswith(AXIS_PREFIX):
            continue
        slug = low[len(AXIS_PREFIX):].strip()
        if not slug:
            continue
        out.add(slug)
    return out


def matches_axes(labels: Iterable[Any], wanted: Iterable[str]) -> bool:
    """True iff the issue's axes intersect ``wanted``.

    Used by the dispatcher. ``wanted`` empty means "no filter set" —
    callers handle that branch separately (this function would return
    False for empty intersection, which is the wrong answer).
    """
    want = {w.lower() for w in wanted if w}
    if not want:
        return True
    return bool(extract_axes(labels) & want)


def group_by_axis(
    issues: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    """Bucket issues by axis label.

    Returns ``(buckets, unaligned_count)`` where ``buckets`` maps an
    axis slug -> list of issues carrying that label, plus a special
    ``"unaligned"`` key for issues with zero axis labels. An issue
    carrying multiple axis labels appears under each bucket. The
    ``unaligned_count`` reflects unique issues, not the sum of bucket
    sizes (an issue can be in two axes but only counts once).
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    unaligned = 0
    for issue in issues:
        axes = extract_axes(issue.get("labels") or [])
        if not axes:
            buckets.setdefault(UNALIGNED_BUCKET, []).append(issue)
            unaligned += 1
            continue
        for ax in sorted(axes):
            buckets.setdefault(ax, []).append(issue)
    return buckets, unaligned


def parse_filter_env(env: str | None = None) -> list[str]:
    """Read ``LOOP_AXIS_FILTER`` (comma-separated) -> sorted unique slugs.

    The CLI sets this env var when ``--axis`` is passed; the tick loop
    consumes it. Empty / missing var -> empty list (= no filter).
    """
    raw = env if env is not None else os.environ.get(AXIS_FILTER_ENV, "")
    if not raw:
        return []
    seen: list[str] = []
    for chunk in raw.split(","):
        c = chunk.strip().lower()
        if c and c not in seen:
            seen.append(c)
    return seen


def filter_issues_by_axes(
    issues: list[dict[str, Any]],
    wanted: list[str],
) -> list[dict[str, Any]]:
    """Return issues whose axis labels intersect ``wanted``.

    Empty ``wanted`` -> returns input unchanged (preserves today's
    behaviour exactly, per the regression-guard acceptance criterion).
    """
    if not wanted:
        return issues
    return [i for i in issues if matches_axes(i.get("labels") or [], wanted)]


__all__ = [
    "AXIS_FILTER_ENV",
    "AXIS_PREFIX",
    "UNALIGNED_BUCKET",
    "extract_axes",
    "filter_issues_by_axes",
    "group_by_axis",
    "matches_axes",
    "parse_filter_env",
]
