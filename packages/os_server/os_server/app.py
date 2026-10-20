"""HTTP + WebSocket transport for the public platform and ``/cela/v1`` private API.

Public platform endpoints (guarded by ``CELA_API_TOKEN`` / ``OS_API_TOKEN``):
``POST /runs``                  start a run, returns ``run_id``
``GET  /runs/{id}``             execution-tree snapshot (the UI's source of truth)
``GET  /runs/{id}/events``      SSE stream — works through proxies that block WS
``POST /runs/{id}/ws-ticket``   mint a short-lived, single-use, run-scoped WS ticket
``WS   /ws/{id}``               live event stream (Bearer header or ``?ticket=``)

Both event streams accept ``?after_seq=N`` to resume a disconnected client from
the last sequence number it received (see ADR-0002).
``POST /runs/{id}/approve``     human-in-the-loop answer
``GET  /artifacts/{id}/{name}`` fetch an artifact for the right-hand pane
``GET  /ledger``                persisted outcome of every run + summary
``GET  /platform/info``         read-only deployment/sandbox/store/image facts
``GET  /health``                minimal public liveness check

Reach namespace (ADR-0013, behind the token of its parent API, opt-in via
``CELA_REACH_ENABLE=1``):
``GET  /api/reach/channels``            native + CLI channel health and coverage
``POST /api/reach/read|/rss|/github``   native channels, bound by EgressPolicy
``POST /api/reach/cli/{channel}``       CLI channels, enabled-only and audited
``GET|POST /cela/v1/reach/...``         the same surface in the private namespace

Launcher namespace (ADR-0015, behind the token of its parent API, opt-in via
``CELA_LAUNCHER_ENABLE=1``; read-only — never downloads or installs):
``GET  /api/launcher/catalog``              the Craft App Suite catalog
``GET  /api/launcher/apps/{id}/release``    latest release of one Craft app
``POST /api/launcher/check``                update check over installed versions
``GET|POST /cela/v1/launcher/...``          the same surface in the private namespace

WaAPI namespace (server-side credentials only, explicitly opt-in via
``CELA_WAAPI_ENABLE=1``; webhook requires its separate path secret):
``GET  /api/waapi/status``                  secret-safe connector readiness
``GET  /api/waapi/instances``               allow-listed instance fields
``POST /api/waapi/notifications``           idempotent durable notification enqueue
``GET  /api/waapi/notifications/{id}``       safe delivery status
``POST /api/webhooks/waapi/{secret}``        secret-protected incoming text message

AI Indigo namespace (ADR-0021, optional external directory integration, opt-in via
``CELA_INDIGO_ENABLE=1`` and completely independent of core C5/C6 startup):
``GET  /api/indigo/status``                 secret-safe surface readiness
``GET  /api/indigo/search``                 keyword/semantic/hybrid tool search
``GET  /api/indigo/tools``                  catalog listing with filters
``GET  /api/indigo/tools/compare``          side-by-side comparison of two slugs
``GET  /api/indigo/tools/{slug}``           single tool detail
``GET  /api/indigo/categories``             categories and counts
``GET  /api/indigo/categories/{slug}``      single category detail
``GET  /api/indigo/trending``               rising tools window (public upstream)
``GET  /cela/v1/indigo/...``                the same surface in the private namespace

MCP namespace (ADR-0017, behind the token of its parent API, opt-in via
``CELA_MCP_ENABLE=1`` + ``CELA_MCP_CONFIG``; read-only — connections are made
at startup, never per request):
``GET  /api/mcp/servers``                   configured servers, health, bridged + quarantined tools
``GET  /cela/v1/mcp/servers``               the same report in the private namespace

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
``GET  /cela/v1/ledger``
``GET  /cela/v1/runs/{id}/outcome``
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager, suppress
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from os_core.events import EventType, replay_and_stream
from os_core.hitl import ApprovalDecision
from os_core.ledger import RunLedger
from os_core.models import describe_execution
from os_mcp.layer import MCPLayer, mcp_enabled, process_layer, register_mcp_tools
from os_orchestration.platform import Platform
from os_tools.egress import EgressPolicy
from pydantic import BaseModel

from os_server.automation import ActivepiecesDispatcher, InboundWebhookVerifier
from os_server.cela import (
    CelaBrowser,
    create_local_ai_router,
    create_private_router,
    private_auth_dependency,
)
from os_server.demo import (
    build_demo_platform_for,
    free_tier_model_from_env,
    is_live_provider_enabled,
)
from os_server.indigo_api import (
    INDIGO_API_PREFIX,
    INDIGO_PRIVATE_PREFIX,
    IndigoService,
    create_indigo_router,
    indigo_enabled_from_env,
)
from os_server.launcher_api import (
    LAUNCHER_API_PREFIX,
    LAUNCHER_PRIVATE_PREFIX,
    LauncherService,
    create_launcher_router,
    launcher_enabled_from_env,
)
from os_server.platform_info import collect_platform_info
from os_server.reach_api import (
    REACH_API_PREFIX,
    REACH_PRIVATE_PREFIX,
    ReachService,
    create_reach_router,
    reach_enabled_from_env,
)
from os_server.runtime import (
    DEFAULT_WS_TICKET_TTL,
    PRODUCT_VERSION,
     ApiRuntime,
    ArtifactIntegrityError,
    PlatformFactory,
    RunHandle,
    resolve_private_token,
    resolve_token,
    valid_bearer_token,
)
from os_server.waapi import WaapiService, create_waapi_router

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
    platform_or_factory: Platform | PlatformFactory | None = None,
    *,
    api_token: str | None = None,
    private_api_token: str | None = None,
    egress_policy: EgressPolicy | None = None,
    ws_ticket_ttl: float = DEFAULT_WS_TICKET_TTL,
    runtime: ApiRuntime | None = None,
    store_url: str | None = None,
    reach_enabled: bool | None = None,
    reach_service: ReachService | None = None,
    reach_policy: EgressPolicy | None = None,
    launcher_enabled: bool | None = None,
    launcher_service: LauncherService | None = None,
    launcher_policy: EgressPolicy | None = None,
    indigo_enabled: bool | None = None,
    indigo_service: IndigoService | None = None,
    indigo_policy: EgressPolicy | None = None,
    waapi_service: WaapiService | None = None,
    ledger: RunLedger | None = None,
    mcp_enabled_flag: bool | None = None,
    mcp_layer: MCPLayer | None = None,
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

    # Fail fast at startup if live mode is requested but misconfigured, rather
    # than silently serving scripted/demo responses under a provider label.
    _live_enabled = is_live_provider_enabled()
    if _live_enabled and free_tier_model_from_env() is None and platform_or_factory is None:
        raise RuntimeError(
            "CELA_USE_LIVE_PROVIDER=1 is set but no valid LLM provider is configured. "
            "Set CELA_PROVIDER_BASE_URL + CELA_PROVIDER_MODEL + the corresponding API key "
            "env var (default: GEMINI_API_KEY), or unset CELA_USE_LIVE_PROVIDER to run "
            "in offline demo mode."
        )

    resolved_factory: Platform | PlatformFactory = (
        platform_or_factory
        if platform_or_factory is not None
        else (
            lambda mode, max_steps: build_demo_platform_for(
                mode,
                interactive=True,
                max_steps=max_steps,
                require_live=_live_enabled,
            )
        )
    )
    rt = (
        runtime
        if runtime is not None
        else ApiRuntime(
            resolved_factory,
            egress_policy=egress_policy,
            ws_ticket_ttl=ws_ticket_ttl,
            store_url=store_url,
            ledger=ledger,
        )
    )
    if egress_policy is not None:
        rt.egress_policy = egress_policy

    waapi = waapi_service if waapi_service is not None else WaapiService()
    indigo = (
        indigo_service
        if indigo_service is not None
        else IndigoService(
            enabled=indigo_enabled_from_env() if indigo_enabled is None else indigo_enabled,
            policy=indigo_policy,
        )
    )

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

    # MCP (ADR-0017): external tool servers are connected once, at startup,
    # inside the server's own event loop (a stdio server is a child process
    # bound to that loop) and their bridged tools join the shared registry.
    # Disabled unless CELA_MCP_ENABLE=1 or an explicit layer is passed — the
    # default app never spawns anything.
    use_mcp = mcp_enabled() if mcp_enabled_flag is None else mcp_enabled_flag
    if mcp_layer is not None:
        use_mcp = True

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        layer: MCPLayer | None = None
        waapi_worker: asyncio.Task[None] | None = None
        try:
            if use_mcp:
                layer = mcp_layer if mcp_layer is not None else process_layer()
            if layer is not None:
                await layer.discover()
                register_mcp_tools(rt.platform.registry, layer)
            app.state.mcp = layer
            if waapi.worker_enabled:
                waapi_worker = asyncio.create_task(
                    waapi.run_worker(rt), name="waapi-message-worker"
                )
            app.state.waapi_worker = waapi_worker
            yield
        finally:
            if waapi_worker is not None:
                waapi_worker.cancel()
                with suppress(asyncio.CancelledError):
                    await waapi_worker
            if layer is not None:
                await layer.close()
            await indigo.aclose()
            await waapi.aclose()

    app = FastAPI(
        title="cela_use_browser private + platform API",
        version=PRODUCT_VERSION,
        lifespan=lifespan,
    )
    from os_server.documents import document_router

    app.include_router(document_router(rt.store, require_api_auth))
    cela_browser = CelaBrowser(runtime=rt)
    webhook_dispatcher = ActivepiecesDispatcher()
    webhook_verifier = InboundWebhookVerifier()
    app.state.runtime = rt
    app.state.cela = cela_browser
    app.state.mcp = None
    app.state.waapi = waapi
    app.state.webhook_dispatcher = webhook_dispatcher
    app.state.webhook_verifier = webhook_verifier

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/mission/status", dependencies=[Depends(require_api_auth)])
    async def mission_status() -> dict[str, Any]:
        model_obj = getattr(rt.platform, "model", None)
        env_provider = free_tier_model_from_env()
        return {
            "status": "ok",
            "product": "Celia.sh — Mission OS",
            "version": PRODUCT_VERSION,
            "core_capabilities": {
                "deterministic_inspector_core": {
                    "active": True,
                    "requires_api_key": False,
                    "tools": [
                        "inspect_public_url",
                        "inspect_project_files",
                        "generate_deployment_plan_report",
                    ],
                },
                "ssrf_and_egress_guard": {
                    "active": True,
                    "blocks_private_ip_and_metadata": True,
                },
                "durable_run_store": {
                    "active": True,
                    "backend": type(rt.store).__name__,
                },
                "verified_report_downloads": {
                    "active": True,
                    "formats": ["md", "json", "html"],
                    "sha256_verified": True,
                },
            },
            "optional_integrations": {
                "llm_provider": {
                    "configured": env_provider is not None,
                    "provider_name": env_provider.name if env_provider is not None else None,
                    "execution": describe_execution(model_obj).model_dump(),
                    "fallback_mode": "deterministic_mission_core",
                },
                "activepieces_webhook": {
                    "configured": webhook_dispatcher.enabled,
                    "quota": webhook_dispatcher.quota_guard.snapshot(),
                },
            },
        }

    @app.get("/platform/info", dependencies=[Depends(require_api_auth)])
    async def platform_info() -> dict[str, Any]:
        """Read-only facts for the console: no secrets, no writes, no probes.

        The four web sections (App Launchpad, Devbox, Database, Image
        Registry) are views over this payload plus ``/ledger``. Nothing here
        can deploy, create or pull anything — those actions live in the
        platform console and the GitHub workflow, not in this API.
        """
        payload = collect_platform_info(
            store=rt.store,
            ledger=rt.ledger,
            execution=describe_execution(getattr(rt.platform, "model", None)),
            live_provider=free_tier_model_from_env() is not None,
            version=PRODUCT_VERSION,
        )
        registry = getattr(rt.platform, "registry", None)
        tools = sorted(getattr(registry, "tools", {}) or {})
        payload["tools"] = {"count": len(tools), "registered": tools}
        return payload

    @app.post("/webhooks/activepieces")
    async def receive_activepieces_webhook(request: Request) -> dict[str, Any]:
        raw_body = await request.body()
        sig = request.headers.get("x-celia-signature-256")
        idem = request.headers.get("x-celia-idempotency-key")
        try:
            return webhook_verifier.verify_and_record(
                payload=raw_body,
                signature=sig,
                idempotency_key=idem,
            )
        except PermissionError as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(401, str(exc)) from exc

    @app.get("/runtime-info", dependencies=[Depends(require_api_auth)])
    async def runtime_info() -> dict[str, Any]:
        # Reference configuration only. Each run carries its own executor info.
        return {"execution": describe_execution(getattr(rt.platform, "model", None)).model_dump()}

    @app.get("/runs", dependencies=[Depends(require_api_auth)])
    async def list_runs() -> dict[str, Any]:
        return {"runs": rt.list_runs()}

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
    async def stream_events(
        run_id: str, after_seq: int = Query(default=0, ge=0)
    ) -> StreamingResponse:
        if not rt.has_run(run_id):
            raise HTTPException(404, "unknown run_id")
        return StreamingResponse(
            rt.sse_events(run_id, after_seq=after_seq), media_type="text/event-stream"
        )

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
        if handle is None and not rt.has_run(run_id):
            await ws.close(code=4404)
            return
        after_seq = 0
        raw_cursor = ws.query_params.get("after_seq")
        if raw_cursor is not None:
            try:
                after_seq = int(raw_cursor)
            except ValueError:
                after_seq = -1
            if after_seq < 0:
                await ws.close(code=4422)  # malformed resume cursor
                return
        await ws.accept()
        try:
            if handle is None:
                # Recovered finished run: replay the durable event log.
                for event in rt.stored_events(run_id, after_seq=after_seq):
                    await ws.send_text(event.to_wire())
                return
            if rt.stream_exhausted(run_id, after_seq):
                return  # nothing left to send; the finally block closes cleanly
            # Subscribe before the backlog is delivered so a client that connects
            # mid-run loses nothing between replay and live streaming.
            async with aclosing(replay_and_stream(handle.bus, after_seq=after_seq)) as stream:
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

    def _download_artifact_response(run_id: str, filename: str) -> Response:
        try:
            dl = rt.download_artifact(run_id, filename)
        except KeyError as exc:
            raise HTTPException(404, "no such artifact") from exc
        except ArtifactIntegrityError as exc:
            raise HTTPException(409, "artifact integrity check failed") from exc
        return Response(
            content=dl["content_bytes"],
            media_type=dl["mime_type"],
            headers={
                "Content-Disposition": f'attachment; filename="{dl["filename"]}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Artifact-SHA256": dl["sha256"],
            },
        )

    @app.get(
        "/runs/{run_id}/artifacts/{filename}/download",
        dependencies=[Depends(require_api_auth)],
    )
    async def download_run_artifact(run_id: str, filename: str) -> Response:
        return _download_artifact_response(run_id, filename)

    @app.get(
        "/artifacts/{run_id}/{filename}/download",
        dependencies=[Depends(require_api_auth)],
    )
    async def download_artifact(run_id: str, filename: str) -> Response:
        return _download_artifact_response(run_id, filename)

    reach = (
        reach_service
        if reach_service is not None
        else ReachService(
            enabled=reach_enabled_from_env() if reach_enabled is None else reach_enabled,
            # None -> the reach policy (public internet minus the SSRF floor),
            # the same one the reach tools carry. Pass reach_policy= to bind the
            # surface to a stricter list.
            policy=reach_policy,
        )
    )
    app.state.reach = reach

    @app.get("/ledger", dependencies=[Depends(require_api_auth)])
    async def ledger_report(
        limit: int = Query(default=20, ge=1, le=500),
    ) -> dict[str, Any]:
        """Persisted outcome of every run: recent entries + aggregate summary."""
        return rt.ledger_report(limit=limit)

    def _mcp_report() -> dict[str, Any]:
        layer: MCPLayer | None = getattr(app.state, "mcp", None)
        if layer is None:
            return {
                "enabled": False,
                "reason": "set CELA_MCP_ENABLE=1 and point CELA_MCP_CONFIG at an mcpServers file",
            }
        return {"enabled": True, **layer.report()}

    @app.get("/api/mcp/servers", dependencies=[Depends(require_api_auth)])
    async def mcp_servers() -> dict[str, Any]:
        """Configured MCP servers, their health and what was bridged or quarantined."""
        return _mcp_report()

    @app.get(
        "/cela/v1/mcp/servers",
        dependencies=[Depends(private_auth_dependency(private_api_token))],
    )
    async def mcp_servers_private() -> dict[str, Any]:
        return _mcp_report()

    app.include_router(create_local_ai_router(cela_browser, api_token=api_token))
    app.include_router(create_private_router(cela_browser, private_api_token=private_api_token))
    app.include_router(
        create_reach_router(service=reach, require_auth=require_api_auth, prefix=REACH_API_PREFIX)
    )
    app.include_router(
        create_reach_router(
            service=reach,
            require_auth=private_auth_dependency(private_api_token),
            prefix=REACH_PRIVATE_PREFIX,
        )
    )

    launcher = (
        launcher_service
        if launcher_service is not None
        else LauncherService(
            enabled=launcher_enabled_from_env() if launcher_enabled is None else launcher_enabled,
            # None -> the launcher policy (four allow-listed hosts minus the SSRF
            # floor). Pass launcher_policy= to bind the surface to a stricter list.
            policy=launcher_policy,
        )
    )
    app.state.launcher = launcher
    app.include_router(
        create_launcher_router(
            service=launcher, require_auth=require_api_auth, prefix=LAUNCHER_API_PREFIX
        )
    )
    app.include_router(
        create_launcher_router(
            service=launcher,
            require_auth=private_auth_dependency(private_api_token),
            prefix=LAUNCHER_PRIVATE_PREFIX,
        )
    )
    app.state.indigo = indigo
    app.include_router(
        create_indigo_router(
            service=indigo, require_auth=require_api_auth, prefix=INDIGO_API_PREFIX
        )
    )
    app.include_router(
        create_indigo_router(
            service=indigo,
            require_auth=private_auth_dependency(private_api_token),
            prefix=INDIGO_PRIVATE_PREFIX,
        )
    )
    app.include_router(create_waapi_router(waapi, require_api_auth))
    return app


def serve(
    platform: Platform | PlatformFactory,
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    api_token: str | None = None,
    private_api_token: str | None = None,
    store_url: str | None = None,
    ledger: RunLedger | None = None,
) -> None:
    import uvicorn

    token = resolve_token(api_token)
    if not token:
        raise RuntimeError(
            "API authentication is required; set CELA_API_TOKEN (or OS_API_TOKEN) before serving"
        )
    priv_token = resolve_private_token(private_api_token)
    uvicorn.run(
        create_app(
            platform,
            api_token=token,
            private_api_token=priv_token,
            store_url=store_url,
            ledger=ledger,
        ),
        host=host,
        port=port,
        log_level="info",
        access_log=False,
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
