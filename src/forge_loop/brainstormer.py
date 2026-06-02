"""Brainstormer — propose axis-aligned epics + tickets from product vision.

Issue #123 (part of epic #121). Depends on ``ProductVision`` from #122.

The brainstormer is the sprint loop's *generator* of new work. Without
it, the loop drains the existing backlog and then drifts into cosmetic
tinkering. With it, every newly proposed item must:

1. Cite an axis (by exact name) declared in :class:`ProductVision`.
2. Carry a customer story tied to that axis's ``customer`` field.
3. Survive the anti-cosmetic guardrail — substring-match against the
   axis's ``rejected_as_cosmetic`` list.

The SDK session does the proposing; the guardrail (this module) drops
anything that fails the rubric. The session is *not* trusted to enforce
its own refusal contract — defense in depth.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from forge_loop.log import get_logger
from forge_loop.product_vision import ProductVision

__all__ = [
    "BrainstormReport",
    "Brainstormer",
    "ProposedEpic",
    "ProposedTicket",
    "filter_report_for_vision",
]

_log = get_logger("forge_loop.brainstormer")


class _ProposedBase(BaseModel):
    """Common shape for proposed epics + tickets.

    Extra keys ignored for forward compatibility with future SDK output
    versions — we only enforce the fields the rubric cares about.
    """

    model_config = ConfigDict(extra="ignore")

    title: str
    body: str = ""
    axis: str = ""
    customer_story: str = ""


class ProposedEpic(_ProposedBase):
    """An epic proposed by the brainstormer SDK session."""


class ProposedTicket(_ProposedBase):
    """A ticket proposed by the brainstormer SDK session."""


class BrainstormReport(BaseModel):
    """Filtered output — only items that survived the anti-cosmetic guardrail."""

    model_config = ConfigDict(extra="ignore")

    proposed_epics: list[ProposedEpic] = Field(default_factory=list)
    proposed_tickets: list[ProposedTicket] = Field(default_factory=list)


def filter_report_for_vision(
    report: BrainstormReport, vision: ProductVision
) -> tuple[BrainstormReport, int]:
    """Re-apply the brainstormer rubric to an existing report.

    Returns the filtered report plus the number of proposals dropped. This
    keeps the reviewed-report apply path on the same guardrails as fresh SDK
    output without invoking a new SDK session.
    """
    epics = _filter_items(list(report.proposed_epics), vision, kind="epic")
    tickets = _filter_items(list(report.proposed_tickets), vision, kind="ticket")
    return (
        BrainstormReport(proposed_epics=epics, proposed_tickets=tickets),
        len(report.proposed_epics) + len(report.proposed_tickets) - len(epics) - len(tickets),
    )


# ---------------------------------------------------------------------------
# Internals: prompt rendering + filtering
# ---------------------------------------------------------------------------


def _render_axes_block(vision: ProductVision) -> str:
    """Render axes as a deterministic, prompt-friendly structured block."""
    import yaml

    payload = {
        "axes": [
            {
                "name": a.name,
                "customer": a.customer,
                "valuable_means": a.valuable_means,
                "acceptable_work": list(a.acceptable_work),
                "rejected_as_cosmetic": list(a.rejected_as_cosmetic),
            }
            for a in vision.axes
        ]
    }
    return yaml.safe_dump(payload, sort_keys=False).strip()


def _render_backlog_block(backlog: Any) -> str:
    """Render the open backlog as a compact, prompt-friendly block.

    Accepts an ``OpenBacklog`` (preferred) or any object exposing ``epics``
    and ``tickets`` iterables of ``(number, title)``-shaped items. The
    flexibility keeps tests free of githubkit-shape coupling.
    """

    def _fmt(items: Any) -> str:
        out: list[str] = []
        for it in items or []:
            num = getattr(it, "number", None) or (
                it.get("number") if isinstance(it, dict) else None
            )
            title = getattr(it, "title", None) or (it.get("title") if isinstance(it, dict) else "")
            out.append(f"  - #{num}: {title}")
        return "\n".join(out) if out else "  (none)"

    epics = getattr(backlog, "epics", []) or []
    tickets = getattr(backlog, "tickets", []) or []
    return f"Open epics:\n{_fmt(epics)}\n\nOpen tickets:\n{_fmt(tickets)}"


def _parse_sdk_payload(last_message: str) -> dict[str, Any]:
    """Extract the trailing JSON object from the SDK's last message.

    The prompt requires the JSON object to be on the LAST line. We accept
    "anywhere in the tail" defensively — find the last ``{`` that begins
    a balanced object. Raises ``ValueError`` (callers re-raise as
    ``RuntimeError``) on malformed input.
    """
    text = (last_message or "").strip()
    if not text:
        raise ValueError("empty SDK output")

    # Try a fast path: the whole message is JSON.
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # Fall back: scan for the last balanced ``{...}`` substring.
    depth = 0
    start = -1
    best: str | None = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                best = text[start : i + 1]
    if best is None:
        raise ValueError("no JSON object found in SDK output")
    try:
        obj = json.loads(best)
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON in SDK output: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("SDK output JSON is not an object")
    return obj


def _filter_items(
    items: list[Any],
    vision: ProductVision,
    *,
    kind: str,
) -> list[Any]:
    """Drop items that fail the rubric. Returns the surviving list.

    Drop reasons (logged at INFO):
      * ``missing_axis`` — empty ``axis``
      * ``missing_customer_story`` — empty ``customer_story``
      * ``unknown_axis`` — ``axis`` not in ``vision.axes``
      * ``cosmetic_match`` — title/body contains a ``rejected_as_cosmetic``
        phrase (case-insensitive substring) for the cited axis
    """
    axes_by_name = {a.name: a for a in vision.axes}
    survivors: list[Any] = []
    for it in items:
        title = it.title
        axis_name = (it.axis or "").strip()
        story = (it.customer_story or "").strip()
        if not axis_name:
            _log.info("brainstormer_dropped", kind=kind, title=title, reason="missing_axis")
            continue
        if not story:
            _log.info(
                "brainstormer_dropped", kind=kind, title=title, reason="missing_customer_story"
            )
            continue
        axis = axes_by_name.get(axis_name)
        if axis is None:
            _log.info(
                "brainstormer_dropped",
                kind=kind,
                title=title,
                axis=axis_name,
                reason="unknown_axis",
            )
            continue
        haystack = f"{title}\n{it.body}".lower()
        cosmetic_hit = next(
            (
                phrase
                for phrase in axis.rejected_as_cosmetic
                if phrase and phrase.lower() in haystack
            ),
            None,
        )
        if cosmetic_hit is not None:
            _log.info(
                "brainstormer_dropped",
                kind=kind,
                title=title,
                axis=axis_name,
                phrase=cosmetic_hit,
                reason="cosmetic_match",
            )
            continue
        survivors.append(it)
    return survivors


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------


@dataclass
class Brainstormer:
    """Drive one brainstormer SDK session and return a filtered report.

    Args:
        repo_path: Working directory the SDK session runs in.
        owner / repo: GitHub coordinates for the open-backlog scan. If
            either is empty, the backlog block renders as "(none)" — the
            session still runs but without backlog context.
        gh_client: Injected :class:`forge_loop.gh_client.GhClient`. If
            None, a real :class:`GithubkitClient` is constructed lazily.
        sdk_fn: Injection point for ``run_brainstormer_sdk`` (tests stub
            this to avoid network + SDK install).
        timeout_s: Hard cap on the SDK session.
        provider: Agent provider for the default backend. ``claude`` uses
            the Claude SDK shim; ``codex`` uses ``codex exec``.
    """

    repo_path: Path = Path(".")
    owner: str = ""
    repo: str = ""
    gh_client: Any = None
    sdk_fn: Callable[..., Any] | None = None
    timeout_s: int = 300
    model: str | None = None
    provider: str = "claude"

    def run(self, vision: ProductVision) -> BrainstormReport:
        """Entry point — see module docstring."""
        if vision is None or not getattr(vision, "axes", None):
            raise ValueError(
                "brainstormer requires a non-empty ProductVision with at least one axis"
            )

        # 1. Build the prompt.
        backlog = self._scan_backlog()
        prompt = self._render_prompt(vision, backlog)

        # 2. Drive the SDK session.
        sdk_fn = self.sdk_fn or self._default_sdk_fn()
        result = sdk_fn(prompt, cwd=self.repo_path, timeout_s=self.timeout_s, model=self.model)
        err = getattr(result, "error", None)
        timed_out = getattr(result, "timed_out", False)
        if timed_out or err == "timeout":
            raise RuntimeError("brainstormer SDK session timed out — no report produced")
        if err:
            raise RuntimeError(f"brainstormer SDK session failed: {err}")

        last_message = getattr(result, "last_message", "") or ""

        # 3. Parse + validate the payload.
        try:
            payload = _parse_sdk_payload(last_message)
            raw = BrainstormReport.model_validate(payload)
        except (ValueError, ValidationError) as exc:
            raise RuntimeError(
                f"brainstormer SDK returned malformed output ({exc}); last_message={last_message!r}"
            ) from exc

        # 4. Apply the anti-cosmetic guardrail.
        return filter_report_for_vision(raw, vision)[0]

    # -- helpers --------------------------------------------------------

    def _scan_backlog(self) -> Any:
        """Return open backlog via gh_client (or an empty stand-in)."""
        from forge_loop.gh_client import OpenBacklog, list_open_backlog

        if not self.owner or not self.repo:
            return OpenBacklog()
        client = self.gh_client
        if client is None:
            try:
                from forge_loop.gh_client import GithubkitClient

                client = GithubkitClient()
            except Exception:  # noqa: BLE001 — boundary; degrade gracefully
                return OpenBacklog()
        try:
            return list_open_backlog(client, self.owner, self.repo)
        except Exception:  # noqa: BLE001 — boundary; degrade gracefully
            return OpenBacklog()

    def _render_prompt(self, vision: ProductVision, backlog: Any) -> str:
        from forge_loop.briefs import render_brief

        return render_brief(
            "brainstormer",
            vision_markdown=vision.vision_markdown,
            axes_block=_render_axes_block(vision),
            backlog_block=_render_backlog_block(backlog),
        )

    def _default_sdk_fn(self) -> Callable[..., Any]:
        if self.provider == "codex":
            return self._codex_sdk_fn
        if self.provider != "claude":
            raise ValueError(f"unknown brainstormer provider: {self.provider!r}")
        from forge_loop._brainstormer_sdk import run_brainstormer_sdk

        return run_brainstormer_sdk

    def _codex_sdk_fn(
        self, prompt: str, *, cwd: Path, timeout_s: int, model: str | None = None
    ) -> Any:
        from forge_loop.agent_backend import run_codex_exec

        log_dir = Path(cwd) / "docs" / "ops" / "loop-runner-logs"
        log_path = log_dir / f"brainstormer-{int(time.time())}.jsonl"
        return run_codex_exec(
            prompt=prompt,
            cwd=Path(cwd),
            log_path=log_path,
            timeout_s=timeout_s,
            model=model or None,
        )
