"""SessionReplayer — replay a recorded Claude Agent SDK session fixture.

A fixture (see :mod:`forge_loop._testing.recorder`) captures a complete
`claude -p` stream-json conversation. The replayer reads it back, asserts
the header schema, optionally feeds each event to a consumer callback in
recorded order, and produces a :class:`forge_loop.worker.WorkerOutcome`
identical to what `run_worker` would have returned live — but with NO
network, NO subprocess, and in milliseconds.

This is the integration test seam: a test loads `happy_path.jsonl`,
replays it, and asserts the worker's downstream outcome-extraction
behaves as it did during recording.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge_loop._testing.recorder import SCHEMA_VERSION


class FixtureCorruptError(ValueError):
    """Raised when a fixture file is missing required structure.

    The error message names the offending field / line so a CI failure
    points at the exact problem instead of a generic JSONDecodeError.
    """


@dataclass
class ReplayedSession:
    header: dict[str, Any]
    events: list[dict[str, Any]]
    outcome: dict[str, Any]

    @property
    def pr_url(self) -> str | None:
        return self.outcome.get("pr")

    @property
    def status(self) -> str:
        return str(self.outcome.get("status", "no_pr"))


REQUIRED_HEADER_FIELDS = ("schema", "issue", "title", "recorded_at")


class SessionReplayer:
    """Load and replay a recorded session fixture."""

    def __init__(self, fixture_path: Path) -> None:
        self._path = Path(fixture_path)
        if not self._path.exists():
            raise FileNotFoundError(f"fixture not found: {self._path}")

    def load(self) -> ReplayedSession:
        """Parse the fixture top-to-bottom. Raises FixtureCorruptError on any
        structural problem (bad JSON, wrong schema, missing trailer, etc.)."""
        lines = self._path.read_text(encoding="utf-8").splitlines()
        if not lines:
            raise FixtureCorruptError(f"{self._path}: empty fixture")

        try:
            header = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise FixtureCorruptError(f"{self._path}:1 header is not JSON: {exc}") from exc

        for field in REQUIRED_HEADER_FIELDS:
            if field not in header:
                raise FixtureCorruptError(
                    f"{self._path}:1 header missing required field '{field}'"
                )
        if header["schema"] != SCHEMA_VERSION:
            raise FixtureCorruptError(
                f"{self._path}:1 unknown schema {header['schema']!r} "
                f"(expected {SCHEMA_VERSION!r})"
            )

        events: list[dict[str, Any]] = []
        outcome: dict[str, Any] | None = None
        last_seq = 0
        for i, raw in enumerate(lines[1:], start=2):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise FixtureCorruptError(f"{self._path}:{i} not JSON: {exc}") from exc
            if obj.get("type") == "outcome" and "pr" in obj and "status" in obj:
                outcome = obj
                continue
            seq = obj.get("seq")
            if not isinstance(seq, int):
                raise FixtureCorruptError(
                    f"{self._path}:{i} event missing integer 'seq' field"
                )
            if seq <= last_seq:
                raise FixtureCorruptError(
                    f"{self._path}:{i} out-of-order event: seq={seq} "
                    f"after seq={last_seq}"
                )
            last_seq = seq
            events.append(obj)

        if outcome is None:
            raise FixtureCorruptError(f"{self._path}: no trailer outcome line")

        return ReplayedSession(header=header, events=events, outcome=outcome)

    def iter_events(self) -> Iterator[dict[str, Any]]:
        """Yield events in recorded order (skips header + trailer)."""
        yield from self.load().events

    def replay_to_worker(
        self,
        consumer: Callable[[dict[str, Any]], None] | None = None,
    ) -> WorkerOutcomeLike:  # noqa: F821 — forward ref for the dataclass below
        """Replay the fixture and synthesise a WorkerOutcome.

        The events are written to a temp stream-json log and passed through
        :func:`forge_loop.worker._extract_outcome` — the same parser the live
        worker uses — so this exercise the real outcome-extraction code path,
        not a mock of it.

        ``consumer`` (optional): called once per event in recorded order,
        useful for tests that want to assert on event sequencing.
        """
        from tempfile import NamedTemporaryFile

        from forge_loop.worker import _extract_outcome

        session = self.load()
        with NamedTemporaryFile(
            "w", suffix=".log", delete=False, encoding="utf-8"
        ) as tmp:
            for e in session.events:
                if consumer is not None:
                    consumer(e)
                # Strip the recorder's bookkeeping field before re-emitting
                # so the parser sees an authentic stream-json line.
                payload = {k: v for k, v in e.items() if k != "seq"}
                tmp.write(json.dumps(payload) + "\n")
            tmp_path = Path(tmp.name)

        try:
            pr_url, parsed_status = _extract_outcome(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

        return WorkerOutcomeLike(
            issue=int(session.header["issue"]),
            title=str(session.header["title"]),
            pr_url=pr_url,
            status=parsed_status if pr_url is not None else session.status,
            recorded_pr_url=session.pr_url,
            recorded_status=session.status,
            event_count=len(session.events),
        )


@dataclass
class WorkerOutcomeLike:
    """A subset of WorkerOutcome reconstructed from a replayed fixture.

    Carries both the *parsed* outcome (what `_extract_outcome` derived from
    the events) and the *recorded* outcome (what the trailer says happened
    live) so tests can assert they match — a parser regression will diverge
    them.
    """
    issue: int
    title: str
    pr_url: str | None
    status: str
    recorded_pr_url: str | None
    recorded_status: str
    event_count: int

    @property
    def matches_recording(self) -> bool:
        return self.pr_url == self.recorded_pr_url and self.status == self.recorded_status
