"""Anti-starvation slot-reservation tests for issue #262.

The tick is structured so repair work is a *terminal* tick body: when any
repair path fires, ``_tick`` used to return early and the new-work dispatch
block lower in the same tick never ran. A single perpetually-blocked repair
PR therefore starved the whole ``loop:ready`` backlog to zero forward
progress.

The #262 fix is ONE fairness mechanism: the pure helper
``reserved_new_work_slots`` decides how many of the ``parallel`` worker slots
to steal back from repairs for NEW dispatch, and ``_tick`` no longer returns
early when a slot is reserved.

These tests pin:

* the pure helper's slot-allocation matrix (mirrors the style of
  ``tests/test_dispatch_slot_accounting.py``);
* the integration behaviour — driving the real ``_tick`` with a stub that
  returns a perpetually-blocked repair every tick AND a ready issue;
* the adversarial cases the testing manifesto requires (T1 default-branch,
  T6 no-starvation-over-a-window guard): the bug is "0 dispatched forever",
  so the regression test runs N ticks and asserts at least one dispatch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from forge_loop.config import (
    AttemptsConfig,
    Briefs,
    Config,
    CriticConfig,
    Labels,
    LumenConfig,
    POConfig,
)
from forge_loop.runner import tick as _tick_mod
from forge_loop.runner.dispatch import RESERVED_NEW_WORK_SLOTS, reserved_new_work_slots

# --------------------------------------------------------------------------- #
# Unit — pure slot-allocation helper (the ONE #262 mechanism).
# --------------------------------------------------------------------------- #


def test_repairs_and_ready_reserve_exactly_one_at_parallel_two() -> None:
    """repairs>0, ready>0, parallel=2 → reserve exactly 1 slot for new work."""
    assert reserved_new_work_slots(2, repairs_pending=1, ready_count=3) == 1


def test_no_ready_reserves_zero_repairs_take_all_slots() -> None:
    """repairs>0, ready=0 → reserve 0 (repairs keep every slot)."""
    assert reserved_new_work_slots(2, repairs_pending=2, ready_count=0) == 0
    assert reserved_new_work_slots(8, repairs_pending=5, ready_count=0) == 0


def test_no_repairs_reserves_zero_new_work_already_has_all_slots() -> None:
    """repairs=0, ready>0 → reserve 0 *from repairs* (new work gets all slots)."""
    assert reserved_new_work_slots(2, repairs_pending=0, ready_count=4) == 0
    assert reserved_new_work_slots(8, repairs_pending=0, ready_count=4) == 0


def test_parallel_one_tie_break_repairs_win() -> None:
    """parallel=1 documented tie-break: repairs win, reservation is 0.

    With a single slot there is nothing to spare for new work, so the
    anti-starvation pre-emption is disabled — an operator who wants it must
    raise ``parallel``.
    """
    assert reserved_new_work_slots(1, repairs_pending=1, ready_count=5) == 0


@pytest.mark.parametrize("parallel", [1, 2, 5, 8])
def test_reserved_count_always_within_zero_to_parallel_minus_one(parallel: int) -> None:
    """The reserve is always in ``[0, parallel-1]`` — repairs never lose every
    slot, and the count is never negative."""
    reserved = reserved_new_work_slots(parallel, repairs_pending=3, ready_count=3)
    assert 0 <= reserved <= max(0, parallel - 1)
    # ☠ CONTRACT CHANGED DELIBERATELY. `reserve` is a FLOOR, not a ceiling: new work takes
    # every slot the repairs in flight are not using. The old assertion pinned
    # min(RESERVED_NEW_WORK_SLOTS, p-1), which capped new dispatch at ONE issue whenever any
    # repair ran — so raising `parallel` bought nothing and the extra workers idled while the
    # backlog waited. Repairs keep exactly `repairs_pending`; the remainder goes to new work.
    free = parallel - 3
    assert reserved == max(0, min(max(RESERVED_NEW_WORK_SLOTS, free), max(0, parallel - 1)))


def test_custom_reserve_is_clamped_to_parallel_minus_one() -> None:
    """A larger requested reserve never starves repairs of their last slot."""
    assert reserved_new_work_slots(5, repairs_pending=1, ready_count=9, reserve=10) == 4
    assert reserved_new_work_slots(3, repairs_pending=1, ready_count=9, reserve=2) == 2


def test_non_positive_reserve_constant_disables_mechanism() -> None:
    """Adversarial: reserve<=0 ⇒ 0 (the mechanism is opt-outable to a no-op)."""
    assert reserved_new_work_slots(8, repairs_pending=4, ready_count=4, reserve=0) == 0
    assert reserved_new_work_slots(8, repairs_pending=4, ready_count=4, reserve=-1) == 0


# --------------------------------------------------------------------------- #
# Integration — drive the real ``_tick`` against a stuck repair + ready work.
# --------------------------------------------------------------------------- #


def _make_cfg(tmp_path: Path, *, parallel: int) -> Config:
    return Config(
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
        attempts=AttemptsConfig(enabled=False, max_history_in_brief=5),
        lumen=LumenConfig(),
    )


def _issue(num: int) -> dict[str, Any]:
    return {"number": num, "title": f"issue {num}", "body": "b", "labels": []}


def _read_events(cfg: Config) -> list[dict[str, Any]]:
    if not cfg.events_file.exists():
        return []
    return [json.loads(ln) for ln in cfg.events_file.read_text().splitlines() if ln.strip()]


def _wire_stuck_repair_tick(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ready_issues: list[dict[str, Any]],
    pre_repairs: int = 1,
) -> list[list[int]]:
    """Stub ``_tick``'s collaborators: a perpetually-stuck pre-dispatch repair
    plus a fixed ready backlog. Returns a sink that records the issue numbers
    handed to ``_dispatch_and_iterate`` per tick (empty list ⇒ nothing
    dispatched that tick — the starvation symptom)."""
    dispatched: list[list[int]] = []

    monkeypatch.setattr(_tick_mod, "_maybe_run_maintenance", lambda *_a, **_k: False)
    # A repair fires EVERY tick and never clears (the #262 starvation trigger).
    monkeypatch.setattr(
        _tick_mod, "_run_pre_dispatch_repairs", lambda *_a, **_k: pre_repairs
    )
    monkeypatch.setattr(_tick_mod, "_run_ready_issue_repairs", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        _tick_mod, "_select_candidates", lambda *_a, **_k: list(ready_issues) or None
    )
    monkeypatch.setattr(_tick_mod, "_expand_specs", lambda _cfg, _t, issues: issues)
    monkeypatch.setattr(_tick_mod, "_apply_maestro_plan", lambda _cfg, _t, issues: (issues, ""))
    monkeypatch.setattr(
        _tick_mod,
        "_select_dispatch_set",
        lambda _cfg, issues: (list(issues), [{"risk_gated": False} for _ in issues]),
    )

    def _spy_dispatch(_cfg: Any, _t: Any, issues: list[dict[str, Any]], *_a: Any, **_k: Any) -> Any:
        dispatched.append([i["number"] for i in issues])
        return [], False

    monkeypatch.setattr(_tick_mod, "_dispatch_and_iterate", _spy_dispatch)
    monkeypatch.setattr(_tick_mod, "_run_merge_gate", lambda *_a, **_k: None)
    monkeypatch.setattr(_tick_mod, "_finalize_tick", lambda *_a, **_k: None)
    return dispatched


def test_stuck_repair_does_not_starve_ready_work_within_one_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC: parallel=2, a perpetually-blocked repair every tick, >=1 ready issue
    ⇒ the ready issue IS dispatched within ONE tick, and a
    ``dispatch_slot_reserved`` telemetry event is appended."""
    cfg = _make_cfg(tmp_path, parallel=2)
    dispatched = _wire_stuck_repair_tick(monkeypatch, ready_issues=[_issue(5)])

    _tick_mod._tick(cfg, 1)

    assert dispatched == [[5]]  # the ready issue was dispatched despite the repair
    reserved_evs = [e for e in _read_events(cfg) if e["kind"] == "dispatch_slot_reserved"]
    assert len(reserved_evs) == 1
    ev = reserved_evs[0]
    assert ev["reserved"] == 1
    assert ev["repairs_pending"] == 1
    assert ev["ready_count"] == 1
    assert ev["tick"] == 1


def test_reserved_slot_caps_new_work_to_one_when_repairs_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With parallel=2 and a stuck repair, only the single RESERVED slot is used
    for new work even when many ready issues are waiting (repairs keep the
    rest)."""
    cfg = _make_cfg(tmp_path, parallel=2)
    dispatched = _wire_stuck_repair_tick(
        monkeypatch, ready_issues=[_issue(5), _issue(6), _issue(7)]
    )

    _tick_mod._tick(cfg, 1)

    assert dispatched == [[5]]  # exactly one reserved slot's worth of new work


def test_no_starvation_over_a_window_of_consecutive_ticks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T6 / no-starvation regression guard: run N consecutive ticks with the
    SAME stuck repair + a non-empty ready backlog. At least one ready issue is
    dispatched across the window.

    On ``main`` (repairs are an unconditional terminal tick body) this is "0
    forever" and the assertion fails — exactly the bug #262 fixes.
    """
    cfg = _make_cfg(tmp_path, parallel=2)
    dispatched = _wire_stuck_repair_tick(monkeypatch, ready_issues=[_issue(5)])

    for t in range(1, 6):
        _tick_mod._tick(cfg, t)

    total_dispatched = sum(len(d) for d in dispatched)
    assert total_dispatched >= 1
    # Stronger: EVERY tick made forward progress, not just one lucky tick.
    assert dispatched == [[5], [5], [5], [5], [5]]


def test_ready_zero_with_repairs_forces_no_dispatch_and_no_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adversarial: ready=0 AND repairs>0 ⇒ NO ``dispatch_slot_reserved`` event
    fires and NO new-work dispatch is forced (the repair path takes the tick).

    This guards the byte-identical contract: the reservation must not kick in
    when there is no ready backlog.
    """
    cfg = _make_cfg(tmp_path, parallel=2)
    # ready_issues empty ⇒ the stubbed _select_candidates returns None.
    dispatched = _wire_stuck_repair_tick(monkeypatch, ready_issues=[])

    _tick_mod._tick(cfg, 1)

    assert dispatched == []  # no forced empty dispatch
    assert not [e for e in _read_events(cfg) if e["kind"] == "dispatch_slot_reserved"]


def test_no_repairs_uses_all_slots_and_emits_no_reservation_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Byte-identical contract: no repairs in flight ⇒ new work uses all slots
    and no ``dispatch_slot_reserved`` event fires."""
    cfg = _make_cfg(tmp_path, parallel=2)
    dispatched = _wire_stuck_repair_tick(
        monkeypatch, ready_issues=[_issue(5), _issue(6)], pre_repairs=0
    )

    _tick_mod._tick(cfg, 1)

    assert dispatched == [[5, 6]]  # both ready issues dispatched, no slot stolen
    assert not [e for e in _read_events(cfg) if e["kind"] == "dispatch_slot_reserved"]


def test_parallel_one_with_repairs_returns_early_no_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """parallel=1 tie-break: a stuck repair still takes the whole tick — no
    reservation, no new-work dispatch (repairs win, documented)."""
    cfg = _make_cfg(tmp_path, parallel=1)
    dispatched = _wire_stuck_repair_tick(monkeypatch, ready_issues=[_issue(5)])

    _tick_mod._tick(cfg, 1)

    assert dispatched == []
    assert not [e for e in _read_events(cfg) if e["kind"] == "dispatch_slot_reserved"]


def test_quiet_select_candidates_suppresses_idle_event_and_sleep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #262 quiet re-fetch must not emit ``tick_idle`` or sleep when the
    repair tick already owned them (keeps the no-ready path byte-identical)."""
    cfg = _make_cfg(tmp_path, parallel=2)
    monkeypatch.setattr(_tick_mod, "_resolve_axis_filter", lambda _cfg, _t: [])
    monkeypatch.setattr(_tick_mod, "top_issues", lambda *_a, **_k: [])
    slept: list[Any] = []

    out = _tick_mod._select_candidates(
        cfg, 5, short_sleep=lambda *a, **k: slept.append(a), quiet=True
    )

    assert out is None
    assert _read_events(cfg) == []  # no tick_idle in quiet mode
    assert slept == []  # no idle sleep in quiet mode
