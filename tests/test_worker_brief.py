"""Tests for worker brief rendering."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from forge_loop.memory.models import MemoryItem, MemoryKind, MemoryProvenance
from forge_loop.memory.store import SqliteMemoryStore
from forge_loop.sandbox import CapabilityPolicy, FilesystemScope, McpGrant, NetworkPolicy
from forge_loop.worker import make_brief, make_repair_brief

_EPISODE_NOW = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)


def test_make_brief_includes_issue_number_and_body(tmp_path: Path) -> None:
    issue = {"number": 947, "title": "feat(pdl): onFailure", "body": "Some body text"}
    brief = make_brief(issue, tmp_path / "wt-947")
    assert "#947" in brief
    assert "feat(pdl): onFailure" in brief
    assert "Some body text" in brief
    assert str(tmp_path / "wt-947") in brief
    assert "CONTRACT" in brief


def test_make_brief_reflects_granted_capabilities(tmp_path: Path) -> None:
    issue = {"number": 166, "title": "bind worker policy", "body": "Ship it."}
    policy = CapabilityPolicy(
        filesystem=FilesystemScope(
            read_roots=("/repo", str(tmp_path / "wt-166")),
            write_roots=(str(tmp_path / "wt-166"),),
        ),
        network=NetworkPolicy(allow_domains=("github.com", "api.github.com")),
        mcp=(McpGrant(server="github", tools=("*",)), McpGrant(server="lumen", tools=("search",))),
        secret_names=("GITHUB_TOKEN",),
    )

    brief = make_brief(issue, tmp_path / "wt-166", capability_policy=policy)

    assert "CAPABILITY POLICY" in brief
    assert f"filesystem read: /repo, {tmp_path / 'wt-166'}" in brief
    assert f"filesystem write: {tmp_path / 'wt-166'}" in brief
    assert "network: deny-by-default; allow github.com, api.github.com" in brief
    assert "mcp: github (*), lumen (search)" in brief
    assert "secrets: GITHUB_TOKEN" in brief


def test_make_brief_caps_body_at_6000_chars(tmp_path: Path) -> None:
    long_body = "a" * 10000
    brief = make_brief({"number": 1, "title": "x", "body": long_body}, tmp_path / "w")
    assert brief.count("a") <= 6500


def test_make_brief_includes_history_section_when_past_attempts(tmp_path: Path) -> None:
    issue = {"number": 942, "title": "fix x", "body": "Do the thing."}
    past = [
        {"ts": "2026-05-26T10:00:00Z", "status": "failed", "note": "test missing", "pr_url": None},
        {
            "ts": "2026-05-26T11:00:00Z",
            "status": "merged",
            "note": "shipped",
            "pr_url": "https://github.com/h/r/pull/9",
        },
    ]
    brief = make_brief(issue, tmp_path / "w", past_attempts=past)
    assert "PREVIOUS ATTEMPTS" in brief
    assert "test missing" in brief
    assert "https://github.com/h/r/pull/9" in brief


def test_make_brief_promotes_critic_blockers_to_hard_contract(tmp_path: Path) -> None:
    issue = {"number": 23, "title": "AR4 proof", "body": "Do AR4."}
    brief = make_brief(
        issue,
        tmp_path / "w",
        blocking_comments=[
            "Post-merge critic found AR4 is not actually complete.\n"
            "Required repair: add a test with at least two candidate summaries."
        ],
    )

    assert "CRITIC / OPERATOR BLOCKERS" in brief
    assert "HARD ACCEPTANCE CONTRACT" in brief
    assert "two candidate summaries" in brief
    assert "Do not satisfy this issue with adjacent cleanup" in brief


def test_make_brief_no_history_section_when_empty(tmp_path: Path) -> None:
    issue = {"number": 942, "title": "fix x", "body": ""}
    brief = make_brief(issue, tmp_path / "w", past_attempts=[])
    assert "PREVIOUS ATTEMPTS" not in brief


def test_make_brief_risk_gated_disables_automerge(tmp_path: Path) -> None:
    issue = {"number": 942, "title": "fix x", "body": ""}
    brief = make_brief(issue, tmp_path / "w", risk_gated=True)
    assert "DO NOT enable auto-merge" in brief
    assert "ready for human review" in brief
    assert "Fixes #942" in brief
    assert '"status": "open"' in brief


def test_make_brief_default_stops_before_automerge(tmp_path: Path) -> None:
    issue = {"number": 942, "title": "fix x", "body": ""}
    brief = make_brief(issue, tmp_path / "w")
    assert "DO NOT enable auto-merge" in brief
    assert "DO NOT merge the PR" in brief
    assert "owns merge after critic approval" in brief
    assert "Fixes #942" in brief
    assert '"status": "open|failed"' in brief


def test_make_brief_reflects_granted_capability_policy(tmp_path: Path) -> None:
    issue = {"number": 166, "title": "policy", "body": ""}
    policy = CapabilityPolicy(
        filesystem=FilesystemScope(
            read_roots=("/repo", "/tmp/wt-loop-166"),
            write_roots=("/tmp/wt-loop-166",),
        ),
        network=NetworkPolicy(allow_domains=("github.com", "api.github.com")),
        mcp=(McpGrant(server="github", tools=("*",)),),
        secret_names=("GITHUB_TOKEN",),
    )

    brief = make_brief(issue, tmp_path / "wt-166", capability_policy=policy)

    assert "CAPABILITY POLICY:" in brief
    assert "- filesystem read: /repo, /tmp/wt-loop-166" in brief
    assert "- filesystem write: /tmp/wt-loop-166" in brief
    assert "- network: deny-by-default; allow github.com, api.github.com" in brief
    assert "- mcp: github (*)" in brief
    assert "- secrets: GITHUB_TOKEN" in brief


def test_make_repair_brief_keeps_same_pr_contract(tmp_path: Path) -> None:
    issue = {"number": 42, "title": "fix blocked pr", "body": "Acceptance"}
    pr = {
        "number": 7,
        "url": "https://github.com/o/r/pull/7",
        "headRefName": "loop/42-fix-blocked-pr",
    }
    brief = make_repair_brief(
        issue,
        tmp_path / "wt",
        pr=pr,
        review_context="[sev1] fix the real consumer",
    )
    assert "Repair the EXISTING PR branch" in brief
    assert "Do not create a new branch" in brief
    assert "https://github.com/o/r/pull/7" in brief
    assert "[sev1] fix the real consumer" in brief
    assert '"pr": "https://github.com/o/r/pull/7"' in brief


# ---------------------------------------------------------------------------
# Worker environment contract — canonical verify section (2026-06-05 incident).
# ---------------------------------------------------------------------------


def test_make_brief_injects_verify_commands(tmp_path: Path) -> None:
    issue = {"number": 5, "title": "x", "body": "y"}
    brief = make_brief(
        issue,
        tmp_path / "w",
        verify_commands=("ruff check src tests", "pyright src/forge_loop"),
    )
    assert "DEFINITION OF DONE" in brief
    # The EXACT commands appear so the worker never guesses variants.
    assert "`ruff check src tests`" in brief
    assert "`pyright src/forge_loop`" in brief


def test_make_brief_no_verify_section_when_unset(tmp_path: Path) -> None:
    brief = make_brief({"number": 5, "title": "x", "body": "y"}, tmp_path / "w")
    assert "DEFINITION OF DONE" not in brief


def test_make_repair_brief_injects_verify_commands(tmp_path: Path) -> None:
    issue = {"number": 42, "title": "fix", "body": "z"}
    pr = {"number": 7, "url": "https://x/pull/7", "headRefName": "loop/42-fix"}
    brief = make_repair_brief(
        issue,
        tmp_path / "wt",
        pr=pr,
        review_context="ctx",
        verify_commands=("python -m pytest -q",),
    )
    assert "DEFINITION OF DONE" in brief
    assert "`python -m pytest -q`" in brief


# ---------------------------------------------------------------------------
# Upfront SCOPE DISCIPLINE — the #261 convergence failure (PR grew, not shrank).
# ---------------------------------------------------------------------------


def test_make_brief_includes_scope_discipline_with_default_cap(tmp_path: Path) -> None:
    brief = make_brief({"number": 5, "title": "x", "body": "y"}, tmp_path / "w")
    assert "SCOPE DISCIPLINE" in brief
    assert "ONE mechanism" in brief
    # The default cap (150) is cited as a concrete soft net-LOC ceiling.
    assert "~150 LOC" in brief
    # The minimal-diff philosophy line is present.
    assert "shrinks under review, it never grows" in brief


def test_make_brief_scope_cap_number_is_configurable(tmp_path: Path) -> None:
    brief = make_brief(
        {"number": 5, "title": "x", "body": "y"},
        tmp_path / "w",
        scope_soft_loc_cap=42,
    )
    assert "~42 LOC" in brief
    assert "~150 LOC" not in brief


def test_make_brief_scope_cap_zero_drops_the_number(tmp_path: Path) -> None:
    brief = make_brief(
        {"number": 5, "title": "x", "body": "y"},
        tmp_path / "w",
        scope_soft_loc_cap=0,
    )
    # Single-mechanism prose survives; no LOC number is cited.
    assert "SCOPE DISCIPLINE" in brief
    assert "ONE mechanism" in brief
    assert "LOC:" not in brief
    assert "~0 LOC" not in brief


def test_make_repair_brief_includes_cut_not_grow_directive(tmp_path: Path) -> None:
    issue = {"number": 261, "title": "fix", "body": "z"}
    pr = {"number": 7, "url": "https://x/pull/7", "headRefName": "loop/261-fix"}
    brief = make_repair_brief(
        issue,
        tmp_path / "wt",
        pr=pr,
        review_context="ctx",
    )
    assert "SCOPE DISCIPLINE" in brief
    # The repair variant MUST tell the worker to CUT, not grow (the #261 mode).
    assert "THIS IS A REPAIR" in brief
    assert "CUT, do not grow" in brief
    assert "never ship a larger diff than you started with" in brief


# ---------------------------------------------------------------------------
# PRIOR ATTEMPTS / LESSONS — inject prior episodic memory into the repair brief
# (#349). The repair worker re-learning a dead-end from scratch is the failure.
# ---------------------------------------------------------------------------


def _episodic_item(
    memory_id: str,
    *,
    issue: int,
    title: str,
    body: str,
    tag: str,
) -> MemoryItem:
    """Build an EPISODIC memory item mirroring runner.learning's write shape."""
    return MemoryItem(
        memory_id=memory_id,
        kind=MemoryKind.EPISODIC,
        title=title,
        body=body,
        provenance=MemoryProvenance(
            source_event=None,
            authored_by="maestro",
            source_task_ref=f"issue:#{issue}",
            confidence=1.0,
            created_at=_EPISODE_NOW,
        ),
        tags=(tag,),
    )


def test_make_repair_brief_no_store_is_byte_identical(tmp_path: Path) -> None:
    issue = {"number": 349, "title": "fix", "body": "z"}
    pr = {"number": 7, "url": "https://x/pull/7", "headRefName": "loop/349-fix"}
    baseline = make_repair_brief(issue, tmp_path / "wt", pr=pr, review_context="ctx")
    with_none = make_repair_brief(
        issue, tmp_path / "wt", pr=pr, review_context="ctx", memory_store=None
    )
    # Default (no kwarg) and explicit None must both be byte-identical, and must
    # not render an empty PRIOR ATTEMPTS header.
    assert with_none == baseline
    assert "PRIOR ATTEMPTS" not in baseline


def test_make_repair_brief_empty_store_is_byte_identical(tmp_path: Path) -> None:
    issue = {"number": 349, "title": "fix", "body": "z"}
    pr = {"number": 7, "url": "https://x/pull/7", "headRefName": "loop/349-fix"}
    baseline = make_repair_brief(issue, tmp_path / "wt", pr=pr, review_context="ctx")
    store = SqliteMemoryStore(tmp_path / "memory.db")
    with_empty = make_repair_brief(
        issue, tmp_path / "wt", pr=pr, review_context="ctx", memory_store=store
    )
    assert with_empty == baseline


def test_make_repair_brief_injects_active_failed_episode(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")
    lesson = "repair enlarged the diff instead of cutting it"
    store.put(
        _episodic_item(
            "episodic-failed-261",
            issue=261,
            title="failed #261: critic bounced twice",
            body=lesson,
            tag="failed",
        )
    )
    issue = {"number": 261, "title": "fix", "body": "z"}
    pr = {"number": 7, "url": "https://x/pull/7", "headRefName": "loop/261-fix"}
    brief = make_repair_brief(
        issue, tmp_path / "wt", pr=pr, review_context="ctx", memory_store=store
    )
    assert "PRIOR ATTEMPTS / LESSONS" in brief
    assert lesson in brief
    assert "failed #261: critic bounced twice" in brief


def test_make_repair_brief_injects_both_failed_and_shipped(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.put(
        _episodic_item(
            "episodic-failed-261",
            issue=261,
            title="failed #261",
            body="FAILED-BODY-MARKER",
            tag="failed",
        )
    )
    store.put(
        _episodic_item(
            "episodic-shipped-261",
            issue=261,
            title="shipped #261",
            body="SHIPPED-BODY-MARKER",
            tag="shipped",
        )
    )
    issue = {"number": 261, "title": "fix", "body": "z"}
    pr = {"number": 7, "url": "https://x/pull/7", "headRefName": "loop/261-fix"}
    brief = make_repair_brief(
        issue, tmp_path / "wt", pr=pr, review_context="ctx", memory_store=store
    )
    assert "FAILED-BODY-MARKER" in brief
    assert "SHIPPED-BODY-MARKER" in brief
    # Failed must render before shipped.
    assert brief.index("FAILED-BODY-MARKER") < brief.index("SHIPPED-BODY-MARKER")


def test_make_repair_brief_truncates_long_episode_body(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")
    long_body = "Q" * 5000
    store.put(
        _episodic_item(
            "episodic-failed-261",
            issue=261,
            title="failed #261",
            body=long_body,
            tag="failed",
        )
    )
    issue = {"number": 261, "title": "fix", "body": "z"}
    pr = {"number": 7, "url": "https://x/pull/7", "headRefName": "loop/261-fix"}
    brief = make_repair_brief(
        issue, tmp_path / "wt", pr=pr, review_context="ctx", memory_store=store
    )
    # The truncation marker is present and the full body is absent.
    assert "…[truncated]" in brief
    assert long_body not in brief
    # The rendered run of Q's is bounded by the per-episode body cap.
    assert brief.count("Q") <= 600


def test_make_repair_brief_skips_superseded_failed_episode(tmp_path: Path) -> None:
    store = SqliteMemoryStore(tmp_path / "memory.db")
    store.put(
        _episodic_item(
            "episodic-failed-261",
            issue=261,
            title="failed #261",
            body="STALE-FAILED-LESSON",
            tag="failed",
        )
    )
    # Later the ticket merged → the failed episode is superseded by the shipped one.
    store.put(
        _episodic_item(
            "episodic-shipped-261",
            issue=261,
            title="shipped #261",
            body="shipped successfully",
            tag="shipped",
        )
    )
    store.supersede("episodic-failed-261", by_memory_id="episodic-shipped-261")
    issue = {"number": 261, "title": "fix", "body": "z"}
    pr = {"number": 7, "url": "https://x/pull/7", "headRefName": "loop/261-fix"}
    brief = make_repair_brief(
        issue, tmp_path / "wt", pr=pr, review_context="ctx", memory_store=store
    )
    # The superseded failed episode must NOT appear...
    assert "STALE-FAILED-LESSON" not in brief
    # ...but the still-active shipped episode does.
    assert "shipped successfully" in brief


def test_make_repair_brief_store_raising_degrades_to_no_section(tmp_path: Path) -> None:
    class _RaisingStore:
        def list_active(self, *, kind: object | None = None) -> tuple[MemoryItem, ...]:
            raise RuntimeError("db is wedged")

    issue = {"number": 349, "title": "fix", "body": "z"}
    pr = {"number": 7, "url": "https://x/pull/7", "headRefName": "loop/349-fix"}
    baseline = make_repair_brief(issue, tmp_path / "wt", pr=pr, review_context="ctx")
    degraded = make_repair_brief(
        issue,
        tmp_path / "wt",
        pr=pr,
        review_context="ctx",
        memory_store=_RaisingStore(),  # type: ignore[arg-type]
    )
    # A raising store yields the byte-identical no-episode brief, not a crash.
    assert degraded == baseline
    assert "PRIOR ATTEMPTS" not in degraded
