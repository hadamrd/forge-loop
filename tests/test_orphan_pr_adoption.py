"""Unit tests for issue #213 — orphaned-PR recovery + adoption selector.

Covers the three recovery sites that previously threw away a PR URL when a
worker was cancelled at the deadline *after* opening its PR, plus the new
``repairs.orphaned_clean_pr_adoptions`` selector that decides which open loop
PRs are safe to re-critic + merge.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from forge_loop import worker
from forge_loop.config import (
    AttemptsConfig,
    Briefs,
    Config,
    CriticConfig,
    Labels,
    LumenConfig,
    POConfig,
)
from forge_loop.runner.repairs import loop_issue_from_branch, orphaned_clean_pr_adoptions

PR = "https://github.com/o/r/pull/205"


def _cfg(tmp_path: Path) -> Config:
    cfg = Config(
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
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.events_file.touch()
    return cfg


def _events(cfg: Config) -> list[dict[str, Any]]:
    if not cfg.events_file.exists():
        return []
    return [json.loads(line) for line in cfg.events_file.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Recovery helpers (acceptance criterion 1)
# ---------------------------------------------------------------------------


def test_recover_pr_url_from_sprint_events(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "sprint-events.jsonl").write_text(json.dumps({"kind": "pr_opened", "pr": PR}) + "\n")
    assert worker.recover_orphaned_pr_url(wt, None) == PR


def test_recover_pr_url_from_log_when_no_events(tmp_path: Path) -> None:
    log = tmp_path / "w.log"
    log.write_text(f"step 1\nopened {PR}\nmore output\n")
    # No worktree events present → fall back to scanning the raw worker log.
    assert worker.recover_orphaned_pr_url(tmp_path / "missing_wt", log) == PR


def test_recover_pr_url_none_when_absent(tmp_path: Path) -> None:
    """Adversarial: nothing PR-shaped anywhere → None (preserve pr_url=None)."""
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "sprint-events.jsonl").write_text(json.dumps({"kind": "worker_start"}) + "\n")
    log = tmp_path / "w.log"
    log.write_text("no urls in here at all")
    assert worker.recover_orphaned_pr_url(wt, log) is None


def test_recover_pr_url_events_take_priority_over_log(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "sprint-events.jsonl").write_text(json.dumps({"kind": "pr_opened", "pr": PR}) + "\n")
    log = tmp_path / "w.log"
    log.write_text("opened https://github.com/o/r/pull/999\n")
    assert worker.recover_orphaned_pr_url(wt, log) == PR


# ---------------------------------------------------------------------------
# worker._run_worker_sdk timeout branch (acceptance criterion 1)
# ---------------------------------------------------------------------------


def _raise_timeout(*_a: Any, **_k: Any) -> Any:
    raise TimeoutError


def test_run_worker_sdk_timeout_recovers_pr_url(tmp_path: Path, monkeypatch) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "sprint-events.jsonl").write_text(json.dumps({"kind": "pr_opened", "pr": PR}) + "\n")
    monkeypatch.setattr(worker, "_run_with_timeout", _raise_timeout)
    out = worker._run_worker_sdk(
        issue={"number": 205, "title": "x"},
        worktree=wt,
        log_path=tmp_path / "w.log",
        brief="b",
        timeout_s=1,
        emit=None,
        tick=1,
    )
    assert out.status == "timeout"
    assert out.pr_url == PR


def test_run_worker_sdk_timeout_no_pr_stays_none(tmp_path: Path, monkeypatch) -> None:
    """Adversarial: timeout with no recoverable PR → pr_url stays None."""
    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setattr(worker, "_run_with_timeout", _raise_timeout)
    out = worker._run_worker_sdk(
        issue={"number": 205, "title": "x"},
        worktree=wt,
        log_path=tmp_path / "w.log",
        brief="b",
        timeout_s=1,
        emit=None,
        tick=1,
    )
    assert out.status == "timeout"
    assert out.pr_url is None


# ---------------------------------------------------------------------------
# dispatch._run_worker_with_saga BaseException branch (acceptance criterion 1)
# ---------------------------------------------------------------------------


def test_run_worker_with_saga_baseexception_recovers_pr_url(tmp_path: Path, monkeypatch) -> None:
    from forge_loop.runner import dispatch as d

    cfg = _cfg(tmp_path)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "sprint-events.jsonl").write_text(json.dumps({"kind": "pr_opened", "pr": PR}) + "\n")
    captured: dict[str, Any] = {}
    monkeypatch.setattr(d, "get_or_resume_session", lambda *_a, **_k: (object(), False))
    monkeypatch.setattr(d, "mark_running", lambda store, session, events_file: session)
    monkeypatch.setattr(
        d,
        "record_outcome",
        lambda store, session, outcome, events_file: captured.__setitem__("o", outcome),
    )

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("crash after PR open")

    monkeypatch.setattr(d, "run_worker", boom)

    with pytest.raises(RuntimeError):
        d._run_worker_with_saga(
            cfg,
            {"number": 205, "title": "x"},
            {"risk_gated": False, "past_attempts": [], "blocking_comments": []},
            tick=1,
            bus_emit=None,
            store=object(),
            saga_store=None,
            task_id="t",
            capability_policy=None,  # passed straight to run_worker, which we replace
            worktree_path=str(wt),
            branch="loop/205-x",
            maestro_context="",
        )

    assert captured["o"].pr_url == PR
    assert captured["o"].status == "failed"


# ---------------------------------------------------------------------------
# loop_issue_from_branch
# ---------------------------------------------------------------------------


def test_loop_issue_from_branch_matches_loop_only() -> None:
    assert loop_issue_from_branch({"headRefName": "loop/205-add-thing"}) == 205
    assert loop_issue_from_branch({"headRefName": "feature/manual-fix"}) is None
    assert loop_issue_from_branch({"headRefName": ""}) is None
    assert loop_issue_from_branch({}) is None


# ---------------------------------------------------------------------------
# orphaned_clean_pr_adoptions selector (acceptance criteria 2, 3, 4)
# ---------------------------------------------------------------------------


def test_selector_returns_only_clean_adoptable_prs(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    prs = [
        {"number": 1, "url": "u1", "headRefName": "loop/1-a", "labels": []},
        {
            "number": 2,
            "url": "u2",
            "headRefName": "loop/2-b",
            "labels": [{"name": "critic:blocking"}],
        },
        {"number": 3, "url": "u3", "headRefName": "loop/3-c", "labels": []},
        {"number": 4, "url": "u4", "headRefName": "loop/4-d", "labels": []},
        {"number": 5, "url": "u5", "headRefName": "feature/manual", "labels": []},
        {"number": 6, "url": "u6", "headRefName": "loop/6-e", "labels": [{"name": "loop:adopted"}]},
    ]
    issue_states = {1: "open", 2: "open", 3: "closed", 4: "open", 6: "open"}
    issue_labels: dict[int, list[dict[str, str]]] = {4: [{"name": "risk:high"}]}

    def open_prs_fn(limit: int, repo: str | None = None) -> list[dict[str, Any]]:
        return prs

    def fetch_issue_fn(num: int, repo: str | None = None) -> dict[str, Any]:
        return {
            "number": num,
            "title": f"t{num}",
            "state": issue_states.get(num, "open"),
            "labels": issue_labels.get(num, []),
        }

    result = orphaned_clean_pr_adoptions(
        cfg,
        open_prs_fn=open_prs_fn,
        fetch_issue_fn=fetch_issue_fn,
    )
    assert [(o.issue, pr["url"]) for o, pr in result] == [(1, "u1")]
    assert result[0][0].status == "open"

    reasons = {(e["issue"], e["reason"]) for e in _events(cfg) if e["kind"] == "orphan_pr_skipped"}
    assert (2, "critic_blocked") in reasons
    assert (3, "issue_closed") in reasons
    assert (4, "risk_gated") in reasons
    assert (6, "already_adopted") in reasons
    # The human PR (#5, non-loop branch) is silently ignored — never adopted,
    # never even emitted as a skip (it is simply not our PR).
    assert all(e["issue"] != 5 for e in _events(cfg) if e["kind"] == "orphan_pr_skipped")


def test_selector_dedupes_two_prs_for_same_issue(tmp_path: Path) -> None:
    """Adversarial: two open PRs for one issue → only the first is adopted."""
    cfg = _cfg(tmp_path)
    prs = [
        {"number": 10, "url": "a", "headRefName": "loop/7-first", "labels": []},
        {"number": 11, "url": "b", "headRefName": "loop/7-second", "labels": []},
    ]

    def open_prs_fn(limit: int, repo: str | None = None) -> list[dict[str, Any]]:
        return prs

    def fetch_issue_fn(num: int, repo: str | None = None) -> dict[str, Any]:
        return {"number": num, "title": "t", "state": "open", "labels": []}

    result = orphaned_clean_pr_adoptions(
        cfg,
        open_prs_fn=open_prs_fn,
        fetch_issue_fn=fetch_issue_fn,
    )
    assert [pr["url"] for _o, pr in result] == ["a"]
    reasons = [e["reason"] for e in _events(cfg) if e["kind"] == "orphan_pr_skipped"]
    assert "issue_already_selected" in reasons


def test_selector_skips_when_issue_fetch_fails(tmp_path: Path) -> None:
    """Adversarial: fetch_issue returns None → skip with issue_fetch_failed."""
    cfg = _cfg(tmp_path)

    def open_prs_fn(limit: int, repo: str | None = None) -> list[dict[str, Any]]:
        return [{"number": 9, "url": "u", "headRefName": "loop/9-x", "labels": []}]

    def fetch_issue_fn(num: int, repo: str | None = None) -> dict[str, Any] | None:
        return None

    result = orphaned_clean_pr_adoptions(
        cfg,
        open_prs_fn=open_prs_fn,
        fetch_issue_fn=fetch_issue_fn,
    )
    assert result == []
    reasons = [e["reason"] for e in _events(cfg) if e["kind"] == "orphan_pr_skipped"]
    assert reasons == ["issue_fetch_failed"]


# ---------------------------------------------------------------------------
# _enable_automerge_for_adopted_prs — adoption merge gates (sev2b sad paths)
# ---------------------------------------------------------------------------

from forge_loop import gh as _ghmod  # noqa: E402
from forge_loop.runner.tick import (  # noqa: E402
    _enable_automerge_for_adopted_prs,
    _run_adoption_tick,
)
from forge_loop.worker import WorkerOutcome  # noqa: E402


def _outcome(issue: int, *, status: str = "open", error: str | None = None) -> WorkerOutcome:
    return WorkerOutcome(
        issue=issue,
        title=f"t{issue}",
        pr_url=f"https://github.com/o/r/pull/{issue}",
        status=status,
        duration_s=1.0,
        stdout_tail="",
        error=error,
    )


def _patch_gh(monkeypatch, *, threads: list[Any] | None = None) -> list[str]:
    """Stub gh so adoption never touches the network; return the auto-merge log."""
    merged: list[str] = []
    monkeypatch.setattr(_ghmod, "unresolved_review_threads", lambda *_a, **_k: threads or [])
    monkeypatch.setattr(
        _ghmod, "enable_pr_auto_merge", lambda url, repo=None: merged.append(url) or True
    )
    return merged


def test_adopted_automerge_skips_critic_blocked(tmp_path: Path, monkeypatch) -> None:
    """A PR the critic blocked during adoption (outcome.error set) is NOT merged."""
    cfg = _cfg(tmp_path)
    merged = _patch_gh(monkeypatch)
    o = _outcome(50, error="critic blocked merge: [sev1/correctness] ...")
    pr = {"number": 50, "url": o.pr_url, "mergeStateStatus": "CLEAN"}

    _enable_automerge_for_adopted_prs(cfg, [(o, pr)], refused_issues=set(), emit=None)

    assert merged == []  # auto-merge never attempted
    assert o.status == "open"  # not flipped to merged
    reasons = {(e["issue"], e["reason"]) for e in _events(cfg) if e["kind"] == "orphan_pr_skipped"}
    assert (50, "critic_blocked") in reasons


def test_adopted_automerge_skips_not_mergeable(tmp_path: Path, monkeypatch) -> None:
    """A non-CLEAN merge state (BEHIND/DIRTY) is skipped, never merged."""
    cfg = _cfg(tmp_path)
    merged = _patch_gh(monkeypatch)
    o = _outcome(51)
    pr = {"number": 51, "url": o.pr_url, "mergeStateStatus": "BEHIND"}

    _enable_automerge_for_adopted_prs(cfg, [(o, pr)], refused_issues=set(), emit=None)

    assert merged == []
    assert o.status == "open"
    reasons = {(e["issue"], e["reason"]) for e in _events(cfg) if e["kind"] == "orphan_pr_skipped"}
    assert (51, "not_mergeable:behind") in reasons


def test_adopted_automerge_skips_unknown_merge_state(tmp_path: Path, monkeypatch) -> None:
    """sev3a: an absent/empty mergeStateStatus must NOT bypass the CLEAN gate."""
    cfg = _cfg(tmp_path)
    merged = _patch_gh(monkeypatch)
    o = _outcome(52)
    pr = {"number": 52, "url": o.pr_url}  # no mergeStateStatus at all

    _enable_automerge_for_adopted_prs(cfg, [(o, pr)], refused_issues=set(), emit=None)

    assert merged == []  # absent state is treated as not-mergeable
    reasons = {(e["issue"], e["reason"]) for e in _events(cfg) if e["kind"] == "orphan_pr_skipped"}
    assert (52, "not_mergeable:unknown") in reasons


def test_adopted_automerge_happy_path_merges_clean(tmp_path: Path, monkeypatch) -> None:
    """CLEAN + no threads + no critic block → auto-merge enabled, status=merged."""
    cfg = _cfg(tmp_path)
    merged = _patch_gh(monkeypatch)
    o = _outcome(53)
    pr = {"number": 53, "url": o.pr_url, "mergeStateStatus": "CLEAN"}

    _enable_automerge_for_adopted_prs(cfg, [(o, pr)], refused_issues=set(), emit=None)

    assert merged == [o.pr_url]
    assert o.status == "merged"


def test_adoption_tick_stamps_only_terminal_prs(tmp_path: Path, monkeypatch) -> None:
    """sev2a regression: the loop:adopted marker must NOT land on a PR skipped
    for a transient reason, or the selector would permanently re-orphan it.

    One CLEAN PR auto-merges (terminal -> stamped); one BEHIND PR is skipped
    (transient -> NOT stamped, so the next scan re-adopts it once CLEAN).
    """
    cfg = _cfg(tmp_path)
    _patch_gh(monkeypatch)
    stamped: list[str] = []
    monkeypatch.setattr(_ghmod, "add_pr_label", lambda url, labels, repo=None: stamped.append(url))
    # No issue-closed refusals in this scenario.
    monkeypatch.setattr(
        "forge_loop.runner.merge_gate.apply_issue_closed_gate",
        lambda outcomes, **_k: [],
    )

    clean = _outcome(60)
    behind = _outcome(61)
    adoptions = [
        (clean, {"number": 60, "url": clean.pr_url, "mergeStateStatus": "CLEAN"}),
        (behind, {"number": 61, "url": behind.pr_url, "mergeStateStatus": "BEHIND"}),
    ]

    _run_adoption_tick(cfg, 1, adoptions, bus_emit=None, short_sleep=lambda *_a, **_k: None)

    assert clean.status == "merged"
    assert behind.status == "open"
    # Only the merged PR is stamped; the transient-skipped one stays adoptable.
    assert stamped == [clean.pr_url]


# ---------------------------------------------------------------------------
# Issue #230 — approved-mergeable PRs must MERGE, not re-enter the repair loop
# ---------------------------------------------------------------------------


def test_adopted_automerge_merges_clean_despite_sev3_threads(tmp_path: Path, monkeypatch) -> None:
    """#230: an approved + CLEAN PR that still carries unresolved sev3 critic
    inline-comment threads MUST auto-merge — the leftover threads no longer
    gate the merge (that gating caused the #229 multi-hour stall). The thread
    count is recorded on the enabled event for observability."""
    cfg = _cfg(tmp_path)
    sev3 = [{"id": "t-sev3", "isResolved": False, "comments": []}]
    merged = _patch_gh(monkeypatch, threads=sev3)
    o = _outcome(70)
    pr = {"number": 70, "url": o.pr_url, "mergeStateStatus": "CLEAN", "labels": []}

    _enable_automerge_for_adopted_prs(cfg, [(o, pr)], refused_issues=set(), emit=None)

    assert merged == [o.pr_url]  # merged DESPITE the sev3 thread
    assert o.status == "merged"
    enabled = [e for e in _events(cfg) if e["kind"] == "orphan_pr_automerge_enabled"]
    assert len(enabled) == 1
    assert enabled[0]["over_unresolved_sev3_threads"] == 1
    # No skip emitted for unresolved threads anymore.
    skips = [e for e in _events(cfg) if e["kind"] == "orphan_pr_skipped"]
    assert skips == []


def test_adopted_automerge_idempotent_second_pass_is_noop(tmp_path: Path, monkeypatch) -> None:
    """AC4: re-running the adopted-automerge step on an already-merged outcome
    enables auto-merge AT MOST once (the second pass is a terminal no-op)."""
    cfg = _cfg(tmp_path)
    sev3 = [{"id": "t-sev3", "isResolved": False, "comments": []}]
    merged = _patch_gh(monkeypatch, threads=sev3)
    o = _outcome(71)
    pr = {"number": 71, "url": o.pr_url, "mergeStateStatus": "CLEAN", "labels": []}

    _enable_automerge_for_adopted_prs(cfg, [(o, pr)], refused_issues=set(), emit=None)
    # Second pass: outcome.status is now "merged" → skipped at the top guard.
    _enable_automerge_for_adopted_prs(cfg, [(o, pr)], refused_issues=set(), emit=None)

    assert merged == [o.pr_url]  # enable_pr_auto_merge called exactly once
    enabled = [e for e in _events(cfg) if e["kind"] == "orphan_pr_automerge_enabled"]
    assert len(enabled) == 1


def _import_repaired_automerge():
    from forge_loop.runner.repairs import enable_automerge_for_repaired_prs

    return enable_automerge_for_repaired_prs


def test_repaired_automerge_merges_despite_sev3_threads(tmp_path: Path, monkeypatch) -> None:
    """#230: after a repair worker fixes a PR and the re-critic APPROVES it
    (no ``outcome.error``), auto-merge is enabled even though the approve verdict
    left sev3 inline-comment threads open."""
    cfg = _cfg(tmp_path)
    enable_automerge_for_repaired_prs = _import_repaired_automerge()
    merged: list[str] = []
    monkeypatch.setattr(
        _ghmod, "enable_pr_auto_merge", lambda url, repo=None: merged.append(url) or True
    )
    # unresolved_review_threads must NOT be consulted any more — make it explode
    # if it ever is, to prove the gate is truly gone.
    monkeypatch.setattr(
        _ghmod,
        "unresolved_review_threads",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("thread gate must be gone")),
    )
    monkeypatch.setattr(
        "forge_loop.runner.merge_gate.apply_issue_closed_gate", lambda outcomes, **_k: []
    )
    o = _outcome(72)  # status=open, error=None → approved

    enable_automerge_for_repaired_prs(cfg, [o], lambda *_a, **_k: None)

    assert merged == [o.pr_url]
    assert o.status == "merged"
    assert "repair_automerge_enabled" in cfg.events_file.read_text()


def test_repaired_automerge_skips_when_critic_reblocked(tmp_path: Path, monkeypatch) -> None:
    """Regression guard: a repaired PR the re-critic RE-BLOCKED (``outcome.error``
    set) is NOT auto-merged — it stays for the repair loop."""
    cfg = _cfg(tmp_path)
    enable_automerge_for_repaired_prs = _import_repaired_automerge()
    merged: list[str] = []
    monkeypatch.setattr(
        _ghmod, "enable_pr_auto_merge", lambda url, repo=None: merged.append(url) or True
    )
    monkeypatch.setattr(
        "forge_loop.runner.merge_gate.apply_issue_closed_gate", lambda outcomes, **_k: []
    )
    o = _outcome(73, error="critic blocked merge: [sev1/correctness] boom")

    enable_automerge_for_repaired_prs(cfg, [o], lambda *_a, **_k: None)

    assert merged == []  # never auto-merged
    assert o.status == "open"
    reasons = {
        (e["issue"], e["reason"]) for e in _events(cfg) if e["kind"] == "repair_automerge_skipped"
    }
    assert (73, "critic_blocked") in reasons


def test_blocking_pr_repairs_emits_skip_for_approved_mergeable(tmp_path: Path) -> None:
    """#230 AC1/AC5: when the selector excludes an approved-mergeable PR (via the
    ``on_skip`` callback), ``blocking_pr_repairs`` emits a structured
    ``repair_pr_skipped`` event with ``reason="approved_mergeable"`` and does
    NOT add it to the repair set (no repair worker)."""
    from forge_loop.runner.repairs import blocking_pr_repairs

    cfg = _cfg(tmp_path)
    skipped_pr = {
        "number": 229,
        "url": "https://github.com/o/r/pull/229",
        "headRefName": "loop/229-debt-fix",
        "approvedMergeableSkip": True,
    }

    def fake_selector(limit, repo=None, *, on_skip=None):
        # The real selector excludes the approved-mergeable PR and notifies via
        # on_skip; here we mimic exactly that contract.
        if on_skip is not None:
            on_skip(skipped_pr)
        return []

    repairs = blocking_pr_repairs(
        cfg,
        prs_requiring_repair_fn=fake_selector,
        fetch_issue_fn=lambda *_a, **_k: {"number": 229, "title": "t", "labels": []},
        pr_review_context_fn=lambda *_a, **_k: "ctx",
    )

    assert repairs == []  # no repair worker for the approved PR
    skips = {
        (e.get("pr"), e.get("reason")) for e in _events(cfg) if e["kind"] == "repair_pr_skipped"
    }
    assert (skipped_pr["url"], "approved_mergeable") in skips


def test_blocking_pr_repairs_selects_blocked_but_skips_approved_in_same_batch(
    tmp_path: Path,
) -> None:
    """E2E adversarial (#230): in ONE batch carrying both a still-``critic:blocking``
    PR and an approved-mergeable PR, the blocked one IS selected for a repair
    worker while the approved one is skipped — proving the fix doesn't disable
    repair wholesale (and the #229 stall cannot recur for the approved PR)."""
    from forge_loop.runner.repairs import blocking_pr_repairs

    cfg = _cfg(tmp_path)
    blocked_pr = {
        "number": 300,
        "url": "https://github.com/o/r/pull/300",
        "headRefName": "loop/300-blocked",
        "repairReasons": ["critic:blocking"],
    }
    approved_pr = {
        "number": 229,
        "url": "https://github.com/o/r/pull/229",
        "headRefName": "loop/229-debt-fix",
        "approvedMergeableSkip": True,
    }

    def fake_selector(limit, repo=None, *, on_skip=None):
        if on_skip is not None:
            on_skip(approved_pr)
        return [blocked_pr]

    repairs = blocking_pr_repairs(
        cfg,
        prs_requiring_repair_fn=fake_selector,
        fetch_issue_fn=lambda num, repo=None: {"number": num, "title": "t", "labels": []},
        pr_review_context_fn=lambda *_a, **_k: "ctx",
    )

    # The blocked PR gets a repair; the approved PR does not.
    assert [pr["number"] for _issue, pr, _ctx in repairs] == [300]
    events = _events(cfg)
    assert any(
        e["kind"] == "repair_pr_skipped" and e.get("reason") == "approved_mergeable" for e in events
    )
    assert any(
        e["kind"] == "repair_pr_selected" and e.get("pr") == blocked_pr["url"] for e in events
    )
