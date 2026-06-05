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
from forge_loop.gh_client import MockGhClient
from forge_loop.runner.repairs import loop_issue_from_branch, orphaned_clean_pr_adoptions
from tests.conftest import make_critic_thread, make_human_thread

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

from forge_loop import gh_issues as _ghmod  # noqa: E402
from forge_loop.gh_issues import MergeOutcome as _MergeOutcome  # noqa: E402
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


# Centralised in conftest (#230 sev3/tests): the critic body is derived from
# the real ``critic_format.finding_tag`` so a producer drift breaks these tests.
def _critic_thread(id_: str = "t-sev3") -> dict[str, Any]:
    return make_critic_thread(id_, sev="sev3", category="style")


def _human_thread(id_: str = "t-human") -> dict[str, Any]:
    return make_human_thread(id_)


def _patch_gh(monkeypatch, *, threads: list[Any] | None = None) -> list[str]:
    """Stub gh so adoption never touches the network; return the merge log."""
    merged: list[str] = []
    monkeypatch.setattr(_ghmod, "unresolved_review_threads", lambda *_a, **_k: threads or [])
    monkeypatch.setattr(
        _ghmod,
        "ensure_pr_merged",
        lambda url, repo=None: (merged.append(url), _MergeOutcome(True, "auto"))[1],
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


def test_adopted_automerge_skips_critic_error_verdict(tmp_path: Path, monkeypatch) -> None:
    """Issue #267: a verdict=error adopted PR is NOT merged (allow-list dual).

    The #267 hole: a crashed critic review sets status=open WITHOUT
    ``outcome.error`` (an error is not an adjudicated block), so the
    ``if outcome.error`` deny-list above does NOT catch it. Without the
    allow-list it would auto-merge an UNREVIEWED PR. It must be withheld and
    left UNstamped for the next adoption scan to re-review.
    """
    cfg = _cfg(tmp_path)
    merged = _patch_gh(monkeypatch)
    o = _outcome(54)
    o.critic_verdict = "error"  # crashed review — no affirmative approval
    pr = {"number": 54, "url": o.pr_url, "mergeStateStatus": "CLEAN"}

    _enable_automerge_for_adopted_prs(cfg, [(o, pr)], refused_issues=set(), emit=None)

    assert merged == []  # unreviewed PR never merged
    assert o.status == "open"
    reasons = {(e["issue"], e["reason"]) for e in _events(cfg) if e["kind"] == "orphan_pr_skipped"}
    assert (54, "critic_verdict_not_approved:error") in reasons


def test_adopted_automerge_merges_approved_verdict(tmp_path: Path, monkeypatch) -> None:
    """Issue #267 happy path: an affirmatively-approved adopted PR still merges."""
    cfg = _cfg(tmp_path)
    merged = _patch_gh(monkeypatch)
    o = _outcome(55)
    o.critic_verdict = "approved"
    pr = {"number": 55, "url": o.pr_url, "mergeStateStatus": "CLEAN"}

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
    sev3 = [_critic_thread()]
    merged = _patch_gh(monkeypatch, threads=sev3)
    o = _outcome(70)
    pr = {"number": 70, "url": o.pr_url, "mergeStateStatus": "CLEAN", "labels": []}

    _enable_automerge_for_adopted_prs(cfg, [(o, pr)], refused_issues=set(), emit=None)

    assert merged == [o.pr_url]  # merged DESPITE the sev3 critic thread
    assert o.status == "merged"
    enabled = [e for e in _events(cfg) if e["kind"] == "orphan_pr_automerge_enabled"]
    assert len(enabled) == 1
    assert enabled[0]["over_unresolved_critic_threads"] == 1
    # No skip emitted for leftover critic threads anymore.
    skips = [e for e in _events(cfg) if e["kind"] == "orphan_pr_skipped"]
    assert skips == []


def test_adopted_automerge_skips_unresolved_human_thread(tmp_path: Path, monkeypatch) -> None:
    """#230 AC3: an approved + CLEAN PR carrying an unresolved *human*
    request-changes thread is NOT auto-merged — a human inline comment leaves
    merge state CLEAN, so we must inspect authorship. It skips with
    ``reason="human_review_unresolved"`` (not silently dropped)."""
    cfg = _cfg(tmp_path)
    merged = _patch_gh(monkeypatch, threads=[_human_thread(), _critic_thread()])
    o = _outcome(74)
    pr = {"number": 74, "url": o.pr_url, "mergeStateStatus": "CLEAN", "labels": []}

    _enable_automerge_for_adopted_prs(cfg, [(o, pr)], refused_issues=set(), emit=None)

    assert merged == []  # held back by the human thread
    assert o.status == "open"
    reasons = {(e["issue"], e["reason"]) for e in _events(cfg) if e["kind"] == "orphan_pr_skipped"}
    assert (74, "human_review_unresolved") in reasons


def test_adopted_automerge_idempotent_second_pass_is_noop(tmp_path: Path, monkeypatch) -> None:
    """AC4: re-running the adopted-automerge step on an already-merged outcome
    enables auto-merge AT MOST once (the second pass is a terminal no-op)."""
    cfg = _cfg(tmp_path)
    sev3 = [_critic_thread()]
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
        _ghmod,
        "ensure_pr_merged",
        lambda url, repo=None: (merged.append(url), _MergeOutcome(True, "auto"))[1],
    )
    # The leftover threads are the critic's OWN sev3 notes — they must NOT hold
    # the PR back (that gating caused the #229 stall).
    monkeypatch.setattr(_ghmod, "unresolved_review_threads", lambda *_a, **_k: [_critic_thread()])
    monkeypatch.setattr(
        "forge_loop.runner.merge_gate.apply_issue_closed_gate", lambda outcomes, **_k: []
    )
    o = _outcome(72)  # status=open, error=None → approved

    enable_automerge_for_repaired_prs(cfg, [o], lambda *_a, **_k: None)

    assert merged == [o.pr_url]
    assert o.status == "merged"
    assert "repair_automerge_enabled" in cfg.events_file.read_text()


def test_repaired_automerge_skips_unresolved_human_thread(tmp_path: Path, monkeypatch) -> None:
    """#230 AC3: a repaired PR the re-critic APPROVED but which still carries an
    unresolved *human* request-changes thread is NOT auto-merged — it stays for
    the repair loop with ``reason="human_review_unresolved"``."""
    cfg = _cfg(tmp_path)
    enable_automerge_for_repaired_prs = _import_repaired_automerge()
    merged: list[str] = []
    monkeypatch.setattr(
        _ghmod,
        "ensure_pr_merged",
        lambda url, repo=None: (merged.append(url), _MergeOutcome(True, "auto"))[1],
    )
    monkeypatch.setattr(
        _ghmod, "unresolved_review_threads", lambda *_a, **_k: [_human_thread(), _critic_thread()]
    )
    monkeypatch.setattr(
        "forge_loop.runner.merge_gate.apply_issue_closed_gate", lambda outcomes, **_k: []
    )
    o = _outcome(75)  # approved (no error) but human thread open

    enable_automerge_for_repaired_prs(cfg, [o], lambda *_a, **_k: None)

    assert merged == []
    assert o.status == "open"
    reasons = {
        (e["issue"], e["reason"]) for e in _events(cfg) if e["kind"] == "repair_automerge_skipped"
    }
    assert (75, "human_review_unresolved") in reasons


def test_repaired_automerge_skips_when_critic_reblocked(tmp_path: Path, monkeypatch) -> None:
    """Regression guard: a repaired PR the re-critic RE-BLOCKED (``outcome.error``
    set) is NOT auto-merged — it stays for the repair loop."""
    cfg = _cfg(tmp_path)
    enable_automerge_for_repaired_prs = _import_repaired_automerge()
    merged: list[str] = []
    monkeypatch.setattr(
        _ghmod,
        "ensure_pr_merged",
        lambda url, repo=None: (merged.append(url), _MergeOutcome(True, "auto"))[1],
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


# ---------------------------------------------------------------------------
# #230 sev2/tests — drive the REAL prs_requiring_repair through the tick wiring
# (no fake_selector substitution): prove the #229 5h stall cannot recur E2E.
# ---------------------------------------------------------------------------


def _open_pr(num: int, *, blocked: bool = False, state: str = "CLEAN") -> dict[str, Any]:
    return {
        "number": num,
        "title": f"t{num}",
        "body": f"fixes #{num}",
        "headRefName": f"loop/{num}-{'blocked' if blocked else 'debt-fix'}",
        "baseRefName": "trunk",
        "url": f"https://github.com/o/r/pull/{num}",
        "labels": [{"name": "critic:blocking"}] if blocked else [],
        "updatedAt": f"2026-06-04T19:0{num % 10}:00Z",
        "mergeStateStatus": state,
    }


def test_real_selector_excludes_approved_pr_across_n_ticks(tmp_path: Path, monkeypatch) -> None:
    """E2E (#230 sev2/tests): the REAL ``prs_requiring_repair`` — not a fake
    selector — excludes an approved + CLEAN PR whose only open threads are
    leftover critic sev3 inline comments, across N consecutive ticks, so NO
    repair worker is ever dispatched against it (the #229 ~5h stall cannot
    recur). A sibling ``critic:blocking`` PR in the same batch IS selected each
    tick, proving repair is not disabled wholesale."""
    from forge_loop.gh_client import MockGhClient
    from forge_loop.runner.repairs import blocking_pr_repairs

    cfg = _cfg(tmp_path)
    open_list = [_open_pr(229), _open_pr(300, blocked=True)]
    critic = _critic_thread()
    monkeypatch.setattr(
        _ghmod,
        "_GH_CLIENT",
        MockGhClient(
            open_prs_response=list(open_list),
            review_threads_by_pr={229: [critic], 300: [critic]},
        ),
    )

    for _ in range(3):  # N consecutive ticks
        repairs = blocking_pr_repairs(
            cfg,
            fetch_issue_fn=lambda num, repo=None: {"number": num, "title": "t", "labels": []},
            pr_review_context_fn=lambda *_a, **_k: "ctx",
        )
        nums = [pr["number"] for _i, pr, _c in repairs]
        assert 229 not in nums  # approved PR NEVER dispatched to a repair worker
        assert nums == [300]  # blocked sibling IS selected

    approved_skips = [
        e
        for e in _events(cfg)
        if e["kind"] == "repair_pr_skipped" and e.get("reason") == "approved_mergeable"
    ]
    assert len(approved_skips) == 3  # surfaced every tick (no silent drop)
    assert all(str(e.get("pr", "")).endswith("/229") for e in approved_skips)


def test_tick_merges_approved_pr_and_dispatches_no_repair(tmp_path: Path, monkeypatch) -> None:
    """Integration (#230 AC2 ordering): within a tick, an approved + CLEAN PR
    with only critic sev3 threads is EXCLUDED by the REAL repair selector (no
    repair worker) AND landed by the adoption enable-merge path. In ``_tick``
    the selector runs first but excludes the PR (defence in depth), so merge
    always wins over repair regardless of step order — the approved PR can never
    be dispatched to a repair worker."""
    from forge_loop.gh_client import MockGhClient
    from forge_loop.runner.repairs import blocking_pr_repairs

    cfg = _cfg(tmp_path)
    approved = _open_pr(229)
    critic = _critic_thread()
    monkeypatch.setattr(
        _ghmod,
        "_GH_CLIENT",
        MockGhClient(
            open_prs_response=[approved],
            review_threads_by_pr={229: [critic]},
        ),
    )

    # Repair selector (real): the approved PR is NOT selected for repair.
    repairs = blocking_pr_repairs(
        cfg,
        fetch_issue_fn=lambda num, repo=None: {"number": num, "title": "t", "labels": []},
        pr_review_context_fn=lambda *_a, **_k: "ctx",
    )
    assert repairs == []  # zero repair workers dispatched

    # Adoption enable-merge step: the same PR lands.
    merged = _patch_gh(monkeypatch, threads=[critic])
    o = _outcome(229)
    _enable_automerge_for_adopted_prs(cfg, [(o, approved)], refused_issues=set(), emit=None)

    assert merged == [o.pr_url]  # merge enabled exactly once
    assert o.status == "merged"
    assert any(e["kind"] == "orphan_pr_automerge_enabled" for e in _events(cfg))


class _StampingMockGh(MockGhClient):  # type: ignore[misc, valid-type]
    """A ``MockGhClient`` whose ``add_pr_label`` also mutates the in-memory open
    PR so a *subsequent* tick's selectors observe the stamped label — letting us
    prove terminal idempotency (``loop:adopted``) through the REAL tick path."""

    def add_pr_label(self, owner: str, repo: str, number: int, labels: list[str]) -> bool:
        for pr in self.open_prs_response:
            if pr.get("number") == number:
                pr["labels"] = [*(pr.get("labels") or []), *({"name": lbl} for lbl in labels)]
        return super().add_pr_label(owner, repo, number, labels)


def test_pre_dispatch_repairs_merges_approved_pr_no_repair_worker_across_n_ticks(
    tmp_path: Path, monkeypatch
) -> None:
    """E2E ordering (#230 sev2/tests): drive the REAL ``_run_pre_dispatch_repairs``
    — the function that owns the within-tick ordering of the repair selector vs
    the adoption enable-merge step — not the two helpers in isolation.

    Across N consecutive ticks against an approved + CLEAN PR (#229) that still
    carries leftover sev3 *critic* threads:
      * the repair-worker dispatch (``_run_repair_workers``) is NEVER invoked —
        the selector excludes the PR, so the #229 5-hour repair-stall cannot
        recur;
      * the adoption enable-merge path lands it, and auto-merge is enabled
        EXACTLY ONCE (the ``loop:adopted`` stamp makes every later tick a
        terminal no-op — AC4);
      * a ``repair_pr_skipped reason=approved_mergeable`` event is surfaced
        every tick (no silent drop — AC1/AC5).
    """
    from forge_loop.gh_client import Issue
    from forge_loop.runner import tick as tickmod

    cfg = _cfg(tmp_path)  # critic disabled → adoption skips re-critic
    approved = _open_pr(229)
    client = _StampingMockGh(
        open_prs_response=[approved],
        review_threads_by_pr={229: [_critic_thread()]},
        issues={("o", "r", 229): Issue(number=229, title="t229", state="open")},
    )
    monkeypatch.setattr(_ghmod, "_GH_CLIENT", client)

    # Spy: a repair worker must NEVER be dispatched against the approved PR.
    repair_calls: list[Any] = []
    monkeypatch.setattr(
        tickmod, "_run_repair_workers", lambda *a, **k: repair_calls.append(a) or []
    )
    # Keep the stuck-issue sweep off the network in the test.
    monkeypatch.setattr(tickmod, "_run_stuck_sweep", lambda *_a, **_k: None)

    for _ in range(3):
        tickmod._run_pre_dispatch_repairs(
            cfg, 1, bus_emit=None, short_sleep=lambda *_a, **_k: None
        )

    assert repair_calls == []  # zero repair-worker dispatches, ever

    enable_calls = [c for c in client.calls if c[0] == "enable_pr_auto_merge"]
    assert len(enable_calls) == 1  # auto-merge enabled exactly once (idempotent)
    assert enable_calls[0][1]["number"] == 229

    skips = [
        e
        for e in _events(cfg)
        if e["kind"] == "repair_pr_skipped" and e.get("reason") == "approved_mergeable"
    ]
    assert len(skips) == 3  # surfaced every tick — no silent drop
    assert any(e["kind"] == "orphan_pr_automerge_enabled" for e in _events(cfg))
    # Second/third ticks are terminal no-ops: the stamped PR is skipped as
    # already-adopted rather than re-enabled.
    assert any(
        e["kind"] == "orphan_pr_skipped" and e.get("reason") == "already_adopted"
        for e in _events(cfg)
    )
