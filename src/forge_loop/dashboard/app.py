"""Operator dashboard — FastAPI app for the forge-loop control surface.

Issue #20: ship a visual dashboard so operators can see queue depth,
edit role yaml files in-browser, kill runaway workers, and watch
spend — without living in tmux + ``tail -F``.

Endpoints (HTML unless noted):
    GET  /                    live events stream page (HTMX-driven, SSE source)
    GET  /events/stream       text/event-stream of last N + new events
    GET  /roles               list role yaml files
    GET  /roles/{name}        show one role file (editor textarea)
    POST /roles/{name}        save edited role yaml  (audit: dashboard_action)
    GET  /pipeline            render the DAG as ASCII
    GET  /workers             list in-flight workers, kill buttons
    POST /workers/{wid}/kill  mark a worker for termination  (audit: dashboard_action)
    GET  /budget              today's spend + top-10 expensive issues

Auth: a single bearer token from ``LOOP_DASHBOARD_TOKEN``. If no token is
configured, the server refuses to bind to anything but loopback (``serve``
hard-fails on 0.0.0.0). With a token, any request without a correct
``Authorization: Bearer <token>`` header gets 401.

The handler is intentionally test-friendly: ``build_app`` accepts every
path / token explicitly so tests don't have to monkey-patch a global
``Config``. ``build_handler`` is kept for backward-compat with the older
Prometheus-only entry (and the test in ``tests/test_observability.py``
that pokes that handler directly).
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import yaml

from ..observability import render_prometheus

# ---------------------------------------------------------------------------
# Legacy stdlib Prom handler (kept for tests/test_observability.py)
# ---------------------------------------------------------------------------


class _MetricsHandler(BaseHTTPRequestHandler):
    """``GET /metrics`` + ``GET /healthz`` — stdlib, dependency-free."""

    server_version = "ForgeLoopDashboard/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") == "/metrics":
            body, content_type = render_prometheus()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.rstrip("/") in {"/healthz", ""}:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok\n")
            return
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"not found\n")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


def build_handler() -> type[BaseHTTPRequestHandler]:
    """Legacy entry — returns the stdlib Prom handler class."""
    return _MetricsHandler


# ---------------------------------------------------------------------------
# Helpers shared by the FastAPI app
# ---------------------------------------------------------------------------


_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


def _today_iso() -> str:
    return datetime.now(UTC).date().isoformat()


def _read_events(events_path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    """Return events as a list of dicts, oldest-first. Skip junk lines."""
    if not events_path.exists():
        return []
    out: list[dict[str, Any]] = []
    with open(events_path) as f:
        lines = f.readlines()
    if limit is not None:
        lines = lines[-limit:]
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def _append_audit(
    events_path: Path, action: str, *, actor: str = "dashboard", **fields: Any
) -> None:
    """Append a ``dashboard_action`` event to the loop's jsonl stream."""
    events_path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "kind": "dashboard_action",
        "action": action,
        "actor": actor,
        **fields,
    }
    with open(events_path, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def _validate_role_yaml(text: str) -> dict[str, Any]:
    """Parse + minimally validate a role yaml.

    Schema (v1): top-level mapping with at least ``name`` (non-empty str)
    and ``prompt`` (non-empty str). Anything else is allowed through;
    we're a guard against shape-breaking edits, not a strict schema gate.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"yaml parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("role yaml must be a mapping at the top level")
    for required in ("name", "prompt"):
        if required not in data:
            raise ValueError(f"role yaml missing required field: {required!r}")
        if not isinstance(data[required], str) or not data[required].strip():
            raise ValueError(f"role yaml field {required!r} must be a non-empty string")
    return data


def _list_roles(roles_dir: Path) -> list[str]:
    if not roles_dir.exists():
        return []
    return sorted(p.name for p in roles_dir.iterdir() if p.suffix in {".yaml", ".yml"})


def _queue_depth(events: list[dict[str, Any]]) -> int:
    """Best-effort: dispatched - completed in the visible window."""
    dispatched = sum(1 for e in events if e.get("kind") == "worker_dispatched")
    completed = sum(
        1 for e in events if e.get("kind") in {"worker_completed", "worker_failed", "worker_merged"}
    )
    return max(0, dispatched - completed)


def _in_flight_workers(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Walk events left-to-right; track open workers, drop on completion."""
    open_w: dict[str, dict[str, Any]] = {}
    for e in events:
        wid = e.get("worker_id") or e.get("worker") or str(e.get("issue") or "")
        if not wid:
            continue
        if e.get("kind") == "worker_dispatched":
            open_w[wid] = {
                "worker_id": wid,
                "issue": e.get("issue"),
                "started_ts": e.get("ts"),
            }
        elif e.get("kind") in {"worker_completed", "worker_failed", "worker_merged"}:
            open_w.pop(wid, None)
    return list(open_w.values())


def _budget_today(events: list[dict[str, Any]]) -> dict[str, Any]:
    today = _today_iso()
    per_issue: dict[Any, float] = {}
    total = 0.0
    for e in events:
        ts = str(e.get("ts", ""))
        if not ts.startswith(today):
            continue
        cost = e.get("cost_usd") or e.get("spend_usd") or e.get("usd")
        if cost is None:
            continue
        try:
            c = float(cost)
        except (TypeError, ValueError):
            continue
        total += c
        issue = e.get("issue") or "(unknown)"
        per_issue[issue] = per_issue.get(issue, 0.0) + c
    top = sorted(per_issue.items(), key=lambda kv: kv[1], reverse=True)[:10]
    return {"total_usd": round(total, 4), "top_issues": top}


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------


def build_app(
    *,
    state_dir: Path,
    roles_dir: Path,
    token: str | None = None,
    pipeline_renderer: Any = None,
    kill_dir: Path | None = None,
    events_path: Path | None = None,
):
    """Build the FastAPI app. All paths are injected for testability."""

    # Local imports — fastapi is in the [experimental] extra, gated by
    # ``forge_loop._extras``. The package __init__ has already required it.
    from fastapi import FastAPI, Form, HTTPException, Request
    from fastapi import Path as FPath
    from fastapi.responses import (
        HTMLResponse,
        JSONResponse,
        PlainTextResponse,
        RedirectResponse,
        StreamingResponse,
    )
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates

    state_dir = Path(state_dir)
    roles_dir = Path(roles_dir)
    events_path = Path(events_path) if events_path else state_dir / "loop-runner-events.jsonl"
    kill_dir = Path(kill_dir) if kill_dir else state_dir / "worker-kills"
    state_dir.mkdir(parents=True, exist_ok=True)

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    app = FastAPI(title="forge-loop dashboard")

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ----- auth -----
    def _check_auth(request: Request) -> None:
        if token is None:
            return
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        provided = header.split(None, 1)[1].strip()
        if provided != token:
            raise HTTPException(status_code=401, detail="invalid bearer token")

    @app.middleware("http")
    async def _auth_mw(request: Request, call_next):
        # Health endpoints are always open so the operator can probe liveness
        # without leaking the token into curl history.
        if request.url.path in {"/healthz", "/metrics"}:
            return await call_next(request)
        try:
            _check_auth(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return await call_next(request)

    # ----- health / metrics (legacy) -----
    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    @app.get("/metrics")
    def metrics() -> PlainTextResponse:
        body, ctype = render_prometheus()
        return PlainTextResponse(body.decode("utf-8"), media_type=ctype)

    # ----- index: live events stream UI -----
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Any:
        events = _read_events(events_path, limit=50)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "events": list(reversed(events)),
                "queue_depth": _queue_depth(events),
                "workers": _in_flight_workers(events),
            },
        )

    @app.get("/events/stream")
    async def events_stream(request: Request, n: int = 25, once: bool = False) -> StreamingResponse:
        async def gen() -> AsyncIterator[bytes]:
            # 1) replay the last N events on connect
            for e in _read_events(events_path, limit=n):
                yield f"data: {json.dumps(e, default=str)}\n\n".encode()
            # 2) optional one-shot: terminate after replay (test hook).
            if once:
                return
            # 3) tail the file for new lines
            offset = events_path.stat().st_size if events_path.exists() else 0
            while True:
                if await request.is_disconnected():
                    return
                if events_path.exists() and events_path.stat().st_size > offset:
                    with open(events_path) as f:
                        f.seek(offset)
                        chunk = f.read()
                        offset = f.tell()
                    for raw in chunk.splitlines():
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            obj = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        yield f"data: {json.dumps(obj, default=str)}\n\n".encode()
                else:
                    yield b": keepalive\n\n"
                await asyncio.sleep(0.05)

        return StreamingResponse(gen(), media_type="text/event-stream")

    # ----- roles -----
    @app.get("/roles", response_class=HTMLResponse)
    def roles_index(request: Request) -> Any:
        return templates.TemplateResponse(
            request,
            "roles.html",
            {"roles": _list_roles(roles_dir)},
        )

    @app.get("/roles/{name}", response_class=HTMLResponse)
    def role_show(request: Request, name: str = FPath(...)) -> Any:
        path = roles_dir / name
        if not path.exists() or path.parent.resolve() != roles_dir.resolve():
            raise HTTPException(status_code=404, detail="role not found")
        return templates.TemplateResponse(
            request,
            "role_edit.html",
            {"name": name, "body": path.read_text()},
        )

    @app.post("/roles/{name}")
    def role_save(
        request: Request,
        name: str = FPath(...),
        body: str = Form(...),
    ) -> Any:
        path = roles_dir / name
        if path.parent.resolve() != roles_dir.resolve():
            raise HTTPException(status_code=400, detail="invalid role path")
        if path.suffix not in {".yaml", ".yml"}:
            raise HTTPException(status_code=400, detail="role file must be .yaml/.yml")
        try:
            _validate_role_yaml(body)
        except ValueError as exc:
            # Adversarial: file is NOT overwritten on validation failure.
            _append_audit(
                events_path,
                "role_save_rejected",
                role=name,
                error=str(exc),
            )
            return JSONResponse({"detail": str(exc)}, status_code=400)
        roles_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        _append_audit(events_path, "role_save", role=name, bytes=len(body))
        return RedirectResponse(url=f"/roles/{name}", status_code=303)

    # ----- pipeline -----
    @app.get("/pipeline", response_class=HTMLResponse)
    def pipeline_view(request: Request) -> Any:
        if pipeline_renderer is not None:
            try:
                rendered = str(pipeline_renderer())
            except Exception as exc:  # pragma: no cover
                rendered = f"(pipeline render failed: {exc})"
        else:
            rendered = "(no pipeline configured)"
        return templates.TemplateResponse(
            request,
            "pipeline.html",
            {"rendered": rendered},
        )

    # ----- workers -----
    @app.get("/workers", response_class=HTMLResponse)
    def workers_view(request: Request) -> Any:
        events = _read_events(events_path, limit=500)
        return templates.TemplateResponse(
            request,
            "workers.html",
            {"workers": _in_flight_workers(events)},
        )

    @app.post("/workers/{wid}/kill")
    def worker_kill(request: Request, wid: str = FPath(...)) -> Any:
        if not wid or "/" in wid or wid in {".", ".."}:
            raise HTTPException(status_code=400, detail="invalid worker id")
        kill_dir.mkdir(parents=True, exist_ok=True)
        flag = kill_dir / wid
        flag.write_text(
            json.dumps(
                {
                    "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                    "worker_id": wid,
                }
            )
        )
        _append_audit(events_path, "worker_kill", worker_id=wid)
        return JSONResponse({"status": "marked_for_kill", "worker_id": wid})

    # ----- budget -----
    @app.get("/budget", response_class=HTMLResponse)
    def budget_view(request: Request) -> Any:
        events = _read_events(events_path, limit=10_000)
        budget = _budget_today(events)
        return templates.TemplateResponse(
            request,
            "budget.html",
            {"budget": budget, "today": _today_iso()},
        )

    return app


# ---------------------------------------------------------------------------
# CLI entry — invoked by ``forge-loop dashboard``
# ---------------------------------------------------------------------------


class DashboardBindError(RuntimeError):
    """Refused to bind: 0.0.0.0 without a configured bearer token."""


def serve(
    host: str = "127.0.0.1",
    port: int = 9464,
    *,
    state_dir: Path | None = None,
    roles_dir: Path | None = None,
    token: str | None = None,
) -> None:
    """Run the dashboard server in the foreground.

    Backward-compat: the legacy stdlib metrics server is still reachable as
    ``build_handler`` for the older Prometheus test; ``serve`` itself now
    runs the FastAPI app via uvicorn so the operator gets the full UI.
    """
    token = token if token is not None else os.environ.get("LOOP_DASHBOARD_TOKEN") or None
    # Safety: refuse to expose a no-auth dashboard to the world.
    if host in {"0.0.0.0", "::", "*"} and not token:  # noqa: S104
        raise DashboardBindError(
            "refusing to bind to "
            f"{host!r} without LOOP_DASHBOARD_TOKEN. Set the env var or "
            "bind to 127.0.0.1."
        )
    if state_dir is None or roles_dir is None:
        httpd = HTTPServer((host, port), _MetricsHandler)
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()
        return

    import uvicorn

    app = build_app(state_dir=Path(state_dir), roles_dir=Path(roles_dir), token=token)
    uvicorn.run(app, host=host, port=port, log_level="warning")


__all__ = [
    "DashboardBindError",
    "build_app",
    "build_handler",
    "serve",
    "_append_audit",
    "_budget_today",
    "_in_flight_workers",
    "_queue_depth",
    "_read_events",
    "_validate_role_yaml",
]
