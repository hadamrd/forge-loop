"""Textual TUI dashboard (issue #47).

Operator-facing terminal UI that lives alongside the FastAPI dashboard.
Launched via ``forge-loop dashboard --tui``. Shows:

* a live events stream (tail of the events JSONL),
* queue depth (count of ``loop:ready``-labelled open issues, refreshed
  on a slow timer to avoid hammering ``gh``),
* in-flight workers panel with a ``k`` keybinding that fires the
  ``worker_kill_requested`` event — actual signal delivery is the
  runner's job; the TUI's contract is *publishing intent*,
* a budget panel rolled up from recent events.

The textual dependency lives in the ``[ui]`` extra. This module is
imported lazily from ``cli.py`` so the stable surface keeps importing
when textual is absent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:  # textual is an optional dep — fail with a clean message if missing.
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical
    from textual.message import Message
    from textual.widgets import Footer, Header, Static

    _TEXTUAL_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised in --tui error path
    _TEXTUAL_AVAILABLE = False
    App = object  # type: ignore[assignment,misc]
    ComposeResult = Any  # type: ignore[assignment,misc]


def _tail_jsonl(path: Path, n: int = 20) -> list[dict[str, Any]]:
    """Return the last *n* parseable JSON lines from *path*.

    Bad lines are skipped silently — the TUI must not crash on a
    half-written event during a runner write.
    """
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-n:]
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _compute_queue_depth(state_dir: Path) -> int:
    """Best-effort queue-depth from the cached state file.

    The TUI prefers the cached value over shelling out to ``gh`` on
    every tick — operators running ``forge-loop run`` already refresh
    the cached value once per tick.
    """
    cache = state_dir / "queue-depth.cache"
    if not cache.exists():
        return -1
    try:
        return int(cache.read_text().strip())
    except (OSError, ValueError):
        return -1


def _compute_inflight(events: list[dict[str, Any]]) -> dict[int, str]:
    """Derive currently in-flight workers from a recent events slice.

    A worker is considered in-flight if we've seen ``worker_start`` for
    its issue without a matching terminal event (``worker_done``,
    ``worker_failed``, ``worker_skip_*``, ``budget_worker_killed``).
    """
    terminal_kinds = {
        "worker_done", "worker_failed",
        "worker_skip_in_flight", "worker_skip_cooldown",
        "budget_worker_killed", "watchdog_worker_killed",
    }
    inflight: dict[int, str] = {}
    for e in events:
        kind = e.get("kind", "")
        issue = e.get("issue") or e.get("issue_number")
        if not isinstance(issue, int):
            continue
        if kind == "worker_start":
            inflight[issue] = str(e.get("ts", ""))[-9:-1] or "?"
        elif kind in terminal_kinds:
            inflight.pop(issue, None)
    return inflight


def _compute_budget(events: list[dict[str, Any]]) -> dict[str, float]:
    """Roll up budget signals from recent events."""
    import contextlib

    spent = 0.0
    cap = 0.0
    for e in events:
        kind = e.get("kind", "")
        if kind in {"worker_done", "critic_done", "po_done"}:
            with contextlib.suppress(TypeError, ValueError):
                spent += float(e.get("cost_usd") or 0.0)
        if kind == "budget_cap":
            with contextlib.suppress(TypeError, ValueError):
                cap = float(e.get("cap_usd") or cap)
    return {"spent": round(spent, 4), "cap": cap}


# ---------------------------------------------------------------------------
# Textual App
# ---------------------------------------------------------------------------


if _TEXTUAL_AVAILABLE:

    class WorkerKillRequested(Message):
        """Fired by the TUI when the operator presses ``k`` to kill a worker.

        The runner subscribes to the events JSONL and translates this
        intent into the actual SIGTERM. The TUI never reaches into the
        worker subprocess directly.
        """

        def __init__(self, issue: int) -> None:
            self.issue = issue
            super().__init__()

    class EventsPanel(Static):
        """Scrollable live tail of the events JSONL."""

        # Mirror the last rendered text on the instance so tests can
        # introspect content without going through Textual internals.
        last_text: str = ""

        def update_events(self, events: list[dict[str, Any]]) -> None:
            lines = []
            for ev in events[-15:]:
                ts = str(ev.get("ts", "?"))[-9:-1] if ev.get("ts") else "?"
                kind = ev.get("kind", "?")
                lines.append(f"[dim]{ts}[/dim]  [cyan]{kind}[/cyan]")
            text = "\n".join(lines) or "(no events yet)"
            self.last_text = text
            self.update(text)

    class QueuePanel(Static):
        def update_queue(self, depth: int) -> None:
            if depth < 0:
                self.update("[yellow]queue: unknown[/yellow]")
            else:
                self.update(f"[bold]queue depth:[/bold] {depth}")

    class WorkersPanel(Static):
        """In-flight workers; press ``k`` to fire a kill event for the
        currently-selected one (cursor stays on the first worker for
        simplicity in this initial cut).
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._workers: dict[int, str] = {}
            self.last_text: str = ""

        def update_workers(self, workers: dict[int, str]) -> None:
            self._workers = dict(workers)
            if not workers:
                text = "[dim](no workers in flight)[/dim]"
            else:
                rows = [
                    f"  #{issue:<5} started {ts}"
                    for issue, ts in sorted(workers.items())
                ]
                text = "[bold]in-flight workers[/bold]\n" + "\n".join(rows)
            self.last_text = text
            self.update(text)

        @property
        def first_worker(self) -> int | None:
            if not self._workers:
                return None
            return sorted(self._workers)[0]

    class BudgetPanel(Static):
        def update_budget(self, budget: dict[str, float]) -> None:
            spent = budget.get("spent", 0.0)
            cap = budget.get("cap", 0.0)
            cap_s = f"${cap:.2f}" if cap else "uncapped"
            self.update(f"[bold]budget[/bold]: spent ${spent:.4f} / cap {cap_s}")

    class ForgeLoopTUI(App):  # type: ignore[misc]
        """Textual app that surfaces the live operator view."""

        CSS = """
        Screen { layout: vertical; }
        #top { height: 3; }
        #middle { height: 1fr; }
        EventsPanel { border: solid cyan; padding: 0 1; height: 1fr; }
        QueuePanel  { border: solid green;   padding: 0 1; width: 30; }
        WorkersPanel{ border: solid yellow;  padding: 0 1; width: 1fr; height: 1fr; }
        BudgetPanel { border: solid magenta; padding: 0 1; height: 3; }
        """

        BINDINGS = [
            Binding("k", "kill_worker", "Kill selected worker"),
            Binding("r", "refresh", "Refresh"),
            Binding("q", "quit", "Quit"),
        ]

        def __init__(
            self,
            *,
            events_file: Path,
            state_dir: Path,
            kill_event_sink: Path | None = None,
            tick_interval: float = 1.0,
        ) -> None:
            super().__init__()
            self.events_file = events_file
            self.state_dir = state_dir
            # Where to publish kill intent. Default: sidecar in state_dir
            # so the runner has a single well-known place to watch.
            self.kill_event_sink = (
                kill_event_sink if kill_event_sink is not None
                else (state_dir / "kill-requests.jsonl")
            )
            self.tick_interval = tick_interval
            self._workers_panel: WorkersPanel | None = None
            self._events_panel: EventsPanel | None = None
            self._queue_panel: QueuePanel | None = None
            self._budget_panel: BudgetPanel | None = None

        def compose(self) -> ComposeResult:
            yield Header(name="forge-loop dashboard")
            with Horizontal(id="top"):
                self._queue_panel = QueuePanel()
                yield self._queue_panel
                self._budget_panel = BudgetPanel()
                yield self._budget_panel
            with Vertical(id="middle"):
                self._events_panel = EventsPanel()
                yield self._events_panel
                self._workers_panel = WorkersPanel()
                yield self._workers_panel
            yield Footer()

        def on_mount(self) -> None:
            self.set_interval(self.tick_interval, self.refresh_panels)
            self.refresh_panels()

        def refresh_panels(self) -> None:
            events = _tail_jsonl(self.events_file, n=200)
            if self._events_panel is not None:
                self._events_panel.update_events(events)
            if self._queue_panel is not None:
                self._queue_panel.update_queue(_compute_queue_depth(self.state_dir))
            if self._workers_panel is not None:
                self._workers_panel.update_workers(_compute_inflight(events))
            if self._budget_panel is not None:
                self._budget_panel.update_budget(_compute_budget(events))

        def action_refresh(self) -> None:
            self.refresh_panels()

        def action_kill_worker(self) -> None:
            if self._workers_panel is None:
                return
            issue = self._workers_panel.first_worker
            if issue is None:
                return
            self.post_message(WorkerKillRequested(issue))
            self.publish_kill_request(issue)

        def publish_kill_request(self, issue: int) -> None:
            """Append a kill-request event the runner can observe."""
            try:
                self.kill_event_sink.parent.mkdir(parents=True, exist_ok=True)
                with open(self.kill_event_sink, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"kind": "worker_kill_requested", "issue": issue}) + "\n")
            except OSError:
                # Don't take the TUI down on a flaky filesystem.
                pass


def run_tui(*, state_dir: Path, events_file: Path) -> int:
    """Entry point used by ``forge-loop dashboard --tui``.

    Returns 0 on clean exit, 2 if textual is not installed.
    """
    if not _TEXTUAL_AVAILABLE:
        raise ImportError(
            "textual is required for `forge-loop dashboard --tui`. "
            "Install with: pip install 'forge-loop[ui]'"
        )
    # NO_COLOR / TERM=dumb still works — Textual respects the env. We do
    # not force-spawn the app in a non-tty environment (tests use the
    # Textual harness directly).
    if not os.isatty(0) and not os.environ.get("FORGE_LOOP_TUI_FORCE"):
        # Print a friendly message rather than crashing on a non-tty.
        print("forge-loop dashboard --tui requires a TTY", flush=True)
        return 2
    app = ForgeLoopTUI(events_file=events_file, state_dir=state_dir)
    app.run()
    return 0
