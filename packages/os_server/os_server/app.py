"""HTTP + WebSocket transport.

Endpoints
---------
``POST /runs``                  start a run, returns ``run_id``
``GET  /runs/{id}``             execution-tree snapshot (the UI's source of truth)
``GET  /runs/{id}/events``      SSE stream — works through proxies that block WS
``WS   /ws/{id}``               live event stream (canvas synchronisation)
``POST /runs/{id}/approve``     human-in-the-loop answer
``GET  /artifacts/{id}/{name}`` fetch an artifact for the right-hand pane
``GET  /health``                liveness + tool inventory

The same :class:`Platform` is used, so server and CLI share security semantics.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from os_core.events import EventBus, EventType
from os_core.hitl import ApprovalDecision, QueueApprover
from os_core.state import RunState, RunStatus
from os_orchestration.platform import Platform
from pydantic import BaseModel, ConfigDict


class StartRunRequest(BaseModel):
    task: str
    mode: str = "auto"  # auto | react | flow
    max_steps: int = 25


class ApproveRequest(BaseModel):
    tool_call_id: str
    decision: str = "approved"  # approved | denied


class RunHandle(BaseModel):
    run_id: str
    bus: EventBus
    task: asyncio.Task[RunState]
    platform: Platform

    model_config = ConfigDict(arbitrary_types_allowed=True)


PlatformFactory = Callable[[str, int], Platform]


def _single_platform_factory(platform: Platform) -> PlatformFactory:
    """Adapt one shared platform into the per-run factory shape."""

    def factory(mode: str, max_steps: int) -> Platform:
        platform.config.mode = mode
        platform.config.max_steps = max_steps
        return platform

    return factory


def create_app(platform_or_factory: Platform | PlatformFactory) -> FastAPI:
    """Build the API.

    Pass a :class:`Platform` for a shared instance, or a factory
    ``(mode, max_steps) -> Platform`` so each run gets an isolated one — the
    latter is what the demo and the tests use, since a run must never mutate
    state that a concurrent run depends on.
    """
    app = FastAPI(title="open-source autonomous platform", version="0.1.0")
    make_platform: PlatformFactory = (
        platform_or_factory
        if callable(platform_or_factory) and not isinstance(platform_or_factory, Platform)
        else _single_platform_factory(platform_or_factory)  # type: ignore[arg-type]
    )
    platform = make_platform("auto", 25)
    runs: dict[str, RunHandle] = {}
    states: dict[str, RunState] = {}
    artifacts: dict[str, dict[str, Any]] = {}

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "tools": platform.registry.names(),
            "mode": platform.config.mode,
            "runs_active": sum(1 for h in runs.values() if not h.task.done()),
        }

    @app.post("/runs", status_code=202)
    async def start_run(req: StartRunRequest) -> dict[str, str]:
        run_id = f"run_{uuid.uuid4().hex[:10]}"
        run_platform = make_platform(req.mode, req.max_steps)
        bus = run_platform.make_bus(run_id)

        async def _collect(event) -> None:
            if event.type is EventType.ARTIFACT:
                artifacts.setdefault(run_id, {})[event.data["filename"]] = event.data

        bus.subscribe(_collect)
        task = asyncio.create_task(run_platform.run(req.task, run_id=run_id, bus=bus))
        task.add_done_callback(lambda _t: states.setdefault(run_id, _final_state(_t, run_id)))
        runs[run_id] = RunHandle(run_id=run_id, bus=bus, task=task, platform=run_platform)
        return {"run_id": run_id, "mode": run_platform.choose_mode(req.task)}

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        handle = runs.get(run_id)
        if handle is None:
            raise HTTPException(404, "unknown run_id")
        if handle.task.done():
            state = states.get(run_id)
            return state.snapshot() if state else {"run_id": run_id, "status": "unknown"}
        return {
            "run_id": run_id,
            "status": RunStatus.RUNNING.value,
            "events": len(handle.bus.buffer),
            "pending_approvals": _pending(platform),
        }

    @app.get("/runs/{run_id}/events")
    async def stream_events(run_id: str) -> StreamingResponse:
        handle = runs.get(run_id)
        if handle is None:
            raise HTTPException(404, "unknown run_id")

        async def _gen():
            finished = False
            for event in handle.bus.replay():
                yield f"data: {event.to_wire()}\n\n"
                finished = finished or event.type is EventType.RUN_FINISHED
            if finished:
                # The run already ended: the replay above is the whole story.
                return
            with handle.bus.stream() as stream:
                async for event in stream:
                    yield f"data: {event.to_wire()}\n\n"
                    if event.type is EventType.RUN_FINISHED:
                        break

        return StreamingResponse(_gen(), media_type="text/event-stream")

    @app.websocket("/ws/{run_id}")
    async def websocket_endpoint(ws: WebSocket, run_id: str) -> None:
        handle = runs.get(run_id)
        if handle is None:
            await ws.close(code=4404)
            return
        await ws.accept()
        try:
            finished = False
            for event in handle.bus.replay():
                await ws.send_text(event.to_wire())
                finished = finished or event.type is EventType.RUN_FINISHED
            if finished:
                return
            with handle.bus.stream() as stream:
                async for event in stream:
                    await ws.send_text(event.to_wire())
                    if event.type is EventType.RUN_FINISHED:
                        break
        except WebSocketDisconnect:
            return
        finally:
            await ws.close()

    @app.post("/runs/{run_id}/approve")
    async def approve(run_id: str, req: ApproveRequest) -> dict[str, Any]:
        if run_id not in runs:
            raise HTTPException(404, "unknown run_id")
        handle = runs[run_id]
        if not isinstance(handle.platform.approver, QueueApprover):
            raise HTTPException(409, "this platform is not configured for interactive approval")
        try:
            decision = ApprovalDecision(req.decision)
        except ValueError as exc:
            raise HTTPException(422, "decision must be 'approved' or 'denied'") from exc
        ok = handle.platform.approver.answer(req.tool_call_id, decision)
        return {"delivered": ok}

    @app.get("/artifacts/{run_id}/{filename}")
    async def get_artifact(run_id: str, filename: str) -> Response:
        meta = artifacts.get(run_id, {}).get(filename)
        if meta is None:
            raise HTTPException(404, "no such artifact")
        return Response(content=json.dumps(meta), media_type="application/json")

    def _final_state(task: asyncio.Task[RunState], run_id: str) -> RunState:
        try:
            return task.result()
        except Exception as exc:
            state = RunState(run_id=run_id, status=RunStatus.FAILED, error=str(exc))
            return state

    def _pending(plat: Platform) -> list[dict[str, Any]]:  # pragma: no cover - introspection
        if not isinstance(plat.approver, QueueApprover):
            return []
        return [{"id": c.id, "name": c.name, "args": c.args} for c in plat.approver.pending]

    return app


def serve(platform: Platform, *, host: str = "0.0.0.0", port: int = 8080) -> None:
    import uvicorn

    uvicorn.run(create_app(platform), host=host, port=port, log_level="info")
