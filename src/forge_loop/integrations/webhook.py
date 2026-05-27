"""Generic HTTP-webhook adapter.

A POST with ``{"text": ...}``. Operators that need a different schema can
override the wire shape by writing their own adapter — this one mirrors
Slack's contract so that ``ngrok``-style local listeners work out of the
box during development.
"""

from __future__ import annotations

from typing import Any


def build_payload(text: str) -> dict[str, Any]:
    return {"text": text}
