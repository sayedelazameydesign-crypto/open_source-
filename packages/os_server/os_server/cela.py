"""cela_use_browser — Python facade, private ``/cela/v1`` router, and ``/api`` AI router.

One implementation, two front doors:

* :class:`CelaBrowser` — direct in-process Python API (no HTTP required).
* :func:`create_private_router` — ``/cela/v1`` FastAPI router protected by its
  own router-level bearer-token dependency (``CELA_PRIVATE_API_TOKEN`` or
  ``create_app(..., private_api_token=...)``), completely isolated from the
  public platform token.
* :func:`create_local_ai_router` — ``/api`` FastAPI router exposing the local
  CPU AI engine protected by the platform token (``CELA_API_TOKEN`` /
  ``OS_API_TOKEN``).

Every browse request validates the target URL and enforces the explicit egress
policy **before** any agent run is created.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from os_core.hitl import ApprovalDecision
from os_core.state import RunStatus
from os_orchestration.platform import Platform
from os_tools.egress import EgressPolicy, EgressVerdict, parse_http_target
from os_tools.local_ai import LocalAIEngine
from os_tools.ssrf import BlockedURLError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from os_server.demo import build_demo_platform_for
from os_server.runtime import (
    PRODUCT_NAME,
    PRODUCT_VERSION,
    ApiRuntime,
    PlatformFactory,
    resolve_private_token,
    resolve_token,
    valid_bearer_token,
)

PRIVATE_API_PREFIX: str = "/cela/v1"
PUBLIC_AI_API_PREFIX: str = "/api"


class BrowseRequest(BaseModel):
    url: str
    goal: str = "Inspect page"
    mode: str = "react"
    max_steps: int = Field(default=10, ge=1, le=100)
    wait: bool = False
    timeout: float = Field(default=30.0, gt=0.0, le=300.0)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        parse_http_target(value)
        return value.strip()


class EgressCheckRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        parse_http_target(value)
        return value.strip()


class WaitRunRequest(BaseModel):
    timeout: float = Field(default=30.0, gt=0.0, le=300.0)


class PrivateApproveRequest(BaseModel):
    tool_call_id: str
    decision: str = "approved"


class EntitiesRequest(BaseModel):
    text: str
    language: str = "auto"


class IndexDocumentsRequest(BaseModel):
    documents: list[Any]
    namespace: str = "default"
    clear: bool = False


class SemanticSearchRequest(BaseModel):
    query: str
    documents: list[Any] | None = None
    namespace: str = "default"
    top_k: int = Field(default=5, ge=1, le=100)


class MLAnalyzeRequest(BaseModel):
    task: str

    model_config = ConfigDict(extra="allow")


class VisionAnalyzeRequest(BaseModel):
    image_b64: str


class DocumentOCRRequest(BaseModel):
    content_b64: str | None = None
    text: str | None = None
    extract_fields: bool = True


class AudioTranscribeRequest(BaseModel):
    audio_b64: str
    language: str = "auto"


class CelaBrowser:
    """In-process Python facade sharing :class:`ApiRuntime` with the HTTP server."""

    def __init__(
        self,
        platform_or_factory: Platform | PlatformFactory | None = None,
        *,
        runtime: ApiRuntime | None = None,
        egress_policy: EgressPolicy | None = None,
    ) -> None:
        if runtime is not None:
            self.runtime = runtime
            if egress_policy is not None:
                self.runtime.egress_policy = egress_policy
        else:
            factory: Platform | PlatformFactory = (
                platform_or_factory
                if platform_or_factory is not None
                else (lambda mode, max_steps: build_demo_platform_for(mode))
            )
            self.runtime = ApiRuntime(factory, egress_policy=egress_policy)

    def whoami(self) -> dict[str, Any]:
        return {
            "product": PRODUCT_NAME,
            "version": PRODUCT_VERSION,
            "surface": "private",
            "namespace": PRIVATE_API_PREFIX,
            "tools": self.runtime.platform.registry.names(),
            "allowed_hosts": self.runtime.egress_policy.allowed_hosts_summary(tool="browser"),
            "runs_active": sum(1 for h in self.runtime.runs.values() if not h.task.done()),
        }

    def check_url(self, url: str) -> EgressVerdict:
        """Validate ``url`` syntax and evaluate the browser egress policy."""
        parse_http_target(url)
        return self.runtime.egress_policy.check(url.strip(), tool="browser")

    def enforce_url(self, url: str) -> EgressVerdict:
        """Validate ``url`` syntax and raise :class:`EgressPolicyError` if denied."""
        return self.runtime.egress_policy.enforce(url.strip(), tool="browser")

    async def browse(
        self,
        url: str,
        goal: str = "Inspect page",
        *,
        mode: str = "react",
        max_steps: int = 10,
        wait: bool = False,
        timeout: float | None = 30.0,
    ) -> dict[str, Any]:
        """Pre-flight check ``url`` and start a browser-oriented run.

        If ``url`` is malformed (:class:`ValueError`) or blocked by the egress
        policy (:class:`EgressPolicyError`), the exception is raised **before**
        any run is created.
        """
        verdict = self.enforce_url(url)
        clean_url = url.strip()
        task_text = (
            f"Navigate to {clean_url} and complete goal: {goal}"
            if goal
            else f"Navigate to {clean_url}"
        )
        handle, chosen_mode = self.runtime.start_run(
            task_text,
            mode=mode,
            max_steps=max_steps,
            target_url=clean_url,
        )
        if not wait:
            return {
                "run_id": handle.run_id,
                "status": RunStatus.RUNNING.value,
                "mode": chosen_mode,
                "url": clean_url,
                "goal": goal,
                "host": verdict.host,
            }
        snapshot = await self.wait(handle.run_id, timeout=timeout)
        return {
            **snapshot,
            "url": clean_url,
            "goal": goal,
            "host": verdict.host,
        }

    async def wait(self, run_id: str, *, timeout: float | None = 30.0) -> dict[str, Any]:
        """Wait for ``run_id`` to complete without cancelling it on caller timeout."""
        handle = self.runtime.get_handle(run_id)
        if handle is None:
            raise KeyError(run_id)
        await asyncio.wait_for(asyncio.shield(handle.task), timeout=timeout)
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        snap = self.runtime.get_run_snapshot(run_id)
        if snap is None:
            raise KeyError(run_id)
        return snap

    def list_runs(self) -> list[dict[str, Any]]:
        return self.runtime.list_runs()

    def approve(self, run_id: str, tool_call_id: str, decision: str = "approved") -> dict[str, Any]:
        try:
            parsed = ApprovalDecision(decision)
        except ValueError as exc:
            raise ValueError("decision must be 'approved' or 'denied'") from exc
        ok = self.runtime.approve(run_id, tool_call_id, parsed)
        return {"delivered": ok}

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        items = self.runtime.list_artifacts(run_id)
        if items is None:
            raise KeyError(run_id)
        return items

    def get_artifact(self, run_id: str, filename: str) -> dict[str, Any]:
        if self.runtime.get_handle(run_id) is None:
            raise KeyError(run_id)
        meta = self.runtime.get_artifact(run_id, filename)
        if meta is None:
            raise KeyError(f"{run_id}/{filename}")
        return meta

    # --- Synchronous LocalAIEngine methods ----------------------------------

    def ai_capabilities(self) -> dict[str, Any]:
        return self.runtime.ai.capabilities()

    def extract_entities(self, text: str, language: str = "auto") -> dict[str, Any]:
        return self.runtime.ai.extract_entities(text, language=language)

    def index_documents(
        self,
        documents: Sequence[str | dict[str, Any]],
        namespace: str = "default",
        clear: bool = False,
    ) -> dict[str, Any]:
        return self.runtime.ai.index_documents(
            documents,
            namespace=namespace,
            clear=clear,
        )

    def semantic_search(
        self,
        query: str,
        documents: Sequence[str | dict[str, Any]] | None = None,
        namespace: str = "default",
        top_k: int = 5,
    ) -> dict[str, Any]:
        return self.runtime.ai.semantic_search(
            query,
            documents=documents,
            namespace=namespace,
            top_k=top_k,
        )

    def ml_analyze(self, task: str, **kwargs: Any) -> dict[str, Any]:
        return self.runtime.ai.ml_analyze(task, **kwargs)

    def vision_analyze(self, image_b64: str) -> dict[str, Any]:
        return self.runtime.ai.vision_analyze(image_b64)

    def document_ocr(
        self,
        content_b64: str | None = None,
        text: str | None = None,
        extract_fields: bool = True,
    ) -> dict[str, Any]:
        return self.runtime.ai.document_ocr(
            content_b64=content_b64,
            text=text,
            extract_fields=extract_fields,
        )

    def audio_transcribe(self, audio_b64: str, language: str = "auto") -> dict[str, Any]:
        return self.runtime.ai.audio_transcribe(audio_b64, language=language)


def _resolve_ai_engine(target: CelaBrowser | ApiRuntime | LocalAIEngine) -> LocalAIEngine:
    if isinstance(target, LocalAIEngine):
        return target
    if isinstance(target, ApiRuntime):
        return target.ai
    return target.runtime.ai


def _register_ai_endpoints(
    router: APIRouter,
    engine: LocalAIEngine,
    *,
    capabilities_path: str,
) -> None:
    @router.get(capabilities_path)
    async def ai_capabilities() -> dict[str, Any]:
        return engine.capabilities()

    @router.post("/nlp/entities")
    async def nlp_entities(req: EntitiesRequest) -> dict[str, Any]:
        return engine.extract_entities(req.text, language=req.language)

    @router.post("/search/index")
    async def search_index(req: IndexDocumentsRequest) -> dict[str, Any]:
        try:
            return engine.index_documents(
                req.documents,
                namespace=req.namespace,
                clear=req.clear,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/search/semantic")
    async def search_semantic(req: SemanticSearchRequest) -> dict[str, Any]:
        try:
            return engine.semantic_search(
                req.query,
                documents=req.documents,
                namespace=req.namespace,
                top_k=req.top_k,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/ml/analyze")
    async def ml_analyze(req: MLAnalyzeRequest) -> dict[str, Any]:
        payload = req.model_dump()
        task = payload.pop("task")
        try:
            return engine.ml_analyze(task, **payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/vision/analyze")
    async def vision_analyze(req: VisionAnalyzeRequest) -> dict[str, Any]:
        try:
            return engine.vision_analyze(req.image_b64)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/documents/ocr")
    async def documents_ocr(req: DocumentOCRRequest) -> dict[str, Any]:
        try:
            return engine.document_ocr(
                content_b64=req.content_b64,
                text=req.text,
                extract_fields=req.extract_fields,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/audio/transcribe")
    async def audio_transcribe(req: AudioTranscribeRequest) -> dict[str, Any]:
        try:
            return engine.audio_transcribe(req.audio_b64, language=req.language)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


def create_local_ai_router(
    browser_or_engine: CelaBrowser | ApiRuntime | LocalAIEngine,
    *,
    api_token: str | None = None,
) -> APIRouter:
    """Build the ``/api/*`` local AI router guarded by ``CELA_API_TOKEN`` / ``OS_API_TOKEN``."""
    configured_token = resolve_token(api_token)
    bearer_scheme = HTTPBearer(auto_error=False)
    engine = _resolve_ai_engine(browser_or_engine)

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

    router = APIRouter(
        prefix=PUBLIC_AI_API_PREFIX,
        tags=["local_ai"],
        dependencies=[Depends(require_api_auth)],
    )
    _register_ai_endpoints(router, engine, capabilities_path="/capabilities")
    return router


def create_private_router(
    browser: CelaBrowser,
    *,
    private_api_token: str | None = None,
) -> APIRouter:
    """Build the ``/cela/v1`` router guarded by its own bearer token."""
    configured_private_token = resolve_private_token(private_api_token)
    bearer_scheme = HTTPBearer(auto_error=False)

    async def require_private_auth(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    ) -> None:
        if not configured_private_token:
            raise HTTPException(
                status_code=503,
                detail="private API authentication is not configured; set CELA_PRIVATE_API_TOKEN",
            )
        authorization = (
            f"{credentials.scheme} {credentials.credentials}" if credentials is not None else None
        )
        if not valid_bearer_token(authorization, configured_private_token):
            raise HTTPException(
                status_code=401,
                detail="invalid or missing private API token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    router = APIRouter(
        prefix=PRIVATE_API_PREFIX,
        tags=[PRODUCT_NAME],
        dependencies=[Depends(require_private_auth)],
    )

    @router.get("/whoami")
    async def whoami() -> dict[str, Any]:
        return browser.whoami()

    @router.post("/egress/check")
    async def check_egress(req: EgressCheckRequest) -> dict[str, Any]:
        verdict = browser.check_url(req.url)
        return verdict.model_dump()

    @router.post("/browse", status_code=202)
    async def start_browse(req: BrowseRequest, response: Response) -> dict[str, Any]:
        try:
            result = await browser.browse(
                req.url,
                req.goal,
                mode=req.mode,
                max_steps=req.max_steps,
                wait=req.wait,
                timeout=req.timeout,
            )
        except BlockedURLError as exc:
            raise HTTPException(status_code=403, detail=f"egress denied: {exc}") from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="timed out waiting for browse run") from exc
        if req.wait:
            response.status_code = 200
        return result

    @router.get("/runs")
    async def list_runs() -> dict[str, Any]:
        return {"runs": browser.list_runs()}

    @router.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        try:
            return browser.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(404, "unknown run_id") from exc

    @router.post("/runs/{run_id}/wait")
    async def wait_run(run_id: str, req: WaitRunRequest) -> dict[str, Any]:
        try:
            return await browser.wait(run_id, timeout=req.timeout)
        except KeyError as exc:
            raise HTTPException(404, "unknown run_id") from exc
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="timed out waiting for browse run") from exc

    @router.get("/runs/{run_id}/events")
    async def stream_run_events(
        run_id: str, after_seq: int = Query(default=0, ge=0)
    ) -> StreamingResponse:
        if browser.runtime.get_handle(run_id) is None:
            raise HTTPException(404, "unknown run_id")
        return StreamingResponse(
            browser.runtime.sse_events(run_id, after_seq=after_seq),
            media_type="text/event-stream",
        )

    @router.post("/runs/{run_id}/approve")
    async def approve_run(run_id: str, req: PrivateApproveRequest) -> dict[str, Any]:
        try:
            return browser.approve(run_id, req.tool_call_id, req.decision)
        except KeyError as exc:
            raise HTTPException(404, "unknown run_id") from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/runs/{run_id}/artifacts")
    async def list_run_artifacts(run_id: str) -> dict[str, Any]:
        try:
            return {"run_id": run_id, "artifacts": browser.list_artifacts(run_id)}
        except KeyError as exc:
            raise HTTPException(404, "unknown run_id") from exc

    @router.get("/runs/{run_id}/artifacts/{filename}")
    async def get_run_artifact(run_id: str, filename: str) -> Response:
        try:
            meta = browser.get_artifact(run_id, filename)
        except KeyError as exc:
            raise HTTPException(404, "no such artifact") from exc
        return Response(content=json.dumps(meta), media_type="application/json")

    _register_ai_endpoints(router, browser.runtime.ai, capabilities_path="/ai/capabilities")
    return router
