"""Tests for the periodic backlog audit — issue #125.

Covers ``Brainstormer.audit_backlog`` (both demotion branches, the keep
path, idempotency, and per-issue failure isolation), the typed events, the
``BrainstormerSettings`` field, and the tick-level cadence gate.

The audit drives GitHub exclusively through the module-level
``forge_loop.gh_issues`` helpers (``top_issues`` / ``label`` / ``unlabel`` /
``comment``), so the fake below monkeypatches those four functions with a
single stateful in-memory backlog. Because the audit only ever *fetches*
``loop:ready`` issues, label mutations made by the fake are reflected on the
next fetch — which is exactly what makes the audit idempotent.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

import forge_loop.gh_issues as gh
from forge_loop.brainstormer import (
    LOOP_COLD_LABEL,
    LOOP_READY_LABEL,
    Brainstormer,
)
from forge_loop.settings import BrainstormerSettings

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _write_vision(root: Path, *, rejected: list[str] | None = None) -> None:
    """Write a minimal valid ``.forge/`` tree under ``root``."""
    forge = root / ".forge"
    forge.mkdir(parents=True, exist_ok=True)
    (forge / "product-vision.md").write_text("# Vision\nShip real value.\n")
    axes = {
        "axes": [
            {
                "name": "golden-path-e2e",
                "customer": "operator",
                "valuable_means": "the loop ships PRs end to end",
                "acceptable_work": ["implement a feature"],
                "rejected_as_cosmetic": rejected
                if rejected is not None
                else ["sparkline", "badge"],
            }
        ]
    }
    (forge / "axes.yaml").write_text(yaml.safe_dump(axes))


class _FakeBacklog:
    """Stateful in-memory stand-in for the ``gh_issues`` mutation helpers."""

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self._issues: dict[int, dict[str, Any]] = {
            i["number"]: {**i, "labels": list(i["labels"])} for i in issues
        }
        self.comments: dict[int, list[str]] = {}
        self.label_fail_on: set[int] = set()

    def top_issues(self, label: str, limit: int, repo: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for num, i in self._issues.items():
            if label in i["labels"]:
                out.append(
                    {
                        "number": num,
                        "title": i["title"],
                        "body": i["body"],
                        "labels": [{"name": n} for n in i["labels"]],
                    }
                )
        return out[:limit]

    def label(self, issue: int, labels: list[str], repo: str | None = None) -> bool:
        # Mirror the REAL gh_issues.label contract: a GitHub failure is
        # SUPPRESSED (never raised) and surfaced as a False return. The audit
        # detects the failed mutation by branching on this bool — exactly the
        # production path the adversarial test must exercise (issue #125).
        if issue in self.label_fail_on:
            return False
        for lbl in labels:
            if lbl not in self._issues[issue]["labels"]:
                self._issues[issue]["labels"].append(lbl)
        return True

    def unlabel(self, issue: int, label: str, repo: str | None = None) -> bool:
        labels = self._issues[issue]["labels"]
        if label in labels:
            labels.remove(label)
        return True

    def comment(self, issue: int, body: str, repo: str | None = None) -> bool:
        self.comments.setdefault(issue, []).append(body)
        return True

    # -- assertions helpers --
    def labels_of(self, issue: int) -> list[str]:
        return list(self._issues[issue]["labels"])


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeBacklog) -> None:
    monkeypatch.setattr(gh, "top_issues", fake.top_issues)
    monkeypatch.setattr(gh, "label", fake.label)
    monkeypatch.setattr(gh, "unlabel", fake.unlabel)
    monkeypatch.setattr(gh, "comment", fake.comment)


def _read_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Unit — demotion branches + keep path
# ---------------------------------------------------------------------------


def test_demotes_issue_missing_axis_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_vision(tmp_path)
    fake = _FakeBacklog(
        [{"number": 7, "title": "do a thing", "body": "no axis here", "labels": [LOOP_READY_LABEL]}]
    )
    _install(monkeypatch, fake)

    outcome = Brainstormer(repo_path=tmp_path).audit_backlog("o/r")

    assert outcome.demoted == [7]
    assert outcome.kept == []
    assert outcome.reasons[7] == "missing axis citation"
    assert LOOP_READY_LABEL not in fake.labels_of(7)
    assert LOOP_COLD_LABEL in fake.labels_of(7)
    assert len(fake.comments[7]) == 1
    assert "missing axis citation" in fake.comments[7][0]
    assert ".forge/axes.yaml" in fake.comments[7][0]


def test_demotes_issue_matching_cosmetic_regex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_vision(tmp_path, rejected=["sparkline"])
    fake = _FakeBacklog(
        [
            {
                "number": 9,
                "title": "feat(ui): add a sparkline to the status table",
                "body": "purely visual",
                "labels": [LOOP_READY_LABEL, "axis:golden-path-e2e"],
            }
        ]
    )
    _install(monkeypatch, fake)

    outcome = Brainstormer(repo_path=tmp_path).audit_backlog("o/r")

    assert outcome.demoted == [9]
    reason = outcome.reasons[9]
    assert "sparkline" in reason
    assert "golden-path-e2e" in reason
    assert LOOP_COLD_LABEL in fake.labels_of(9)
    assert LOOP_READY_LABEL not in fake.labels_of(9)


def test_keeps_clean_issue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_vision(tmp_path, rejected=["sparkline"])
    fake = _FakeBacklog(
        [
            {
                "number": 3,
                "title": "implement a feature",
                "body": "delivers end-to-end value",
                "labels": [LOOP_READY_LABEL, "axis:golden-path-e2e"],
            }
        ]
    )
    _install(monkeypatch, fake)

    outcome = Brainstormer(repo_path=tmp_path).audit_backlog("o/r")

    assert outcome.kept == [3]
    assert outcome.demoted == []
    assert 3 not in fake.comments
    assert fake.labels_of(3) == [LOOP_READY_LABEL, "axis:golden-path-e2e"]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_field_default_is_10() -> None:
    assert BrainstormerSettings().audit_every_n_ticks == 10


def test_settings_zero_disables_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    from forge_loop.runner import tick as tick_mod

    calls: list[int] = []
    monkeypatch.setattr(
        tick_mod,
        "_run_brainstormer_audit",
        lambda cfg, tick: calls.append(tick) or True,  # type: ignore[func-returns-value]
    )
    cfg = SimpleNamespace(
        brainstormer=SimpleNamespace(audit_every_n_ticks=0), tick_interval_s=0
    )

    ran = tick_mod._maybe_run_brainstormer_audit(cfg, 10, short_sleep=lambda *_a, **_k: None)

    assert ran is False
    assert calls == []


# ---------------------------------------------------------------------------
# Tick cadence integration
# ---------------------------------------------------------------------------


def test_tick_runs_audit_on_nth_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    from forge_loop.runner import tick as tick_mod

    fired: list[int] = []
    monkeypatch.setattr(
        tick_mod,
        "_run_brainstormer_audit",
        lambda cfg, tick: fired.append(tick) or True,  # type: ignore[func-returns-value]
    )
    cfg = SimpleNamespace(
        brainstormer=SimpleNamespace(audit_every_n_ticks=10), tick_interval_s=0
    )

    for t in range(1, 22):
        tick_mod._maybe_run_brainstormer_audit(cfg, t, short_sleep=lambda *_a, **_k: None)

    assert fired == [10, 20]


def test_audit_emits_typed_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from forge_loop.config import Config
    from forge_loop.runner.tick_checks import run_brainstormer_audit

    _write_vision(tmp_path)
    fake = _FakeBacklog(
        [{"number": 5, "title": "do a thing", "body": "no axis", "labels": [LOOP_READY_LABEL]}]
    )
    _install(monkeypatch, fake)
    cfg = Config(repo=tmp_path, github_repo="o/r")

    ran = run_brainstormer_audit(cfg, tick=10)

    assert ran is True
    events = _read_events(cfg.events_file)
    done = [e for e in events if e.get("kind") == "brainstormer_audit_done"]
    assert len(done) == 1
    assert done[0]["tick"] == 10
    assert done[0]["demoted"] == [5]
    assert done[0]["kept"] == []
    assert "duration_s" in done[0]


# ---------------------------------------------------------------------------
# Adversarial / sad-path
# ---------------------------------------------------------------------------


def test_audit_continues_after_single_issue_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_vision(tmp_path)
    fake = _FakeBacklog(
        [
            {"number": 1, "title": "t1", "body": "no axis", "labels": [LOOP_READY_LABEL]},
            {"number": 2, "title": "t2", "body": "no axis", "labels": [LOOP_READY_LABEL]},
            {"number": 3, "title": "t3", "body": "no axis", "labels": [LOOP_READY_LABEL]},
        ]
    )
    fake.label_fail_on = {2}  # gh.label returns False (suppressed), as in prod
    _install(monkeypatch, fake)
    events_file = tmp_path / "events.jsonl"

    outcome = Brainstormer(repo_path=tmp_path).audit_backlog("o/r", events_file=events_file)

    # #1 and #3 demoted cleanly; #2 recorded as a failure, not aborting the pass.
    # #2 is NOT in demoted — the falsey gh.label return is treated as a failure.
    assert sorted(outcome.demoted) == [1, 3]
    assert 2 in outcome.failures
    assert 2 not in outcome.demoted
    assert LOOP_COLD_LABEL in fake.labels_of(1)
    assert LOOP_COLD_LABEL in fake.labels_of(3)
    assert LOOP_COLD_LABEL not in fake.labels_of(2)  # label never landed
    assert LOOP_READY_LABEL in fake.labels_of(2)  # still ready — short-circuited before unlabel
    assert 2 not in fake.comments

    events = _read_events(events_file)
    failures = [e for e in events if e.get("kind") == "brainstormer_audit_partial_failure"]
    assert len(failures) == 1
    assert failures[0]["issue"] == 2


def test_audit_is_idempotent_on_already_cold_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_vision(tmp_path)
    fake = _FakeBacklog(
        [{"number": 4, "title": "t", "body": "no axis", "labels": [LOOP_READY_LABEL]}]
    )
    _install(monkeypatch, fake)
    bs = Brainstormer(repo_path=tmp_path)

    first = bs.audit_backlog("o/r")
    second = bs.audit_backlog("o/r")

    assert first.demoted == [4]
    assert second.demoted == []  # already cold — no-op
    assert second.kept == []
    assert len(fake.comments[4]) == 1  # NO duplicate comment on the second pass
    assert LOOP_COLD_LABEL in fake.labels_of(4)


def test_malformed_axes_yaml_does_not_crash_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forge_loop.config import Config
    from forge_loop.runner.tick_checks import run_brainstormer_audit

    forge = tmp_path / ".forge"
    forge.mkdir(parents=True)
    (forge / "product-vision.md").write_text("# Vision\nShip value.\n")
    (forge / "axes.yaml").write_text("axes: [this is : not valid yaml")  # malformed
    fake = _FakeBacklog(
        [{"number": 1, "title": "t", "body": "b", "labels": [LOOP_READY_LABEL]}]
    )
    _install(monkeypatch, fake)
    cfg = Config(repo=tmp_path, github_repo="o/r")

    ran = run_brainstormer_audit(cfg, tick=10)

    # Audit skipped (returns False ⇒ tick continues into dispatch), no crash,
    # and the issue was never touched.
    assert ran is False
    assert 1 not in fake.comments
    assert LOOP_READY_LABEL in fake.labels_of(1)
    events = _read_events(cfg.events_file)
    assert any(e.get("kind") == "brainstormer_audit_skipped" for e in events)
