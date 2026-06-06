"""Tests for ``forge_loop.brainstormer`` — issue #123."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from forge_loop._testing.memory_store import FakeMemoryStore
from forge_loop.brainstormer import (
    Brainstormer,
    BrainstormReport,
    ProposedEpic,
    ProposedTicket,
    _parse_sdk_payload,
    _render_axes_block,
    _render_backlog_block,
    _render_rejected_paths_block,
)
from forge_loop.gh_client import Issue, MockGhClient, OpenBacklog, list_open_backlog
from forge_loop.memory.models import (
    REJECTED_PATH_TAG,
    MemoryItem,
    MemoryKind,
    MemoryProvenance,
    axis_tag,
)
from forge_loop.product_vision import Axis, ProductVision, discover

FIXTURES = Path(__file__).parent / "fixtures" / "product_vision"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _vision(
    *,
    axes: list[Axis] | None = None,
    md: str = "# Vision\nDeliver value.",
) -> ProductVision:
    if axes is None:
        axes = [
            Axis(
                name="throughput",
                customer="solo operator",
                valuable_means="more issues closed per hour",
                acceptable_work=["implement features"],
                rejected_as_cosmetic=["rename variables", "reflow whitespace"],
            ),
            Axis(
                name="reliability",
                customer="downstream consumer",
                valuable_means="fewer rollbacks",
                acceptable_work=["add tests"],
                rejected_as_cosmetic=["tweak log levels"],
            ),
        ]
    return ProductVision(vision_markdown=md, axes=axes)


@dataclass
class _StubResult:
    last_message: str = ""
    duration_s: float = 0.0
    timed_out: bool = False
    error: str | None = None


def _stub_sdk(payload: dict | str, *, error: str | None = None, timed_out: bool = False):
    """Build a stub ``run_brainstormer_sdk`` returning a canned payload."""
    body = payload if isinstance(payload, str) else json.dumps(payload)

    captured: dict = {}

    def _fn(prompt: str, *, cwd, timeout_s, model=None, **_kw) -> _StubResult:
        captured["prompt"] = prompt
        captured["cwd"] = cwd
        captured["timeout_s"] = timeout_s
        captured["model"] = model
        return _StubResult(
            last_message=body,
            duration_s=0.01,
            timed_out=timed_out,
            error=error,
        )

    _fn.captured = captured  # type: ignore[attr-defined]
    return _fn


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_two_epics_three_tickets() -> None:
    payload = {
        "proposed_epics": [
            {
                "title": "Lift cost telemetry into the dashboard",
                "body": "Operators need mid-run cost visibility.",
                "axis": "throughput",
                "customer_story": "As the solo operator I want to see live $/hr.",
            },
            {
                "title": "Surface critic verdicts in the runner log",
                "body": "Reviewers want fast feedback.",
                "axis": "reliability",
                "customer_story": "As a downstream consumer, fewer mystery merges.",
            },
        ],
        "proposed_tickets": [
            {
                "title": "Add live $/hr widget",
                "body": "Stream cost events to the dashboard.",
                "axis": "throughput",
                "customer_story": "Operator says: I lose track of spend mid-run.",
            },
            {
                "title": "Persist critic findings",
                "body": "Write findings to eventdb.",
                "axis": "reliability",
                "customer_story": "Consumer: I want a trail when a regression slips.",
            },
            {
                "title": "Add ratelimit smoke test",
                "body": "Confirm github API ratelimit handler trips correctly.",
                "axis": "reliability",
                "customer_story": "Consumer: I lose hours when the loop wedges on 403s.",
            },
        ],
    }
    fn = _stub_sdk(payload)
    report = Brainstormer(sdk_fn=fn).run(_vision())

    assert isinstance(report, BrainstormReport)
    assert len(report.proposed_epics) == 2
    assert len(report.proposed_tickets) == 3
    assert all(isinstance(e, ProposedEpic) for e in report.proposed_epics)
    assert all(isinstance(t, ProposedTicket) for t in report.proposed_tickets)


def test_codex_provider_uses_codex_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    payload = {"proposed_epics": [], "proposed_tickets": []}
    calls: dict[str, object] = {}

    def fake_codex(**kwargs):
        from forge_loop.agent_backend import AgentRunResult

        calls.update(kwargs)
        return AgentRunResult(
            provider="codex",
            log_path=kwargs["log_path"],
            last_message=json.dumps(payload),
            duration_s=0.01,
        )

    from forge_loop import agent_backend

    monkeypatch.setattr(agent_backend, "run_codex_exec", fake_codex)

    report = Brainstormer(
        repo_path=tmp_path,
        provider="codex",
        model="gpt-5-codex",
        timeout_s=17,
    ).run(_vision())

    assert report.proposed_epics == []
    assert report.proposed_tickets == []
    assert calls["cwd"] == tmp_path
    assert calls["timeout_s"] == 17
    assert calls["model"] == "gpt-5-codex"
    assert str(calls["log_path"]).endswith(".jsonl")


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_cosmetic_filter_drops_only_the_cosmetic(monkeypatch) -> None:
    payload = {
        "proposed_epics": [],
        "proposed_tickets": [
            {
                "title": "rename variables for clarity in worker.py",
                "body": "tidy.",
                "axis": "throughput",
                "customer_story": "operator likes tidy names",
            },
            {
                "title": "ship cost telemetry widget",
                "body": "real customer win.",
                "axis": "throughput",
                "customer_story": "operator wants live $/hr",
            },
        ],
    }
    # Capture drop-log invocations directly — structlog's PrintLogger
    # caches sys.stderr at config time, so capsys/capfd don't see them.
    drops: list[dict] = []

    class _RecLogger:
        def info(self, event, **kw):
            drops.append({"event": event, **kw})

        # other levels are no-ops for this assertion.
        def warning(self, *a, **kw):
            pass

        def error(self, *a, **kw):
            pass

        def debug(self, *a, **kw):
            pass

    from forge_loop import brainstormer as bs_mod

    monkeypatch.setattr(bs_mod, "_log", _RecLogger())

    report = Brainstormer(sdk_fn=_stub_sdk(payload)).run(_vision())
    assert len(report.proposed_tickets) == 1
    assert "cost telemetry" in report.proposed_tickets[0].title
    assert any(
        d["event"] == "brainstormer_dropped" and d.get("reason") == "cosmetic_match" for d in drops
    )


def test_missing_citation_filters_drop_both() -> None:
    payload = {
        "proposed_epics": [],
        "proposed_tickets": [
            {
                "title": "no axis",
                "body": "x",
                "axis": "",
                "customer_story": "operator: I need this",
            },
            {
                "title": "no story",
                "body": "x",
                "axis": "throughput",
                "customer_story": "",
            },
            {
                "title": "survivor",
                "body": "x",
                "axis": "throughput",
                "customer_story": "operator: I need this",
            },
        ],
    }
    report = Brainstormer(sdk_fn=_stub_sdk(payload)).run(_vision())
    assert [t.title for t in report.proposed_tickets] == ["survivor"]


def test_unknown_axis_dropped() -> None:
    payload = {
        "proposed_epics": [],
        "proposed_tickets": [
            {
                "title": "bogus axis",
                "body": "x",
                "axis": "no-such-axis",
                "customer_story": "x",
            },
        ],
    }
    report = Brainstormer(sdk_fn=_stub_sdk(payload)).run(_vision())
    assert report.proposed_tickets == []


def test_cosmetic_match_in_body_also_dropped() -> None:
    """Adversarial: SDK cites a valid axis but slips a rejected phrase into the body."""
    payload = {
        "proposed_epics": [],
        "proposed_tickets": [
            {
                "title": "Refresh dashboard styling",
                "body": "We should reflow whitespace in dashboard/css.",
                "axis": "throughput",
                "customer_story": "operator: feels nicer",
            }
        ],
    }
    report = Brainstormer(sdk_fn=_stub_sdk(payload)).run(_vision())
    assert report.proposed_tickets == []


def test_cosmetic_filter_case_insensitive() -> None:
    payload = {
        "proposed_epics": [],
        "proposed_tickets": [
            {
                "title": "RENAME VARIABLES for clarity",
                "body": "x",
                "axis": "throughput",
                "customer_story": "operator: tidy",
            }
        ],
    }
    report = Brainstormer(sdk_fn=_stub_sdk(payload)).run(_vision())
    assert report.proposed_tickets == []


# ---------------------------------------------------------------------------
# Hard failures
# ---------------------------------------------------------------------------


def test_empty_vision_axes_raises_value_error() -> None:
    """vision_with_no_axes is rejected upstream by ProductVision validation
    (min_length=1). The brainstormer must also reject a model-validated
    vision whose axes list was emptied post-construction or whose value is
    None — defense in depth."""
    b = Brainstormer(sdk_fn=_stub_sdk({"proposed_epics": [], "proposed_tickets": []}))
    with pytest.raises(ValueError, match="non-empty ProductVision"):
        b.run(None)  # type: ignore[arg-type]

    # Also: a ProductVision-shaped object whose axes is empty (forced)
    class _Empty:
        axes: list = []
        vision_markdown = ""

    with pytest.raises(ValueError, match="non-empty ProductVision"):
        b.run(_Empty())  # type: ignore[arg-type]


def test_sdk_timeout_raises_runtime_error() -> None:
    fn = _stub_sdk("", error="timeout", timed_out=True)
    with pytest.raises(RuntimeError, match="timed out"):
        Brainstormer(sdk_fn=fn).run(_vision())


def test_sdk_generic_error_raises_runtime_error() -> None:
    fn = _stub_sdk("", error="sdk_session_failed: ValueError: boom")
    with pytest.raises(RuntimeError, match="brainstormer SDK session failed"):
        Brainstormer(sdk_fn=fn).run(_vision())


def test_malformed_sdk_output_raises_with_raw_message() -> None:
    fn = _stub_sdk("this is not JSON at all")
    with pytest.raises(RuntimeError) as excinfo:
        Brainstormer(sdk_fn=fn).run(_vision())
    assert "this is not JSON at all" in str(excinfo.value)


def test_missing_keys_in_sdk_output_treated_as_empty_lists() -> None:
    # JSON parses, but neither key is present — pydantic defaults kick in
    # and the report is just empty. (Not a malformed-output failure.)
    fn = _stub_sdk({})
    report = Brainstormer(sdk_fn=fn).run(_vision())
    assert report.proposed_epics == []
    assert report.proposed_tickets == []


def test_json_with_trailing_prose_still_parsed() -> None:
    # The brief asks for JSON on the LAST line, but SDKs sometimes append
    # whitespace. We accept "balanced object near the end".
    payload = {"proposed_epics": [], "proposed_tickets": []}
    fn = _stub_sdk("Here is the report:\n" + json.dumps(payload) + "\n")
    report = Brainstormer(sdk_fn=fn).run(_vision())
    assert isinstance(report, BrainstormReport)


# ---------------------------------------------------------------------------
# Integration — prompt rendering uses fixtures
# ---------------------------------------------------------------------------


def test_integration_renders_vision_axes_and_rubric_into_prompt() -> None:
    vision = discover(FIXTURES / "valid_full")
    fn = _stub_sdk({"proposed_epics": [], "proposed_tickets": []})
    b = Brainstormer(sdk_fn=fn)  # no owner/repo → backlog block is "(none)"
    b.run(vision)
    prompt = fn.captured["prompt"]  # type: ignore[attr-defined]
    # Vision markdown verbatim
    assert "## Who" in prompt and "## How" in prompt
    # Axes block — uses canonical axis names + rejected_as_cosmetic phrases
    assert "throughput" in prompt
    assert "rename variables" in prompt
    # Rubric language is inlined
    assert "rejected_as_cosmetic" in prompt.lower() or "Hard-refusal rubric" in prompt
    # Backlog placeholder rendered even with no client
    assert "(none)" in prompt


def test_brief_enforces_small_single_mechanism_and_decomposition() -> None:
    """The brainstormer brief must instruct: single-mechanism, ≤~150 LOC,
    one falsifiable AC, and epic-decomposition for large ideas."""
    vision = discover(FIXTURES / "valid_full")
    fn = _stub_sdk({"proposed_epics": [], "proposed_tickets": []})
    b = Brainstormer(sdk_fn=fn)
    b.run(vision)
    prompt = fn.captured["prompt"]  # type: ignore[attr-defined]
    # Single-mechanism + size directive
    assert "single-mechanism" in prompt.lower() or "ONE mechanism" in prompt
    assert "150" in prompt  # the net-LOC ceiling is cited explicitly
    # One falsifiable acceptance criterion
    assert "falsifiable acceptance criterion" in prompt.lower()
    # Epic-decomposition instruction with dispatchable sub-tickets
    assert "Decomposing large" in prompt
    assert "proposed_epics" in prompt and "sub-tickets" in prompt
    assert "epic" in prompt.lower() and "loop:ready" in prompt


# ---------------------------------------------------------------------------
# Backlog helper (gh_client wrapper)
# ---------------------------------------------------------------------------


def test_list_open_backlog_partitions_epics_and_tickets() -> None:
    client = MockGhClient()
    # First call (label="epic") returns epics; second call (label="") returns
    # everything including the epics.
    epic = Issue(number=10, title="epic A", labels=["epic"])
    t1 = Issue(number=11, title="ticket A")
    t2 = Issue(number=12, title="ticket B")

    seq: list[list[Issue]] = [[epic], [epic, t1, t2]]
    original = client.issues_by_label

    def _by_label(owner, repo, label, limit):  # noqa: ARG001
        original(owner, repo, label, limit)  # record the call
        return list(seq.pop(0))

    client.issues_by_label = _by_label  # type: ignore[assignment]
    backlog = list_open_backlog(client, "o", "r", limit=10)
    assert [e.number for e in backlog.epics] == [10]
    assert [t.number for t in backlog.tickets] == [11, 12]


def test_render_backlog_block_handles_empty_and_populated() -> None:
    empty = _render_backlog_block(OpenBacklog())
    assert "(none)" in empty
    full = _render_backlog_block(
        OpenBacklog(epics=[Issue(number=1, title="E")], tickets=[Issue(number=2, title="T")])
    )
    assert "#1: E" in full and "#2: T" in full


def test_render_axes_block_includes_rejected_phrases() -> None:
    block = _render_axes_block(_vision())
    assert "throughput" in block
    assert "rename variables" in block
    assert "tweak log levels" in block


def test_parse_sdk_payload_rejects_empty_message() -> None:
    with pytest.raises(ValueError, match="empty"):
        _parse_sdk_payload("")


def test_parse_sdk_payload_rejects_non_object_json() -> None:
    with pytest.raises(ValueError):
        _parse_sdk_payload("[1, 2, 3]")


# ---------------------------------------------------------------------------
# Rejected-path memory: anti-relitigation feedback (issue #203)
# ---------------------------------------------------------------------------


class _RecLogger:
    """Captures ``_log.info`` calls so drop-reason provenance is assertable."""

    def __init__(self) -> None:
        self.records: list[dict] = []

    def info(self, event, **kw):
        self.records.append({"event": event, **kw})

    def warning(self, *a, **kw):
        self.records.append({"event": a[0] if a else "warning", "_level": "warning", **kw})

    def error(self, *a, **kw):
        pass

    def debug(self, *a, **kw):
        pass


def _rejected_item(
    *,
    title: str,
    axis: str,
    rationale: str = "out of axis — websockets are not a value axis",
    memory_id: str | None = None,
) -> MemoryItem:
    return MemoryItem(
        memory_id=memory_id or f"rejpath-{abs(hash((title, axis)))}",
        kind=MemoryKind.SEMANTIC,
        title=title,
        body=rationale,
        tags=(REJECTED_PATH_TAG, axis_tag(axis)),
        provenance=MemoryProvenance(
            source_event=None,
            authored_by="brainstorm-apply",
            source_task_ref="brainstorm-apply:report-abc",
        ),
    )


def test_rejected_paths_block_empty_renders_none() -> None:
    assert _render_rejected_paths_block(()) == "  (none)"
    assert _render_rejected_paths_block(None) == "  (none)"


def test_rejected_paths_block_populated_shows_idea_and_why() -> None:
    block = _render_rejected_paths_block(
        [
            _rejected_item(
                title="Stream worker logs over websockets",
                axis="throughput",
                rationale="rejected as out-of-axis",
            )
        ]
    )
    assert "Stream worker logs over websockets" in block
    assert "[throughput]" in block
    assert "rejected as out-of-axis" in block


def test_candidate_matching_rejected_path_is_dropped_with_provenance(monkeypatch) -> None:
    rec = _RecLogger()
    from forge_loop import brainstormer as bs_mod

    monkeypatch.setattr(bs_mod, "_log", rec)

    store = FakeMemoryStore()
    store.put(
        _rejected_item(
            title="Stream worker logs over websockets",
            axis="throughput",
            rationale="out-of-axis: realtime streaming is not a value axis",
        )
    )
    payload = {
        "proposed_epics": [],
        "proposed_tickets": [
            {
                "title": "Stream worker logs over websockets",
                "body": "Push logs live.",
                "axis": "throughput",
                "customer_story": "operator: I want live logs",
            },
            {
                "title": "Ship cost telemetry widget",
                "body": "real win",
                "axis": "throughput",
                "customer_story": "operator wants live $/hr",
            },
        ],
    }
    report = Brainstormer(sdk_fn=_stub_sdk(payload), memory_store=store).run(_vision())

    assert [t.title for t in report.proposed_tickets] == ["Ship cost telemetry widget"]
    drop = next(
        r
        for r in rec.records
        if r["event"] == "brainstormer_dropped" and r.get("reason") == "rejected_path"
    )
    # Drop reason cites the prior verdict's provenance: axis + rationale.
    assert drop["prior_axis"] == "throughput"
    assert "out-of-axis" in drop["prior_rationale"]
    assert drop["prior_source"] == "brainstorm-apply:report-abc"


def test_non_matching_candidate_survives_untouched() -> None:
    store = FakeMemoryStore()
    store.put(_rejected_item(title="Some rejected idea", axis="reliability"))
    payload = {
        "proposed_epics": [],
        "proposed_tickets": [
            {
                "title": "A wholly different idea",
                "body": "x",
                "axis": "throughput",
                "customer_story": "operator: I need this",
            }
        ],
    }
    report = Brainstormer(sdk_fn=_stub_sdk(payload), memory_store=store).run(_vision())
    assert [t.title for t in report.proposed_tickets] == ["A wholly different idea"]


def test_rejected_path_match_is_whitespace_and_case_insensitive() -> None:
    """Adversarial: a re-proposal differing only by whitespace/case must still
    match via ``normalize_candidate_key`` — guards against silent re-litigation."""
    store = FakeMemoryStore()
    store.put(_rejected_item(title="Stream Worker Logs", axis="throughput"))
    payload = {
        "proposed_epics": [],
        "proposed_tickets": [
            {
                "title": "  stream   worker  logs ",
                "body": "x",
                "axis": "throughput",
                "customer_story": "operator: I need this",
            }
        ],
    }
    report = Brainstormer(sdk_fn=_stub_sdk(payload), memory_store=store).run(_vision())
    assert report.proposed_tickets == []


def test_store_raising_on_list_rejected_paths_degrades_gracefully() -> None:
    """Sad path: a store that raises must not blow up ``run`` — mirror the
    ``_scan_backlog`` degrade pattern: empty block, no filtering, no crash."""

    class _ExplodingStore:
        def list_rejected_paths(self):
            raise RuntimeError("sqlite is corrupt")

    payload = {
        "proposed_epics": [],
        "proposed_tickets": [
            {
                "title": "survivor",
                "body": "x",
                "axis": "throughput",
                "customer_story": "operator: I need this",
            }
        ],
    }
    report = Brainstormer(sdk_fn=_stub_sdk(payload), memory_store=_ExplodingStore()).run(_vision())
    assert [t.title for t in report.proposed_tickets] == ["survivor"]


def test_scan_backlog_logs_warning_on_failure(monkeypatch) -> None:
    """Parity sad-path: a gh_client that raises must degrade to an empty
    backlog AND surface a ``_log.warning`` — matching the new memory-store
    degrades in this feature, no longer swallowing silently."""
    warnings: list[dict] = []

    class _RecLogger:
        def info(self, *a, **kw):
            pass

        def warning(self, event, **kw):
            warnings.append({"event": event, **kw})

        def error(self, *a, **kw):
            pass

        def debug(self, *a, **kw):
            pass

    class _ExplodingClient:
        pass

    from forge_loop import brainstormer as bs_mod
    from forge_loop import gh_client as gh_mod

    def _boom(*_a, **_k):
        raise RuntimeError("github unreachable")

    monkeypatch.setattr(bs_mod, "_log", _RecLogger())
    # _scan_backlog imports list_open_backlog locally, so patch the source module.
    monkeypatch.setattr(gh_mod, "list_open_backlog", _boom)

    bs = Brainstormer(
        sdk_fn=_stub_sdk({"proposed_epics": [], "proposed_tickets": []}),
        owner="acme",
        repo="widgets",
        gh_client=_ExplodingClient(),
    )
    backlog = bs._scan_backlog()
    # Degrades to an empty backlog (no epics/tickets) without raising.
    assert not getattr(backlog, "epics", []) and not getattr(backlog, "tickets", [])
    assert any(w["event"] == "brainstormer_backlog_unavailable" for w in warnings)


def test_no_store_behaves_identically_to_today() -> None:
    payload = {
        "proposed_epics": [],
        "proposed_tickets": [
            {
                "title": "survivor",
                "body": "x",
                "axis": "throughput",
                "customer_story": "operator: I need this",
            }
        ],
    }
    report = Brainstormer(sdk_fn=_stub_sdk(payload), memory_store=None).run(_vision())
    assert [t.title for t in report.proposed_tickets] == ["survivor"]


def test_rejected_paths_rendered_into_prompt() -> None:
    store = FakeMemoryStore()
    store.put(
        _rejected_item(
            title="Stream worker logs over websockets",
            axis="throughput",
            rationale="out-of-axis verdict",
        )
    )
    fn = _stub_sdk({"proposed_epics": [], "proposed_tickets": []})
    Brainstormer(sdk_fn=fn, memory_store=store).run(_vision())
    prompt = fn.captured["prompt"]  # type: ignore[attr-defined]
    assert "Previously rejected paths" in prompt
    assert "Stream worker logs over websockets" in prompt
    assert "out-of-axis verdict" in prompt


def test_no_store_renders_none_rejected_block() -> None:
    fn = _stub_sdk({"proposed_epics": [], "proposed_tickets": []})
    Brainstormer(sdk_fn=fn, memory_store=None).run(_vision())
    prompt = fn.captured["prompt"]  # type: ignore[attr-defined]
    assert "Previously rejected paths" in prompt
    # The block degrades to "(none)" with no store wired.
    assert "(none)" in prompt
