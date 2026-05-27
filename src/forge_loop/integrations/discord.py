"""Discord webhook adapter.

Discord webhooks accept ``{"content": "..."}``. Discord caps ``content`` at
2000 characters — we truncate with an ellipsis sentinel so a noisy event
never causes a 400.
"""

from __future__ import annotations

from typing import Any

_MAX = 2000


def build_payload(text: str) -> dict[str, Any]:
    if len(text) > _MAX:
        text = text[: _MAX - 1] + "…"
    return {"content": text}
