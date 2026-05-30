"""Titan sprint-loop — parallel claude-code worker dispatcher."""

__version__ = "0.1.0"

# Initialise logging at process boot (issue #89) — idempotent. Imports
# downstream of forge_loop.* get a configured logger automatically; no
# scattered ``configure_logging()`` calls.
from forge_loop.log import configure_logging as _configure_logging

_configure_logging()
