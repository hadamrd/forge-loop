"""DuckDB-backed query layer over the loop event bus.

The source-of-truth events live in two append-only JSONL files:
- ``loop-runner-events.jsonl``  — per-event records (kind + payload)
- ``loop-runner-summaries.jsonl`` — per-tick consolidated summaries

DuckDB can read JSONL directly via ``read_json_auto``. We expose:

- ``query(sql)`` — run an arbitrary SQL statement (SELECT-only)
- ``recent(kind=, since_minutes=, limit=)`` — convenience filter
- ``count_by_kind(since_minutes=)`` — group-by-kind aggregate

The DB is opened lazily per call (cheap; DuckDB cold-start is ~10ms) so
no resource needs lifecycle management — fits the existing module shape.

This is a READ-ONLY layer. Writes still go through ``state.append_event``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, TypedDict

import duckdb

# ---------------------------------------------------------------------------
# WorkerEvent — typed discriminated union emitted by the SDK-based worker
# (forge_loop._worker_sdk). Each event is a dict with a ``kind`` discriminant
# plus ``seq`` (monotonic per-session) and ``ts`` (ISO-8601 UTC). The runner
# consumes this stream instead of parsing stream-json log lines.
# ---------------------------------------------------------------------------


class _BaseWorkerEvent(TypedDict):
    seq: int
    ts: str
    kind: str


class TurnStartEvent(_BaseWorkerEvent):
    kind: Literal["turn_start"]  # type: ignore[misc]
    data: dict[str, Any]


class ToolUseEvent(_BaseWorkerEvent):
    kind: Literal["tool_use"]  # type: ignore[misc]
    tool: str
    input: dict[str, Any]
    tool_use_id: str


class ToolResultEvent(_BaseWorkerEvent):
    kind: Literal["tool_result"]  # type: ignore[misc]
    tool_use_id: str
    is_error: bool
    content: str


class AssistantTextEvent(_BaseWorkerEvent):
    kind: Literal["assistant_text"]  # type: ignore[misc]
    text: str


class FinalResultEvent(_BaseWorkerEvent):
    kind: Literal["final_result"]  # type: ignore[misc]
    result: str
    cost_usd: float
    usage: dict[str, Any]
    model: str
    num_turns: int
    is_error: bool


class ErrorEvent(_BaseWorkerEvent):
    kind: Literal["error"]  # type: ignore[misc]
    error_type: str
    message: str
    retry_hint: str | None


WorkerEvent = (
    TurnStartEvent
    | ToolUseEvent
    | ToolResultEvent
    | AssistantTextEvent
    | FinalResultEvent
    | ErrorEvent
)


WORKER_EVENT_KINDS: tuple[str, ...] = (
    "turn_start",
    "tool_use",
    "tool_result",
    "assistant_text",
    "final_result",
    "error",
)


def _con(events_path: Path, summaries_path: Path | None = None) -> duckdb.DuckDBPyConnection:
    """Open a fresh in-memory DuckDB and register events/summaries as views."""
    con = duckdb.connect(":memory:")
    if events_path.exists() and events_path.stat().st_size > 0:
        # union_by_name handles ragged JSONL records (different payload shapes)
        con.execute(
            f"CREATE VIEW events AS "
            f"SELECT * FROM read_json_auto('{events_path}', "
            f"format='newline_delimited', union_by_name=true)"
        )
    else:
        con.execute("CREATE VIEW events AS SELECT NULL::VARCHAR AS ts, NULL::VARCHAR AS kind WHERE 0")

    if summaries_path is not None:
        if summaries_path.exists() and summaries_path.stat().st_size > 0:
            con.execute(
                f"CREATE VIEW summaries AS "
                f"SELECT * FROM read_json_auto('{summaries_path}', "
                f"format='newline_delimited', union_by_name=true)"
            )
        else:
            con.execute(
                "CREATE VIEW summaries AS SELECT NULL::VARCHAR AS ts, NULL::INTEGER AS tick WHERE 0"
            )
    return con


def query(
    sql: str,
    events_path: Path,
    summaries_path: Path | None = None,
    *,
    max_rows: int = 200,
) -> list[dict[str, Any]]:
    """Run a SELECT statement against ``events`` (+ optional ``summaries``) view.

    Rows are returned as dicts. The ``max_rows`` ceiling prevents accidental
    pagination-explosions when an MCP client misses a WHERE clause.

    Statements outside SELECT / WITH / SHOW raise ValueError — this is a
    read-only surface.
    """
    stripped = sql.strip().lower()
    allowed_prefixes = ("select", "with", "show", "describe", "pragma")
    if not any(stripped.startswith(p) for p in allowed_prefixes):
        raise ValueError(
            f"eventdb.query is read-only — got: {sql[:60]!r}. "
            f"Use one of: {allowed_prefixes}"
        )

    con = _con(events_path, summaries_path)
    try:
        result = con.execute(sql).fetchall()
        cols = [d[0] for d in con.description]
    finally:
        con.close()

    rows = [dict(zip(cols, row, strict=True)) for row in result[:max_rows]]
    return rows


def recent(
    events_path: Path,
    *,
    kind: str | None = None,
    since_minutes: int | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Most-recent events, oldest-to-newest (or filtered by kind + time window)."""
    conditions: list[str] = []
    if kind:
        # naive-quote: DuckDB single-quote escape
        conditions.append(f"kind = '{kind.replace(chr(39), chr(39) + chr(39))}'")
    if since_minutes is not None and since_minutes > 0:
        # `ts` in our log is ISO 8601 with offset. Compare against utc-now minus delta.
        conditions.append(
            f"CAST(ts AS TIMESTAMP) >= "
            f"now() AT TIME ZONE 'UTC' - INTERVAL '{int(since_minutes)} minutes'"
        )
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"SELECT * FROM events {where} ORDER BY ts DESC LIMIT {int(limit)}"
    rows = query(sql, events_path, max_rows=limit)
    return list(reversed(rows))  # caller expects oldest-first


def count_by_kind(
    events_path: Path,
    *,
    since_minutes: int | None = None,
) -> list[dict[str, Any]]:
    """Group event counts by kind, descending."""
    where = ""
    if since_minutes is not None and since_minutes > 0:
        where = (
            f"WHERE CAST(ts AS TIMESTAMP) >= "
            f"now() AT TIME ZONE 'UTC' - INTERVAL '{int(since_minutes)} minutes'"
        )
    sql = (
        f"SELECT kind, COUNT(*)::INTEGER AS n FROM events {where} "
        f"GROUP BY kind ORDER BY n DESC"
    )
    return query(sql, events_path, max_rows=100)
