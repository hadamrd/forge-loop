"""Tests for forge_loop.integrations — adapters, dispatcher, slash commands.

Covers the issue #22 test matrix:

- unit: each adapter formats events + posts to a mock endpoint.
- unit: command parser handles known + unknown commands.
- unit: auth — out-of-channel user trying /forge halt → 403, no state change.
- integration: simulated worker_stuck → mock Slack endpoint sees the payload.
- adversarial: endpoint returns 500 → retry to ``retries`` then drop with
  ``integration_drop`` event.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from forge_loop.integrations import (
    Channel,
    deliver,
    discord,
    dispatch,
    format_event,
    load_channels,
    slack,
    webhook,
)
from forge_loop.integrations import commands as cmd_mod

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _MockPoster:
    """Records every POST. Returns a sequence of pre-programmed statuses."""

    def __init__(self, statuses: list[int] | None = None) -> None:
        self.statuses = statuses or [200]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, payload: dict[str, Any], *, timeout: float) -> int:
        self.calls.append((url, payload))
        idx = min(len(self.calls) - 1, len(self.statuses) - 1)
        return self.statuses[idx]


def _no_sleep(_secs: float) -> None:  # pragma: no cover — trivial
    return None


def _channel(kind: str = "slack", **kw: Any) -> Channel:
    base = {
        "name": "ops",
        "kind": kind,
        "url": "https://example.invalid/hook",
        "on": ("worker_stuck",),
        "format": "{kind} - {message} ({url})",
        "retries": 3,
    }
    base.update(kw)
    return Channel(**base)


# ---------------------------------------------------------------------------
# adapter payload shape
# ---------------------------------------------------------------------------


def test_slack_payload_uses_text_field() -> None:
    assert slack.build_payload("hi") == {"text": "hi"}


def test_discord_payload_uses_content_and_truncates() -> None:
    assert discord.build_payload("hi") == {"content": "hi"}
    long = "x" * 2500
    p = discord.build_payload(long)
    assert len(p["content"]) == 2000
    assert p["content"].endswith("…")


def test_generic_webhook_payload_uses_text() -> None:
    assert webhook.build_payload("hi") == {"text": "hi"}


# ---------------------------------------------------------------------------
# event formatting
# ---------------------------------------------------------------------------


def test_format_event_renders_known_keys() -> None:
    ch = _channel()
    text = format_event(
        ch,
        {"kind": "worker_stuck", "message": "issue 42 stalled", "url": "https://gh/42"},
    )
    assert text == "worker_stuck - issue 42 stalled (https://gh/42)"


def test_format_event_tolerates_missing_keys() -> None:
    ch = _channel(format="{kind} {missing}")
    text = format_event(ch, {"kind": "worker_stuck"})
    assert text == "worker_stuck "


# ---------------------------------------------------------------------------
# deliver / dispatch — happy path + filtering
# ---------------------------------------------------------------------------


def test_deliver_posts_to_mock_endpoint(tmp_path: Path) -> None:
    poster = _MockPoster([200])
    events = tmp_path / "events.jsonl"
    ok = deliver(
        _channel(),
        {"kind": "worker_stuck", "message": "m", "url": "u"},
        poster=poster,
        events_file=events,
        sleep=_no_sleep,
    )
    assert ok is True
    assert len(poster.calls) == 1
    url, payload = poster.calls[0]
    assert url == "https://example.invalid/hook"
    assert payload == {"text": "worker_stuck - m (u)"}
    # integration_event logged with status + attempts=1
    rec = json.loads(events.read_text().splitlines()[-1])
    assert rec["kind"] == "integration_event"
    assert rec["status"] == 200
    assert rec["attempts"] == 1
    assert rec["channel"] == "ops"


def test_dispatch_filters_by_on_list(tmp_path: Path) -> None:
    poster = _MockPoster([200])
    subscribed = _channel(name="a", on=("worker_stuck",))
    other = _channel(name="b", on=("redeploy_failed",))
    out = dispatch(
        [subscribed, other],
        {"kind": "worker_stuck", "message": "m", "url": "u"},
        poster=poster,
        events_file=tmp_path / "events.jsonl",
        sleep=_no_sleep,
    )
    assert out == [("a", True)]
    assert len(poster.calls) == 1


def test_dispatch_empty_on_list_matches_everything(tmp_path: Path) -> None:
    poster = _MockPoster([200])
    wildcard = _channel(on=())
    out = dispatch(
        [wildcard],
        {"kind": "anything", "message": "m", "url": "u"},
        poster=poster,
        events_file=tmp_path / "events.jsonl",
        sleep=_no_sleep,
    )
    assert out == [("ops", True)]


# ---------------------------------------------------------------------------
# adversarial: retry + drop
# ---------------------------------------------------------------------------


def test_deliver_retries_on_500_then_drops_with_integration_drop(tmp_path: Path) -> None:
    poster = _MockPoster([500, 500, 500])
    events = tmp_path / "events.jsonl"
    ok = deliver(
        _channel(retries=3),
        {"kind": "worker_stuck", "message": "m", "url": "u"},
        poster=poster,
        events_file=events,
        sleep=_no_sleep,
    )
    assert ok is False
    assert len(poster.calls) == 3, "should retry exactly `retries` times"
    rec = json.loads(events.read_text().splitlines()[-1])
    assert rec["kind"] == "integration_drop"
    assert rec["status"] == 500
    assert rec["attempts"] == 3


def test_deliver_does_not_retry_4xx(tmp_path: Path) -> None:
    poster = _MockPoster([404, 200, 200])
    ok = deliver(
        _channel(retries=3),
        {"kind": "worker_stuck", "message": "m", "url": "u"},
        poster=poster,
        events_file=tmp_path / "events.jsonl",
        sleep=_no_sleep,
    )
    assert ok is False
    assert len(poster.calls) == 1, "4xx is permanent — must not retry"


def test_deliver_recovers_after_transient_error(tmp_path: Path) -> None:
    class _FlakyPoster:
        def __init__(self) -> None:
            self.n = 0

        def __call__(self, url: str, payload: dict[str, Any], *, timeout: float) -> int:
            self.n += 1
            if self.n == 1:
                raise OSError("connection reset")
            return 200

    poster = _FlakyPoster()
    events = tmp_path / "events.jsonl"
    ok = deliver(
        _channel(retries=3),
        {"kind": "worker_stuck", "message": "m", "url": "u"},
        poster=poster,
        events_file=events,
        sleep=_no_sleep,
    )
    assert ok is True
    assert poster.n == 2
    rec = json.loads(events.read_text().splitlines()[-1])
    assert rec["kind"] == "integration_event"
    assert rec["attempts"] == 2


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------


def test_load_channels_reads_yaml_files(tmp_path: Path) -> None:
    base = tmp_path / ".forge" / "integrations"
    base.mkdir(parents=True)
    (base / "ops.yaml").write_text(
        "kind: slack\n"
        "url: https://hooks.slack.com/x\n"
        "on: [worker_stuck, critic_blocking]\n"
        "format: '{kind} :: {message}'\n"
        "retries: 5\n"
    )
    (base / "discord.yaml").write_text(
        "name: alerts\nkind: discord\nurl: https://discord.com/hook\non: [redeploy_failed]\n"
    )
    channels = load_channels(tmp_path)
    assert {c.name for c in channels} == {"ops", "alerts"}
    ops = next(c for c in channels if c.name == "ops")
    assert ops.kind == "slack"
    assert ops.on == ("worker_stuck", "critic_blocking")
    assert ops.retries == 5
    assert ops.format == "{kind} :: {message}"


def test_load_channels_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert load_channels(tmp_path) == []


def test_load_channels_skips_malformed_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    base = tmp_path / ".forge" / "integrations"
    base.mkdir(parents=True)
    (base / "bad.yaml").write_text("kind: slack\n")  # missing url
    (base / "good.yaml").write_text("kind: slack\nurl: https://x\n")
    channels = load_channels(tmp_path)
    assert {c.name for c in channels} == {"good"}


# ---------------------------------------------------------------------------
# integration: worker_stuck → mock Slack endpoint
# ---------------------------------------------------------------------------


def test_integration_worker_stuck_reaches_slack_endpoint(tmp_path: Path) -> None:
    base = tmp_path / ".forge" / "integrations"
    base.mkdir(parents=True)
    (base / "ops.yaml").write_text(
        "kind: slack\n"
        "url: https://hooks.slack.com/services/T/B/X\n"
        "on: [worker_stuck]\n"
        "format: '[{kind}] {message} -> {url}'\n"
    )
    channels = load_channels(tmp_path)
    poster = _MockPoster([200])
    events = tmp_path / "events.jsonl"
    event = {
        "kind": "worker_stuck",
        "message": "issue 99 idle 15m",
        "url": "https://github.com/x/y/issues/99",
    }
    out = dispatch(channels, event, poster=poster, events_file=events, sleep=_no_sleep)
    assert out == [("ops", True)]
    assert poster.calls[0][0] == "https://hooks.slack.com/services/T/B/X"
    assert poster.calls[0][1] == {
        "text": "[worker_stuck] issue 99 idle 15m -> https://github.com/x/y/issues/99"
    }
    log_line = json.loads(events.read_text().splitlines()[-1])
    assert log_line["kind"] == "integration_event"
    assert log_line["event_kind"] == "worker_stuck"


# ---------------------------------------------------------------------------
# slash-command parser + auth
# ---------------------------------------------------------------------------


def _ctx(tmp_path: Path) -> cmd_mod.CommandContext:
    return cmd_mod.CommandContext(
        pause_file=tmp_path / "loop.pause",
        state_file=tmp_path / "loop.json",
        events_file=tmp_path / "events.jsonl",
    )


def _slack_channel(channel_id: str = "C-ALLOWED") -> Channel:
    return _channel(command_channel_id=channel_id)


def test_parser_extracts_verb_and_args() -> None:
    assert cmd_mod.parse({"text": "halt"}) == ("halt", [])
    assert cmd_mod.parse({"text": "STATUS extra"}) == ("status", ["extra"])
    assert cmd_mod.parse({"text": "  "}) == ("", [])
    assert cmd_mod.parse({}) == ("", [])


def test_halt_creates_pause_file_and_logs(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    res = cmd_mod.handle(
        {"text": "halt", "user_id": "U1", "channel_id": "C-ALLOWED"},
        _slack_channel(),
        ctx,
    )
    assert res.status == 200
    assert "halt" in res.text.lower()
    assert ctx.pause_file.exists()
    rec = json.loads(ctx.events_file.read_text().splitlines()[-1])  # type: ignore[union-attr]
    assert rec["kind"] == "integration_event"
    assert rec["action"] == "command"
    assert rec["verb"] == "halt"


def test_resume_removes_pause_file(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.pause_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.pause_file.touch()
    res = cmd_mod.handle(
        {"text": "resume", "user_id": "U1", "channel_id": "C-ALLOWED"},
        _slack_channel(),
        ctx,
    )
    assert res.status == 200
    assert not ctx.pause_file.exists()


def test_status_reads_queue_and_in_flight_from_state(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.state_file.write_text(json.dumps({"queue": [1, 2, 3], "in_flight": [{"issue": 7}]}))
    res = cmd_mod.handle(
        {"text": "status", "channel_id": "C-ALLOWED"},
        _slack_channel(),
        ctx,
    )
    assert res.status == 200
    assert "3" in res.text
    assert "1" in res.text


def test_status_missing_state_returns_zero(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    res = cmd_mod.handle(
        {"text": "status", "channel_id": "C-ALLOWED"},
        _slack_channel(),
        ctx,
    )
    assert res.status == 200
    assert "0" in res.text


def test_unknown_command_returns_help_and_does_not_mutate(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    res = cmd_mod.handle(
        {"text": "explode", "user_id": "U1", "channel_id": "C-ALLOWED"},
        _slack_channel(),
        ctx,
    )
    assert res.status == 200
    assert "unknown" in res.text.lower()
    assert not ctx.pause_file.exists()
    rec = json.loads(ctx.events_file.read_text().splitlines()[-1])  # type: ignore[union-attr]
    assert rec["kind"] == "integration_event"
    assert rec.get("verb") == "explode"


# ---------------------------------------------------------------------------
# auth: out-of-channel user is rejected with 403 and zero state change
# ---------------------------------------------------------------------------


def test_out_of_channel_halt_is_403_and_no_state_change(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    res = cmd_mod.handle(
        {"text": "halt", "user_id": "U-EVIL", "channel_id": "C-INTRUDER"},
        _slack_channel("C-ALLOWED"),
        ctx,
    )
    assert res.status == 403
    assert not ctx.pause_file.exists(), "halt from foreign channel must NOT pause the loop"
    rec = json.loads(ctx.events_file.read_text().splitlines()[-1])  # type: ignore[union-attr]
    assert rec["kind"] == "integration_event"
    assert rec["action"] == "command_denied"
    assert rec["from_channel"] == "C-INTRUDER"


def test_unconfigured_channel_rejects_all_commands(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ch = _channel()  # command_channel_id="" by default
    res = cmd_mod.handle(
        {"text": "halt", "channel_id": "C-ANYTHING"},
        ch,
        ctx,
    )
    assert res.status == 403
    assert not ctx.pause_file.exists()
