"""SQLite-backed durable queue (WAL mode).

Replaces the deprecated Redis backend. SQLite gives us ACID semantics
and durability across crashes with zero infrastructure — perfect for the
single-host operator profile. WAL journal mode allows concurrent reads
during a write, which matters when the runner pops while a separate
process inspects the queue.

Schema (one row per task):

    CREATE TABLE tasks (
        id           TEXT PRIMARY KEY,
        payload      TEXT NOT NULL,     -- JSON-encoded dict
        state        TEXT NOT NULL,     -- 'waiting' | 'inflight'
        attempts     INTEGER NOT NULL DEFAULT 0,
        enqueued_at  REAL NOT NULL,
        host_id      TEXT
    );

Ordering: FIFO by ``rowid`` (autoincrement), so pop returns the oldest
``waiting`` row. ``nack`` with ``requeue=True`` resets state to waiting
*without* changing rowid, so the requeued task keeps its head position.

Concurrency: one connection per thread is the SQLite-recommended
pattern; we use a lock + a single connection because the queue is
hit only from the runner's main thread for the planned single-host
deployment. The lock keeps multi-thread tests honest.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from forge_loop.queue import QueueUnavailable, Task

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id           TEXT PRIMARY KEY,
    payload      TEXT NOT NULL,
    state        TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    enqueued_at  REAL NOT NULL,
    host_id      TEXT
);
CREATE INDEX IF NOT EXISTS idx_state_enqueued ON tasks(state, enqueued_at);
"""


class SQLiteQueue:
    """Durable embedded queue backed by a single SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        try:
            # check_same_thread=False is safe because every access goes
            # through ``self._lock``.
            self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        except sqlite3.Error as exc:  # pragma: no cover - filesystem error path
            raise QueueUnavailable(f"sqlite queue open failed at {self.path}: {exc}") from exc

    # ---- Queue protocol ---------------------------------------------

    def push(self, payload: dict[str, object]) -> str:
        tid = uuid.uuid4().hex
        blob = json.dumps(payload)
        now = time.time()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO tasks(id, payload, state, attempts, enqueued_at) "
                    "VALUES (?, ?, 'waiting', 0, ?)",
                    (tid, blob, now),
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                raise QueueUnavailable(f"push failed: {exc}") from exc
        return tid

    def pop_blocking(self, host_id: str, timeout: float = 1.0) -> Task | None:
        """Pop the oldest waiting task. ``timeout`` is a soft poll window."""

        deadline = time.monotonic() + max(timeout, 0.0)
        # SQLite has no built-in BLPOP — poll with short sleeps. The cost
        # is negligible at the scales we care about (one operator, a
        # handful of tasks per tick).
        while True:
            with self._lock:
                try:
                    cur = self._conn.execute(
                        "SELECT id, payload, attempts, enqueued_at FROM tasks "
                        "WHERE state='waiting' ORDER BY rowid LIMIT 1"
                    )
                    row = cur.fetchone()
                    if row is not None:
                        tid, blob, attempts, enqueued_at = row
                        attempts += 1
                        self._conn.execute(
                            "UPDATE tasks SET state='inflight', attempts=?, host_id=? WHERE id=?",
                            (attempts, host_id, tid),
                        )
                        self._conn.commit()
                        return Task(
                            id=tid,
                            payload=json.loads(blob),
                            host_id=host_id,
                            attempts=attempts,
                            enqueued_at=float(enqueued_at),
                        )
                except sqlite3.Error as exc:
                    raise QueueUnavailable(f"pop failed: {exc}") from exc
            if time.monotonic() >= deadline:
                return None
            time.sleep(min(0.1, max(timeout / 10.0, 0.01)))

    def ack(self, task_id: str) -> None:
        with self._lock:
            try:
                self._conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
                self._conn.commit()
            except sqlite3.Error as exc:
                raise QueueUnavailable(f"ack failed: {exc}") from exc

    def nack(self, task_id: str, requeue: bool = True) -> None:
        with self._lock:
            try:
                if requeue:
                    self._conn.execute(
                        "UPDATE tasks SET state='waiting', host_id=NULL WHERE id=?",
                        (task_id,),
                    )
                else:
                    self._conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
                self._conn.commit()
            except sqlite3.Error as exc:
                raise QueueUnavailable(f"nack failed: {exc}") from exc

    def depth(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM tasks WHERE state='waiting'")
            return int(cur.fetchone()[0])

    def in_flight(self) -> int:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM tasks WHERE state='inflight'")
            return int(cur.fetchone()[0])

    def close(self) -> None:
        import contextlib

        with self._lock, contextlib.suppress(sqlite3.Error):
            self._conn.close()
