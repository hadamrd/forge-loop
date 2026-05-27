"""Push-notification + chat-command integrations.

A channel is one YAML file under ``.forge/integrations/*.yaml`` describing
either a Slack webhook, a Discord webhook, or a generic HTTP webhook plus
the set of loop events that should be forwarded to it.

The public surface is intentionally small:

- :func:`load_channels` — read every ``.forge/integrations/*.yaml`` file.
- :func:`dispatch` — fan an event out to the channels that subscribe to it.
- :class:`Channel` — the post-load dataclass each adapter consumes.

Adapters live in :mod:`.slack`, :mod:`.discord`, :mod:`.webhook`. Chat-command
handling (Slack slash commands) lives in :mod:`.commands`.

All notification + command activity is recorded as ``integration_event`` (or
``integration_drop`` on permanent delivery failure) on the loop event bus
via :func:`forge_loop.state.append_event`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from forge_loop.state import append_event

log = logging.getLogger(__name__)

# The four event kinds the spec calls out by name. Listed here so that an
# operator's typo in ``on:`` surfaces as a clear "unknown event" error rather
# than a silently-never-fires subscription.
KNOWN_EVENTS = frozenset(
    {
        "worker_stuck",
        "critic_blocking",
        "redeploy_failed",
    }
)

DEFAULT_FORMAT = "{kind} - {message} ({url})"
DEFAULT_RETRIES = 3


@dataclass(frozen=True)
class Channel:
    """One notification destination loaded from a YAML file."""

    name: str
    kind: str  # "slack" | "discord" | "webhook"
    url: str
    on: tuple[str, ...] = ()
    format: str = DEFAULT_FORMAT
    retries: int = DEFAULT_RETRIES
    # Slack-only: command auth. Members of ``command_channel_id`` may invoke
    # ``/forge`` slash commands. Empty string disables command auth (i.e.,
    # all command requests are rejected).
    command_channel_id: str = ""
    # Slack-only: bot token (xoxb-...) for membership lookups. Optional —
    # if absent, command auth degrades to comparing the slash-command's
    # ``channel_id`` against ``command_channel_id``.
    bot_token: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class Poster(Protocol):
    """HTTP-POST callable used by every adapter. Override in tests."""

    def __call__(self, url: str, payload: dict[str, Any], *, timeout: float) -> int: ...


def _default_poster(url: str, payload: dict[str, Any], *, timeout: float) -> int:
    """Real network POST. Returns HTTP status code; raises on transport error."""
    import json
    import urllib.error
    import urllib.request

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.getcode())
    except urllib.error.HTTPError as e:  # 4xx / 5xx
        return int(e.code)


def _coerce_channel(name: str, raw: dict[str, Any]) -> Channel:
    try:
        kind = str(raw["kind"]).lower()
        url = str(raw["url"])
    except KeyError as exc:
        raise ValueError(f"channel {name!r}: missing required field {exc.args[0]!r}") from exc
    if kind not in {"slack", "discord", "webhook"}:
        raise ValueError(f"channel {name!r}: unknown kind {kind!r}")

    # PyYAML parses YAML 1.1, where ``on:`` is the boolean ``True``. Accept
    # both forms so operators don't have to remember to quote ``"on":``.
    on_raw = raw.get("on")
    if on_raw is None:
        # PyYAML parses bare ``on:`` as the bool True (YAML 1.1). Look it up
        # by bool key via the untyped Mapping protocol so mypy stays quiet.
        raw_any: Any = raw
        on_raw = raw_any.get(True)
    on = tuple(on_raw or ())
    unknown = set(on) - KNOWN_EVENTS
    if unknown:
        # Soft-warn rather than hard-fail — operators may legitimately
        # subscribe to custom event kinds emitted by their own code.
        log.warning("channel %s subscribes to non-canonical events: %s", name, sorted(unknown))

    return Channel(
        name=name,
        kind=kind,
        url=url,
        on=on,
        format=str(raw.get("format") or DEFAULT_FORMAT),
        retries=int(raw.get("retries", DEFAULT_RETRIES)),
        command_channel_id=str(raw.get("command_channel_id") or ""),
        bot_token=str(raw.get("bot_token") or ""),
        extra={
            k: v
            for k, v in raw.items()
            if k
            not in {
                "kind",
                "url",
                "on",
                "format",
                "retries",
                "command_channel_id",
                "bot_token",
                "name",
                True,
            }
        },
    )


def load_channels(root: Path) -> list[Channel]:
    """Load every channel YAML under ``root / '.forge' / 'integrations'``.

    Missing directory ⇒ empty list (integrations are opt-in). A malformed
    YAML file is logged and skipped — one broken channel must not silence
    the rest.
    """
    base = root / ".forge" / "integrations"
    if not base.is_dir():
        return []
    channels: list[Channel] = []
    for path in sorted(base.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text()) or {}
            if not isinstance(raw, dict):
                raise ValueError("top-level YAML must be a mapping")
            name = str(raw.get("name") or path.stem)
            channels.append(_coerce_channel(name, raw))
        except Exception as exc:  # noqa: BLE001
            log.warning("integrations: skipping %s — %s", path, exc)
    return channels


def format_event(channel: Channel, event: dict[str, Any]) -> str:
    """Apply the channel's mini-template to a loop event dict.

    Missing template keys render as ``""`` rather than raising — operators
    don't need to memorise the full schema of every event kind.
    """

    class _Defaulting(dict[str, Any]):
        def __missing__(self, key: str) -> str:  # noqa: D401
            return ""

    return channel.format.format_map(_Defaulting(event))


def _post_for_kind(channel: Channel, text: str) -> dict[str, Any]:
    """Build the wire payload for the given channel kind."""
    if channel.kind == "slack":
        from .slack import build_payload as _slack

        return _slack(text)
    if channel.kind == "discord":
        from .discord import build_payload as _discord

        return _discord(text)
    from .webhook import build_payload as _webhook

    return _webhook(text)


def deliver(
    channel: Channel,
    event: dict[str, Any],
    *,
    poster: Poster | None = None,
    events_file: Path | None = None,
    sleep: Any = None,
    timeout: float = 5.0,
) -> bool:
    """Format ``event`` for ``channel`` and POST it with retry.

    Returns ``True`` if the endpoint eventually accepts the payload (HTTP
    2xx/3xx within ``channel.retries`` attempts). On permanent failure an
    ``integration_drop`` event is appended to ``events_file`` and we return
    ``False`` — caller continues to the next channel.

    Note: 4xx responses are treated as permanent (no retry); only 5xx and
    transport errors are retried. Mirrors Slack/Discord's own webhook
    documentation.
    """
    poster = poster or _default_poster
    if sleep is None:
        import time

        sleep = time.sleep

    text = format_event(channel, event)
    payload = _post_for_kind(channel, text)

    attempts = max(1, channel.retries)
    last_status: int | str = "unsent"
    for attempt in range(1, attempts + 1):
        try:
            status = poster(channel.url, payload, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — adapter must never bubble
            last_status = f"err:{exc.__class__.__name__}"
            log.warning("integrations: %s attempt %d errored: %s", channel.name, attempt, exc)
        else:
            last_status = status
            if 200 <= status < 400:
                if events_file is not None:
                    append_event(
                        events_file,
                        "integration_event",
                        channel=channel.name,
                        channel_kind=channel.kind,
                        event_kind=event.get("kind"),
                        status=status,
                        attempts=attempt,
                    )
                return True
            if 400 <= status < 500:
                # Permanent — don't retry a misconfigured URL or auth failure.
                break
        if attempt < attempts:
            sleep(min(2 ** (attempt - 1), 8))

    if events_file is not None:
        append_event(
            events_file,
            "integration_drop",
            channel=channel.name,
            channel_kind=channel.kind,
            event_kind=event.get("kind"),
            status=last_status,
            attempts=attempts,
        )
    return False


def dispatch(
    channels: list[Channel],
    event: dict[str, Any],
    *,
    poster: Poster | None = None,
    events_file: Path | None = None,
    sleep: Any = None,
) -> list[tuple[str, bool]]:
    """Fan ``event`` out to every channel whose ``on:`` list contains its kind.

    Returns ``[(channel_name, delivered), ...]`` for inspection in tests.
    """
    kind = event.get("kind")
    out: list[tuple[str, bool]] = []
    for ch in channels:
        if ch.on and kind not in ch.on:
            continue
        ok = deliver(
            ch,
            event,
            poster=poster,
            events_file=events_file,
            sleep=sleep,
        )
        out.append((ch.name, ok))
    return out


__all__ = [
    "Channel",
    "DEFAULT_FORMAT",
    "DEFAULT_RETRIES",
    "KNOWN_EVENTS",
    "Poster",
    "deliver",
    "dispatch",
    "format_event",
    "load_channels",
]
