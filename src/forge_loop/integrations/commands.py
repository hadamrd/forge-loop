"""Slack slash-command handler for the ``/forge`` command.

Wire shape mirrors Slack's slash-command HTTP contract:

    POST application/x-www-form-urlencoded
    text=halt
    user_id=U123
    channel_id=C456

The handler is HTTP-server agnostic — :func:`handle` takes the parsed form
dict and returns ``(status_code, response_text)``. Mount it under any
framework (FastAPI, Flask, raw ``BaseHTTPRequestHandler``) you like.

Auth model: a request is authorised iff its ``channel_id`` matches the
configured ``command_channel_id`` on a Slack channel. Out-of-channel
requests get a flat 403 with no state mutation — the test matrix explicitly
calls this out.

Commands:

    halt    — touch the loop's pause file (loop stops at next tick boundary)
    resume  — remove the pause file
    status  — reply with queue depth + in-flight count
    budget  — reply with today's spend

Each handled command is logged on the loop event bus as ``integration_event``.
Unknown commands and 403s are logged too — silent failures here are how
operators end up debugging "why didn't /forge halt do anything?" at 3am.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge_loop.state import append_event, read_state

from . import Channel

log = logging.getLogger(__name__)

KNOWN_COMMANDS = frozenset({"halt", "resume", "status", "budget"})


@dataclass(frozen=True)
class CommandContext:
    """All the loop-side state the command handler needs to operate.

    Passing this in (rather than reaching for the global :func:`config.load`)
    keeps :func:`handle` trivial to unit-test against a tmp_path.
    """

    pause_file: Path
    state_file: Path
    events_file: Path | None = None
    # Returns today's spend in USD. Injected so tests don't need a ledger.
    today_spend: Callable[[], float] | None = None


@dataclass(frozen=True)
class CommandResult:
    status: int
    text: str


def parse(form: dict[str, Any]) -> tuple[str, list[str]]:
    """Split Slack's ``text`` field into ``(verb, args)``.

    Empty / missing text ⇒ ``("", [])``. Verb is lower-cased so ``/forge HALT``
    and ``/forge halt`` are equivalent.
    """
    raw = str(form.get("text") or "").strip()
    if not raw:
        return "", []
    parts = raw.split()
    return parts[0].lower(), parts[1:]


def _authorised(form: dict[str, Any], channel: Channel) -> bool:
    if not channel.command_channel_id:
        # Operator hasn't opted in to command auth — refuse everything by
        # default. The spec is explicit: "only members of a configured Slack
        # channel can invoke commands". No config ⇒ no commands.
        return False
    return str(form.get("channel_id") or "") == channel.command_channel_id


def _queue_status(ctx: CommandContext) -> tuple[int, int]:
    """Return ``(queue_depth, in_flight)`` from the loop state file.

    Missing / unparsable state ⇒ ``(0, 0)`` — operators get a "0 / 0" reply
    rather than a 500 when the loop has never run.
    """
    try:
        state = read_state(ctx.state_file)
    except Exception:  # noqa: BLE001
        return 0, 0
    def _count(val: Any) -> int:
        if val is None:
            return 0
        if hasattr(val, "__len__"):
            return len(val)
        try:
            return int(val)
        except (TypeError, ValueError):
            return 0

    queue = state.get("queue") or state.get("ready")
    in_flight = state.get("in_flight") or state.get("workers")
    return _count(queue), _count(in_flight)


def handle(
    form: dict[str, Any],
    channel: Channel,
    ctx: CommandContext,
) -> CommandResult:
    """Process one slash-command request.

    Never raises — every reachable error path returns a :class:`CommandResult`
    so the calling HTTP layer can serialise it straight to the wire.
    """
    if not _authorised(form, channel):
        if ctx.events_file is not None:
            append_event(
                ctx.events_file,
                "integration_event",
                channel=channel.name,
                action="command_denied",
                user=str(form.get("user_id") or ""),
                from_channel=str(form.get("channel_id") or ""),
            )
        return CommandResult(403, "forbidden: command not allowed from this channel")

    verb, _args = parse(form)
    if verb not in KNOWN_COMMANDS:
        if ctx.events_file is not None:
            append_event(
                ctx.events_file,
                "integration_event",
                channel=channel.name,
                action="command_unknown",
                verb=verb,
                user=str(form.get("user_id") or ""),
            )
        known = ", ".join(sorted(KNOWN_COMMANDS))
        return CommandResult(200, f"unknown command {verb!r}. try: {known}")

    if verb == "halt":
        ctx.pause_file.parent.mkdir(parents=True, exist_ok=True)
        ctx.pause_file.touch()
        text = "loop will halt at the next tick boundary."
    elif verb == "resume":
        if ctx.pause_file.exists():
            ctx.pause_file.unlink()
        text = "loop resumed."
    elif verb == "status":
        q, f = _queue_status(ctx)
        text = f"queue depth: {q}  |  in-flight: {f}"
    else:  # budget
        if ctx.today_spend is None:
            text = "today's spend: unavailable (no ledger configured)"
        else:
            try:
                amount = ctx.today_spend()
            except Exception as exc:  # noqa: BLE001
                log.warning("today_spend lookup failed: %s", exc)
                text = "today's spend: unavailable"
            else:
                text = f"today's spend: ${amount:.2f}"

    if ctx.events_file is not None:
        append_event(
            ctx.events_file,
            "integration_event",
            channel=channel.name,
            action="command",
            verb=verb,
            user=str(form.get("user_id") or ""),
        )
    return CommandResult(200, text)


__all__ = [
    "KNOWN_COMMANDS",
    "CommandContext",
    "CommandResult",
    "handle",
    "parse",
]
