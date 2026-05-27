"""Per-ticket + per-tick token-cost tracking and budget gates.

Loop has wall-clock timeouts but zero token-cost awareness. A runaway agent
on a small ticket can rack up $30 before timeout fires. This module gives the
runner and the worker a single source of truth for:

* Pricing per model (input / output / cache_creation / cache_read per 1M tokens).
  New / unknown models fall back to the most expensive Opus rate — the
  philosophy is "fail loud and overestimate" rather than silently undercount.
* Cost calculation from a Claude Agent SDK usage dict.
* Per-ticket budget — env LOOP_TICKET_BUDGET_USD (default $5), overridable
  per-issue with a ``budget:<n>`` label (e.g. ``budget:0.5``).
* Per-tick budget — env LOOP_TICK_BUDGET_USD (default $20). The runner stops
  dispatching new workers once the in-flight + completed spend would exceed
  the ceiling; workers already running continue to THEIR own ticket ceiling.
* A small JSONL ledger so the ``forge-loop budget`` CLI can show today's
  spend, the current tick's spend, and the top 5 most expensive issues.

Out of scope (issue #6): real billing API, per-org budgets.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Pricing in USD per 1M tokens. Source: public Anthropic pricing page
# at issue-author time (Sonnet 4.6, Haiku 4.5, Opus 4.7). When the SDK reports
# a new model id we don't know about, we use _OPUS_RATE — overestimation is
# always safer than silent undercount for a budget gate.
#
# Each entry: (input, output, cache_creation_5m, cache_read).
# cache_creation_5m is the typical "write" rate; for the 1h variant the SDK
# reports a separate field — we use the same column as a conservative proxy.
_PRICING: dict[str, tuple[float, float, float, float]] = {
    # Opus 4.x — premium tier.
    "claude-opus-4-7":     (15.0, 75.0, 18.75, 1.50),
    "claude-opus-4-6":     (15.0, 75.0, 18.75, 1.50),
    "claude-opus-4-5":     (15.0, 75.0, 18.75, 1.50),
    "claude-opus-4":       (15.0, 75.0, 18.75, 1.50),
    # Sonnet 4.x — mid tier.
    "claude-sonnet-4-6":   ( 3.0, 15.0,  3.75, 0.30),
    "claude-sonnet-4-5":   ( 3.0, 15.0,  3.75, 0.30),
    "claude-sonnet-4":     ( 3.0, 15.0,  3.75, 0.30),
    # Haiku 4.x — cheap tier.
    "claude-haiku-4-5":    ( 1.0,  5.0,  1.25, 0.10),
    "claude-haiku-4":      ( 1.0,  5.0,  1.25, 0.10),
}

# Worst-case rate (Opus). Used for unknown models AND for the
# "missing usage data" fallback so the gate never silently undercounts.
_OPUS_RATE = (15.0, 75.0, 18.75, 1.50)

# Conservative ceiling for the missing-usage fallback: assume one full
# 200k-token context window at Opus output rate (= $15). Big enough that a
# single un-attributed event will trip a default budget; small enough that
# a single transient parse hiccup does not nuke a healthy worker on the first
# event. Tunable via env for paranoid operators.
_FALLBACK_TOKENS = int(os.environ.get("LOOP_BUDGET_FALLBACK_TOKENS", "200000"))


def _rate_for(model: str | None) -> tuple[float, float, float, float]:
    """Look up pricing for a model id. Unknown → most expensive (Opus)."""
    if not model:
        return _OPUS_RATE
    # Strip any vendor prefix the SDK might tack on (e.g. "anthropic/").
    key = model.split("/")[-1].strip().lower()
    # Strip trailing dated suffix like "-20251001".
    key = re.sub(r"-\d{8}$", "", key)
    # Strip context-window suffix like "[1m]".
    key = re.sub(r"\[[^\]]+\]$", "", key)
    if key in _PRICING:
        return _PRICING[key]
    # Family fallback so a new minor revision doesn't crash the gate.
    for prefix, rate in (
        ("claude-opus", _PRICING["claude-opus-4-7"]),
        ("claude-sonnet", _PRICING["claude-sonnet-4-6"]),
        ("claude-haiku", _PRICING["claude-haiku-4-5"]),
    ):
        if key.startswith(prefix):
            return rate
    return _OPUS_RATE


def cost_for_usage(model: str | None, usage: dict[str, Any] | None) -> float:
    """USD cost for a single usage dict from the Claude Agent SDK.

    Adversarial-safe: if usage is missing / empty / non-numeric we return
    the worst-case fallback cost so a missing-data bug cannot silently
    undercount the budget. Returns 0.0 ONLY when every counted field is
    explicitly present and zero.
    """
    if not isinstance(usage, dict):
        return _fallback_cost()
    keys = (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    )
    vals: list[int] = []
    for k in keys:
        v = usage.get(k, 0)
        try:
            vals.append(int(v or 0))
        except (TypeError, ValueError):
            return _fallback_cost()
    # If literally none of the four fields were present at all, fall back.
    if not any(k in usage for k in keys):
        return _fallback_cost()
    rate = _rate_for(model)
    inp_t, out_t, ccr_t, crd_t = vals
    return (
        inp_t * rate[0]
        + out_t * rate[1]
        + ccr_t * rate[2]
        + crd_t * rate[3]
    ) / 1_000_000.0


def _fallback_cost() -> float:
    """Worst-case cost used when usage data is missing or malformed."""
    return _FALLBACK_TOKENS * _OPUS_RATE[1] / 1_000_000.0


_LABEL_RE = re.compile(r"^budget:([0-9]+(?:\.[0-9]+)?)$")


def parse_budget_label(labels: list[Any] | None) -> float | None:
    """Pick out the per-issue budget override from a list of GH labels.

    Accepts both the gh JSON shape (``{"name": "budget:0.5"}``) and bare
    strings. Returns the LOWEST value found if multiple are set (strictest
    wins — operators add a tighter budget label as a brake, not a release).
    """
    if not labels:
        return None
    out: list[float] = []
    for lab in labels:
        name = lab.get("name", "") if isinstance(lab, dict) else str(lab)
        m = _LABEL_RE.match(name.strip())
        if m:
            try:
                out.append(float(m.group(1)))
            except ValueError:
                continue
    return min(out) if out else None


def ticket_budget_for(labels: list[Any] | None, *, default: float | None = None) -> float:
    """Resolve the per-ticket budget for a given issue.

    Precedence: ``budget:<n>`` label > LOOP_TICKET_BUDGET_USD env > default
    arg > built-in 5.0.
    """
    label_val = parse_budget_label(labels)
    if label_val is not None:
        return label_val
    env_val = os.environ.get("LOOP_TICKET_BUDGET_USD")
    if env_val:
        try:
            return float(env_val)
        except ValueError:
            pass
    if default is not None:
        return default
    return 5.0


def tick_budget(default: float | None = None) -> float:
    """Resolve the per-tick budget ceiling."""
    env_val = os.environ.get("LOOP_TICK_BUDGET_USD")
    if env_val:
        try:
            return float(env_val)
        except ValueError:
            pass
    if default is not None:
        return default
    return 20.0


# ---------------------------------------------------------------------------
# Per-worker tracker — accumulates usage events from a stream and decides
# when the configured ticket ceiling has been crossed.
# ---------------------------------------------------------------------------


@dataclass
class CostSnapshot:
    """A single point-in-time totals snapshot for a worker."""

    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    events: int = 0
    fallbacks: int = 0  # how many events used the worst-case fallback


class TicketBudgetTracker:
    """Thread-safe per-worker accumulator with a ceiling check."""

    def __init__(self, ceiling_usd: float):
        self.ceiling_usd = float(ceiling_usd)
        self._snap = CostSnapshot()
        self._lock = threading.Lock()
        self._tripped_at: float | None = None

    @property
    def snapshot(self) -> CostSnapshot:
        with self._lock:
            return CostSnapshot(
                cost_usd=self._snap.cost_usd,
                input_tokens=self._snap.input_tokens,
                output_tokens=self._snap.output_tokens,
                cache_creation_input_tokens=self._snap.cache_creation_input_tokens,
                cache_read_input_tokens=self._snap.cache_read_input_tokens,
                events=self._snap.events,
                fallbacks=self._snap.fallbacks,
            )

    @property
    def exceeded(self) -> bool:
        return self.snapshot.cost_usd >= self.ceiling_usd

    def add(self, model: str | None, usage: dict[str, Any] | None) -> bool:
        """Record one usage event. Returns True iff this push crossed the line."""
        was_over = self.exceeded
        c = cost_for_usage(model, usage)
        with self._lock:
            self._snap.cost_usd += c
            self._snap.events += 1
            if not isinstance(usage, dict) or not any(
                k in usage for k in (
                    "input_tokens", "output_tokens",
                    "cache_creation_input_tokens", "cache_read_input_tokens",
                )
            ):
                self._snap.fallbacks += 1
            else:
                self._snap.input_tokens += int(usage.get("input_tokens", 0) or 0)
                self._snap.output_tokens += int(usage.get("output_tokens", 0) or 0)
                self._snap.cache_creation_input_tokens += int(
                    usage.get("cache_creation_input_tokens", 0) or 0
                )
                self._snap.cache_read_input_tokens += int(
                    usage.get("cache_read_input_tokens", 0) or 0
                )
            crossed = (not was_over) and self._snap.cost_usd >= self.ceiling_usd
            if crossed and self._tripped_at is None:
                self._tripped_at = time.time()
        return crossed

    def feed_event(self, event: dict[str, Any]) -> bool:
        """Convenience: extract usage from a stream-json event and record it.

        Returns True iff the ceiling was crossed by this event.
        """
        model, usage = extract_usage(event)
        if usage is None and model is None:
            return False
        return self.add(model, usage)


def extract_usage(event: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    """Pull ``(model, usage)`` from one Claude Agent SDK stream-json event.

    Returns ``(None, None)`` if the event carries no usage at all (e.g. a
    plain ``user`` tool-result event or a system event). Callers should
    ignore that case — it is not a missing-data condition, it is just an
    event that genuinely has no usage attached.
    """
    if not isinstance(event, dict):
        return (None, None)

    # Final `result` event — has `usage` and often `total_cost_usd`.
    if event.get("type") == "result":
        usage = event.get("usage")
        if isinstance(usage, dict):
            return (event.get("model"), usage)
        return (None, None)

    # Per-turn `assistant` event — usage lives under message.usage.
    if event.get("type") == "assistant":
        msg = event.get("message") or {}
        if isinstance(msg, dict) and "usage" in msg:
            return (msg.get("model") or event.get("model"), msg.get("usage"))

    return (None, None)


# ---------------------------------------------------------------------------
# Ledger — append-only JSONL of every worker outcome's spend.
# Used by `forge-loop budget` and by the runner's per-tick gate to look up
# in-flight + completed spend without coupling to runtime state.
# ---------------------------------------------------------------------------


@dataclass
class SpendRecord:
    ts: str
    issue: int
    cost_usd: float
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    status: str = ""
    model: str = ""
    tick: int | None = None
    fallbacks: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "ts": self.ts,
            "issue": self.issue,
            "cost_usd": round(self.cost_usd, 6),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "status": self.status,
            "model": self.model,
            "fallbacks": self.fallbacks,
        }
        if self.tick is not None:
            d["tick"] = self.tick
        if self.extra:
            d["extra"] = self.extra
        return d


def append_spend(ledger_path: Path, rec: SpendRecord) -> None:
    """Append one spend record to the ledger. Best-effort, never raises."""
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger_path, "a") as f:
            f.write(json.dumps(rec.to_dict()) + "\n")
    except OSError:
        pass


def read_spend(ledger_path: Path) -> list[dict[str, Any]]:
    """Read all spend records. Returns [] on missing file / parse errors."""
    if not ledger_path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in ledger_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return out


def today_spend(ledger_path: Path, *, now: datetime | None = None) -> float:
    """Sum of cost_usd for records dated today (UTC)."""
    today = (now or datetime.now(UTC)).date()
    total = 0.0
    for rec in read_spend(ledger_path):
        try:
            d = datetime.fromisoformat(str(rec.get("ts", "")).replace("Z", "+00:00")).date()
        except (ValueError, TypeError):
            continue
        if d == today:
            total += float(rec.get("cost_usd", 0.0) or 0.0)
    return total


def tick_spend(ledger_path: Path, tick: int) -> float:
    """Sum of cost_usd for one tick. Used by the runner gate."""
    total = 0.0
    for rec in read_spend(ledger_path):
        if rec.get("tick") == tick:
            total += float(rec.get("cost_usd", 0.0) or 0.0)
    return total


def top_expensive_issues(
    ledger_path: Path, n: int = 5, *, since: datetime | None = None,
) -> list[tuple[int, float]]:
    """Return [(issue_number, total_cost_usd), ...] sorted desc, top n.

    ``since``: optional datetime cutoff (inclusive). If omitted, all-time.
    """
    by_issue: dict[int, float] = {}
    for rec in read_spend(ledger_path):
        if since is not None:
            try:
                ts = datetime.fromisoformat(str(rec.get("ts", "")).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if ts < since:
                continue
        try:
            issue = int(rec.get("issue", 0))
        except (TypeError, ValueError):
            continue
        if issue <= 0:
            continue
        by_issue[issue] = by_issue.get(issue, 0.0) + float(rec.get("cost_usd", 0.0) or 0.0)
    return sorted(by_issue.items(), key=lambda kv: kv[1], reverse=True)[:n]


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
