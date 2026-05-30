"""Unified logging surface for forge-loop (issue #89).

Before: three logging patterns coexisted — bare ``print()`` for some
operator-facing output, ``master_log.info(path, msg)`` for the master
log file, ``append_event(file, kind, **)`` for the structured audit
trail. Same audit need, three sinks, no consistent fields, no shared
formatter.

This module ships a single :mod:`structlog`-based logger. Output format
auto-detects:

* **TTY** → Rich-formatted, color-coded, human-readable.
* **Non-TTY** (CI, pipes, log files) → newline-delimited JSON.

Every log line carries: ISO timestamp, level, event message, and the
free-form **kwargs the caller passes. ``logger.info("worker_dispatched",
issue=1234, model="opus")`` becomes one structured record sinkable to
anything.

Init is idempotent and lazy: :func:`configure_logging` runs once at
process boot (via :mod:`forge_loop.__init__`); subsequent calls no-op.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, cast

import structlog
from structlog.stdlib import BoundLogger

_CONFIGURED = False


def _is_tty() -> bool:
    """Pretty-print only when stderr is an interactive terminal.

    Honours ``FORGE_LOOP_LOG_JSON=1`` as an explicit override for
    operators who want JSON output even at the terminal (e.g. piping to
    ``jq``).
    """
    if os.environ.get("FORGE_LOOP_LOG_JSON") == "1":
        return False
    return sys.stderr.isatty()


def configure_logging(level: int = logging.INFO) -> None:
    """Wire structlog. Safe to call multiple times — subsequent calls no-op.

    The configuration is intentionally minimal: stdlib logging is the
    transport, structlog handles structured field assembly + rendering.
    This means third-party libs that use stdlib logging (urllib3,
    httpcore, ...) flow through the same formatter — no orphaned plain
    log lines.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    # The shared processor chain — adds the structured context.
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    # Renderer: Rich+color when TTY; JSON otherwise.
    if _is_tty():
        renderer: Any = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Route the stdlib logging through structlog too — so logs from
    # third-party libraries get the same format.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=level,
    )

    _CONFIGURED = True


def get_logger(name: str | None = None) -> BoundLogger:
    """Return a configured logger.

    Bare ``get_logger()`` is fine for module-level use; pass ``name``
    when you want the logger name to appear in the structured payload
    (matches the stdlib convention).
    """
    if not _CONFIGURED:
        configure_logging()
    return cast(BoundLogger, structlog.get_logger(name) if name else structlog.get_logger())


__all__ = ["configure_logging", "get_logger"]
