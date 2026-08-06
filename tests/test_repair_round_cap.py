"""A PR that never converges must stop consuming the loop.

☠ WHY THIS EXISTS. Repair ticks run their workers SYNCHRONOUSLY — dispatch collects `fut.result()`
inside the ThreadPoolExecutor — so the tick sits inside a repair until the worker finishes, up to
worker_timeout_s. One PR stuck in review therefore holds the ENTIRE loop and no new issue is
dispatched. Measured on a live repo: two PRs consumed a whole day at 17-44 min a round while the
backlog sat untouched and the north-star number stayed unmeasured.

`block_on_spec` handles the case where the critic RECOGNISES the issue is at fault. This cap handles
the case where it does not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from forge_loop.runner.repairs import blocking_pr_repairs


class _Cfg:
    def __init__(self, tmp: Path, cap: int) -> None:
        self.events_file = tmp / "events.jsonl"
        self.logs_dir = tmp / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.github_repo = "o/r"
        self.parallel = 2
        self.critic = type("C", (), {"max_repair_rounds": cap})()


def _pr() -> dict[str, Any]:
    return {"url": "https://github.com/o/r/pull/173", "number": 173, "body": "Fixes #168"}


def _seed_rounds(cfg: _Cfg, issue: int, n: int) -> None:
    """count_prior_critic_rounds reads critic-<issue>-*.log files off disk."""
    for i in range(n):
        (cfg.logs_dir / f"critic-{issue}-{1000 + i}.log").write_text("x", encoding="utf-8")


def _select(cfg: _Cfg) -> list[Any]:
    return blocking_pr_repairs(
        cfg,  # type: ignore[arg-type]
        prs_requiring_repair_fn=lambda *a, **k: [_pr()],
        fetch_issue_fn=lambda n, **k: {"number": n, "title": "t", "labels": []},
        pr_review_context_fn=lambda *a, **k: "ctx",
    )


def test_pr_over_the_cap_is_not_selected_for_repair(tmp_path: Path) -> None:
    cfg = _Cfg(tmp_path, cap=4)
    _seed_rounds(cfg, 168, 5)  # already had five reviews
    assert _select(cfg) == [], "a PR past the cap must stop eating repair ticks"
    events = cfg.events_file.read_text(encoding="utf-8")
    assert "repair_round_cap_reached" in events, "parking must be observable, never silent"


def test_pr_under_the_cap_is_still_repaired(tmp_path: Path) -> None:
    """NEV-CTL-04: prove the selector can still RETURN work, or the test above is vacuous."""
    cfg = _Cfg(tmp_path, cap=4)
    _seed_rounds(cfg, 168, 2)
    assert len(_select(cfg)) == 1, "a converging PR must keep being repaired"


def test_cap_zero_disables_the_guard(tmp_path: Path) -> None:
    cfg = _Cfg(tmp_path, cap=0)
    _seed_rounds(cfg, 168, 99)
    assert len(_select(cfg)) == 1, "cap=0 must opt out entirely"
