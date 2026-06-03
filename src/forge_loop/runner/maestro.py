"""Maestro tick: let durable frontier + memory inform dispatch.

This is the first step toward a boot-context-driven loop. Once per dispatching
tick the runner loads the frontier cursor and curated rejected paths and uses
them to (a) reorder candidate issues — frontier-aligned work first, known
dead-end work last — and (b) hand each worker a compact advisory context block
(product goal, next expansion, hot files, approaches not to re-attempt).

Everything here is advisory and best-effort: ``load_maestro_inputs`` swallows
any failure and returns empty inputs, and an empty plan leaves dispatch
byte-identical to the legacy tick.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from forge_loop.frontier import FrontierCursor

_WORD = re.compile(r"[a-z0-9]{4,}")
# Path separators / extension boundary used to split hot-file refs into tokens.
_PATH_SPLIT = re.compile(r"[\\/]+")


@dataclass(frozen=True)
class MaestroPlan:
    """A per-tick dispatch plan derived from durable context."""

    frontier_goal: str
    next_expansion: str
    hot_files: tuple[str, ...]
    rejected: tuple[str, ...]
    prioritized_issue_numbers: tuple[int, ...]
    deprioritized_issue_numbers: tuple[int, ...]
    brief_context: str

    def event_payload(self) -> dict[str, Any]:
        return {
            "frontier_goal": self.frontier_goal,
            "next_expansion": self.next_expansion,
            "hot_files": list(self.hot_files),
            "rejected_paths": list(self.rejected),
            "prioritized_issues": list(self.prioritized_issue_numbers),
            "deprioritized_issues": list(self.deprioritized_issue_numbers),
            "context_applied": bool(self.brief_context),
        }


def load_maestro_inputs(cfg: Any) -> tuple[FrontierCursor | None, tuple[str, ...]]:
    """Load the frontier cursor + rejected-path titles. Best-effort.

    Returns ``(None, ())`` on ANY failure (uninitialised repo, unreadable
    store, …) so the maestro step is a no-op rather than a tick breaker.
    """
    try:
        from forge_loop.control.boot import build_boot_sources

        sources = build_boot_sources(cfg.repo)
        frontier = sources.frontier_store.load()
        rejected: tuple[str, ...] = ()
        if sources.memory_store is not None:
            rejected = tuple(item.title for item in sources.memory_store.list_rejected_paths())
        return frontier, rejected
    except Exception:  # noqa: BLE001 - control-plane reads are advisory
        return None, ()


def build_maestro_plan(
    issues: list[dict[str, Any]],
    *,
    frontier: FrontierCursor | None,
    rejected_path_titles: tuple[str, ...],
) -> MaestroPlan:
    """Reorder ``issues`` by frontier alignment and render advisory context.

    Frontier-aligned issues sort first; issues matching a rejected path sort
    last (deprioritised, never dropped — a stale rejected path must not be able
    to starve the loop). The reorder is a stable permutation: it never adds or
    removes issues, so the caller's ``issues``/``workers_meta`` lockstep holds.
    """
    hot_files = ()
    rejected_ideas: tuple[str, ...] = tuple(t for t in rejected_path_titles if t.strip())
    goal = next_expansion = ""
    keywords: set[str] = set()

    if frontier is not None:
        goal = frontier.product_goal
        next_expansion = frontier.next_expansion
        hot_files = tuple(artifact.ref for artifact in frontier.hot_files)
        rejected_ideas = (
            *rejected_ideas,
            *(path.idea for path in frontier.rejected_paths if path.idea.strip()),
        )
        keywords = set(_WORD.findall(next_expansion.lower()))
        for ref in hot_files:
            keywords.update(_hot_file_tokens(ref))

    rejected_matchers = tuple(_rejected_matcher(idea) for idea in rejected_ideas)
    rejected_matchers = tuple(m for m in rejected_matchers if m is not None)

    def _bucket(issue: dict[str, Any]) -> int:
        text = _issue_text(issue)
        if any(matcher.search(text) for matcher in rejected_matchers):
            return 2  # known dead end → last
        if keywords and (keywords & _issue_tokens(issue)):
            return 0  # frontier-aligned → first
        return 1  # neutral

    indexed = list(enumerate(issues))
    # Stable sort by bucket: preserves the upstream pickup order within a bucket.
    ordered = sorted(indexed, key=lambda pair: (_bucket(pair[1]), pair[0]))
    prioritized = tuple(int(issue["number"]) for _, issue in ordered)
    deprioritized = tuple(int(issue["number"]) for _, issue in ordered if _bucket(issue) == 2)

    return MaestroPlan(
        frontier_goal=goal,
        next_expansion=next_expansion,
        hot_files=hot_files,
        rejected=rejected_ideas,
        prioritized_issue_numbers=prioritized,
        deprioritized_issue_numbers=deprioritized,
        brief_context=_render_brief_context(goal, next_expansion, hot_files, rejected_ideas),
    )


def _issue_text(issue: dict[str, Any]) -> str:
    labels = " ".join(lab.get("name", "") for lab in (issue.get("labels") or []))
    return f"{issue.get('title', '')} {issue.get('body', '')} {labels}".lower()


def _issue_tokens(issue: dict[str, Any]) -> set[str]:
    """Word tokens from an issue's title/body plus its ``axis:*`` label names.

    ``axis:foo`` labels contribute the axis name (``foo``) as a token so that
    frontier keywords can align with an issue purely via its axis labelling,
    not only via free text.
    """
    tokens = set(_WORD.findall(_issue_text(issue)))
    for lab in issue.get("labels") or []:
        name = lab.get("name", "")
        if name.lower().startswith("axis:"):
            tokens.update(_WORD.findall(name.lower()))
    return tokens


def _hot_file_tokens(ref: str) -> set[str]:
    """Tokenise a hot-file ref into useful path components.

    ``src/forge_loop/runner/dispatch.py`` -> ``{dispatch, runner}`` (basename
    without its extension plus the immediate parent dir), in addition to any
    4+ char tokens the raw ref happens to contain.
    """
    lowered = ref.lower()
    tokens = set(_WORD.findall(lowered))
    parts = [p for p in _PATH_SPLIT.split(lowered) if p]
    if parts:
        basename = parts[-1].rsplit(".", 1)[0]
        tokens.update(_WORD.findall(basename))
        if len(parts) >= 2:
            tokens.update(_WORD.findall(parts[-2]))
    return tokens


def _rejected_matcher(idea: str) -> re.Pattern[str] | None:
    """Compile a word-boundary matcher for a rejected idea.

    Single tokens match on word boundaries (so ``poll`` does not match
    ``polling``); multi-word ideas match as a whitespace-flexible phrase. Ideas
    with no usable tokens yield ``None`` (skipped).
    """
    words = re.findall(r"[a-z0-9]+", idea.lower())
    if not words:
        return None
    phrase = r"\s+".join(re.escape(w) for w in words)
    return re.compile(rf"\b{phrase}\b")


def _render_brief_context(
    goal: str,
    next_expansion: str,
    hot_files: tuple[str, ...],
    rejected: tuple[str, ...],
) -> str:
    if not (goal or next_expansion or hot_files or rejected):
        return ""
    lines = ["=== MAESTRO CONTEXT (durable frontier + memory; advisory) ==="]
    if goal:
        lines.append(f"Product goal: {goal}")
    if next_expansion:
        lines.append(f"Next expansion: {next_expansion}")
    if hot_files:
        lines.append("Hot files: " + ", ".join(hot_files))
    if rejected:
        lines.append("Do NOT re-attempt these rejected approaches (only with new evidence):")
        lines.extend(f"  - {idea}" for idea in rejected)
    lines.append("=== END MAESTRO CONTEXT ===")
    return "\n".join(lines)
