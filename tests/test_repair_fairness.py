"""Fair repair scheduling tests (issue #248).

In-flight blocking-PR repairs used to starve the ready backlog: the repair path
was a *terminal tick body* that re-selected the same ``critic:blocking`` PRs
every tick with no backoff and no fairness, pinning every worker slot forever.
The 2026-06-05 cleanup sprint dispatched ZERO of six ready tickets for hours.

These tests pin the fix's contract per the testing manifesto:

* T1 (state machine ⇒ one test per edge + adversarial default arm):
  ``classify_repair_backoff`` is tested on the skip / no-skip / disabled /
  cooldown-expiry / missing-timestamp arms.
* T2 (external-dep assumption ⇒ adversarial false case): the durable sidecar
  is tested for the missing-file and corrupt-file read paths.
* T6 (guard fires): the starvation regression drives the scheduler past the
  round-robin guard and asserts it yields within K<=3 ticks (and that with the
  feature OFF it never yields — the pre-#248 starvation behaviour).
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
    RepairFairnessConfig,
)
from forge_loop.runner import repair_backoff as _rb
from forge_loop.runner import repairs as _repairs
from forge_loop.runner import tick as _tick_mod
from forge_loop.runner.repairs import blocking_pr_repairs
from forge_loop.worker import WorkerOutcome

# --------------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------------- #

_BASE = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)


def _clock_now(clock: FakeClock) -> datetime:
    """Map a FakeClock's virtual seconds onto a UTC datetime for classify_*.

    Drives cooldown tests off the project's fake-clock seam
    (``adapters/clock.py``) without sleeping real wall-clock time.
    """
    return _BASE + timedelta(seconds=clock.now())


def _iso(clock: FakeClock) -> str:
    return _clock_now(clock).isoformat(timespec="seconds")


def _make_cfg(tmp_path: Path, *, fairness: RepairFairnessConfig | None = None) -> Config:
    return Config(
        repo=tmp_path,
        github_repo="o/r",
        parallel=2,
        tick_interval_s=0,
        max_ticks=1,
        worker_timeout_s=60,
        labels=Labels(),
        briefs=Briefs(),
        critic=CriticConfig(enabled=False, timeout_s=10),
        po=POConfig(enabled=False, timeout_s=10, max_to_expand_per_tick=0),
        attempts=AttemptsConfig(enabled=True, max_history_in_brief=5),
        repair_fairness=fairness or RepairFairnessConfig(enabled=True),
        lumen=LumenConfig(),
    )


def _read_events(cfg: Config) -> list[dict[str, Any]]:
    if not cfg.events_file.exists():
        return []
    return [json.loads(ln) for ln in cfg.events_file.read_text().splitlines() if ln.strip()]


def _pr(num: int, url: str) -> dict[str, Any]:
    return {
        "number": num,
        "url": url,
        "headRefName": f"loop/{num}-some-fix",
        "labels": [{"name": "critic:blocking"}],
        "repairReasons": ["critic_blocking"],
    }


def _issue(num: int) -> dict[str, Any]:
    return {"number": num, "title": f"issue {num}", "state": "OPEN", "labels": []}


# --------------------------------------------------------------------------- #
# Unit: classify_repair_backoff (attempts.py) — T1 edges + default arm
# --------------------------------------------------------------------------- #


def test_backoff_skips_pr_at_or_above_threshold_within_cooldown() -> None:
    clock = FakeClock(start=0.0)
    last = _iso(clock)
    clock.advance(10)  # 10s after the last block, well within a 3600s cooldown
    decision = _attempts.classify_repair_backoff(
        3, last, max_consecutive_blocks=3, cooldown_s=3600, now=_clock_now(clock)
    )
    assert decision.skip is True
    assert decision.consecutive_blocks == 3
    assert 3580 <= decision.cooldown_remaining_s <= 3600


def test_backoff_selects_pr_below_threshold() -> None:
    clock = FakeClock(start=0.0)
    decision = _attempts.classify_repair_backoff(
        2, _iso(clock), max_consecutive_blocks=3, cooldown_s=3600, now=_clock_now(clock)
    )
    assert decision.skip is False
    assert decision.consecutive_blocks == 2


def test_backoff_disabled_when_threshold_zero() -> None:
    """max_consecutive_blocks <= 0 disables backoff (feature-off arm)."""
    clock = FakeClock(start=0.0)
    decision = _attempts.classify_repair_backoff(
        99, _iso(clock), max_consecutive_blocks=0, cooldown_s=3600, now=_clock_now(clock)
    )
    assert decision.skip is False


def test_backoff_cooldown_expiry_reselects_pr() -> None:
    """Cooldown expiry: a backed-off PR becomes selectable once the window
    elapses (driven via the fake clock seam)."""
    clock = FakeClock(start=0.0)
    last = _iso(clock)
    # still inside the window
    clock.sleep(1800)
    inside = _attempts.classify_repair_backoff(
        3, last, max_consecutive_blocks=3, cooldown_s=3600, now=_clock_now(clock)
    )
    assert inside.skip is True
    # advance past the window
    clock.sleep(1801)  # total 3601s > 3600s
    expired = _attempts.classify_repair_backoff(
        3, last, max_consecutive_blocks=3, cooldown_s=3600, now=_clock_now(clock)
    )
    assert expired.skip is False


def test_backoff_missing_timestamp_does_not_skip() -> None:
    """Adversarial: a PR at threshold but with no recorded last-block ts is not
    skipped (we cannot prove it is inside any cooldown)."""
    decision = _attempts.classify_repair_backoff(
        5, None, max_consecutive_blocks=3, cooldown_s=3600, now=_BASE
    )
    assert decision.skip is False


# --------------------------------------------------------------------------- #
# Unit: repair_backoff sidecar state — T2 false cases + roundtrip
# --------------------------------------------------------------------------- #


def test_load_state_missing_file_is_empty(tmp_path: Path) -> None:
    state = _rb.load_state(tmp_path / "nope.json")
    assert state.streak == 0
    assert state.prs == {}
    assert state.block_count("x") == 0
    assert state.last_block_ts("x") is None


def test_load_state_corrupt_file_is_empty(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{ this is not json")
    state = _rb.load_state(p)
    assert state.streak == 0 and state.prs == {}


def test_record_block_and_clear_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "rb.json"
    state = _rb.RepairBackoffState()
    _rb.record_block(state, "u1", now_iso="2026-06-05T12:00:00+00:00")
    _rb.record_block(state, "u1", now_iso="2026-06-05T12:01:00+00:00")
    state.streak = 2
    _rb.save_state(p, state)

    reloaded = _rb.load_state(p)
    assert reloaded.block_count("u1") == 2
    assert reloaded.last_block_ts("u1") == "2026-06-05T12:01:00+00:00"
    assert reloaded.streak == 2

    _rb.clear_pr(reloaded, "u1")
    assert reloaded.block_count("u1") == 0


def test_prune_stale_drops_aged_entries_keeps_recent() -> None:
    """A PR that merged via a non-repair path (never clear_pr'd) ages out.

    Entry older than ``cooldown_s * retention_multiplier`` is dropped; an entry
    still inside that horizon is kept (it could still gate selection).
    """
    now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
    state = _rb.RepairBackoffState()
    # last block 4h ago, cooldown 1h, retention 3 → horizon 3h → stale.
    _rb.record_block(state, "stale", now_iso=(now - timedelta(hours=4)).isoformat())
    # last block 30m ago → inside horizon → kept.
    _rb.record_block(state, "fresh", now_iso=(now - timedelta(minutes=30)).isoformat())
    removed = _rb.prune_stale(state, cooldown_s=3600, now=now, retention_multiplier=3)
    assert removed == 1
    assert "stale" not in state.prs
    assert "fresh" in state.prs


def test_prune_stale_noop_when_cooldown_disabled() -> None:
    """cooldown_s<=0 (feature off) ⇒ no pruning, state untouched."""
    now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
    state = _rb.RepairBackoffState()
    _rb.record_block(state, "old", now_iso=(now - timedelta(days=30)).isoformat())
    assert _rb.prune_stale(state, cooldown_s=0, now=now) == 0
    assert "old" in state.prs


def test_prune_stale_ignores_unparseable_timestamp() -> None:
    """An entry with a missing/garbage timestamp can't be aged → left in place."""
    now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
    state = _rb.RepairBackoffState()
    state.prs["bad"] = {"blocks": 2, "last_block": "not-a-timestamp"}
    state.prs["none"] = {"blocks": 1, "last_block": None}
    assert _rb.prune_stale(state, cooldown_s=3600, now=now) == 0
    assert "bad" in state.prs
    assert "none" in state.prs


# --------------------------------------------------------------------------- #
# Unit: blocking_pr_repairs backoff filter + event emission
# --------------------------------------------------------------------------- #


def _stub_selectors(monkeypatch: pytest.MonkeyPatch, prs: list[dict[str, Any]]) -> None:
    def _prs(limit: int, repo: str | None = None, *, on_skip: Any = None) -> list[dict[str, Any]]:
        return list(prs)

    monkeypatch.setattr(_repairs, "prs_requiring_repair", _prs)
    monkeypatch.setattr(_repairs, "fetch_issue", lambda n, repo=None: _issue(int(n)))
    monkeypatch.setattr(_repairs, "pr_review_context", lambda n, repo=None: "")


def test_blocking_repairs_excludes_backed_off_pr_and_emits_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    _stub_selectors(monkeypatch, [_pr(241, "https://gh/pr/1")])

    state = _rb.RepairBackoffState()
    _rb.record_block(state, "https://gh/pr/1", now_iso=_BASE.isoformat())
    state.prs["https://gh/pr/1"]["blocks"] = 3  # at threshold

    out = blocking_pr_repairs(
        cfg,
        prs_requiring_repair_fn=_repairs.prs_requiring_repair,
        fetch_issue_fn=_repairs.fetch_issue,
        pr_review_context_fn=_repairs.pr_review_context,
        backoff_state=state,
        max_consecutive_blocks=3,
        cooldown_s=3600,
        now=_BASE + timedelta(seconds=5),
    )
    assert out == []  # the only candidate was backed off
    events = _read_events(cfg)
    backoff = [e for e in events if e["kind"] == "repair_pr_backoff"]
    assert len(backoff) == 1
    assert backoff[0]["pr"] == "https://gh/pr/1"
    assert backoff[0]["issue"] == 241
    assert backoff[0]["consecutive_blocks"] == 3
    assert backoff[0]["cooldown_remaining_s"] > 0


def test_blocking_repairs_selects_pr_below_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    _stub_selectors(monkeypatch, [_pr(241, "https://gh/pr/1")])

    state = _rb.RepairBackoffState()
    state.prs["https://gh/pr/1"] = {"blocks": 1, "last_block": _BASE.isoformat()}

    out = blocking_pr_repairs(
        cfg,
        prs_requiring_repair_fn=_repairs.prs_requiring_repair,
        fetch_issue_fn=_repairs.fetch_issue,
        pr_review_context_fn=_repairs.pr_review_context,
        backoff_state=state,
        max_consecutive_blocks=3,
        cooldown_s=3600,
        now=_BASE + timedelta(seconds=5),
    )
    assert len(out) == 1
    assert out[0][0]["number"] == 241


def test_blocking_repairs_byte_identical_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Feature-disabled: with max_consecutive_blocks=0 a heavily-blocked PR is
    still selected — selection is unchanged from legacy."""
    cfg = _make_cfg(tmp_path, fairness=RepairFairnessConfig(enabled=False))
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    _stub_selectors(monkeypatch, [_pr(241, "https://gh/pr/1")])

    state = _rb.RepairBackoffState()
    state.prs["https://gh/pr/1"] = {"blocks": 99, "last_block": _BASE.isoformat()}

    out = blocking_pr_repairs(
        cfg,
        prs_requiring_repair_fn=_repairs.prs_requiring_repair,
        fetch_issue_fn=_repairs.fetch_issue,
        pr_review_context_fn=_repairs.pr_review_context,
        backoff_state=state,
        max_consecutive_blocks=0,  # disabled
        cooldown_s=3600,
    )
    assert len(out) == 1
    assert not any(e["kind"] == "repair_pr_backoff" for e in _read_events(cfg))


# --------------------------------------------------------------------------- #
# Integration: _run_blocking_pr_repairs_phase — starvation regression + guards
# --------------------------------------------------------------------------- #


def _drive_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cfg: Config,
    ready_exist: bool,
    ticks: int,
) -> list[bool]:
    """Drive ``_run_blocking_pr_repairs_phase`` over ``ticks`` ticks with two
    perpetually re-blocking PRs, returning the per-tick return value (True =
    ran a repair tick / terminal; False = yielded to dispatch)."""
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    prs = [_pr(241, "https://gh/pr/241"), _pr(242, "https://gh/pr/242")]
    _stub_selectors(monkeypatch, prs)
    # blocking_pr_repairs in tick.py imports these names from tick module scope.
    monkeypatch.setattr(_tick_mod, "prs_requiring_repair", _repairs.prs_requiring_repair)
    monkeypatch.setattr(_tick_mod, "fetch_issue", _repairs.fetch_issue)
    monkeypatch.setattr(_tick_mod, "pr_review_context", _repairs.pr_review_context)
    monkeypatch.setattr(_tick_mod, "_ready_issues_exist", lambda _cfg: ready_exist)

    def _fake_repair_tick(
        cfg_: Config,
        tick_: int,
        repairs: list[tuple[dict[str, Any], dict[str, Any], str]],
        **_kw: Any,
    ) -> list[WorkerOutcome]:
        # Every repaired PR re-blocks (the worst case from the incident).
        return [
            WorkerOutcome(
                issue=issue["number"],
                title=issue["title"],
                pr_url=pr["url"],
                status="open",
                duration_s=0.0,
                stdout_tail="",
                error="critic blocked merge: still failing",
            )
            for issue, pr, _ctx in repairs
        ]

    monkeypatch.setattr(_tick_mod, "_run_repair_tick", _fake_repair_tick)

    returns: list[bool] = []
    for t in range(1, ticks + 1):
        returns.append(
            _tick_mod._run_blocking_pr_repairs_phase(
                cfg, t, bus_emit=lambda *_a, **_k: None, short_sleep=lambda *_a, **_k: None
            )
        )
    return returns


def test_starvation_regression_yields_within_k_ticks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HEADLINE: parallel=2, two perpetually-blocking PRs, ready backlog present.

    The fair scheduler MUST yield the tick to new dispatch within K<=3 ticks.
    Against pre-#248 ``main`` (feature off) it never yields — see the companion
    assertion below.
    """
    cfg = _make_cfg(
        tmp_path,
        fairness=RepairFairnessConfig(
            enabled=True, max_consecutive_blocks=3, max_repair_streak=2
        ),
    )
    returns = _drive_phase(tmp_path, monkeypatch, cfg=cfg, ready_exist=True, ticks=3)
    # A False return = the phase yielded, so _run_pre_dispatch_repairs falls
    # through to dispatch and a ready issue is picked up.
    assert returns[-1] is False, returns
    assert any(r is False for r in returns[:3])
    # the yield decision is observable
    events = _read_events(cfg)
    assert any(e["kind"] == "repair_phase_yielded" for e in events)


def test_pre_dispatch_repairs_falls_through_on_yield_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lock the terminal-vs-fallthrough coupling at the ``_tick`` boundary.

    ``_tick`` runs ``if _run_pre_dispatch_repairs(...): return`` — a True return
    is terminal (dispatch is skipped); a False return falls through to
    ``_select_dispatch_set`` / dispatch. With two perpetually re-blocking PRs and
    ready work waiting, the round-robin yield MUST make ``_run_pre_dispatch_repairs``
    return False within K<=3 ticks so a ready issue actually gets dispatched.
    """
    cfg = _make_cfg(
        tmp_path,
        fairness=RepairFairnessConfig(
            enabled=True, max_consecutive_blocks=3, max_repair_streak=2
        ),
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    prs = [_pr(241, "https://gh/pr/241"), _pr(242, "https://gh/pr/242")]
    _stub_selectors(monkeypatch, prs)
    monkeypatch.setattr(_tick_mod, "prs_requiring_repair", _repairs.prs_requiring_repair)
    monkeypatch.setattr(_tick_mod, "fetch_issue", _repairs.fetch_issue)
    monkeypatch.setattr(_tick_mod, "pr_review_context", _repairs.pr_review_context)
    monkeypatch.setattr(_tick_mod, "_ready_issues_exist", lambda _cfg: True)
    # Isolate the blocking-repair phase: stuck sweep + adoption do nothing here.
    monkeypatch.setattr(_tick_mod, "_run_stuck_sweep", lambda *_a, **_k: None)
    monkeypatch.setattr(_tick_mod, "_orphaned_clean_pr_adoptions", lambda _cfg: [])

    def _fake_repair_tick(
        cfg_: Config,
        tick_: int,
        repairs: list[tuple[dict[str, Any], dict[str, Any], str]],
        **_kw: Any,
    ) -> list[WorkerOutcome]:
        return [
            WorkerOutcome(
                issue=issue["number"],
                title=issue["title"],
                pr_url=pr["url"],
                status="open",
                duration_s=0.0,
                stdout_tail="",
                error="critic blocked merge: still failing",
            )
            for issue, pr, _ctx in repairs
        ]

    monkeypatch.setattr(_tick_mod, "_run_repair_tick", _fake_repair_tick)

    returns = [
        _tick_mod._run_pre_dispatch_repairs(
            cfg, t, bus_emit=lambda *_a, **_k: None, short_sleep=lambda *_a, **_k: None
        )
        for t in range(1, 4)
    ]
    # Within K<=3 ticks the terminal body yields (False) so _tick reaches dispatch.
    assert returns[-1] is False, returns
    assert any(r is False for r in returns)


def test_disabled_feature_never_yields_starves_backlog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-#248 behaviour: with the feature OFF the repair phase is terminal
    every tick (always True) — the backlog is starved. This is the regression
    the headline test would hit against today's main."""
    cfg = _make_cfg(tmp_path, fairness=RepairFairnessConfig(enabled=False))
    returns = _drive_phase(tmp_path, monkeypatch, cfg=cfg, ready_exist=True, ticks=3)
    assert returns == [True, True, True]


def test_no_deadlock_when_all_repairs_backed_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All blocking PRs in backoff ⇒ the phase yields cleanly (no repair tick,
    no crash) so the tick can idle / dispatch. No busy-loop."""
    cfg = _make_cfg(
        tmp_path,
        fairness=RepairFairnessConfig(
            enabled=True, max_consecutive_blocks=1, cooldown_s=3600, max_repair_streak=2
        ),
    )
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    # Pre-seed both PRs over threshold within cooldown.
    state = _rb.RepairBackoffState()
    now_iso = (datetime.now(UTC)).isoformat(timespec="seconds")
    _rb.record_block(state, "https://gh/pr/241", now_iso=now_iso)
    _rb.record_block(state, "https://gh/pr/242", now_iso=now_iso)
    _rb.save_state(_tick_mod._repair_backoff_file(cfg), state)

    prs = [_pr(241, "https://gh/pr/241"), _pr(242, "https://gh/pr/242")]
    _stub_selectors(monkeypatch, prs)
    monkeypatch.setattr(_tick_mod, "prs_requiring_repair", _repairs.prs_requiring_repair)
    monkeypatch.setattr(_tick_mod, "fetch_issue", _repairs.fetch_issue)
    monkeypatch.setattr(_tick_mod, "pr_review_context", _repairs.pr_review_context)
    monkeypatch.setattr(_tick_mod, "_ready_issues_exist", lambda _cfg: True)

    def _boom(*_a: Any, **_k: Any) -> list[WorkerOutcome]:
        raise AssertionError("no repair tick should run when all PRs are backed off")

    monkeypatch.setattr(_tick_mod, "_run_repair_tick", _boom)

    ran = _tick_mod._run_blocking_pr_repairs_phase(
        cfg, 1, bus_emit=lambda *_a, **_k: None, short_sleep=lambda *_a, **_k: None
    )
    assert ran is False
    events = _read_events(cfg)
    assert sum(1 for e in events if e["kind"] == "repair_pr_backoff") == 2
