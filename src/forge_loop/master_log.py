"""Master loop log — plain-text timestamped lines the operator can `tail -f`.

Sits alongside the structured events JSONL. The events log is the bus
(machine-readable); the master log is the timeline (human-readable). Both
are written; neither replaces the other.
"""

from __future__ import annotations

import contextlib
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

_LOCK = threading.Lock()


def append(log_path: Path, line: str) -> None:
    """Append a single timestamped line to the master log. Thread-safe."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = f"{ts}  {line}\n"
    with _LOCK, open(log_path, "a") as f:
        f.write(payload)


def info(log_path: Path, line: str) -> None:
    """Log an INFO line + echo to stderr so foreground operators see it."""
    append(log_path, f"INFO  {line}")
    with contextlib.suppress(OSError):
        os.write(2, (f"[loop] {line}\n").encode())


def warn(log_path: Path, line: str) -> None:
    append(log_path, f"WARN  {line}")
    with contextlib.suppress(OSError):
        os.write(2, (f"[loop WARN] {line}\n").encode())


def error(log_path: Path, line: str) -> None:
    append(log_path, f"ERROR {line}")
    with contextlib.suppress(OSError):
        os.write(2, (f"[loop ERROR] {line}\n").encode())
