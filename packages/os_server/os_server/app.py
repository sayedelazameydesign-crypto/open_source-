"""HTTP + WebSocket transport for the public platform and ``/cela/v1`` private API.

Public platform endpoints (guarded by ``CELA_API_TOKEN`` / ``OS_API_TOKEN``):
``POST /runs``                  start a run, returns ``run_id``
``GET  /runs/{id}``             execution-tree snapshot (the UI's source of truth)
``GET  /runs/{id}/events``      SSE stream — works through proxies that block WS
``POST /runs/{id}/ws-ticket``   mint a short-lived, single-use, run-scoped WS ticket
``WS   /ws/{id}``               live event stream (Bearer header or ``?ticket=``)
``POST /runs/{id}/approve``     human-in-the-loop answer
``GET  /artifacts/{id}/{name}`` fetch an artifact for the right-hand pane
``GET  /health``                minimal public liveness check

Private browser namespace (guarded by ``CELA_PRIVATE_API_TOKEN``):
``GET  /cela/v1/whoami``
``POST /cela/v1/egress/check``
``POST /cela/v1/browse``
``GET  /cela/v1/runs``
``GET  /cela/v1/runs/{id}``
``POST /cela/v1/runs/{id}/wait``
``GET  /cela/v1/runs/{id}/events``
``POST /cela/v1/runs/{id}/approve``
``GET  /cela/v1/runs/{id}/artifacts``
``GET  /cela/v1/runs/{id}/artifacts/{name}``
"""

from __future__ import annotations

import json
import secrets
from contextlib import aclosing
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from os_core.events import EventType, replay_and_stream
from os_core.hitl import ApprovalDecision
from os_orchestration.platform import Platform
from os_tools.egress import EgressPolicy
from pydantic import BaseModel

from os_server.cela import CelaBrowser, create_local_ai_router, create_private_router
from os_server.runtime import (
    DEFAULT_WS_TICKET_TTL,
    PRODUCT_VERSION,
    ApiRuntime,
    PlatformFactory,
    RunHandle,
    resolve_private_token,
    resolve_token,
    valid_bearer_token,
)

# Backwards-compatible alias for internal callers.
_valid_bearer_token = valid_bearer_token


class StartRunRequest(BaseModel):
    task: str
    mode: str = "auto"  # auto | react | flow
    max_steps: int = 25


class ApproveRequest(BaseModel):
    tool_call_id: str
    decision: str = "approved"  # approved | denied


def create_app(
    platform_or_factory: Platform | PlatformFactory,
    *,
    api_token: str | None = None,
    private_api_token: str | None = None,
    egress_policy: EgressPolicy | None = None,
    ws_ticket_ttl: float = DEFAULT_WS_TICKET_TTL,
    runtime: ApiRuntime | None = None,
) -> FastAPI:
    """Build the FastAPI application hosting both the platform and ``/cela/v1``."""
    configured_token = resolve_token(api_token)
    configured_private_token = resolve_private_token(private_api_token)
    if (
        configured_token
        and configured_private_token
        and secrets.compare_digest(
            configured_token.encode("utf-8"), configured_private_token.encode("utf-8")
        )
    ):
        raise ValueError("private_api_token must differ from api_token")

    rt = (
        runtime
        if runtime is not None
        else ApiRuntime(
            platform_or_factory,
            egress_policy=egress_policy,
            ws_ticket_ttl=ws_ticket_ttl,
        )
    )
    if egress_policy is not None:
        rt.egress_policy = egress_policy

    bearer_scheme = HTTPBearer(auto_error=False)

    async def require_api_auth(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    ) -> None:
        if not configured_token:
            raise HTTPException(
                status_code=503,
                detail="API authentication is not configured; set CELA_API_TOKEN or OS_API_TOKEN",
            )
        authorization = (
            f"{credentials.scheme} {credentials.credentials}" if credentials is not None else None
        )
        if not valid_bearer_token(authorization, configured_token):
            raise HTTPException(
                status_code=401,
                detail="invalid or missing API token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    app = FastAPI(title="cela_use_browser private + platform API", version=PRODUCT_VERSION)
    cela_browser = CelaBrowser(runtime=rt)
    app.state.runtime = rt
    app.state.cela = cela_browser

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok"}

    @app.post("/runs", status_code=202, dependencies=[Depends(require_api_auth)])
    async def start_run(req: StartRunRequest) -> dict[str, str]:
        handle, chosen_mode = rt.start_run(req.task, mode=req.mode, max_steps=req.max_steps)
        return {"run_id": handle.run_id, "mode": chosen_mode}

    @app.get("/runs/{run_id}", dependencies=[Depends(require_api_auth)])
    async def get_run(run_id: str) -> dict[str, Any]:
        snap = rt.get_run_snapshot(run_id)
        if snap is None:
            raise HTTPException(404, "unknown run_id")
        return snap

    @app.get("/runs/{run_id}/events", dependencies=[Depends(require_api_auth)])
    async def stream_events(run_id: str) -> StreamingResponse:
        if rt.get_handle(run_id) is None:
            raise HTTPException(404, "unknown run_id")
        return StreamingResponse(rt.sse_events(run_id), media_type="text/event-stream")

    @app.post("/runs/{run_id}/ws-ticket", dependencies=[Depends(require_api_auth)])
    async def create_ws_ticket(run_id: str) -> dict[str, Any]:
        issued = rt.issue_ws_ticket(run_id)
        if issued is None:
            raise HTTPException(404, "unknown run_id")
        return issued

    @app.websocket("/ws/{run_id}")
    async def websocket_endpoint(ws: WebSocket, run_id: str) -> None:
        if not configured_token:
            await ws.close(code=1011)
            return
        header_ok = valid_bearer_token(ws.headers.get("authorization"), configured_token)
        ticket_ok = (
            False if header_ok else rt.consume_ws_ticket(ws.query_params.get("ticket"), run_id)
        )
        if not (header_ok or ticket_ok):
            await ws.close(code=4401)
            return
        handle = rt.get_handle(run_id)
        if handle is None:
            await ws.close(code=4404)
            return
        await ws.accept()
        try:
            # Subscribe before the backlog is delivered so a client that connects
            # mid-run loses nothing between replay and live streaming.
            async with aclosing(replay_and_stream(handle.bus)) as stream:
                async for event in stream:
                    await ws.send_text(event.to_wire())
                    if event.type is EventType.RUN_FINISHED:
                        break
        except WebSocketDisconnect:
            return
        finally:
            await ws.close()

    @app.post("/runs/{run_id}/approve", dependencies=[Depends(require_api_auth)])
    async def approve(run_id: str, req: ApproveRequest) -> dict[str, Any]:
        if rt.get_handle(run_id) is None:
            raise HTTPException(404, "unknown run_id")
        try:
            decision = ApprovalDecision(req.decision)
        except ValueError as exc:
            raise HTTPException(422, "decision must be 'approved' or 'denied'") from exc
        try:
            ok = rt.approve(run_id, req.tool_call_id, decision)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"delivered": ok}

    @app.get("/artifacts/{run_id}/{filename}", dependencies=[Depends(require_api_auth)])
    async def get_artifact(run_id: str, filename: str) -> Response:
        meta = rt.get_artifact(run_id, filename)
        if meta is None:
            raise HTTPException(404, "no such artifact")
        return Response(content=json.dumps(meta), media_type="application/json")

    app.include_router(create_local_ai_router(cela_browser, api_token=api_token))
    app.include_router(create_private_router(cela_browser, private_api_token=private_api_token))
    return app


def serve(
    platform: Platform,
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    api_token: str | None = None,
    private_api_token: str | None = None,
) -> None:
    import uvicorn

    token = resolve_token(api_token)
    if not token:
        raise RuntimeError(
            "API authentication is required; set CELA_API_TOKEN (or OS_API_TOKEN) before serving"
        )
    priv_token = resolve_private_token(private_api_token)
    uvicorn.run(
        create_app(platform, api_token=token, private_api_token=priv_token),
        host=host,
        port=port,
        log_level="info",
    )


__all__ = [
    "ApproveRequest",
    "PlatformFactory",
    "RunHandle",
    "StartRunRequest",
    "create_app",
    "create_local_ai_router",
    "serve",
]
