"""Unit tests for the ``_tick`` phase helpers (issue #225 decomposition).

``_tick`` was a ~580-line god-function. Issue #225 split it into named phase
helpers (``_select_candidates``, ``_classify_issue_for_dispatch``,
``_select_dispatch_set``, ...). These tests exercise those phases DIRECTLY —
without running a full tick — which the monolith made impossible.

The two phases under test are state machines / external-dependency gates, so
per the testing manifesto they get:

* T1 (state machine ⇒ one test per edge + adversarial default-branch test):
  ``_classify_issue_for_dispatch`` dispatches on the in_flight / cooldown /
  dispatch edges AND on an unrecognised ``classify_skip`` kind (the
  fall-through/default arm).
* T2 (external-dep assumption ⇒ adversarial false-case test):
  ``_select_candidates`` is tested for the ``gh`` list FAILURE branch
  (``CalledProcessError``), not only the happy "gh returned issues" path.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from forge_loop import attempts as _attempts
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

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _make_cfg(tmp_path: Path) -> Config:
    return Config(
        repo=tmp_path,
        github_repo="o/r",
        parallel=1,
        tick_interval_s=0,
        max_ticks=1,
        worker_timeout_s=60,
        deploy_task="",
        labels=Labels(),
        briefs=Briefs(),
        critic=CriticConfig(enabled=False, timeout_s=10),
        po=POConfig(enabled=False, timeout_s=10, max_to_expand_per_tick=0),
        attempts=AttemptsConfig(enabled=True, max_history_in_brief=5),
        lumen=LumenConfig(),
    )


def _read_events(cfg: Config) -> list[dict[str, Any]]:
    if not cfg.events_file.exists():
        return []
    return [json.loads(ln) for ln in cfg.events_file.read_text().splitlines() if ln.strip()]


@dataclass
class _FakeAttempts:
    history: list[dict[str, Any]]
    corrupt: int
    blocking_comments: list[str]


def _patch_attempts(
    monkeypatch: pytest.MonkeyPatch,
    *,
    history: list[dict[str, Any]] | None = None,
    corrupt: int = 0,
    blocking: list[str] | None = None,
) -> None:
    view = _FakeAttempts(history=history or [], corrupt=corrupt, blocking_comments=blocking or [])
    monkeypatch.setattr(_attempts, "fetch_issue_attempts", lambda *_a, **_k: view)


def _issue(num: int = 1, *, labels: list[str] | None = None, body: str = "b") -> dict[str, Any]:
    return {
        "number": num,
        "title": f"issue {num}",
        "body": body,
        "labels": [{"name": n} for n in (labels or [])],
    }


# --------------------------------------------------------------------------- #
# _classify_issue_for_dispatch — skip-classification state machine (T1)
# --------------------------------------------------------------------------- #


def test_classify_dispatches_clean_issue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No prior history ⇒ classify_skip returns no-skip ⇒ issue dispatched.

    This is the default/fall-through arm: neither in_flight nor cooldown
    matched, so the helper returns a worker-meta dict.
    """
    cfg = _make_cfg(tmp_path)
    _patch_attempts(monkeypatch, history=[])

    meta = _tick_mod._classify_issue_for_dispatch(
        cfg,
        _issue(1),
        force_set=set(),
        cooldown_s=0.0,
        brief_hash="h",
        risk_gate_label=cfg.labels.risk_gate,
    )

    assert meta is not None
    assert meta["risk_gated"] is False
    assert meta["forced"] is False
    assert meta["brief_fingerprint"]  # a fingerprint was computed
    assert _read_events(cfg) == []  # no skip event emitted


def test_classify_detects_risk_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An issue carrying the risk-gate label dispatches but is flagged gated."""
    cfg = _make_cfg(tmp_path)
    _patch_attempts(monkeypatch, history=[])

    meta = _tick_mod._classify_issue_for_dispatch(
        cfg,
        _issue(1, labels=[cfg.labels.risk_gate]),
        force_set=set(),
        cooldown_s=0.0,
        brief_hash="h",
        risk_gate_label=cfg.labels.risk_gate,
    )

    assert meta is not None
    assert meta["risk_gated"] is True


def test_classify_skips_in_flight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """in_flight edge: returns None, emits worker_skip_in_flight, drops ready label."""
    cfg = _make_cfg(tmp_path)
    _patch_attempts(monkeypatch, history=[{"any": "row"}])
    monkeypatch.setattr(
        _attempts,
        "classify_skip",
        lambda *_a, **_k: _attempts.SkipDecision(
            kind="in_flight", pr_url="http://pr/9", matched_ts="2026-01-01T00:00:00Z"
        ),
    )
    removed: list[tuple[int, str]] = []
    monkeypatch.setattr(
        _tick_mod,
        "_remove_ready_label",
        lambda _cfg, num, *, status, pr_url=None: removed.append((num, status)),
    )

    meta = _tick_mod._classify_issue_for_dispatch(
        cfg,
        _issue(7),
        force_set=set(),
        cooldown_s=0.0,
        brief_hash="h",
        risk_gate_label=cfg.labels.risk_gate,
    )

    assert meta is None
    assert removed == [(7, "in_flight")]
    kinds = [e["kind"] for e in _read_events(cfg)]
    assert "worker_skip_in_flight" in kinds


def test_classify_skips_cooldown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cooldown edge: returns None and emits worker_skip_cooldown."""
    cfg = _make_cfg(tmp_path)
    _patch_attempts(monkeypatch, history=[{"any": "row"}])
    monkeypatch.setattr(
        _attempts,
        "classify_skip",
        lambda *_a, **_k: _attempts.SkipDecision(kind="cooldown", cooldown_remaining_s=42),
    )

    meta = _tick_mod._classify_issue_for_dispatch(
        cfg,
        _issue(8),
        force_set=set(),
        cooldown_s=3600.0,
        brief_hash="h",
        risk_gate_label=cfg.labels.risk_gate,
    )

    assert meta is None
    evs = _read_events(cfg)
    assert [e["kind"] for e in evs] == ["worker_skip_cooldown"]
    assert evs[0]["cooldown_remaining_s"] == 42


def test_classify_forced_bypasses_skip_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forced issue dispatches even when classify_skip would say in_flight.

    classify_skip must NOT be consulted at all for forced issues — patch it to
    blow up to prove the guard path is skipped.
    """
    cfg = _make_cfg(tmp_path)
    _patch_attempts(monkeypatch, history=[{"any": "row"}])

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("classify_skip must not run for forced issues")

    monkeypatch.setattr(_attempts, "classify_skip", _boom)

    meta = _tick_mod._classify_issue_for_dispatch(
        cfg,
        _issue(9),
        force_set={9},
        cooldown_s=3600.0,
        brief_hash="h",
        risk_gate_label=cfg.labels.risk_gate,
    )

    assert meta is not None
    assert meta["forced"] is True


def test_classify_unknown_skip_kind_falls_through_to_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T1 adversarial default-branch: an UNRECOGNISED skip kind dispatches.

    Neither the ``in_flight`` nor the ``cooldown`` arm matches, so control must
    reach the fall-through return (dispatch). A regression that turned the last
    ``if`` into an ``elif``/``else`` swallowing unknown kinds would fail here.
    """
    cfg = _make_cfg(tmp_path)
    _patch_attempts(monkeypatch, history=[{"any": "row"}])
    monkeypatch.setattr(
        _attempts,
        "classify_skip",
        lambda *_a, **_k: _attempts.SkipDecision(kind="totally-unknown-kind"),
    )

    meta = _tick_mod._classify_issue_for_dispatch(
        cfg,
        _issue(10),
        force_set=set(),
        cooldown_s=0.0,
        brief_hash="h",
        risk_gate_label=cfg.labels.risk_gate,
    )

    assert meta is not None  # fell through to dispatch
    assert _read_events(cfg) == []  # no skip event for an unknown kind


def test_classify_emits_attempts_corrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Corrupt history rows surface an attempts_corrupt event and don't crash."""
    cfg = _make_cfg(tmp_path)
    _patch_attempts(monkeypatch, history=[], corrupt=2)

    meta = _tick_mod._classify_issue_for_dispatch(
        cfg,
        _issue(11),
        force_set=set(),
        cooldown_s=0.0,
        brief_hash="h",
        risk_gate_label=cfg.labels.risk_gate,
    )

    assert meta is not None
    corrupt_evs = [e for e in _read_events(cfg) if e["kind"] == "attempts_corrupt"]
    assert corrupt_evs and corrupt_evs[0]["rows"] == 2


# --------------------------------------------------------------------------- #
# _select_dispatch_set — alignment of the (issues, workers_meta) lists
# --------------------------------------------------------------------------- #


def test_select_dispatch_set_drops_skipped_and_keeps_alignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mixed batch: skipped issues drop from BOTH lists, survivors stay aligned."""
    cfg = _make_cfg(tmp_path)
    _patch_attempts(monkeypatch, history=[])
    monkeypatch.setattr(_attempts, "cooldown_from_env", lambda: 0.0)
    monkeypatch.setattr(_tick_mod._worker, "brief_template_hash", lambda: "h")
    monkeypatch.setattr(_tick_mod, "_consume_force_set", lambda _cfg: set())

    # Skip the middle issue only.
    def _skip_middle(cfg_: Config, i: dict[str, Any], **_k: Any) -> dict[str, Any] | None:
        if i["number"] == 2:
            return None
        return {"brief_fingerprint": f"fp{i['number']}", "risk_gated": i["number"] == 3}

    monkeypatch.setattr(_tick_mod, "_classify_issue_for_dispatch", _skip_middle)

    issues_in = [_issue(1), _issue(2), _issue(3)]
    issues_out, metas = _tick_mod._select_dispatch_set(cfg, issues_in)

    assert [i["number"] for i in issues_out] == [1, 3]
    assert len(metas) == 2
    # alignment: meta[k] belongs to issues_out[k]
    assert metas[0]["brief_fingerprint"] == "fp1"
    assert metas[1]["brief_fingerprint"] == "fp3"
    assert metas[1]["risk_gated"] is True


# --------------------------------------------------------------------------- #
# _select_candidates — candidate selection + external-dep gate (T2)
# --------------------------------------------------------------------------- #


def test_select_candidates_returns_issues_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: gh returns issues, no axis filter ⇒ list flows through."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(_tick_mod, "_resolve_axis_filter", lambda _cfg, _t: [])
    monkeypatch.setattr(_tick_mod, "top_issues", lambda *_a, **_k: [_issue(1), _issue(2)])
    slept: list[Any] = []

    out = _tick_mod._select_candidates(cfg, 1, short_sleep=lambda *a, **k: slept.append(a))

    assert out is not None
    assert [i["number"] for i in out] == [1, 2]
    assert slept == []  # a productive tick does not sleep here


def test_select_candidates_gh_failure_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T2 adversarial: the gh list call FAILS ⇒ None, gh_list_failed, 60s sleep.

    The happy 'gh returned issues' path is not sufficient — the external
    dependency's failure branch is the one that silently broke ticks before.
    """
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(_tick_mod, "_resolve_axis_filter", lambda _cfg, _t: [])

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise subprocess.CalledProcessError(1, ["gh"], stderr="gh exploded")

    monkeypatch.setattr(_tick_mod, "top_issues", _boom)
    slept: list[Any] = []

    out = _tick_mod._select_candidates(cfg, 5, short_sleep=lambda *a, **k: slept.append(a))

    assert out is None
    evs = _read_events(cfg)
    assert [e["kind"] for e in evs] == ["gh_list_failed"]
    assert "gh exploded" in evs[0]["err"]
    assert slept == [(60, cfg)]  # backoff sleep fired


def test_select_candidates_empty_returns_none_and_idles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ready issues ⇒ None, tick_idle event, idle sleep."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(_tick_mod, "_resolve_axis_filter", lambda _cfg, _t: [])
    monkeypatch.setattr(_tick_mod, "top_issues", lambda *_a, **_k: [])
    slept: list[Any] = []

    out = _tick_mod._select_candidates(cfg, 5, short_sleep=lambda *a, **k: slept.append(a))

    assert out is None
    assert [e["kind"] for e in _read_events(cfg)] == ["tick_idle"]
    assert slept == [(cfg.tick_interval_s, cfg)]


def test_select_candidates_axis_filter_empty_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Active axis filter that matches nothing ⇒ axis_filter_empty + tick_idle."""
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(_tick_mod, "_resolve_axis_filter", lambda _cfg, _t: ["perf"])
    monkeypatch.setattr(_tick_mod, "top_issues", lambda *_a, **_k: [_issue(1)])
    # filter_issues_by_axes is imported inside the function from forge_loop.axis
    import forge_loop.axis as _axis

    monkeypatch.setattr(_axis, "filter_issues_by_axes", lambda _issues, _axes: [])

    out = _tick_mod._select_candidates(cfg, 5, short_sleep=lambda *a, **k: None)

    assert out is None
    kinds = [e["kind"] for e in _read_events(cfg)]
    assert "axis_filter_active" in kinds
    assert "axis_filter_empty" in kinds
    assert "tick_idle" in kinds
