"""Unit tests for forge_loop.budget — pricing, label parse, ledger, fallbacks."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from forge_loop import budget as B

# ---------------------------------------------------------------------------
# cost_for_usage — per-(model, input, output, cache_*)
# ---------------------------------------------------------------------------


def test_cost_for_sonnet_4_6_input_only() -> None:
    # 1_000_000 input tokens at $3 = $3
    cost = B.cost_for_usage("claude-sonnet-4-6", {"input_tokens": 1_000_000, "output_tokens": 0,
                                                  "cache_creation_input_tokens": 0,
                                                  "cache_read_input_tokens": 0})
    assert cost == pytest.approx(3.0)


def test_cost_for_haiku_4_5_output() -> None:
    # 200k output at $5 / 1M = $1
    cost = B.cost_for_usage("claude-haiku-4-5", {
        "input_tokens": 0, "output_tokens": 200_000,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    })
    assert cost == pytest.approx(1.0)


def test_cost_for_opus_full_breakdown() -> None:
    # 1M input + 100k output + 1M cache_create + 1M cache_read
    # = 15 + 7.5 + 18.75 + 1.5 = 42.75
    cost = B.cost_for_usage("claude-opus-4-7", {
        "input_tokens": 1_000_000, "output_tokens": 100_000,
        "cache_creation_input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
    })
    assert cost == pytest.approx(15.0 + 7.5 + 18.75 + 1.5)


def test_dated_suffix_normalizes() -> None:
    # SDK reports model names with -YYYYMMDD; still resolves to haiku rate.
    cost = B.cost_for_usage("claude-haiku-4-5-20251001", {
        "input_tokens": 1_000_000, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    })
    assert cost == pytest.approx(1.0)


def test_context_window_suffix_normalizes() -> None:
    cost = B.cost_for_usage("claude-opus-4-7[1m]", {
        "input_tokens": 1_000_000, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    })
    assert cost == pytest.approx(15.0)


def test_unknown_model_defaults_to_opus_worst_case() -> None:
    # New model id we don't know about -> Opus rate (most expensive).
    cost = B.cost_for_usage("claude-zeus-9", {
        "input_tokens": 1_000_000, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    })
    assert cost == pytest.approx(15.0)


def test_zero_usage_is_zero_cost() -> None:
    cost = B.cost_for_usage("claude-sonnet-4-6", {
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    })
    assert cost == 0.0


# ---------------------------------------------------------------------------
# Adversarial: missing usage data → worst-case fallback (NOT silent zero)
# ---------------------------------------------------------------------------


def test_missing_usage_returns_fallback_not_zero() -> None:
    """Adversarial: if a runaway worker emits events with no usage block, we
    MUST NOT silently treat them as $0 — that would defeat the gate."""
    assert B.cost_for_usage("claude-opus-4-7", None) > 0.0
    assert B.cost_for_usage("claude-opus-4-7", {}) > 0.0


def test_garbage_usage_returns_fallback() -> None:
    assert B.cost_for_usage("claude-opus-4-7", {"input_tokens": "lol"}) > 0.0
    # Even partial garbage should fall back, not silently undercount.
    assert B.cost_for_usage(
        "claude-opus-4-7", {"input_tokens": None, "output_tokens": "x"}
    ) > 0.0


def test_no_model_falls_back_to_opus() -> None:
    cost = B.cost_for_usage(None, {
        "input_tokens": 1_000_000, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    })
    assert cost == pytest.approx(15.0)


# ---------------------------------------------------------------------------
# Label override
# ---------------------------------------------------------------------------


def test_parse_budget_label_gh_shape() -> None:
    assert B.parse_budget_label([{"name": "budget:0.5"}, {"name": "loop:ready"}]) == 0.5


def test_parse_budget_label_string_shape() -> None:
    assert B.parse_budget_label(["budget:2", "other"]) == 2.0


def test_parse_budget_label_missing_returns_none() -> None:
    assert B.parse_budget_label([{"name": "loop:ready"}]) is None
    assert B.parse_budget_label(None) is None
    assert B.parse_budget_label([]) is None


def test_parse_budget_label_picks_strictest() -> None:
    # If multiple labels set, strictest (smallest) wins.
    assert B.parse_budget_label([{"name": "budget:5"}, {"name": "budget:1"}]) == 1.0


def test_ticket_budget_label_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOP_TICKET_BUDGET_USD", "5.0")
    assert B.ticket_budget_for([{"name": "budget:0.1"}]) == 0.1


def test_ticket_budget_env_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOP_TICKET_BUDGET_USD", "7.5")
    assert B.ticket_budget_for(None) == 7.5


def test_ticket_budget_built_in_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOOP_TICKET_BUDGET_USD", raising=False)
    assert B.ticket_budget_for(None) == 5.0


def test_tick_budget_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOOP_TICK_BUDGET_USD", raising=False)
    assert B.tick_budget() == 20.0


def test_tick_budget_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOP_TICK_BUDGET_USD", "100")
    assert B.tick_budget() == 100.0


# ---------------------------------------------------------------------------
# TicketBudgetTracker — accumulates and trips on crossing
# ---------------------------------------------------------------------------


def test_tracker_accumulates_then_trips() -> None:
    t = B.TicketBudgetTracker(ceiling_usd=10.0)
    # 1M input @ $3 each call (sonnet) — 3rd call should cross $10
    usage = {"input_tokens": 1_000_000, "output_tokens": 0,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    assert t.add("claude-sonnet-4-6", usage) is False  # 3
    assert t.add("claude-sonnet-4-6", usage) is False  # 6
    assert t.add("claude-sonnet-4-6", usage) is False  # 9
    assert t.add("claude-sonnet-4-6", usage) is True   # 12 → crossed
    assert t.exceeded is True
    assert t.snapshot.input_tokens == 4_000_000


def test_tracker_kill_on_missing_data() -> None:
    """Adversarial: a stream of events with NO usage block must still trip
    the gate — worst-case fallback prevents silent overrun."""
    t = B.TicketBudgetTracker(ceiling_usd=10.0)
    # Each fallback adds 200_000 * $75/1M = $15 -> first call trips.
    crossed = t.add("claude-opus-4-7", None)
    assert crossed is True
    assert t.snapshot.fallbacks == 1


def test_tracker_feed_event_with_assistant_message() -> None:
    t = B.TicketBudgetTracker(ceiling_usd=100.0)
    event = {
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-6",
            "usage": {"input_tokens": 1_000_000, "output_tokens": 0,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }
    t.feed_event(event)
    assert t.snapshot.cost_usd == pytest.approx(3.0)


def test_tracker_ignores_events_without_usage() -> None:
    t = B.TicketBudgetTracker(ceiling_usd=10.0)
    # tool_result, system, user events shouldn't move the needle
    for event in (
        {"type": "system"},
        {"type": "user", "message": {"content": []}},
        {"type": "assistant", "message": {"content": []}},
    ):
        t.feed_event(event)
    assert t.snapshot.cost_usd == 0.0


# ---------------------------------------------------------------------------
# Ledger — read / write / today / tick / top spenders
# ---------------------------------------------------------------------------


def test_append_and_read_spend(tmp_path: Path) -> None:
    ledger = tmp_path / "spend.jsonl"
    B.append_spend(ledger, B.SpendRecord(
        ts=B.utc_now_iso(), issue=42, cost_usd=1.23, status="merged",
        model="claude-sonnet-4-6", tick=7,
    ))
    rows = B.read_spend(ledger)
    assert len(rows) == 1
    assert rows[0]["issue"] == 42
    assert rows[0]["cost_usd"] == 1.23
    assert rows[0]["tick"] == 7


def test_today_spend_excludes_old(tmp_path: Path) -> None:
    ledger = tmp_path / "spend.jsonl"
    today_iso = datetime.now(UTC).isoformat(timespec="seconds")
    old_iso = (datetime.now(UTC) - timedelta(days=3)).isoformat(timespec="seconds")
    B.append_spend(ledger, B.SpendRecord(ts=today_iso, issue=1, cost_usd=2.0))
    B.append_spend(ledger, B.SpendRecord(ts=today_iso, issue=2, cost_usd=0.5))
    B.append_spend(ledger, B.SpendRecord(ts=old_iso, issue=3, cost_usd=99.0))
    assert B.today_spend(ledger) == pytest.approx(2.5)


def test_tick_spend(tmp_path: Path) -> None:
    ledger = tmp_path / "spend.jsonl"
    B.append_spend(ledger, B.SpendRecord(ts=B.utc_now_iso(), issue=1, cost_usd=1.0, tick=5))
    B.append_spend(ledger, B.SpendRecord(ts=B.utc_now_iso(), issue=2, cost_usd=2.0, tick=5))
    B.append_spend(ledger, B.SpendRecord(ts=B.utc_now_iso(), issue=3, cost_usd=99.0, tick=6))
    assert B.tick_spend(ledger, 5) == pytest.approx(3.0)
    assert B.tick_spend(ledger, 6) == pytest.approx(99.0)


def test_top_expensive_issues_aggregates(tmp_path: Path) -> None:
    ledger = tmp_path / "spend.jsonl"
    # issue 7 spends $1 + $4 = $5 across two attempts
    B.append_spend(ledger, B.SpendRecord(ts=B.utc_now_iso(), issue=7, cost_usd=1.0))
    B.append_spend(ledger, B.SpendRecord(ts=B.utc_now_iso(), issue=7, cost_usd=4.0))
    B.append_spend(ledger, B.SpendRecord(ts=B.utc_now_iso(), issue=8, cost_usd=2.0))
    B.append_spend(ledger, B.SpendRecord(ts=B.utc_now_iso(), issue=9, cost_usd=10.0))
    top = B.top_expensive_issues(ledger, n=2)
    assert top == [(9, 10.0), (7, 5.0)]


def test_read_spend_missing_file(tmp_path: Path) -> None:
    assert B.read_spend(tmp_path / "nope.jsonl") == []


def test_read_spend_skips_malformed(tmp_path: Path) -> None:
    ledger = tmp_path / "spend.jsonl"
    ledger.write_text(
        json.dumps({"ts": "now", "issue": 1, "cost_usd": 0.5}) + "\n"
        + "this is not json\n"
        + "\n"
        + json.dumps({"ts": "now", "issue": 2, "cost_usd": 0.3}) + "\n"
    )
    rows = B.read_spend(ledger)
    assert len(rows) == 2


def test_extract_usage_result_event() -> None:
    event = {"type": "result", "model": "claude-opus-4-7",
             "usage": {"input_tokens": 10, "output_tokens": 5,
                       "cache_creation_input_tokens": 0,
                       "cache_read_input_tokens": 0}}
    model, usage = B.extract_usage(event)
    assert model == "claude-opus-4-7"
    assert usage and usage["input_tokens"] == 10


def test_extract_usage_no_payload_returns_none() -> None:
    assert B.extract_usage({"type": "system"}) == (None, None)
    assert B.extract_usage({"type": "user"}) == (None, None)
