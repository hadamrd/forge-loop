"""Slack incoming-webhook adapter.

Slack's incoming-webhook contract is "POST JSON with a ``text`` field". We
keep the payload deliberately minimal: no blocks, no mrkdwn flag, no
attachments. The mini-template already gives operators control over the
displayed text; richer formatting belongs to a future v2.
"""

from __future__ import annotations

from typing import Any


def build_payload(text: str) -> dict[str, Any]:
    """Render the wire payload for a Slack incoming webhook."""
    return {"text": text}
