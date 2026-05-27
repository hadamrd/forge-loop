"""Extensible help / runbook tool — AI agents can query operator knowledge.

The "manual" is a directory of markdown files; each file is one topic. Agents
discover topics via ``manual_topics()`` and read content via ``manual_lookup(topic)``.

Operators extend the manual by dropping new .md files into the manual dir
(default ``dev/sprint-loop/manual/``). No code change needed. Per-repo overrides
are supported: a file in the repo root takes precedence over the package default.

This module is intentionally side-effect-free + filesystem-only — no network,
no secrets resolution. The manual TEXT may DESCRIBE how to use secrets (e.g.
"run `your-secret-tool get KEY`") but does not perform the lookup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ManualEntry:
    topic: str
    path: Path
    title: str
    body: str


def _candidate_dirs(repo_root: Path) -> list[Path]:
    """Where to look for manual entries, in priority order."""
    return [
        repo_root / "dev" / "sprint-loop" / "manual",
        Path(__file__).resolve().parent.parent.parent / "manual",  # package dir
    ]


def _read_entry(path: Path) -> ManualEntry:
    text = path.read_text()
    # First non-empty line as title; strip leading '#'.
    title = path.stem
    for line in text.splitlines():
        s = line.strip()
        if s:
            title = s.lstrip("#").strip() or path.stem
            break
    return ManualEntry(topic=path.stem, path=path, title=title, body=text)


def list_topics(repo_root: Path) -> list[ManualEntry]:
    """Return all known manual entries (deduped by topic key — first dir wins)."""
    seen: dict[str, ManualEntry] = {}
    for d in _candidate_dirs(repo_root):
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.md")):
            topic = path.stem
            if topic in seen:
                continue
            seen[topic] = _read_entry(path)
    return list(seen.values())


def lookup(repo_root: Path, topic: str) -> ManualEntry | None:
    """Find a manual entry by exact topic key (file stem).

    Topic keys are case-insensitive; the lookup tries case-sensitive first
    then falls back to a case-insensitive match.
    """
    entries = list_topics(repo_root)
    for e in entries:
        if e.topic == topic:
            return e
    lowered = topic.lower()
    for e in entries:
        if e.topic.lower() == lowered:
            return e
    return None


def search(repo_root: Path, query: str, limit: int = 5) -> list[ManualEntry]:
    """Return entries whose topic or body contains ``query`` (case-insensitive)."""
    q = query.lower()
    if not q:
        return []
    matches: list[ManualEntry] = []
    for e in list_topics(repo_root):
        if q in e.topic.lower() or q in e.title.lower() or q in e.body.lower():
            matches.append(e)
    # Stable sort: topic-match first, then title-match, then body-match.
    def _score(e: ManualEntry) -> int:
        if q in e.topic.lower():
            return 0
        if q in e.title.lower():
            return 1
        return 2

    matches.sort(key=_score)
    return matches[:limit]


# ── Pure helpers for tests ──────────────────────────────────────────────────


_TITLE_RE = re.compile(r"^#+\s*(.+?)\s*$", re.MULTILINE)


def extract_title_from_text(text: str) -> str | None:
    """Best-effort title extraction from a markdown body."""
    m = _TITLE_RE.search(text)
    return m.group(1).strip() if m else None
