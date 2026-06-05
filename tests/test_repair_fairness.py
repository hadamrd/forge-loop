"""Fair-scheduling tests for blocking-PR repair selection (issue #248).

In-flight repairs used to starve the ready backlog: ``_run_pre_dispatch_repairs``
short-circuited the tick whenever any blocking PR needed repair, and
``blocking_pr_repairs`` re-selected the same ``critic:blocking`` PRs every tick
with no backoff. With ``parallel=2`` and two perpetually-blocking PRs the loop
pinned both worker slots forever and dispatched zero ready tickets.

This suite pins both halves of the fix:

* **Per-PR backoff** (``blocking_pr_repairs`` + ``classify_repair_backoff``):
  a PR re-blocked ``N`` consecutive ticks is excluded until a cooldown elapses.
* **Dispatch-slot reservation / round-robin** (``repair_slot_budget`` +
  ``_run_pre_dispatch_repairs``): the repair phase yields the tick to new
  dispatch within ``K`` ticks so a ready issue always makes progress.

Plus the adversarial trio the issue requires: the starvation regression (must
fail against ``main``), no-deadlock idle, and feature-disabled byte-identity.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from forge_loop import attempts as _attempts
from forge_loop.adapters.clock import FakeClock
from forge_loop.config import (
    AttemptsConfig,
    Briefs,
    Config,
    CriticConfig,
    Labels,
    LumenConfig,
    POConfig,
    RepairConfig,
)
from forge_loop.runner import repairs as _repairs
from forge_loop.runner import tick as _tick_mod
from forge_loop.runner.dispatch import repair_slot_budget

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _make_cfg(tmp_path: Path, repair: RepairConfig, *, parallel: int = 2) -> Config:
    cfg = Config(
        repo=tmp_path,
        github_repo="o/r",
        parallel=parallel,
        tick_interval_s=0,
        max_ticks=1,
        worker_timeout_s=60,
        deploy_task="",
        labels=Labels(),
        briefs=Briefs(),
        critic=CriticConfig(enabled=False, timeout_s=10),
        po=POConfig(enabled=False, timeout_s=10, max_to_expand_per_tick=0),
        attempts=AttemptsConfig(enabled=True, max_history_in_brief=5),
        repair=repair,
        lumen=LumenConfig(),
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _read_events(cfg: Config) -> list[dict[str, Any]]:
    if not cfg.events_file.exists():
        return []
    return [json.loads(ln) for ln in cfg.events_file.read_text().splitlines() if ln.strip()]


def _pr(num: int) -> dict[str, Any]:
    return {
        "number": num,
        "url": f"https://github.com/o/r/pull/{num}",
        "headRefName": f"loop/{num}-blocked",
        "repairReasons": ["critic:blocking"],
    }


def _make_blocking_repairs_fn(prs: list[dict[str, Any]]) -> Any:
    """Build the (prs_requiring_repair_fn, fetch_issue_fn, pr_review_context_fn)
    stubs ``blocking_pr_repairs`` needs, given a list of blocking PRs."""

    def prs_requiring_repair_fn(parallel: int, *, repo: str | None, on_skip: Any) -> list[dict]:
        return list(prs)

    def fetch_issue_fn(issue_num: int, *, repo: str | None) -> dict[str, Any]:
        return {"number": issue_num, "title": f"issue {issue_num}", "labels": []}

    def pr_review_context_fn(number: int, *, repo: str | None) -> str:
        return ""

    return prs_requiring_repair_fn, fetch_issue_fn, pr_review_context_fn


def _call_blocking(
    cfg: Config,
    prs: list[dict[str, Any]],
    *,
    block_timestamps_fn: Any,
    now: datetime | None = None,
) -> list[int]:
    prs_fn, issue_fn, ctx_fn = _make_blocking_repairs_fn(prs)
    selected = _repairs.blocking_pr_repairs(
        cfg,
        prs_requiring_repair_fn=prs_fn,
        fetch_issue_fn=issue_fn,
        pr_review_context_fn=ctx_fn,
        block_timestamps_fn=block_timestamps_fn,
        now=now,
    )
    return [issue["number"] for issue, _, _ in selected]


# --------------------------------------------------------------------------- #
# Unit — slot-reservation math (modeled on test_dispatch_slot_accounting.py)
# --------------------------------------------------------------------------- #


def test_repair_slot_budget_reserves_one_for_dispatch() -> None:
    """parallel=2, ready present, 2 pending repairs → >=1 slot held for dispatch."""
    repairs_to_run, dispatch_reserved = repair_slot_budget(
        2, 2, ready_present=True, reserve=1
    )
    assert dispatch_reserved >= 1
    assert repairs_to_run == 1
    assert repairs_to_run + dispatch_reserved == 2


def test_repair_slot_budget_no_ready_lets_repairs_use_all_slots() -> None:
    """No ready work ⇒ repairs may claim every slot (legacy behaviour)."""
    repairs_to_run, dispatch_reserved = repair_slot_budget(
        2, 2, ready_present=False, reserve=1
    )
    assert repairs_to_run == 2
    assert dispatch_reserved == 0


def test_repair_slot_budget_reserve_zero_is_noop() -> None:
    repairs_to_run, dispatch_reserved = repair_slot_budget(
        3, 5, ready_present=True, reserve=0
    )
    assert repairs_to_run == 3
    assert dispatch_reserved == 0


# --------------------------------------------------------------------------- #
# Unit — classify_repair_backoff (mirrors attempts.classify_skip)
# --------------------------------------------------------------------------- #


def test_classify_repair_backoff_under_threshold_does_not_skip() -> None:
    now = datetime(2026, 6, 5, tzinfo=UTC)
    stamps = [(now - timedelta(minutes=i)).isoformat() for i in (2, 1)]  # 2 blocks
    d = _attempts.classify_repair_backoff(stamps, max_consecutive=3, cooldown_s=3600, now=now)
    assert d.skip is False
    assert d.consecutive_blocks == 2


def test_classify_repair_backoff_at_threshold_within_window_skips() -> None:
    now = datetime(2026, 6, 5, tzinfo=UTC)
    stamps = [(now - timedelta(minutes=i)).isoformat() for i in (3, 2, 1)]  # 3 blocks
    d = _attempts.classify_repair_backoff(stamps, max_consecutive=3, cooldown_s=3600, now=now)
    assert d.skip is True
    assert d.consecutive_blocks == 3
    assert 0 < d.cooldown_remaining_s <= 3600


def test_classify_repair_backoff_disabled_never_skips() -> None:
    now = datetime(2026, 6, 5, tzinfo=UTC)
    stamps = [now.isoformat()] * 10
    d = _attempts.classify_repair_backoff(stamps, max_consecutive=0, cooldown_s=3600, now=now)
    assert d.skip is False


# --------------------------------------------------------------------------- #
# Unit — backoff selector inside blocking_pr_repairs
# --------------------------------------------------------------------------- #


def test_backoff_selector_excludes_pr_at_threshold_keeps_pr_below(tmp_path: Path) -> None:
    """A PR with >=N consecutive recorded blocks is excluded; a PR with <N stays."""
    cfg = _make_cfg(tmp_path, RepairConfig(enabled=True, max_consecutive_blocks=3, cooldown_s=3600))
    now = datetime(2026, 6, 5, tzinfo=UTC)
    blocked = _pr(241)  # 3 consecutive blocks → backoff
    healthy = _pr(242)  # 1 block → still selected

    def block_timestamps_fn(pr_url: str) -> list[str]:
        if pr_url.endswith("/241"):
            return [(now - timedelta(minutes=i)).isoformat() for i in (3, 2, 1)]
        return [(now - timedelta(minutes=1)).isoformat()]

    selected = _call_blocking(cfg, [blocked, healthy], block_timestamps_fn=block_timestamps_fn, now=now)
    assert selected == [242]


def test_backoff_skip_emits_event_with_payload(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path, RepairConfig(enabled=True, max_consecutive_blocks=3, cooldown_s=3600))
    now = datetime(2026, 6, 5, tzinfo=UTC)
    blocked = _pr(241)

    def block_timestamps_fn(pr_url: str) -> list[str]:
        return [(now - timedelta(minutes=i)).isoformat() for i in (3, 2, 1)]

    _call_blocking(cfg, [blocked], block_timestamps_fn=block_timestamps_fn, now=now)
    events = [e for e in _read_events(cfg) if e.get("kind") == "repair_pr_backoff"]
    assert len(events) == 1
    ev = events[0]
    assert ev["pr"] == blocked["url"]
    assert ev["issue"] == 241
    assert ev["consecutive_blocks"] == 3
    assert ev["cooldown_remaining_s"] > 0


def test_backoff_cooldown_expiry_makes_pr_selectable_again(tmp_path: Path) -> None:
    """Drive the fake clock past the cooldown window → the PR is selectable again."""
    cfg = _make_cfg(tmp_path, RepairConfig(enabled=True, max_consecutive_blocks=3, cooldown_s=1800))
    clock = FakeClock(start=1_700_000_000.0)
    base = datetime.fromtimestamp(clock.now(), UTC)
    stamps = [(base - timedelta(minutes=i)).isoformat() for i in (3, 2, 1)]
    blocked = _pr(241)

    def block_timestamps_fn(pr_url: str) -> list[str]:
        return stamps

    # Within the window → excluded.
    now_in = datetime.fromtimestamp(clock.now(), UTC)
    assert _call_blocking(cfg, [blocked], block_timestamps_fn=block_timestamps_fn, now=now_in) == []

    # Advance the fake clock past the cooldown → selectable again.
    clock.sleep(1800 + 1)
    now_after = datetime.fromtimestamp(clock.now(), UTC)
    assert _call_blocking(cfg, [blocked], block_timestamps_fn=block_timestamps_fn, now=now_after) == [241]


def test_backoff_disabled_selects_everything(tmp_path: Path) -> None:
    """Feature off ⇒ even a PR blocked 99 times is still selected (legacy)."""
    cfg = _make_cfg(tmp_path, RepairConfig(enabled=False))
    now = datetime(2026, 6, 5, tzinfo=UTC)

    def block_timestamps_fn(pr_url: str) -> list[str]:
        return [now.isoformat()] * 99

    selected = _call_blocking(cfg, [_pr(241), _pr(242)], block_timestamps_fn=block_timestamps_fn, now=now)
    assert selected == [241, 242]


# --------------------------------------------------------------------------- #
# Unit — durable block ledger
# --------------------------------------------------------------------------- #


def test_consecutive_block_timestamps_counts_trailing_run(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    url = "https://github.com/o/r/pull/241"
    other = "https://github.com/o/r/pull/9"
    _repairs.record_repair_block(ledger, url, 241, blocked=True, ts="t1")
    _repairs.record_repair_block(ledger, other, 9, blocked=True, ts="tX")
    _repairs.record_repair_block(ledger, url, 241, blocked=False, ts="t2")  # resets
    _repairs.record_repair_block(ledger, url, 241, blocked=True, ts="t3")
    _repairs.record_repair_block(ledger, url, 241, blocked=True, ts="t4")
    assert _repairs.consecutive_block_timestamps(ledger, url) == ["t3", "t4"]
    assert _repairs.consecutive_block_timestamps(ledger, other) == ["tX"]


# --------------------------------------------------------------------------- #
# Tick-level — dispatch-slot reservation / round-robin yield
# --------------------------------------------------------------------------- #


@pytest.fixture
def _stub_pre_dispatch(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Neutralise the heavy parts of _run_pre_dispatch_repairs.

    The stuck sweep, the actual repair worker run, and the adoption scan are
    all no-ops; only the repair *selection* + reservation decision under test
    runs for real.
    """
    state: dict[str, Any] = {"repair_ticks": 0}

    monkeypatch.setattr(_tick_mod, "_run_stuck_sweep", lambda cfg, tick: None)
    monkeypatch.setattr(_tick_mod, "_orphaned_clean_pr_adoptions", lambda cfg: [])

    def _fake_run_repair_tick(*args: Any, **kwargs: Any) -> None:
        state["repair_ticks"] += 1

    monkeypatch.setattr(_tick_mod, "_run_repair_tick", _fake_run_repair_tick)
    return state


def _drive_pre_dispatch(cfg: Config, *, ticks: int) -> list[bool]:
    """Run _run_pre_dispatch_repairs over N ticks; return the short-circuit flags."""
    flags: list[bool] = []
    for t in range(1, ticks + 1):
        flags.append(
            _tick_mod._run_pre_dispatch_repairs(
                cfg, t, bus_emit=lambda *a, **k: None, short_sleep=lambda *a, **k: None
            )
        )
    return flags


def test_reserve_yields_to_dispatch_within_k_ticks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stub_pre_dispatch: dict[str, Any]
) -> None:
    """Two perpetually-blocking PRs + ready backlog: the repair phase must NOT
    short-circuit every tick — a ready issue reaches dispatch within K ticks."""
    cfg = _make_cfg(tmp_path, RepairConfig(enabled=True, reserve_dispatch_after_ticks=2))
    # blocking_pr_repairs always returns two repairs (perpetual block).
    monkeypatch.setattr(_tick_mod, "_blocking_pr_repairs", lambda cfg: [("i", _pr(241), ""), ("i", _pr(242), "")])
    monkeypatch.setattr(_tick_mod, "_any_ready_issue", lambda cfg: True)

    flags = _drive_pre_dispatch(cfg, ticks=3)
    # K = reserve_dispatch_after_ticks + 1 = 3. At least one tick within K must
    # yield (return False) so the tick proceeds to _select_dispatch_set.
    assert flags[:3].count(False) >= 1
    assert flags[2] is False, "3rd tick must yield to dispatch"


def test_starvation_regression_ready_dispatched_within_k(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stub_pre_dispatch: dict[str, Any]
) -> None:
    """HEADLINE regression. parallel=2, two PRs that re-block every tick,
    non-empty ready backlog → a ready issue is dispatched within K ticks.

    Fails against main: there, _run_pre_dispatch_repairs short-circuits on
    every tick that has a blocking repair, so it returns True for all K ticks
    and ``any(not f)`` is never satisfied.
    """
    cfg = _make_cfg(tmp_path, RepairConfig(enabled=True, reserve_dispatch_after_ticks=2), parallel=2)
    monkeypatch.setattr(_tick_mod, "_blocking_pr_repairs", lambda cfg: [("i", _pr(241), ""), ("i", _pr(242), "")])
    monkeypatch.setattr(_tick_mod, "_any_ready_issue", lambda cfg: True)

    K = 3
    flags = _drive_pre_dispatch(cfg, ticks=K)
    assert any(not f for f in flags), "ready backlog starved: repairs short-circuited every tick"

    # And a reservation event was emitted so the yield is observable.
    reserved = [e for e in _read_events(cfg) if e.get("kind") == "repair_dispatch_slot_reserved"]
    assert reserved and reserved[0]["dispatch_slots_reserved"] >= 1


def test_no_deadlock_all_backed_off_idles_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stub_pre_dispatch: dict[str, Any]
) -> None:
    """When every repair is in backoff (blocking_pr_repairs returns nothing),
    the repair phase falls through (returns False) without crashing or
    spinning — the tick proceeds to normal candidate selection."""
    cfg = _make_cfg(tmp_path, RepairConfig(enabled=True, reserve_dispatch_after_ticks=2))
    monkeypatch.setattr(_tick_mod, "_blocking_pr_repairs", lambda cfg: [])
    monkeypatch.setattr(_tick_mod, "_any_ready_issue", lambda cfg: True)

    flags = _drive_pre_dispatch(cfg, ticks=3)
    assert flags == [False, False, False]
    assert _stub_pre_dispatch["repair_ticks"] == 0


def test_feature_disabled_repairs_short_circuit_every_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stub_pre_dispatch: dict[str, Any]
) -> None:
    """Knob off ⇒ byte-identical legacy behaviour: repairs short-circuit every
    tick (return True) and the reservation counter file is never written."""
    cfg = _make_cfg(tmp_path, RepairConfig(enabled=False))
    monkeypatch.setattr(_tick_mod, "_blocking_pr_repairs", lambda cfg: [("i", _pr(241), ""), ("i", _pr(242), "")])

    # _any_ready_issue must never even be consulted when the feature is off.
    def _boom(cfg: Config) -> bool:
        raise AssertionError("_any_ready_issue called while repair fairness disabled")

    monkeypatch.setattr(_tick_mod, "_any_ready_issue", _boom)

    flags = _drive_pre_dispatch(cfg, ticks=3)
    assert flags == [True, True, True]
    assert not cfg.repair_scheduler_file.exists()


# --------------------------------------------------------------------------- #
# Unit — ready-issue probe failure is logged, never silent (review EH-001)
# --------------------------------------------------------------------------- #


def test_any_ready_issue_probe_failure_is_logged_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gh failure in ``_any_ready_issue`` must fall back to False *and* leave
    a trace (structured event + master-log warning), not silently flip the
    scheduler's repair/dispatch reservation (review EH-001, #248)."""
    from forge_loop.gh_client import GhError

    cfg = _make_cfg(tmp_path, RepairConfig(enabled=True))

    def _boom(label: str, limit: int, *, repo: str | None = None) -> list[dict[str, Any]]:
        raise GhError("list_for_repo(loop:ready)", 503, "upstream unavailable")

    monkeypatch.setattr(_tick_mod, "top_issues", _boom)

    assert _tick_mod._any_ready_issue(cfg) is False

    events = _read_events(cfg)
    probe_failures = [e for e in events if e.get("kind") == "repair_ready_probe_failed"]
    assert len(probe_failures) == 1
    assert "GhError" in probe_failures[0]["err"]

    master_log = cfg.logs_dir / "master.log"
    assert master_log.exists()
    assert "_any_ready_issue probe failed" in master_log.read_text()
